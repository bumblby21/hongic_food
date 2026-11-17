"""
홍슐랭 가이드 (Hongchelin Guide) 평점 알고리즘 및 자동 검증 모듈
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

class User:
    def __init__(self, name: str, review_count: int, rating_sum: float):
        self.name = name
        self.review_count = review_count
        self.rating_sum = rating_sum

    @property
    def average_rating(self) -> float:
        if self.review_count == 0:
            return 3.0  # 보통 유저 기본 평균 (가상 베이스라인 3.0점)
        return self.rating_sum / self.review_count

    def add_review(self, score: float):
        self.review_count += 1
        self.rating_sum += score


class Restaurant:
    def __init__(self, name: str, category: str, lat: float, lng: float, base_score: float = 3.0, k: float = 2.0):
        self.name = name
        self.category = category
        self.lat = lat
        self.lng = lng
        self.base_score = base_score
        self.k = k  # 안정화 상수 (작을수록 소규모에서 편차 반응 민감도 높음)
        self.deltas = []

    @property
    def hongchelin_score(self) -> float:
        if not self.deltas:
            return self.base_score
        # 기본 점수 3.0 + (편차의 합 / (k + 리뷰수))
        total_delta = sum(self.deltas)
        score = self.base_score + (total_delta / (self.k + len(self.deltas)))
        # 1.0 ~ 5.0 사이로 클램핑
        return round(max(1.0, min(5.0, score)), 2)

    @property
    def is_gold_pin(self) -> bool:
        return self.hongchelin_score >= 3.50

    def add_review(self, user: User, given_score: float) -> dict:
        # 리뷰 작성 전 유저의 기존 성향(평균)
        user_prior_avg = user.average_rating
        
        # 유저 평균 갱신
        user.add_review(given_score)
        
        # 유저 본인 평균 대비 상대 편차(Delta)
        # 깐깐한 유저(평균 2.5)가 5.0점을 주면 편차는 5.0 - 2.5 = +2.5
        delta = given_score - user_prior_avg
        self.deltas.append(delta)

        return {
            "restaurant": self.name,
            "user": user.name,
            "user_prior_avg": round(user_prior_avg, 2),
            "given_score": given_score,
            "delta": round(delta, 2),
            "new_restaurant_score": self.hongchelin_score,
            "is_gold_pin": self.is_gold_pin
        }


def verify_scenario():
    print("=" * 60)
    print("🧪 [홍슐랭 가이드 알고리즘 검증 시나리오]")
    print("시나리오: 3.1점 식당에 '평소 2.5점 주는 깐깐한 유저'가 5.0점을 줄 때 3.6 골드 핀 승격 여부 검증")
    print("=" * 60)

    # 0. 신규 식당 기본 점수 검증 (리뷰 0건 = 기본 3.00점)
    brand_new = Restaurant(name="신규 등록 식당", category="카페", lat=37.5, lng=127.0, base_score=3.0)
    assert brand_new.hongchelin_score == 3.00, f"신규 식당 기본 점수 오류: {brand_new.hongchelin_score}"
    print(f"0. [신규 식당 기본값 검증] {brand_new.name}: 홍슐랭 기본점수 {brand_new.hongchelin_score}점 (리뷰 0건 = 3.00점 확인)")

    # 1. 식당 초기 상태: 초기 점수 3.10점 (과거 편차 +0.3 보유, 리뷰 1개 가정, k=2)
    # 3.0 + 0.3 / (2 + 1) = 3.10
    euljiro = Restaurant(name="을지로 을지다락", category="양식/오므라이스", lat=37.5662, lng=126.9922, base_score=3.0, k=2.0)
    euljiro.deltas.append(0.3)
    initial_score = euljiro.hongchelin_score
    print(f"1. [초기 식당 상태] {euljiro.name}: 홍슐랭 점수 {initial_score}점 (일반 핀 ⚪)")
    assert initial_score == 3.10, f"초기 점수 오류: {initial_score}"

    # 2. 대조군 테스트: 평소 4.8점 주는 '후한 유저'가 5.0점을 줄 때
    generous_user = User(name="세상 친절한 천사", review_count=10, rating_sum=48.0)  # 평균 4.8점
    print(f"\n2. [대조군 실험] 유저: {generous_user.name} (평소 평균 {generous_user.average_rating:.1f}점)")
    
    # 식당 복제본으로 대조군 계산
    control_restaurant = Restaurant(name="대조군 식당", category="양식", lat=37.56, lng=126.99, base_score=3.0, k=2.0)
    control_restaurant.deltas.append(0.3)
    res_control = control_restaurant.add_review(generous_user, 5.0)
    print(f"   -> 5.0점 부여 시 편차(Δ): +{res_control['delta']}점")
    print(f"   -> 대조군 식당 새 점수: {res_control['new_restaurant_score']}점 (골드 핀 승격 실패 ❌, 거의 안 오름)")

    # 3. 핵심 타겟 테스트: 평소 2.5점 주는 '깐깐한 유저'가 5.0점을 줄 때
    strict_user = User(name="지옥의 미식가", review_count=10, rating_sum=25.0)  # 평균 2.5점
    print(f"\n3. [핵심 실험] 유저: {strict_user.name} (평소 평균 {strict_user.average_rating:.1f}점)")
    result = euljiro.add_review(strict_user, 5.0)
    
    print(f"   -> 5.0점 부여 시 편차(Δ): +{result['delta']}점 (강력한 신호!)")
    print(f"   -> 식당 새 점수: {result['new_restaurant_score']}점 (골드 핀 여부: {result['is_gold_pin']})")
    
    # 검증: 3.1에서 3.60 이상으로 뛰어서 골드 핀(>= 3.50)이 되었는가?
    assert result['new_restaurant_score'] >= 3.60, f"점수 미달: {result['new_restaurant_score']}"
    assert result['is_gold_pin'] == True, "골드 핀 미달성"
    
    print("\n" + "=" * 60)
    print(f"🎉 [검증 성공!] 점수 변화: {initial_score}점 ⚪ ➡️ {result['new_restaurant_score']}점 🌟 (골드 핀 등극!)")
    print("=" * 60)


if __name__ == "__main__":
    verify_scenario()

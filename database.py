"""
홍슐랭 가이드 (Hongchelin Guide) - SQLite 데이터베이스 모듈
"""

import sqlite3
import json
import os
import hmac
import hashlib
import secrets
import base64
import re
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hongchelin.db")
PII_SECRET = os.environ.get("HONGCHELIN_PII_KEY", "hongchelin-pii-master-key-2026-dev")
DEFAULT_ADMIN_PASSWORD = os.environ.get("HONGCHELIN_ADMIN_PASSWORD", "admin1234")
DEFAULT_ADMIN1_PASSWORD = os.environ.get("HONGCHELIN_ADMIN1_PASSWORD", "hongic")


def _derive_pii_keys():
    enc_key = hashlib.sha256((PII_SECRET + ":enc").encode("utf-8")).digest()
    mac_key = hashlib.sha256((PII_SECRET + ":mac").encode("utf-8")).digest()
    return enc_key, mac_key


def encrypt_pii(plaintext):
    """표준 라이브러리 기반 대칭 암호화 + HMAC-SHA256 무결성 검증 (v1:<nonce>:<cipher>:<tag>)"""
    if not plaintext:
        return ""
    enc_key, mac_key = _derive_pii_keys()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")

    keystream = bytearray()
    counter = 0
    while len(keystream) < len(data):
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(4, "big")).digest()
        keystream.extend(block)
        counter += 1

    cipher = bytes(a ^ b for a, b in zip(data, keystream[:len(data)]))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).hexdigest()[:32]
    return f"v1:{base64.urlsafe_b64encode(nonce).decode()}:{base64.urlsafe_b64encode(cipher).decode()}:{tag}"


def decrypt_pii(token):
    """암호화된 PII 복호화 및 무결성 검증"""
    if not token or not token.startswith("v1:"):
        return token or ""
    try:
        _, nonce_b64, cipher_b64, tag = token.split(":", 3)
        enc_key, mac_key = _derive_pii_keys()
        nonce = base64.urlsafe_b64decode(nonce_b64.encode())
        cipher = base64.urlsafe_b64decode(cipher_b64.encode())
        expected_tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(tag, expected_tag):
            return "[무결성 오류]"

        keystream = bytearray()
        counter = 0
        while len(keystream) < len(cipher):
            block = hashlib.sha256(enc_key + nonce + counter.to_bytes(4, "big")).digest()
            keystream.extend(block)
            counter += 1

        plain = bytes(a ^ b for a, b in zip(cipher, keystream[:len(cipher)]))
        return plain.decode("utf-8")
    except Exception:
        return "[복호화 오류]"


def mask_real_name(name):
    if not name:
        return "-"
    s = name.strip()
    if len(s) <= 1:
        return s
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + ("*" * (len(s) - 2)) + s[-1]


def mask_email(email):
    if not email or "@" not in email:
        return "-"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*" * max(1, len(local) - 1)
    else:
        masked_local = local[:2] + "*" * (len(local) - 2)
    return f"{masked_local}@{domain}"


def mask_phone(phone):
    if not phone:
        return "-"
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11:
        return f"{digits[:3]}-****-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-***-{digits[6:]}"
    if len(phone) >= 4:
        return phone[:3] + "****" + phone[-2:]
    return "****"


def hash_ip(ip_addr):
    if not ip_addr:
        return ""
    return hashlib.sha256((PII_SECRET + ":ip:" + ip_addr).encode("utf-8")).hexdigest()[:16]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password_hash TEXT,
            email_enc TEXT,
            phone_enc TEXT,
            real_name_enc TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            ban_reason TEXT,
            last_ip_hash TEXT,
            review_count INTEGER NOT NULL DEFAULT 1,
            rating_sum REAL NOT NULL DEFAULT 3.0,
            standard_score REAL NOT NULL DEFAULT 3.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate existing users table if needed
    cursor.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "password_hash" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "standard_score" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN standard_score REAL NOT NULL DEFAULT 3.0")
    if "created_at" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP")
    cursor.execute("""
        UPDATE users
        SET created_at = COALESCE(
            (SELECT MIN(created_at) FROM reviews WHERE reviews.user_id = users.id),
            CURRENT_TIMESTAMP
        )
        WHERE created_at IS NULL OR TRIM(created_at) = ''
    """)
    if "email_enc" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email_enc TEXT")
    if "phone_enc" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN phone_enc TEXT")
    if "real_name_enc" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN real_name_enc TEXT")
    if "role" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "status" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "ban_reason" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
    if "last_ip_hash" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN last_ip_hash TEXT")
    if "must_change_password" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

    # 2. Restaurants Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            area TEXT NOT NULL,
            category TEXT NOT NULL,
            address TEXT NOT NULL,
            subway TEXT NOT NULL,
            phone TEXT NOT NULL,
            hours TEXT NOT NULL,
            parking TEXT NOT NULL,
            intro TEXT NOT NULL,
            tags TEXT NOT NULL,       -- JSON array
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            base_score REAL NOT NULL DEFAULT 3.0,
            k REAL NOT NULL DEFAULT 2.0,
            photos TEXT NOT NULL,     -- JSON array
            menus TEXT NOT NULL       -- JSON array
        )
    """)

    # 3. Reviews Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            user_prior_avg REAL NOT NULL,
            score REAL NOT NULL,
            delta REAL NOT NULL,
            comment TEXT NOT NULL,
            date TEXT NOT NULL,
            receipt INTEGER NOT NULL DEFAULT 1,
            receipt_photo TEXT DEFAULT '',
            photos TEXT NOT NULL,     -- JSON array
            criteria TEXT NOT NULL DEFAULT '{}', -- JSON object: taste, service, vibe, drinks, value, time, budget
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_user_id ON reviews(user_id)")
    try:
        cursor.execute("ALTER TABLE reviews ADD COLUMN receipt_photo TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # 4. Bookmarks Table (가고 싶은 맛집)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            restaurant_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, restaurant_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (restaurant_id) REFERENCES restaurants (id)
        )
    """)

    # 5. Admin Audit Logs Table (개인정보 열람 및 제재 감사 로그)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_id TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 6. Reports Table (유저 신고 접수)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            reporter_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            detail TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_by TEXT,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for col_def in ("resolved_by TEXT", "resolved_at TIMESTAMP"):
        try:
            cursor.execute(f"ALTER TABLE reports ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    # 7. Feedbacks Table (사용자 오류/버그 제보 및 개선 의견)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '오류 제보',
            message TEXT NOT NULL,
            device_info TEXT,
            photos TEXT DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_by TEXT,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for col_def in ("photos TEXT DEFAULT '[]'", "resolved_by TEXT", "resolved_at TIMESTAMP"):
        try:
            cursor.execute(f"ALTER TABLE feedbacks ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    # Ensure default admin accounts exist (admin & admin1)
    default_admins = [
        ("admin", "총괄 관리자", DEFAULT_ADMIN_PASSWORD, "HONGCHELIN_ADMIN_PASSWORD", "admin@hongchelin.local", "010-0000-0000"),
        ("admin1", "공동 관리자", DEFAULT_ADMIN1_PASSWORD, "HONGCHELIN_ADMIN1_PASSWORD", "admin1@hongchelin.local", "010-0000-0001"),
    ]
    for adm_id, adm_name, adm_pw, env_key, adm_email, adm_phone in default_admins:
        cursor.execute("SELECT id FROM users WHERE id = ?", (adm_id,))
        if not cursor.fetchone():
            adm_pw_hash = generate_password_hash(adm_pw)
            cursor.execute("""
                INSERT INTO users (id, name, password_hash, email_enc, phone_enc, role, status, review_count, rating_sum, standard_score, created_at)
                VALUES (?, ?, ?, ?, ?, 'admin', 'active', 1, 3.0, 3.0, CURRENT_TIMESTAMP)
            """, (adm_id, adm_name, adm_pw_hash, encrypt_pii(adm_email), encrypt_pii(adm_phone)))
        elif os.environ.get(env_key):
            cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(adm_pw), adm_id))

    conn.commit()

    # Seed if restaurants table is empty
    cursor.execute("SELECT COUNT(*) FROM restaurants")
    if cursor.fetchone()[0] == 0:
        seed_data(conn)

    conn.close()


def seed_data(conn):
    cursor = conn.cursor()

    # Seed Users
    users_data = [
        ("strict", "최진수 (미식 평론가)", 10, 25.0),    # 평균 2.50
        ("generous", "이지은 (친절한 천사)", 10, 48.0),   # 평균 4.80
        ("normal", "김민우 (보통 탐방객)", 10, 30.0)      # 평균 3.00
    ]
    cursor.executemany("INSERT OR REPLACE INTO users (id, name, review_count, rating_sum) VALUES (?, ?, ?, ?)", users_data)

    # Seed Restaurants
    restaurants_data = [
        (
            1, "을지로 을지다락", "을지로/종로", "양식 · 다락오므라이스/매콤크림파스타",
            "서울 중구 수표로 10길 19 4층", "을지로3가역 11번 출구 도보 3분 (180m)",
            "070-8844-4484", "매일 11:30 ~ 21:10 (B.T 15:00~17:00 / L.O 20:30)",
            "주차 불가 (인근 청계천 노상 공영주차장 이용 권장)",
            "부드럽고 몽글몽글한 계란이 올라간 다락오므라이스와 칼칼한 매콤크림파스타가 대표적인 을지로 골목 핫플레이스입니다.",
            json.dumps(["#시그니처오므라이스", "#을지로핫플", "#데이트코스"], ensure_ascii=False),
            37.5662, 126.9922, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1621996346565-e3d5d6281691?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1550547660-d9450f859349?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1525351484163-7529414344d8?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1551183053-bf91a1d81141?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1554118811-1e0d58224f24?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "다락오므라이스", "price": "15,000원", "desc": "보슬보슬한 일본식 수제 오믈렛을 반으로 갈라주는 시그니처"},
                {"name": "매콤크림파스타", "price": "17,000원", "desc": "세 가지 치즈와 신선한 생크림에 청양고춧가루의 칼칼한 조화"},
                {"name": "가츠산도", "price": "12,000원", "desc": "국내산 최고급 돈육 생등심을 바삭하게 튀겨낸 카츠 샌드위치"}
            ], ensure_ascii=False)
        ),
        (
            2, "연남동 소이연남", "홍대/연남", "아시안 · 태국소고기국수/소이뽀삐아",
            "서울 마포구 동교로 267 1층", "홍대입구역 3번 출구 도보 8분 (600m)",
            "02-323-5130", "매일 11:00 ~ 21:20 (B.T 15:00~17:00 / L.O 20:40)",
            "매장 앞 2대 또는 연남동 공영주차장",
            "방콕 현지의 깊은 육수 비법과 두툼하고 부드러운 아롱사태 고기를 맛볼 수 있는 서울 대표 태국 요리점입니다.",
            json.dumps(["#태국쌀국수", "#진한국물", "#블루리본서베이"], ensure_ascii=False),
            37.5628, 126.9248, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1585032226651-759b368d7246?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1552611052-33e04de081de?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1617093727343-374698b1b08d?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1496116218417-1a781b1c416c?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1556679343-c7306c1976bc?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "소고기국수", "price": "12,000원", "desc": "장시간 푹 고아낸 진한 사골육수에 두툼한 소고기가 듬뿍"},
                {"name": "소이뽀삐아", "price": "14,000원", "desc": "새우, 돼지고기, 죽순을 얇은 피로 말아 바삭하게 튀긴 수제 롤"},
                {"name": "쏨땀", "price": "13,000원", "desc": "그린 파파야와 피시소스로 버무린 매콤새콤 태국 전통 샐러드"}
            ], ensure_ascii=False)
        ),
        (
            3, "성수동 대성갈비", "성수/서울숲", "고기요리 · 숯불돼지갈비/김치찌개",
            "서울 성동구 서울숲4길 27", "뚝섬역 8번 출구 도보 4분 (300m)",
            "02-464-3012", "12:00 ~ 21:00 (매주 일요일 정기휴무)",
            "성동구민종합체육센터 유료주차장 이용",
            "수요미식회에 소개된 성수동 갈비골목의 터줏대감. 신선한 쌈채소와 무료 서비스 김치찌개, 양념게장까지 완벽한 구성입니다.",
            json.dumps(["#수요미식회", "#숯불돼지갈비", "#성수갈비골목"], ensure_ascii=False),
            37.5463, 127.0435, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1555939594-58d7cb561ad1?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1547496502-affa22d38842?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1583032015879-c5c7921fb9be?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1504674900247-0877df9cc836?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1529193591184-b1d58069ecdd?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "돼지갈비 (200g)", "price": "17,000원", "desc": "자극적이지 않고 은은한 과일 단맛의 양념에 재운 참숯 갈비"},
                {"name": "뚝배기 김치찌개", "price": "기본 제공", "desc": "돼지고기가 듬뿍 들어가 묵직하고 칼칼한 뚝배기 찌개"}
            ], ensure_ascii=False)
        ),
        (
            4, "신사동 멘야하나비", "강남/신사", "일식 · 나고야마제소바/도니꾸마제소바",
            "서울 강남구 강남대로162길 22", "신사역 8번 출구 도보 5분 (350m)",
            "070-8888-8008", "11:30 ~ 21:00 (B.T 14:30~17:30 / L.O 20:30)",
            "주차 불가 (인근 유료민영주차장 이용)",
            "일본 나고야의 원조 마제소바를 한국에 정착시킨 명소. 감칠맛 나는 민찌와 노른자, 쫄깃한 특제 수제면의 완벽한 밸런스.",
            json.dumps(["#마제소바원조", "#생활의달인", "#비빔라멘"], ensure_ascii=False),
            37.5186, 127.0212, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1557872943-16a5ac26437e?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1591814468924-caf88d1232e1?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1525351484163-7529414344d8?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1496116218417-1a781b1c416c?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "나고야 마제소바", "price": "11,000원", "desc": "국물 없이 특제 민찌 소스와 대파, 노른자를 비벼먹는 비빔면"},
                {"name": "도니꾸 마제소바", "price": "14,000원", "desc": "두툼한 수제 통차슈 4조각이 올라간 일일 한정 메뉴"}
            ], ensure_ascii=False)
        ),
        (
            5, "익선동 온천집", "을지로/종로", "일식 · 된장샤브샤브/로스트비프덮밥",
            "서울 종로구 돈화문로11나길 31-8", "종로3가역 4번, 6번 출구 도보 3분",
            "0507-1378-8218", "매일 11:30 ~ 21:30 (평일 B.T 15:00~17:00)",
            "익선동 한옥마을 내 차량 진입 불가",
            "마당 한가운데 수증기가 피어오르는 온천 정원이 펼쳐진 고즈넉한 한옥에서 정갈한 1인 샤브샤브를 즐길 수 있습니다.",
            json.dumps(["#한옥온천뷰", "#감성샤브", "#익선동골목"], ensure_ascii=False),
            37.5744, 126.9902, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1541544741938-0af808871cc0?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1512058564366-18510be2db19?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1504674900247-0877df9cc836?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1503899036084-c55cdd92da26?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1525351484163-7529414344d8?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "1인 된장 샤브샤브 3단", "price": "19,500원", "desc": "구수한 미소 된장 육수에 차돌박이와 신선한 야채가 어우러진 코스"},
                {"name": "로스트비프 덮밥", "price": "18,000원", "desc": "부드럽게 수비드 조리한 소고기를 듬뿍 올린 특제 덮밥"}
            ], ensure_ascii=False)
        ),
        (
            6, "연남동 오스테리아 루카", "홍대/연남", "이탈리안 · 생면파스타/화덕피자",
            "서울 마포구 연남로 19 1층", "홍대입구역 3번 출구 도보 7분 (500m)",
            "02-333-7722", "12:00 ~ 22:00 (매주 화요일 정기휴무)",
            "인근 유료주차장 이용",
            "이탈리아 유학파 오너 셰프가 매일 아침 직접 뽑아내는 생면 파스타와 참나무 화덕 피자를 선보이는 신규 비스트로입니다.",
            json.dumps(["#신규오픈", "#리뷰0건", "#평가대기중"], ensure_ascii=False),
            37.5645, 126.9215, 3.0, 2.0,
            json.dumps([
                "https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1551183053-bf91a1d81141?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1579684947550-22e945225d9a?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=800&q=80",
                "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=800&q=80"
            ]),
            json.dumps([
                {"name": "트러플 버섯 생면 딸리아뗄레", "price": "21,000원", "desc": "진한 트러플 페이스트와 크림 소스에 비벼낸 수제 생면 파스타"},
                {"name": "프로슈토 루꼴라 화덕피자", "price": "23,000원", "desc": "48시간 숙성 반죽을 화덕에 구워 신선한 프로슈토를 올린 피자"}
            ], ensure_ascii=False)
        )
    ]

    cursor.executemany("""
        INSERT INTO restaurants (id, name, area, category, address, subway, phone, hours, parking, intro, tags, lat, lng, base_score, k, photos, menus)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, restaurants_data)

    # Seed Initial Reviews
    reviews_data = [
        # 을지다락 (1) - 김민우 3.5 (편차 +0.5)
        (
            1, "normal", "김민우 (보통 탐방객)", 3.0, 3.5, 0.5,
            "오므라이스 달걀이 폭신폭신하고 소스 조화가 좋습니다. 인테리어도 예쁩니다.",
            "2026.11.02", 1,
            json.dumps(["https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=400&q=80"]),
            json.dumps({"taste": 3.8, "service": 3.6, "vibe": 4.0, "drinks": 3.4, "value": 3.3, "time": "dinner", "budget": "15,000~20,000원"}, ensure_ascii=False)
        ),
        # 소이연남 (2) - 준수한 평가
        (
            2, "normal", "박준형 (미식가)", 2.8, 4.2, 1.4,
            "국물 한 숟가락 뜨는 순간 방콕 카오산로드 온 줄 알았습니다. 고기가 사르르 녹네요.",
            "2026.10.30", 1,
            json.dumps(["https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=400&q=80"]),
            json.dumps({"taste": 4.5, "service": 3.8, "vibe": 4.1, "drinks": 3.7, "value": 4.0, "time": "lunch", "budget": "12,000~15,000원"}, ensure_ascii=False)
        ),
        # 대성갈비 (3) - 깐깐한 미식가 최진수 4.5 (편차 +2.0)
        (
            3, "strict", "최진수 (미식 평론가)", 2.5, 4.5, 2.0,
            "양념이 과하지 않고 고기 육질이 살아있습니다. 김치찌개는 돈 받고 팔아도 될 수준.",
            "2026.11.01", 1,
            json.dumps([
                "https://images.unsplash.com/photo-1555939594-58d7cb561ad1?auto=format&fit=crop&w=400&q=80",
                "https://images.unsplash.com/photo-1547496502-affa22d38842?auto=format&fit=crop&w=400&q=80"
            ]),
            json.dumps({"taste": 4.8, "service": 3.9, "vibe": 4.2, "drinks": 4.0, "value": 4.5, "time": "dinner", "budget": "20,000~30,000원"}, ensure_ascii=False)
        ),
        # 멘야하나비 (4) - 김민우 3.5 (편차 +0.5)
        (
            4, "normal", "김민우 (보통 탐방객)", 3.0, 3.5, 0.5,
            "마지막에 오이메시(밥 비벼먹기)까지 먹으면 정말 든든합니다. 면발 식감이 최고.",
            "2026.10.25", 1,
            json.dumps(["https://images.unsplash.com/photo-1557872943-16a5ac26437e?auto=format&fit=crop&w=400&q=80"]),
            json.dumps({"taste": 3.9, "service": 3.5, "vibe": 3.6, "drinks": 3.3, "value": 3.6, "time": "lunch", "budget": "11,000~15,000원"}, ensure_ascii=False)
        ),
        # 익선동 온천집 (5) - 이지은 5.0 (편차 +0.2)
        (
            5, "generous", "이지은 (친절한 천사)", 4.8, 5.0, 0.2,
            "온천 김이 모락모락 나는 정원이 너무 아름다워요! 사진 찍기에도 최고입니다.",
            "2026.11.03", 1,
            json.dumps(["https://images.unsplash.com/photo-1541544741938-0af808871cc0?auto=format&fit=crop&w=400&q=80"]),
            json.dumps({"taste": 4.4, "service": 4.6, "vibe": 5.0, "drinks": 4.2, "value": 3.8, "time": "dinner", "budget": "20,000~30,000원"}, ensure_ascii=False)
        )
    ]

    cursor.executemany("""
        INSERT INTO reviews (restaurant_id, user_id, user_name, user_prior_avg, score, delta, comment, date, receipt, photos, criteria)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, reviews_data)

    conn.commit()


def calculate_restaurant_score(base_score, k, deltas):
    """
    홍슐랭 스코어 공식:
    Score = 3.00 + (편차합 / (k + 리뷰수))
    """
    if not deltas:
        return 3.00
    sum_delta = sum(deltas)
    score = base_score + (sum_delta / (k + len(deltas)))
    return round(max(1.0, min(5.0, score)), 2)


def get_all_restaurants():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT r.*, 
               GROUP_CONCAT(rev.delta) as all_deltas,
               COUNT(rev.id) as review_count,
               SUM(CASE WHEN rev.receipt = 1 THEN 1 ELSE 0 END) as receipt_count
        FROM restaurants r
        LEFT JOIN reviews rev ON r.id = rev.restaurant_id
        GROUP BY r.id
    """)

    rows = cursor.fetchall()
    result = []

    for row in rows:
        deltas_str = row["all_deltas"]
        deltas = [float(d) for d in deltas_str.split(",")] if deltas_str else []
        score = calculate_restaurant_score(row["base_score"], row["k"], deltas)

        result.append({
            "id": row["id"],
            "name": row["name"],
            "area": row["area"],
            "category": row["category"],
            "address": row["address"],
            "subway": row["subway"],
            "phone": row["phone"],
            "hours": row["hours"],
            "parking": row["parking"],
            "intro": row["intro"],
            "tags": json.loads(row["tags"]),
            "lat": row["lat"],
            "lng": row["lng"],
            "baseScore": row["base_score"],
            "k": row["k"],
            "photos": json.loads(row["photos"]),
            "menus": json.loads(row["menus"]),
            "deltas": deltas,
            "score": score,
            "isGold": score >= 3.50,
            "reviewCount": row["review_count"],
            "hasReceiptReview": bool((row["receipt_count"] or 0) > 0)
        })

    conn.close()
    return result


def get_restaurant_by_id(restaurant_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    # Get reviews for this restaurant
    cursor.execute("""
        SELECT * FROM reviews 
        WHERE restaurant_id = ? 
        ORDER BY id DESC
    """, (restaurant_id,))
    review_rows = cursor.fetchall()

    reviews = []
    deltas = []
    taste_list, service_list, vibe_list, drinks_list, value_list = [], [], [], [], []
    distribution = {
        "5.0": 0, "4.5 - 4.9": 0, "4.0 - 4.4": 0, "3.5 - 3.9": 0,
        "3.0 - 3.4": 0, "2.5 - 2.9": 0, "2.0 - 2.4": 0, "1.5 - 1.9": 0, "1.0 - 1.4": 0
    }

    for rev in review_rows:
        deltas.append(rev["delta"])
        crit = {}
        try:
            crit = json.loads(rev["criteria"]) if "criteria" in rev.keys() and rev["criteria"] else {}
        except Exception:
            crit = {}

        # Collect criteria ratings if available
        if "taste" in crit: taste_list.append(float(crit["taste"]))
        if "service" in crit: service_list.append(float(crit["service"]))
        if "vibe" in crit: vibe_list.append(float(crit["vibe"]))
        if "drinks" in crit: drinks_list.append(float(crit["drinks"]))
        if "value" in crit: value_list.append(float(crit["value"]))

        # Distribution count
        s = float(rev["score"])
        if s >= 5.0: distribution["5.0"] += 1
        elif s >= 4.5: distribution["4.5 - 4.9"] += 1
        elif s >= 4.0: distribution["4.0 - 4.4"] += 1
        elif s >= 3.5: distribution["3.5 - 3.9"] += 1
        elif s >= 3.0: distribution["3.0 - 3.4"] += 1
        elif s >= 2.5: distribution["2.5 - 2.9"] += 1
        elif s >= 2.0: distribution["2.0 - 2.4"] += 1
        elif s >= 1.5: distribution["1.5 - 1.9"] += 1
        else: distribution["1.0 - 1.4"] += 1

        reviews.append({
            "id": rev["id"],
            "userId": rev["user_id"],
            "userName": rev["user_name"],
            "userAvg": rev["user_prior_avg"],
            "score": rev["score"],
            "delta": rev["delta"],
            "comment": rev["comment"],
            "date": rev["date"],
            "receipt": bool(rev["receipt"]),
            "receiptPhoto": (rev["receipt_photo"] if "receipt_photo" in rev.keys() and rev["receipt_photo"] else ""),
            "photos": json.loads(rev["photos"]),
            "criteria": crit
        })

    score = calculate_restaurant_score(row["base_score"], row["k"], deltas)

    # Calculate criteria averages (real reviews only, fallback to restaurant score if none)
    avg_taste = round(sum(taste_list) / len(taste_list), 2) if taste_list else min(5.0, round(score + 0.15, 2))
    avg_service = round(sum(service_list) / len(service_list), 2) if service_list else max(1.0, round(score - 0.05, 2))
    avg_vibe = round(sum(vibe_list) / len(vibe_list), 2) if vibe_list else min(5.0, round(score + 0.10, 2))
    avg_drinks = round(sum(drinks_list) / len(drinks_list), 2) if drinks_list else max(1.0, round(score - 0.10, 2))
    avg_value = round(sum(value_list) / len(value_list), 2) if value_list else max(1.0, round(score - 0.15, 2))

    criteria_avg = {
        "overall": score,
        "taste": avg_taste,
        "service": avg_service,
        "vibe": avg_vibe,
        "drinks": avg_drinks,
        "value": avg_value
    }

    result = {
        "id": row["id"],
        "name": row["name"],
        "area": row["area"],
        "category": row["category"],
        "address": row["address"],
        "subway": row["subway"],
        "phone": row["phone"],
        "hours": row["hours"],
        "parking": row["parking"],
        "intro": row["intro"],
        "tags": json.loads(row["tags"]),
        "lat": row["lat"],
        "lng": row["lng"],
        "baseScore": row["base_score"],
        "k": row["k"],
        "photos": json.loads(row["photos"]),
        "menus": json.loads(row["menus"]),
        "deltas": deltas,
        "score": score,
        "reviews": reviews,
        "criteriaAvg": criteria_avg,
        "distribution": distribution,
        "budget": {
            "dinner": "15,000원 ~ 25,000원",
            "lunch": "10,000원 ~ 15,000원"
        }
    }

    conn.close()
    return result


def get_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    rows = cursor.fetchall()
    users = {}
    for r in rows:
        avg = round(r["rating_sum"] / r["review_count"], 2) if r["review_count"] > 0 else 3.0
        users[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "reviewCount": r["review_count"],
            "ratingSum": r["rating_sum"],
            "avg": avg
        }
    conn.close()
    return users


def _recalculate_user_review_deltas(cursor, user_id):
    """Recalculate delta and user_prior_avg for all reviews by user_id using leave-one-out user average."""
    cursor.execute("SELECT review_count, rating_sum FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    if not u_row:
        return
    r_count = u_row["review_count"] or 0
    r_sum = u_row["rating_sum"] or 0.0
    if r_count <= 1:
        cursor.execute(
            "UPDATE reviews SET user_prior_avg = 3.0, delta = ROUND(score - 3.0, 2) WHERE user_id = ?",
            (user_id,),
        )
        return
    cursor.execute("SELECT id, score FROM reviews WHERE user_id = ?", (user_id,))
    for rv in cursor.fetchall():
        sc = float(rv["score"])
        other_avg = round((r_sum - sc) / (r_count - 1), 2)
        new_delta = round(sc - other_avg, 2)
        cursor.execute(
            "UPDATE reviews SET user_prior_avg = ?, delta = ? WHERE id = ?",
            (other_avg, new_delta, rv["id"]),
        )


def add_review(restaurant_id, user_id, score, comment, receipt=False, photos=None, criteria=None, receipt_photo=""):
    conn = get_db()
    cursor = conn.cursor()

    if photos is None:
        photos = []
    if criteria is None:
        criteria = {}

    # 1. Fetch User
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    if not u_row:
        user_name = "사용자"
        prior_avg = 3.0
        r_count = 1
        r_sum = 3.0
    else:
        user_name = u_row["name"]
        r_count = u_row["review_count"]
        r_sum = u_row["rating_sum"]
        prior_avg = round(r_sum / r_count, 2) if r_count > 0 else 3.0

    # 2. Calculate Delta (Hongchelin core)
    delta = round(score - prior_avg, 2)

    # 3. Insert Review
    import datetime
    today_str = datetime.date.today().strftime("%Y.%m.%d")
    cursor.execute("""
        INSERT INTO reviews (restaurant_id, user_id, user_name, user_prior_avg, score, delta, comment, date, receipt, receipt_photo, photos, criteria)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (restaurant_id, user_id, user_name, prior_avg, score, delta, comment, today_str, 1 if (receipt or receipt_photo) else 0, receipt_photo or "", json.dumps(photos), json.dumps(criteria, ensure_ascii=False)))

    # 4. Update User Rating Tendency
    new_count = r_count + 1
    new_sum = r_sum + score
    cursor.execute("""
        UPDATE users 
        SET review_count = ?, rating_sum = ? 
        WHERE id = ?
    """, (new_count, new_sum, user_id))
    _recalculate_user_review_deltas(cursor, user_id)

    conn.commit()
    conn.close()

    # Return updated restaurant
    return get_restaurant_by_id(restaurant_id)


def add_restaurant(name, area, category, address, subway, phone, hours, parking, intro, tags=None, lat=37.5665, lng=126.9780, photos=None, menus=None):
    conn = get_db()
    cursor = conn.cursor()

    if photos is None or len(photos) == 0:
        photos = [
            "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80",
            "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80",
            "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=800&q=80"
        ]
    if menus is None or len(menus) == 0:
        menus = [
            {"name": "대표 시그니처 메뉴", "price": "변동", "desc": "정성을 다해 완성하는 대표 요리"}
        ]
    if tags is None:
        tags = ["#사용자등록맛집"]

    cursor.execute("""
        INSERT INTO restaurants (name, area, category, address, subway, phone, hours, parking, intro, tags, lat, lng, base_score, k, photos, menus)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3.0, 2.0, ?, ?)
    """, (
        name, area, category, address, subway, phone, hours, parking, intro,
        json.dumps(tags, ensure_ascii=False),
        float(lat), float(lng),
        json.dumps(photos, ensure_ascii=False),
        json.dumps(menus, ensure_ascii=False)
    ))

    new_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return new_id


def create_user(user_id, name, password, email="", phone="", real_name="", ip_addr=""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if cursor.fetchone():
        conn.close()
        return None, "이미 사용 중인 아이디입니다."

    pw_hash = generate_password_hash(password)
    email_enc = encrypt_pii(email.strip()) if email else ""
    phone_enc = encrypt_pii(phone.strip()) if phone else ""
    real_name_enc = encrypt_pii(real_name.strip()) if real_name else ""
    ip_hash = hash_ip(ip_addr)

    cursor.execute("""
        INSERT INTO users (id, name, password_hash, email_enc, phone_enc, real_name_enc, role, status, last_ip_hash, review_count, rating_sum, standard_score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'user', 'active', ?, 1, 3.0, 3.0, CURRENT_TIMESTAMP)
    """, (user_id, name, pw_hash, email_enc, phone_enc, real_name_enc, ip_hash))
    conn.commit()
    conn.close()
    return get_user_by_id(user_id), None


def change_user_password(user_id, current_password, new_password):
    if not current_password or not new_password:
        return False, "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요."
    if len(new_password) < 4:
        return False, "새 비밀번호는 4자 이상 입력해 주세요."
    if current_password == new_password:
        return False, "새 비밀번호는 현재 비밀번호와 다르게 입력해 주세요."

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash, role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "사용자를 찾을 수 없습니다."

    valid_pw = (current_password == "1234") if not row["password_hash"] else check_password_hash(row["password_hash"], current_password)
    if not valid_pw:
        conn.close()
        return False, "현재 비밀번호가 올바르지 않습니다."

    cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
    if row["role"] == "admin":
        log_admin_action(conn, user_id, "CHANGE_PASSWORD", user_id, "관리자 본인 비밀번호 변경")
    conn.commit()
    conn.close()
    return True, None


def authenticate_user(user_id, password, ip_addr=""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None, "아이디 또는 비밀번호가 올바르지 않습니다."

    # Demo users (strict, generous, normal) without password_hash can log in with "1234"
    valid_pw = False
    if not row["password_hash"]:
        valid_pw = (password == "1234")
    else:
        valid_pw = check_password_hash(row["password_hash"], password)

    if not valid_pw:
        conn.close()
        return None, "아이디 또는 비밀번호가 올바르지 않습니다."

    status = row["status"] if "status" in row.keys() and row["status"] else "active"
    ban_reason = row["ban_reason"] if "ban_reason" in row.keys() and row["ban_reason"] else "관리자 정책 위반"
    if status == "banned":
        conn.close()
        return None, f"영구 정지된 계정입니다. (사유: {ban_reason})"
    if status == "suspended":
        conn.close()
        return None, f"일시 정지된 계정입니다. (사유: {ban_reason})"

    if ip_addr:
        cursor.execute("UPDATE users SET last_ip_hash = ? WHERE id = ?", (hash_ip(ip_addr), user_id))
        conn.commit()

    conn.close()
    return get_user_by_id(user_id), None


def change_user_password(user_id, current_password, new_password):
    if not current_password or not new_password:
        return False, "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요."
    if len(new_password) < 4:
        return False, "새 비밀번호는 4자 이상 입력해 주세요."
    if current_password == new_password:
        return False, "새 비밀번호는 현재 비밀번호와 다르게 입력해 주세요."

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash, role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "사용자를 찾을 수 없습니다."

    valid_pw = (current_password == "1234") if not row["password_hash"] else check_password_hash(row["password_hash"], current_password)
    if not valid_pw:
        conn.close()
        return False, "현재 비밀번호가 올바르지 않습니다."

    cursor.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(new_password), user_id)
    )
    if row["role"] == "admin":
        log_admin_action(conn, user_id, "CHANGE_PASSWORD", user_id, "관리자 본인 비밀번호 변경")
    conn.commit()
    conn.close()
    return True, None


def admin_reset_user_password(admin_id, target_user_id):
    clean_uid = (target_user_id or "").strip()
    if not clean_uid:
        return None, "대상 아이디가 필요합니다."

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, role FROM users WHERE id = ?", (clean_uid,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None, "대상 회원을 찾을 수 없습니다."
    if row["role"] == "admin":
        conn.close()
        return None, "관리자 계정의 비밀번호는 직접 초기화할 수 없습니다."

    temp_pw = str(secrets.randbelow(900000) + 100000)
    cursor.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(temp_pw), clean_uid)
    )
    log_admin_action(conn, admin_id, "RESET_USER_PASSWORD", clean_uid, "임시 비밀번호 발급 (로그인 후 새 비밀번호 변경 필요)")
    conn.commit()
    conn.close()
    return temp_pw, None


def get_user_by_id(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"] if "role" in keys and row["role"] else "user",
        "status": row["status"] if "status" in keys and row["status"] else "active",
        "standardScore": row["standard_score"] if "standard_score" in keys and row["standard_score"] is not None else 3.0,
        "mustChangePassword": bool(row["must_change_password"]) if "must_change_password" in keys and row["must_change_password"] else False
    }


def update_user_standard_score(user_id, standard_score):
    conn = get_db()
    cursor = conn.cursor()
    val = max(1.0, min(5.0, round(float(standard_score), 1)))
    cursor.execute("UPDATE users SET standard_score = ? WHERE id = ?", (val, user_id))
    conn.commit()
    conn.close()
    return val


def delete_user_account(user_id, password, upload_dir=None):
    """Permanently delete a user's account, all their reviews/bookmarks/feedbacks/reports, and uploaded files."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash, role FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return False, "사용자를 찾을 수 없습니다."
    if user_id == "admin" or (user["role"] == "admin"):
        conn.close()
        return False, "관리자 계정은 탈퇴할 수 없습니다."
    if not password or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        conn.close()
        return False, "비밀번호가 올바르지 않습니다."

    # Collect uploaded file paths from reviews and feedbacks before deleting rows
    upload_urls = []
    cursor.execute("SELECT photos, receipt_photo FROM reviews WHERE user_id = ?", (user_id,))
    for row in cursor.fetchall():
        if row["receipt_photo"]:
            upload_urls.append(row["receipt_photo"])
        try:
            photos = json.loads(row["photos"] or "[]")
            if isinstance(photos, list):
                upload_urls.extend(photos)
        except Exception:
            pass

    cursor.execute("SELECT photos FROM feedbacks WHERE user_id = ?", (user_id,))
    for row in cursor.fetchall():
        try:
            photos = json.loads(row["photos"] or "[]")
            if isinstance(photos, list):
                upload_urls.extend(photos)
        except Exception:
            pass

    # Delete all user-related data across tables
    cursor.execute(
        "DELETE FROM reports WHERE reporter_id = ? OR review_id IN (SELECT id FROM reviews WHERE user_id = ?)",
        (user_id, user_id),
    )
    cursor.execute("DELETE FROM reviews WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM bookmarks WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM feedbacks WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    # Remove uploaded image files from disk
    if upload_dir:
        base_dir = os.path.abspath(upload_dir)
        for url in set(str(u).strip() for u in upload_urls if u):
            if url.startswith("/uploads/"):
                fname = os.path.basename(url[len("/uploads/"):])
                if fname:
                    fpath = os.path.abspath(os.path.join(base_dir, fname))
                    if fpath.startswith(base_dir + os.sep) and os.path.isfile(fpath):
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass

    return True, None


def get_user_bookmark_ids(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT restaurant_id FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    ids = [r["restaurant_id"] for r in cursor.fetchall()]
    conn.close()
    return ids


def toggle_bookmark(user_id, restaurant_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM bookmarks WHERE user_id = ? AND restaurant_id = ?", (user_id, restaurant_id))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("DELETE FROM bookmarks WHERE id = ?", (existing["id"],))
        bookmarked = False
    else:
        cursor.execute("INSERT INTO bookmarks (user_id, restaurant_id) VALUES (?, ?)", (user_id, restaurant_id))
        bookmarked = True
    conn.commit()
    cursor.execute("SELECT restaurant_id FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    ids = [r["restaurant_id"] for r in cursor.fetchall()]
    conn.close()
    return bookmarked, ids


def get_user_mypage(user_id):
    """
    타베로그 방식 마이페이지 데이터 반환:
    - 공개: 내 평균 평점, 채점 분포, 나의 기준점(표준점), 내가 남긴 리뷰 목록, 가고 싶은 맛집(북마크)
    - 비공개: 리뷰어의 가중치/영향도(delta 등)는 절대 반환하지 않음
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    if not u_row:
        conn.close()
        return None

    # Fetch user's actual reviews joined with restaurant info (no delta exposed!)
    cursor.execute("""
        SELECT r.id, r.restaurant_id, r.score, r.comment, r.date, r.receipt, r.photos,
               rest.name AS restaurant_name, rest.area AS restaurant_area,
               rest.category AS restaurant_category, rest.photos AS restaurant_photos
        FROM reviews r
        JOIN restaurants rest ON r.restaurant_id = rest.id
        WHERE r.user_id = ?
        ORDER BY r.id DESC
    """, (user_id,))
    rev_rows = cursor.fetchall()

    my_reviews = []
    score_sum = 0.0
    dist = {"5.0": 0, "4.0~4.9": 0, "3.0~3.9": 0, "2.0~2.9": 0, "1.0~1.9": 0}

    for rv in rev_rows:
        sc = float(rv["score"])
        score_sum += sc
        if sc >= 5.0:
            dist["5.0"] += 1
        elif sc >= 4.0:
            dist["4.0~4.9"] += 1
        elif sc >= 3.0:
            dist["3.0~3.9"] += 1
        elif sc >= 2.0:
            dist["2.0~2.9"] += 1
        else:
            dist["1.0~1.9"] += 1

        rest_photos = json.loads(rv["restaurant_photos"]) if rv["restaurant_photos"] else []
        my_reviews.append({
            "id": rv["id"],
            "restaurantId": rv["restaurant_id"],
            "restaurantName": rv["restaurant_name"],
            "restaurantArea": rv["restaurant_area"],
            "restaurantCategory": rv["restaurant_category"],
            "restaurantThumb": rest_photos[0] if rest_photos else "",
            "score": sc,
            "comment": rv["comment"],
            "date": rv["date"],
            "receipt": bool(rv["receipt"])
        })

    actual_count = len(my_reviews)
    actual_avg = round(score_sum / actual_count, 2) if actual_count > 0 else None

    # Fetch bookmarked restaurants
    cursor.execute("""
        SELECT rest.id, rest.name, rest.area, rest.category, rest.subway, rest.address, rest.photos, rest.base_score, rest.k
        FROM bookmarks b
        JOIN restaurants rest ON b.restaurant_id = rest.id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (user_id,))
    bm_rows = cursor.fetchall()

    bookmarks = []
    for bm in bm_rows:
        cursor.execute("SELECT delta FROM reviews WHERE restaurant_id = ?", (bm["id"],))
        deltas = [d["delta"] for d in cursor.fetchall()]
        rest_score = calculate_restaurant_score(bm["base_score"], bm["k"], deltas)
        bm_photos = json.loads(bm["photos"]) if bm["photos"] else []
        bookmarks.append({
            "id": bm["id"],
            "name": bm["name"],
            "area": bm["area"],
            "category": bm["category"],
            "subway": bm["subway"],
            "address": bm["address"],
            "thumb": bm_photos[0] if bm_photos else "",
            "score": rest_score
        })

    conn.close()

    return {
        "user": {
            "id": u_row["id"],
            "name": u_row["name"],
            "standardScore": u_row["standard_score"] if "standard_score" in u_row.keys() and u_row["standard_score"] is not None else 3.0,
            "isDemo": u_row["id"] in ("strict", "generous", "normal")
        },
        "stats": {
            "actualReviewCount": actual_count,
            "actualAvg": actual_avg,
            "distribution": dist
        },
        "reviews": my_reviews,
        "bookmarks": bookmarks
    }


# ============================================================
# 관리자 전용 기능 (회원 관리, 신상 복호화 열람, 제재, 감사 로그)
# ============================================================
def log_admin_action(conn, admin_id, action, target_id, detail=""):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO admin_audit_logs (admin_id, action, target_id, detail)
        VALUES (?, ?, ?, ?)
    """, (admin_id, action, str(target_id), detail))


def _format_kst_timestamp(ts_str):
    if not ts_str:
        return "-"
    s = str(ts_str).strip()
    if not s:
        return "-"
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=9)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


def get_admin_overview():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, name, email_enc, phone_enc, real_name_enc, role, status, ban_reason, last_ip_hash,
               review_count, rating_sum, standard_score, created_at
        FROM users
        ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, created_at DESC, id ASC
    """)
    user_rows = cursor.fetchall()

    users_list = []
    status_counts = {"active": 0, "suspended": 0, "banned": 0}
    for u in user_rows:
        st = u["status"] or "active"
        if st in status_counts:
            status_counts[st] += 1

        raw_email = decrypt_pii(u["email_enc"]) if u["email_enc"] else ""
        raw_phone = decrypt_pii(u["phone_enc"]) if u["phone_enc"] else ""
        raw_real_name = decrypt_pii(u["real_name_enc"]) if u["real_name_enc"] else ""

        cursor.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ?", (u["id"],))
        actual_reviews = cursor.fetchone()[0]

        users_list.append({
            "id": u["id"],
            "name": u["name"],
            "role": u["role"] or "user",
            "status": st,
            "banReason": u["ban_reason"] or "",
            "maskedRealName": mask_real_name(raw_real_name) if raw_real_name else "미등록",
            "maskedEmail": mask_email(raw_email) if raw_email else "미등록",
            "maskedPhone": mask_phone(raw_phone) if raw_phone else "미등록",
            "hasPii": bool(raw_email or raw_phone or raw_real_name),
            "ipHash": u["last_ip_hash"] or "-",
            "actualReviewCount": actual_reviews,
            "isDemo": u["id"] in ("strict", "generous", "normal"),
            "createdAt": _format_kst_timestamp(u["created_at"])
        })

    cursor.execute("""
        SELECT r.id, r.restaurant_id, rest.name AS restaurant_name, r.user_id, r.user_name,
               r.score, r.comment, r.date, r.receipt, r.created_at
        FROM reviews r
        JOIN restaurants rest ON r.restaurant_id = rest.id
        ORDER BY r.id DESC
        LIMIT 500
    """)
    reviews_list = []
    for rv in cursor.fetchall():
        reviews_list.append({
            "id": rv["id"],
            "restaurantId": rv["restaurant_id"],
            "restaurantName": rv["restaurant_name"],
            "userId": rv["user_id"],
            "userName": rv["user_name"],
            "score": rv["score"],
            "comment": rv["comment"],
            "date": rv["date"],
            "createdAt": _format_kst_timestamp(rv["created_at"]),
            "receipt": bool(rv["receipt"])
        })

    cursor.execute("""
        SELECT id, admin_id, action, target_id, detail, created_at
        FROM admin_audit_logs
        ORDER BY id DESC
        LIMIT 100
    """)
    audit_logs = [
        {
            "id": lg["id"],
            "adminId": lg["admin_id"],
            "action": lg["action"],
            "targetId": lg["target_id"],
            "detail": lg["detail"] or "",
            "createdAt": lg["created_at"]
        }
        for lg in cursor.fetchall()
    ]

    cursor.execute("""
        SELECT rp.id, rp.review_id, rp.reporter_id, rp.reason, rp.detail, rp.status,
               rp.resolved_by, rp.resolved_at, rp.created_at,
               r.user_id AS author_id, r.user_name AS author_name, r.comment AS review_comment,
               rest.name AS restaurant_name
        FROM reports rp
        LEFT JOIN reviews r ON rp.review_id = r.id
        LEFT JOIN restaurants rest ON r.restaurant_id = rest.id
        ORDER BY CASE WHEN rp.status = 'pending' THEN 0 ELSE 1 END, rp.id DESC
        LIMIT 50
    """)
    reports_list = [
        {
            "id": rp["id"],
            "reviewId": rp["review_id"],
            "reporterId": rp["reporter_id"],
            "reason": rp["reason"],
            "detail": rp["detail"] or "",
            "status": rp["status"],
            "resolvedBy": rp["resolved_by"] or "",
            "resolvedAt": rp["resolved_at"] or "",
            "createdAt": rp["created_at"],
            "authorId": rp["author_id"] or "-",
            "authorName": rp["author_name"] or "(삭제된 리뷰)",
            "reviewComment": rp["review_comment"] or "(삭제됨)",
            "restaurantName": rp["restaurant_name"] or "-"
        }
        for rp in cursor.fetchall()
    ]
    pending_reports_count = sum(1 for rp in reports_list if rp["status"] == "pending")

    cursor.execute("""
        SELECT id, user_id, category, message, device_info, photos, status, resolved_by, resolved_at, created_at
        FROM feedbacks
        ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC
        LIMIT 50
    """)
    feedbacks_list = []
    for fb in cursor.fetchall():
        try:
            fb_photos = json.loads(fb["photos"] or "[]")
        except Exception:
            fb_photos = []
        feedbacks_list.append({
            "id": fb["id"],
            "userId": fb["user_id"],
            "category": fb["category"],
            "message": fb["message"],
            "deviceInfo": fb["device_info"] or "",
            "photos": fb_photos if isinstance(fb_photos, list) else [],
            "status": fb["status"],
            "resolvedBy": fb["resolved_by"] or "",
            "resolvedAt": fb["resolved_at"] or "",
            "createdAt": fb["created_at"]
        })
    pending_feedbacks_count = sum(1 for fb in feedbacks_list if fb["status"] == "pending")

    conn.close()
    return {
        "summary": {
            "totalUsers": len(users_list),
            "activeUsers": status_counts["active"],
            "suspendedUsers": status_counts["suspended"],
            "bannedUsers": status_counts["banned"],
            "totalReviews": len(reviews_list),
            "totalAuditLogs": len(audit_logs),
            "pendingReports": pending_reports_count,
            "pendingFeedbacks": pending_feedbacks_count
        },
        "users": users_list,
        "reviews": reviews_list,
        "auditLogs": audit_logs,
        "reports": reports_list,
        "feedbacks": feedbacks_list
    }


def unmask_user_pii(admin_id, target_user_id, reason="이상 행동 확인"):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email_enc, phone_enc, real_name_enc, last_ip_hash FROM users WHERE id = ?", (target_user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    real_name = decrypt_pii(row["real_name_enc"]) if row["real_name_enc"] else "미등록"
    email = decrypt_pii(row["email_enc"]) if row["email_enc"] else "미등록"
    phone = decrypt_pii(row["phone_enc"]) if row["phone_enc"] else "미등록"

    log_admin_action(conn, admin_id, "UNMASK_PII", target_user_id, f"신상 정보 복호화 열람 (사유: {reason})")
    conn.commit()
    conn.close()
    return {
        "id": row["id"],
        "name": row["name"],
        "realName": real_name,
        "email": email,
        "phone": phone,
        "ipHash": row["last_ip_hash"] or "-"
    }


def moderate_user(admin_id, target_user_id, new_status, reason=""):
    if new_status not in ("active", "suspended", "banned"):
        return False, "올바르지 않은 상태 값입니다."
    if target_user_id == admin_id:
        return False, "관리자 본인 계정은 제재할 수 없습니다."

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, role FROM users WHERE id = ?", (target_user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "대상 사용자를 찾을 수 없습니다."

    clean_reason = reason.strip() if new_status != "active" else ""
    cursor.execute("UPDATE users SET status = ?, ban_reason = ? WHERE id = ?", (new_status, clean_reason, target_user_id))

    action_map = {"active": "UNBAN_USER", "suspended": "SUSPEND_USER", "banned": "BAN_USER"}
    log_admin_action(conn, admin_id, action_map[new_status], target_user_id, f"상태 변경 -> {new_status} / 사유: {clean_reason or '해제'}")
    conn.commit()
    conn.close()
    return True, None


def update_review(review_id, user_id, score, comment, photos=None, criteria=None, receipt_photo=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, restaurant_id, user_id, user_prior_avg, score, receipt, receipt_photo, photos, criteria FROM reviews WHERE id = ?", (review_id,))
    rv = cursor.fetchone()
    if not rv:
        conn.close()
        return None, "리뷰를 찾을 수 없습니다."
    if rv["user_id"] != user_id:
        conn.close()
        return None, "본인이 작성한 리뷰만 수정할 수 있습니다."

    new_score = round(max(1.0, min(5.0, float(score))), 1)
    new_comment = (comment or "").strip()
    if not new_comment:
        conn.close()
        return None, "리뷰 내용을 입력해 주세요."

    new_delta = round(new_score - float(rv["user_prior_avg"]), 2)
    score_diff = new_score - float(rv["score"])
    photos_json = json.dumps(photos, ensure_ascii=False) if photos is not None else rv["photos"]
    criteria_json = json.dumps(criteria, ensure_ascii=False) if criteria is not None else rv["criteria"]
    if receipt_photo is not None:
        new_receipt_photo = (receipt_photo or "").strip()
        new_receipt = 1 if new_receipt_photo else 0
    else:
        new_receipt_photo = rv["receipt_photo"] if "receipt_photo" in rv.keys() else ""
        new_receipt = rv["receipt"]

    cursor.execute(
        "UPDATE reviews SET score = ?, delta = ?, comment = ?, receipt = ?, receipt_photo = ?, photos = ?, criteria = ? WHERE id = ?",
        (new_score, new_delta, new_comment, new_receipt, new_receipt_photo, photos_json, criteria_json, review_id)
    )
    cursor.execute(
        "UPDATE users SET rating_sum = MAX(0.0, rating_sum + ?) WHERE id = ?",
        (score_diff, user_id)
    )
    _recalculate_user_review_deltas(cursor, user_id)
    rest_id = rv["restaurant_id"]
    conn.commit()
    conn.close()
    return get_restaurant_by_id(rest_id), None


def delete_own_review(review_id, user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, restaurant_id, user_id, score FROM reviews WHERE id = ?", (review_id,))
    rv = cursor.fetchone()
    if not rv:
        conn.close()
        return None, "리뷰를 찾을 수 없습니다."
    if rv["user_id"] != user_id:
        conn.close()
        return None, "본인이 작성한 리뷰만 삭제할 수 있습니다."

    rest_id = rv["restaurant_id"]
    old_score = float(rv["score"])
    cursor.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    cursor.execute(
        "UPDATE users SET review_count = MAX(0, review_count - 1), rating_sum = MAX(0.0, rating_sum - ?) WHERE id = ?",
        (old_score, user_id)
    )
    _recalculate_user_review_deltas(cursor, user_id)
    conn.commit()
    conn.close()
    return get_restaurant_by_id(rest_id), None


def admin_delete_review(admin_id, review_id, reason="관리자 직권 삭제"):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, restaurant_id, score, comment FROM reviews WHERE id = ?", (review_id,))
    rv = cursor.fetchone()
    if not rv:
        conn.close()
        return False, "리뷰를 찾을 수 없습니다."

    cursor.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    cursor.execute(
        "UPDATE users SET review_count = MAX(0, review_count - 1), rating_sum = MAX(0.0, rating_sum - ?) WHERE id = ?",
        (float(rv["score"]), rv["user_id"]),
    )
    _recalculate_user_review_deltas(cursor, rv["user_id"])
    log_admin_action(conn, admin_id, "DELETE_REVIEW", f"review#{review_id} (user:{rv['user_id']})", f"{reason} / 내용: {rv['comment'][:40]}")
    conn.commit()
    conn.close()
    return True, None


def create_report(review_id, reporter_id, reason, detail=""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM reviews WHERE id = ?", (review_id,))
    if not cursor.fetchone():
        conn.close()
        return None, "신고 대상 리뷰를 찾을 수 없습니다."

    cursor.execute(
        "SELECT id FROM reports WHERE review_id = ? AND reporter_id = ? AND status = 'pending'",
        (review_id, reporter_id)
    )
    if cursor.fetchone():
        conn.close()
        return None, "이미 신고 접수된 리뷰입니다."

    cursor.execute("""
        INSERT INTO reports (review_id, reporter_id, reason, detail, status)
        VALUES (?, ?, ?, ?, 'pending')
    """, (review_id, reporter_id, reason.strip(), detail.strip()))
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return report_id, None


def resolve_report(admin_id, report_id, action):
    if action not in ("dismiss", "delete_review", "ban_author"):
        return False, "올바르지 않은 처리 방식입니다."

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
    rp = cursor.fetchone()
    if not rp:
        conn.close()
        return False, "신고 내역을 찾을 수 없습니다."

    if action == "dismiss":
        cursor.execute(
            "UPDATE reports SET status = 'dismissed', resolved_by = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (admin_id, report_id)
        )
        log_admin_action(conn, admin_id, "DISMISS_REPORT", f"report#{report_id}", "신고 기각 처리")
    else:
        cursor.execute("SELECT id, user_id, comment FROM reviews WHERE id = ?", (rp["review_id"],))
        rv = cursor.fetchone()
        author_id = rv["user_id"] if rv else None
        if rv:
            cursor.execute("DELETE FROM reviews WHERE id = ?", (rv["id"],))
        cursor.execute(
            "UPDATE reports SET status = 'resolved', resolved_by = ?, resolved_at = CURRENT_TIMESTAMP WHERE review_id = ?",
            (admin_id, rp["review_id"])
        )

        if action == "ban_author" and author_id and author_id != admin_id:
            cursor.execute(
                "UPDATE users SET status = 'banned', ban_reason = ? WHERE id = ?",
                (f"신고 처리 제재 ({rp['reason']})", author_id)
            )
            log_admin_action(conn, admin_id, "BAN_FROM_REPORT", author_id, f"신고 #{report_id} 기반 리뷰 삭제 및 영구 차단")
        else:
            log_admin_action(conn, admin_id, "RESOLVE_REPORT", f"report#{report_id}", f"신고 처리로 리뷰 #{rp['review_id']} 삭제")

    conn.commit()
    conn.close()
    return True, None


def create_feedback(user_id, category, message, device_info="", photos=None):
    conn = get_db()
    cursor = conn.cursor()
    photos_json = json.dumps(photos or [], ensure_ascii=False)
    cursor.execute("""
        INSERT INTO feedbacks (user_id, category, message, device_info, photos, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (user_id, (category or "오류 제보").strip(), message.strip(), device_info.strip(), photos_json))
    fb_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return fb_id


def resolve_feedback(admin_id, feedback_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, message FROM feedbacks WHERE id = ?", (feedback_id,))
    fb = cursor.fetchone()
    cursor.execute(
        "UPDATE feedbacks SET status = 'resolved', resolved_by = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (admin_id, feedback_id)
    )
    fb_summary = f"제보자 @{fb['user_id']} / {(fb['message'] or '')[:35]}" if fb else "사용자 오류/제보 확인 완료"
    log_admin_action(conn, admin_id, "RESOLVE_FEEDBACK", f"feedback#{feedback_id}", fb_summary)
    conn.commit()
    conn.close()
    return True


def reset_database():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS feedbacks")
    cursor.execute("DROP TABLE IF EXISTS reports")
    cursor.execute("DROP TABLE IF EXISTS admin_audit_logs")
    cursor.execute("DROP TABLE IF EXISTS bookmarks")
    cursor.execute("DROP TABLE IF EXISTS reviews")
    cursor.execute("DROP TABLE IF EXISTS restaurants")
    cursor.execute("DROP TABLE IF EXISTS users")
    conn.commit()
    conn.close()
    init_db()


if __name__ == "__main__":
    init_db()
    print("홍슐랭 SQLite 데이터베이스 초기화 및 시드 데이터 적재 완료!")

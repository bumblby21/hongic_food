from flask import Flask, request, jsonify, send_from_directory, session
import os
import re
import math
import uuid
import shutil
import subprocess
from PIL import Image, ImageOps
import database

app = Flask(__name__, static_folder=".")
app.config['JSON_AS_ASCII'] = False
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max upload
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get("HONGCHELIN_HTTPS") == "1":
    app.config['SESSION_COOKIE_SECURE'] = True
app.secret_key = os.environ.get("HONGCHELIN_SECRET", "hongchelin-local-secret-key-2026")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
OCR_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_receipt.ps1")
VERIFIED_RECEIPT_URLS = set()


def _extract_receipt_text(image_path):
    """Run cross-platform Korean/English OCR (Tesseract on Linux/cloud, Windows.Media.Ocr fallback on local Windows)."""
    temp_path = None
    target_path = os.path.abspath(image_path)
    try:
        with Image.open(target_path) as im:
            oriented = ImageOps.exif_transpose(im).convert("RGB")
            temp_path = target_path + ".ocr.png"
            oriented.save(temp_path, format="PNG")
            target_path = temp_path
    except Exception:
        pass

    try:
        # 1. Cross-platform Tesseract OCR (Linux: apt install tesseract-ocr tesseract-ocr-kor)
        tess_bin = shutil.which("tesseract")
        if tess_bin:
            res = subprocess.run(
                [tess_bin, target_path, "stdout", "-l", "kor+eng", "--psm", "6"],
                capture_output=True,
                timeout=20,
            )
            txt = res.stdout.decode("utf-8", errors="ignore").strip()
            if txt:
                return txt

        # 2. Local Windows fallback (Windows.Media.Ocr) when tesseract binary is not installed
        if os.name == "nt" and os.path.exists(OCR_SCRIPT_PATH):
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_SCRIPT_PATH, target_path],
                capture_output=True,
                timeout=20,
            )
            return res.stdout.decode("utf-8", errors="ignore").strip()

        return ""
    except Exception as e:
        print("Receipt OCR error:", e)
        return ""
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _norm_ocr(text):
    return re.sub(r"[\s\(\)\[\]\-·.,/:;~!?@#$%^&*]+", "", (text or "").lower())


def _verify_receipt_match(ocr_text, rest_name, rest_addr="", rest_phone=""):
    """Verify that OCR text contains enough evidence of the target restaurant."""
    norm_ocr = _norm_ocr(ocr_text)
    if len(norm_ocr) < 4:
        return False, "영수증에서 글자를 인식하지 못했습니다. 선명하게 찍힌 영수증 사진을 올려주세요."

    norm_name = _norm_ocr(rest_name)
    if norm_name and norm_name in norm_ocr:
        return True, f"'{rest_name}' 영수증 확인이 완료되었습니다."

    # Check meaningful tokens from restaurant name (e.g., "삼각지 몽탄" -> ["삼각지", "몽탄"], "삐딱 을지로점" -> ["삐딱", "을지로점"])
    raw_tokens = [t.strip() for t in re.split(r"[\s·\-/()]+", rest_name or "") if len(t.strip()) >= 2]
    generic_words = {"본점", "지점", "식당", "맛집", "카페", "서울", "한국", "음식점"}
    brand_tokens = []
    for t in raw_tokens:
        t_clean = re.sub(r"(점|본점)$", "", t) if len(t) > 2 else t
        nt = _norm_ocr(t_clean)
        if len(nt) >= 2 and nt not in generic_words:
            brand_tokens.append(nt)

    matched_name_token = any(tok in norm_ocr for tok in brand_tokens)

    # Check address road/dong fragment or phone number digits
    addr_match = False
    if rest_addr:
        addr_tokens = [t for t in re.split(r"[\s,]+", rest_addr) if len(t) >= 3 and any(c.isdigit() for c in t) or t.endswith(("길", "로", "동", "가"))]
        for at in addr_tokens:
            nat = _norm_ocr(at)
            if len(nat) >= 3 and nat in norm_ocr:
                addr_match = True
                break

    phone_digits = re.sub(r"\D", "", rest_phone or "")
    phone_match = len(phone_digits) >= 7 and (phone_digits[-8:] in re.sub(r"\D", "", ocr_text or ""))

    if matched_name_token or (phone_match and len(brand_tokens) == 0) or (addr_match and phone_match):
        return True, f"'{rest_name}' 영수증 확인이 완료되었습니다."

    return False, f"영수증에서 '{rest_name}' 매장 정보를 찾지 못했습니다. 해당 가게의 영수증이 맞는지 확인해 주세요."

# Ensure DB initialized
database.init_db()

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/manifest.json")
def pwa_manifest():
    return send_from_directory(".", "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def pwa_sw():
    return send_from_directory(".", "sw.js", mimetype="application/javascript")


@app.route("/icons/<path:filename>")
def pwa_icons(filename):
    return send_from_directory("icons", filename)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


def _save_optimized_image(file_storage, prefix="", max_dim=1600, quality=82):
    """Auto-orient, resize large phone photos, and compress to keep uploads fast and lightweight."""
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext == ".gif":
        new_name = f"{prefix}{uuid.uuid4().hex}{ext}"
        save_path = os.path.join(UPLOAD_DIR, new_name)
        file_storage.save(save_path)
        return new_name, save_path

    try:
        im = Image.open(file_storage.stream)
        im = ImageOps.exif_transpose(im)
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode == "P":
                im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        new_name = f"{prefix}{uuid.uuid4().hex}.jpg"
        save_path = os.path.join(UPLOAD_DIR, new_name)
        im.save(save_path, format="JPEG", quality=quality, optimize=True)
        return new_name, save_path
    except Exception:
        file_storage.stream.seek(0)
        new_name = f"{prefix}{uuid.uuid4().hex}{ext}"
        save_path = os.path.join(UPLOAD_DIR, new_name)
        file_storage.save(save_path)
        return new_name, save_path


@app.route("/api/upload", methods=["POST"])
def upload_photos():
    if not session.get("user_id"):
        return jsonify({"success": False, "error": "사진을 업로드하려면 로그인이 필요합니다."}), 401
    try:
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            return jsonify({"success": False, "error": "업로드할 이미지 파일을 선택해 주세요."}), 400

        saved_urls = []
        for f in files:
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTS:
                return jsonify({"success": False, "error": f"지원하지 않는 이미지 형식입니다: {ext}"}), 400
            new_name, _ = _save_optimized_image(f, max_dim=1600, quality=82)
            saved_urls.append(f"/uploads/{new_name}")

        if not saved_urls:
            return jsonify({"success": False, "error": "유효한 이미지 파일이 없습니다."}), 400
        return jsonify({"success": True, "urls": saved_urls})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/verify-receipt", methods=["POST"])
def verify_receipt_route():
    if not session.get("user_id"):
        return jsonify({"success": False, "error": "영수증 인증을 사용하려면 로그인이 필요합니다."}), 401
    try:
        f = request.files.get("file") or (request.files.getlist("files")[0] if request.files.getlist("files") else None)
        if not f or not f.filename:
            return jsonify({"success": False, "error": "영수증 이미지를 선택해 주세요."}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            return jsonify({"success": False, "error": f"지원하지 않는 이미지 형식입니다: {ext}"}), 400

        rest_id = request.form.get("restaurantId")
        rest_name = (request.form.get("restaurantName") or "").strip()
        rest_addr = (request.form.get("restaurantAddress") or "").strip()
        rest_phone = (request.form.get("restaurantPhone") or "").strip()

        if rest_id:
            r_obj = database.get_restaurant_by_id(int(rest_id))
            if r_obj:
                rest_name = r_obj.get("name", rest_name)
                rest_addr = r_obj.get("address", rest_addr)
                rest_phone = r_obj.get("phone", rest_phone)

        if not rest_name:
            return jsonify({"success": False, "error": "대상 가게 정보가 없습니다."}), 400

        new_name, save_path = _save_optimized_image(f, prefix="receipt_", max_dim=1800, quality=88)

        ocr_text = _extract_receipt_text(save_path)
        ok, msg = _verify_receipt_match(ocr_text, rest_name, rest_addr, rest_phone)
        if not ok:
            try:
                os.remove(save_path)
            except Exception:
                pass
            return jsonify({"success": True, "verified": False, "error": msg})

        url = f"/uploads/{new_name}"
        VERIFIED_RECEIPT_URLS.add(url)
        return jsonify({"success": True, "verified": True, "url": url, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/restaurants", methods=["GET"])
def get_restaurants():
    try:
        q = request.args.get("q", "").strip().lower()
        restaurants = database.get_all_restaurants()
        if q:
            filtered = []
            for r in restaurants:
                tags_text = " ".join(r.get("tags", []))
                menus_text = " ".join([m.get("name", "") for m in r.get("menus", [])])
                corpus = f"{r.get('name', '')} {r.get('category', '')} {r.get('address', '')} {r.get('subway', '')} {tags_text} {menus_text}".lower()
                if q in corpus:
                    filtered.append(r)
            return jsonify({"success": True, "data": filtered})
        return jsonify({"success": True, "data": restaurants})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/restaurants/<int:id>", methods=["GET"])
def get_restaurant(id):
    try:
        restaurant = database.get_restaurant_by_id(id)
        if not restaurant:
            return jsonify({"success": False, "error": "Not found"}), 404
        return jsonify({"success": True, "data": restaurant})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/users", methods=["GET"])
def get_users():
    try:
        users = database.get_users()
        return jsonify({"success": True, "data": users})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# 인증 (회원가입 / 로그인 / 로그아웃) & 마이페이지 / 북마크 API
# ============================================================
@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": True, "user": None, "bookmarkIds": []})
    user = database.get_user_by_id(user_id)
    if not user or user.get("status") != "active":
        session.pop("user_id", None)
        return jsonify({"success": True, "user": None, "bookmarkIds": []})
    bookmark_ids = database.get_user_bookmark_ids(user_id)
    return jsonify({"success": True, "user": user, "bookmarkIds": bookmark_ids})


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    try:
        data = request.json or {}
        user_id = (data.get("userId") or "").strip()
        name = (data.get("name") or "").strip()
        real_name = (data.get("realName") or "").strip()
        password = data.get("password") or ""
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()

        if not user_id or len(user_id) < 2:
            return jsonify({"success": False, "error": "아이디는 2자 이상 입력해 주세요."}), 400
        if not name:
            return jsonify({"success": False, "error": "닉네임을 입력해 주세요."}), 400
        if not real_name or len(real_name) < 2:
            return jsonify({"success": False, "error": "실명을 2자 이상 입력해 주세요."}), 400
        if len(password) < 4:
            return jsonify({"success": False, "error": "비밀번호는 4자 이상 입력해 주세요."}), 400
        if not email or "@" not in email or "." not in email.split("@", 1)[-1]:
            return jsonify({"success": False, "error": "올바른 이메일 주소를 입력해 주세요."}), 400
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 9 or len(digits) > 12:
            return jsonify({"success": False, "error": "올바른 휴대폰 번호를 입력해 주세요. (예: 010-1234-5678)"}), 400

        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        user, err = database.create_user(user_id, name, password, email=email, phone=phone, real_name=real_name, ip_addr=client_ip)
        if err:
            return jsonify({"success": False, "error": err}), 409

        session["user_id"] = user["id"]
        return jsonify({"success": True, "user": user, "bookmarkIds": []})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    try:
        data = request.json or {}
        user_id = (data.get("userId") or "").strip()
        password = data.get("password") or ""
        if not user_id or not password:
            return jsonify({"success": False, "error": "아이디와 비밀번호를 모두 입력해 주세요."}), 400

        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        user, err = database.authenticate_user(user_id, password, ip_addr=client_ip)
        if err:
            status_code = 403 if "정지" in err else 401
            return jsonify({"success": False, "error": err}), status_code

        session["user_id"] = user["id"]
        bookmark_ids = database.get_user_bookmark_ids(user["id"])
        return jsonify({"success": True, "user": user, "bookmarkIds": bookmark_ids})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user_id", None)
    return jsonify({"success": True})


@app.route("/api/auth/delete-account", methods=["POST"])
def auth_delete_account():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "로그인이 필요합니다."}), 401
    try:
        data = request.json or {}
        password = data.get("password") or ""
        if not password:
            return jsonify({"success": False, "error": "비밀번호를 입력해 주세요."}), 400
        ok, err = database.delete_user_account(user_id, password, upload_dir=UPLOAD_DIR)
        if not ok:
            status_code = 403 if "관리자" in (err or "") else 400
            return jsonify({"success": False, "error": err}), status_code
        session.pop("user_id", None)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# 관리자 전용 API (RBAC: role == 'admin' 검증 필수)
# ============================================================
def _require_admin():
    user_id = session.get("user_id")
    if not user_id:
        return None, (jsonify({"success": False, "error": "관리자 로그인이 필요합니다."}), 401)
    user = database.get_user_by_id(user_id)
    if not user or user.get("role") != "admin" or user.get("status") != "active":
        return None, (jsonify({"success": False, "error": "관리자 권한이 없습니다."}), 403)
    return user, None


@app.route("/api/admin/overview", methods=["GET"])
def admin_overview():
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        data = database.get_admin_overview()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<target_id>/unmask", methods=["POST"])
def admin_unmask_user(target_id):
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        data = request.json or {}
        reason = (data.get("reason") or "관리자 이상 행동 확인").strip()
        pii = database.unmask_user_pii(admin_user["id"], target_id, reason=reason)
        if not pii:
            return jsonify({"success": False, "error": "사용자를 찾을 수 없습니다."}), 404
        return jsonify({"success": True, "data": pii})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<target_id>/moderate", methods=["POST"])
def admin_moderate_user(target_id):
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        data = request.json or {}
        status = (data.get("status") or "").strip()
        reason = (data.get("reason") or "").strip()
        ok, err = database.moderate_user(admin_user["id"], target_id, status, reason=reason)
        if not ok:
            return jsonify({"success": False, "error": err}), 400
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/reviews/<int:review_id>", methods=["DELETE"])
def admin_remove_review(review_id):
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        ok, err = database.admin_delete_review(admin_user["id"], review_id)
        if not ok:
            return jsonify({"success": False, "error": err}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reports", methods=["POST"])
def create_report_route():
    try:
        data = request.json or {}
        review_id = data.get("reviewId")
        reason = (data.get("reason") or "부적절한 리뷰").strip()
        detail = (data.get("detail") or "").strip()
        reporter_id = session.get("user_id") or data.get("reporterId") or "guest"
        if not review_id:
            return jsonify({"success": False, "error": "reviewId가 필요합니다."}), 400
        report_id, err = database.create_report(int(review_id), reporter_id, reason, detail=detail)
        if err:
            return jsonify({"success": False, "error": err}), 400
        return jsonify({"success": True, "reportId": report_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/reports/<int:report_id>/resolve", methods=["POST"])
def admin_resolve_report_route(report_id):
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        data = request.json or {}
        action = (data.get("action") or "").strip()
        ok, err = database.resolve_report(admin_user["id"], report_id, action)
        if not ok:
            return jsonify({"success": False, "error": err}), 400
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/feedback", methods=["POST"])
def submit_feedback_route():
    try:
        data = request.json or {}
        category = (data.get("category") or "오류 제보").strip()
        message = (data.get("message") or "").strip()
        device_info = (data.get("deviceInfo") or "").strip()
        photos = data.get("photos") or []
        if not isinstance(photos, list):
            photos = []
        photos = [str(p).strip() for p in photos if str(p).strip()]
        user_id = session.get("user_id") or data.get("userId") or "guest"
        if not message and not photos:
            return jsonify({"success": False, "error": "내용이나 캡처 사진을 입력해 주세요."}), 400
        fb_id = database.create_feedback(
            user_id,
            category,
            message or "(캡처 사진 첨부)",
            device_info=device_info,
            photos=photos,
        )
        return jsonify({"success": True, "feedbackId": fb_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/feedbacks/<int:fb_id>/resolve", methods=["POST"])
def admin_resolve_feedback_route(fb_id):
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        database.resolve_feedback(admin_user["id"], fb_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mypage", methods=["GET"])
def get_mypage():
    try:
        user_id = session.get("user_id") or request.args.get("userId")
        if not user_id:
            return jsonify({"success": False, "error": "로그인이 필요합니다."}), 401
        data = database.get_user_mypage(user_id)
        if not data:
            return jsonify({"success": False, "error": "사용자를 찾을 수 없습니다."}), 404
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mypage/standard-score", methods=["POST"])
def update_standard_score():
    try:
        data = request.json or {}
        user_id = session.get("user_id") or data.get("userId")
        if not user_id:
            return jsonify({"success": False, "error": "로그인이 필요합니다."}), 401
        val = float(data.get("standardScore", 3.0))
        saved = database.update_user_standard_score(user_id, val)
        return jsonify({"success": True, "standardScore": saved})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bookmarks", methods=["GET"])
def get_bookmarks():
    user_id = session.get("user_id") or request.args.get("userId")
    if not user_id:
        return jsonify({"success": True, "bookmarkIds": []})
    ids = database.get_user_bookmark_ids(user_id)
    return jsonify({"success": True, "bookmarkIds": ids})


@app.route("/api/bookmarks/toggle", methods=["POST"])
def toggle_bookmark_route():
    try:
        data = request.json or {}
        user_id = session.get("user_id") or data.get("userId")
        restaurant_id = data.get("restaurantId")
        if not user_id:
            return jsonify({"success": False, "error": "로그인이 필요합니다."}), 401
        if not restaurant_id:
            return jsonify({"success": False, "error": "restaurantId가 필요합니다."}), 400
        bookmarked, ids = database.toggle_bookmark(user_id, int(restaurant_id))
        return jsonify({"success": True, "bookmarked": bookmarked, "bookmarkIds": ids})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/reviews", methods=["POST"])
def create_review():
    try:
        data = request.json or {}
        restaurant_id = data.get("restaurantId")
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"success": False, "error": "로그인이 필요합니다."}), 401
        u_info = database.get_user_by_id(user_id)
        if u_info and u_info.get("status") != "active":
            return jsonify({"success": False, "error": "정지된 계정은 리뷰를 작성할 수 없습니다."}), 403
        score = float(data.get("score", 5.0))
        comment = data.get("comment", "")
        receipt_photo = (data.get("receiptPhoto") or "").strip()
        if receipt_photo and receipt_photo not in VERIFIED_RECEIPT_URLS:
            receipt_photo = ""
        receipt = bool(receipt_photo)
        photos = data.get("photos", [])

        if not restaurant_id:
            return jsonify({"success": False, "error": "restaurantId is required"}), 400

        criteria = data.get("criteria", {})

        updated_restaurant = database.add_review(
            restaurant_id=restaurant_id,
            user_id=user_id,
            score=score,
            comment=comment,
            receipt=receipt,
            photos=photos,
            criteria=criteria,
            receipt_photo=receipt_photo,
        )

        return jsonify({
            "success": True,
            "message": "리뷰가 SQLite 데이터베이스에 성공적으로 영구 저장되었습니다.",
            "data": updated_restaurant
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reviews/<int:review_id>", methods=["PUT"])
def update_own_review_route(review_id):
    try:
        data = request.json or {}
        user_id = session.get("user_id") or data.get("userId")
        if not user_id:
            return jsonify({"success": False, "error": "사용자 정보가 필요합니다."}), 401
        score = float(data.get("score", 5.0))
        comment = data.get("comment", "")
        photos = data.get("photos")
        criteria = data.get("criteria")
        receipt_photo = data.get("receiptPhoto")
        updated_restaurant, err = database.update_review(review_id, user_id, score, comment, photos=photos, criteria=criteria, receipt_photo=receipt_photo)
        if err:
            code = 403 if "본인" in err else 400
            return jsonify({"success": False, "error": err}), code
        return jsonify({"success": True, "data": updated_restaurant})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reviews/<int:review_id>", methods=["DELETE"])
def delete_own_review_route(review_id):
    try:
        data = (request.get_json(silent=True) or {}) if request.data else {}
        user_id = session.get("user_id") or request.args.get("userId") or data.get("userId")
        if not user_id:
            return jsonify({"success": False, "error": "사용자 정보가 필요합니다."}), 401
        updated_restaurant, err = database.delete_own_review(review_id, user_id)
        if err:
            code = 403 if "본인" in err else 400
            return jsonify({"success": False, "error": err}), code
        return jsonify({"success": True, "data": updated_restaurant})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def _fetch_naver_weekly_hours(naver_id):
    """Best-effort 1-shot lookup of weekly business hours via Naver Place GraphQL; returns '' on rate-limit/error."""
    if not naver_id:
        return ""
    try:
        import urllib.request
        url = "https://pcmap-api.place.naver.com/graphql"
        query = """
        query getPlaceBase($id: String!, $deviceType: String) {
          placeDetail(input: {id: $id, deviceType: $deviceType, isNx: false}) {
            newBusinessHours {
              day
              businessHours { start end }
              breakHours { start end }
              description
            }
          }
        }
        """
        payload = json.dumps([{
            "operationName": "getPlaceBase",
            "variables": {"id": str(naver_id), "deviceType": "pcmap"},
            "query": query
        }]).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": f"https://pcmap.place.naver.com/restaurant/{naver_id}/home",
            "Origin": "https://pcmap.place.naver.com"
        })
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
        detail = (raw[0] if isinstance(raw, list) and raw else raw).get("data", {}).get("placeDetail", {})
        items = detail.get("newBusinessHours") or []
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            day = (item.get("day") or "").strip()
            bh = item.get("businessHours") or {}
            start = (bh.get("start") or "").strip()
            end = (bh.get("end") or "").strip()
            desc = (item.get("description") or "").strip()
            if start and end:
                txt = f"{day} {start}~{end}".strip()
                brks = item.get("breakHours") or []
                if brks and isinstance(brks, list) and isinstance(brks[0], dict):
                    bs, be = brks[0].get("start"), brks[0].get("end")
                    if bs and be:
                        txt += f" (휴게 {bs}~{be})"
                lines.append(txt)
            elif day and desc:
                lines.append(f"{day} {desc}")
        return "\n".join(lines)
    except Exception:
        return ""


@app.route("/api/restaurants", methods=["POST"])
def create_restaurant():
    try:
        data = request.json or {}
        name = data.get("name")
        if not name:
            return jsonify({"success": False, "error": "식당 이름은 필수입니다."}), 400

        area = data.get("area", "기타/서울")
        category = data.get("category", "음식점")
        address = data.get("address", "서울시")
        subway = data.get("subway", "지하철역 인근")
        phone = data.get("phone", "정보 없음")
        hours = data.get("hours", "매일 11:30 ~ 21:00")
        naver_id = (data.get("naverId") or "").strip()
        parking = data.get("parking", "주차 문의")
        intro = data.get("intro", f"{name}에 방문한 미식가가 직접 등록한 신규 맛집입니다.")
        tags = data.get("tags", ["#신규등록", "#사용자추천"])
        lat = float(data.get("lat", 37.5665))
        lng = float(data.get("lng", 126.9780))
        photos = data.get("photos", [])
        menus = data.get("menus", [])

        # 1. Check if restaurant with the same name already exists in SQLite
        existing_restaurants = database.get_all_restaurants()
        existing_match = next((r for r in existing_restaurants if r.get("name", "").strip() == name.strip()), None)

        if existing_match:
            target_id = existing_match["id"]
        else:
            weekly_hours = _fetch_naver_weekly_hours(naver_id)
            if weekly_hours:
                hours = weekly_hours
            target_id = database.add_restaurant(
                name=name, area=area, category=category, address=address,
                subway=subway, phone=phone, hours=hours, parking=parking,
                intro=intro, tags=tags, lat=lat, lng=lng,
                photos=photos, menus=menus
            )

        # 2. Add initial review if provided
        initial_review = data.get("initialReview")
        if initial_review:
            user_id = initial_review.get("userId", "normal")
            score = float(initial_review.get("score", 4.0))
            comment = initial_review.get("comment", "정말 만족스러운 식사였습니다. 추천합니다!")
            criteria = initial_review.get("criteria", {})
            rev_photos = initial_review.get("photos", photos[:1] if photos else [])
            receipt_photo = (initial_review.get("receiptPhoto") or "").strip()
            if receipt_photo and receipt_photo not in VERIFIED_RECEIPT_URLS:
                receipt_photo = ""
            database.add_review(
                restaurant_id=target_id,
                user_id=user_id,
                score=score,
                comment=comment,
                receipt=bool(receipt_photo),
                photos=rev_photos,
                criteria=criteria,
                receipt_photo=receipt_photo,
            )

        restaurant = database.get_restaurant_by_id(target_id)
        return jsonify({
            "success": True,
            "message": f"'{name}' 식당이 홍슐랭 랭킹판에 성공적으로 반영되었습니다!",
            "data": restaurant
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# Built-in Seoul Top Restaurants Catalog for instant search
BUILTIN_PLACES = [
    {
        "name": "삼각지 몽탄", "area": "용산/삼각지", "category": "고기요리 · 짚불구이우대갈비",
        "address": "서울 용산구 백범로99길 50", "subway": "삼각지역 8번 출구 도보 2분 (120m)",
        "phone": "02-794-8592", "lat": 37.5358, "lng": 126.9729,
        "hours": "12:00 ~ 22:00 (L.O 21:00)", "parking": "인근 삼각지 유료주차장 이용",
        "intro": "전남 무안 짚불구이 전통 방식을 계승한 압도적인 육향의 우대갈비 전문점.",
        "photos": ["https://images.unsplash.com/photo-1555939594-58d7cb561ad1?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "우대갈비 (280g)", "price": "32,000원", "desc": "짚불로 초벌하여 볏짚 향이 가득 밴 소갈비"}, {"name": "짚불삼겹살", "price": "18,000원", "desc": "짚불에 구워 담백하고 쫄깃한 삼겹살"}]
    },
    {
        "name": "신당동 금돼지식당", "area": "신당/약수", "category": "고기요리 · 본삼겹/눈꽃목살",
        "address": "서울 중구 다산로 149", "subway": "약수역 2번 출구 도보 3분 (190m)",
        "phone": "0507-1307-8750", "lat": 37.5562, "lng": 127.0145,
        "hours": "11:30 ~ 23:00 (브레이크타임 없음)", "parking": "주차 불가 (청구공영주차장 이용 권장)",
        "intro": "미쉐린 빕 구르망에 연속 선정된 프리미엄 YBD 뼈붙은 본삼겹살의 최고봉.",
        "photos": ["https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "본삼겹 (150g)", "price": "19,000원", "desc": "뼈가 붙어있는 진짜 갈비 삼겹살 부위"}, {"name": "눈꽃목살", "price": "18,000원", "desc": "마블링이 눈꽃처럼 피어난 육즙 가득 목살"}]
    },
    {
        "name": "명동교자 본점", "area": "명동/중구", "category": "한식 · 칼국수/만두",
        "address": "서울 중구 명동10길 29", "subway": "명동역 8번 출구 도보 3분 (210m)",
        "phone": "02-776-5348", "lat": 37.5630, "lng": 126.9858,
        "hours": "10:30 ~ 21:00 (L.O 20:30)", "parking": "주차 불가 (명동 밀리오레 유료주차장 이용)",
        "intro": "1966년 창업 이래 진한 닭육수와 불맛 고명, 마늘김치로 전설이 된 전통 칼국수.",
        "photos": ["https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "명동칼국수", "price": "11,000원", "desc": "진한 닭육수에 얇고 부드러운 완당면"}, {"name": "전통 만두", "price": "12,000원", "desc": "육즙이 풍부한 최고급 돈육 소로 빚은 만두"}]
    },
    {
        "name": "을지로 우래옥", "area": "을지로/종로", "category": "한식 · 평양냉면/불고기",
        "address": "서울 중구 창경궁로 62-29", "subway": "을지로4가역 4번 출구 도보 1분 (90m)",
        "phone": "02-2265-0151", "lat": 37.5682, "lng": 126.9987,
        "hours": "11:30 ~ 21:30 (매주 월요일 정기휴무)", "parking": "매장 전용 주차장 무료 발렛 가능",
        "intro": "1946년 개업하여 서울에서 가장 진한 100% 한우 순면 평양냉면을 자랑하는 노포.",
        "photos": ["https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "전통 평양냉면", "price": "16,000원", "desc": "한우 육수의 묵직한 육향과 메밀면의 정수"}, {"name": "우래옥 불고기", "price": "37,000원", "desc": "한우 암소만을 사용한 달콤한 직화 불고기"}]
    },
    {
        "name": "대학로 정돈 본점", "area": "혜화/대학로", "category": "일식 · 프리미엄수제돈카츠",
        "address": "서울 종로구 대학로9길 12", "subway": "혜화역 3번 출구 도보 3분 (180m)",
        "phone": "0507-1408-0924", "lat": 37.5824, "lng": 127.0009,
        "hours": "11:30 ~ 21:30 (B.T 15:30~17:00)", "parking": "주차 불가",
        "intro": "국내 최초로 저온 튀김 안심 돈카츠 열풍을 일으킨 대학로 대표 프리미엄 카츠.",
        "photos": ["https://images.unsplash.com/photo-1591814468924-caf88d1232e1?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "안심 돈카츠 (220g)", "price": "15,000원", "desc": "부드러운 육즙이 입안 가득 퍼지는 동글한 안심카츠"}, {"name": "등심 돈카츠", "price": "14,000원", "desc": "지방층의 고소함과 쫄깃한 식감이 조화로운 등심"}]
    },
    {
        "name": "한남동 다운타우너", "area": "한남/이태원", "category": "양식 · 수제버거/프라이즈",
        "address": "서울 용산구 대사관로5길 12 1층", "subway": "한강진역 3번 출구 도보 7분 (480m)",
        "phone": "0507-1405-3696", "lat": 37.5359, "lng": 127.0022,
        "hours": "11:00 ~ 21:00 (매일 운영)", "parking": "발렛 파킹 가능 (유료)",
        "intro": "아보카도가 꽃처럼 들어간 시그니처 아보카도 버거로 트렌드를 선도한 한남동 버거 성지.",
        "photos": ["https://images.unsplash.com/photo-1550547660-d9450f859349?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "아보카도 버거", "price": "11,800원", "desc": "신선한 슬라이스 아보카도와 베이컨, 비프 패티"}, {"name": "갈릭 버터 프라이즈", "price": "6,800원", "desc": "달콤알싸한 특제 갈릭 버터 소스를 듬뿍 얹은 감자튀김"}]
    },
    {
        "name": "연남동 툭툭누들타이", "area": "홍대/연남", "category": "아시안 · 태국음식/똠양꿍",
        "address": "서울 마포구 성미산로 161-8", "subway": "홍대입구역 3번 출구 도보 11분 (750m)",
        "phone": "0507-1400-5130", "lat": 37.5645, "lng": 126.9242,
        "hours": "11:30 ~ 21:20 (B.T 15:00~17:00)", "parking": "매장 전용 주차장 4대 가능",
        "intro": "연남동을 태국 요리의 메카로 만든 미쉐린 빕 구르망 태국 현지식 전문점.",
        "photos": ["https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "뿌님 팟퐁커리", "price": "29,000원", "desc": "바삭하게 튀긴 소프트쉘 크랩에 부드러운 옐로 커리 계란 소스"}, {"name": "똠양꿍", "price": "18,000원", "desc": "레몬그라스와 라임즙으로 맛을 낸 새콤매콤 전통 스프"}]
    },
    {
        "name": "경복궁 토속촌 삼계탕", "area": "서촌/경복궁", "category": "한식 · 전통삼계탕/해물파전",
        "address": "서울 종로구 자하문로5길 5", "subway": "경복궁역 2번 출구 도보 3분 (180m)",
        "phone": "02-737-7444", "lat": 37.5778, "lng": 126.9715,
        "hours": "10:00 ~ 22:00 (연중무휴)", "parking": "전용 무료 주차장 완비 (1시간 무료)",
        "intro": "역대 대통령들의 단골집이자 견과류와 한약재를 갈아 넣은 걸쭉한 백탕 삼계탕의 명가.",
        "photos": ["https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "토속촌 삼계탕", "price": "20,000원", "desc": "잣, 호박씨, 해바라기씨가 들어간 걸쭉하고 진한 사골 삼계탕"}, {"name": "해물파전", "price": "18,000원", "desc": "파와 오징어가 듬뿍 들어간 바삭한 부침개"}]
    },
    {
        "name": "논현동 진미평양냉면", "area": "강남/학동", "category": "한식 · 평양냉면/어복쟁반",
        "address": "서울 강남구 학동로 305-3", "subway": "학동역 10번 출구 도보 5분 (320m)",
        "phone": "02-515-3469", "lat": 37.5162, "lng": 127.0360,
        "hours": "11:00 ~ 21:30 (연중무휴)", "parking": "발렛 파킹 가능 (3,000원)",
        "intro": "의정부파 20년 경력 장인의 맑고 깊은 육수와 쫄깃한 편육, 어복쟁반이 일품인 강남 최고의 냉면집.",
        "photos": ["https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "냉면", "price": "15,000원", "desc": "슴슴하면서도 깊은 감칠맛의 육수와 메밀 순면"}, {"name": "제육 (돼지고기)", "price": "30,000원", "desc": "차갑고 쫀득하게 삶아낸 수육"}]
    },
    {
        "name": "압구정 삼원가든 본점", "area": "신사/압구정", "category": "고기요리 · 한우양념갈비",
        "address": "서울 강남구 언주로 835", "subway": "압구정역 2번 출구 도보 8분 (550m)",
        "phone": "02-548-3030", "lat": 37.5265, "lng": 127.0325,
        "hours": "11:30 ~ 22:00 (B.T 15:00~17:00)", "parking": "대형 전용 주차장 완비 (발렛 지원)",
        "intro": "1976년 개업하여 도심 속 거대한 인공폭포와 전통 한옥 정원을 품은 유서 깊은 전통 갈비 명가.",
        "photos": ["https://images.unsplash.com/photo-1555939594-58d7cb561ad1?auto=format&fit=crop&w=800&q=80"],
        "menus": [{"name": "삼원 양념갈비 (200g)", "price": "52,000원", "desc": "다이아몬드 칼집에 특제 비법 양념을 숙성시킨 시그니처"}, {"name": "한우 갈비탕", "price": "22,000원", "desc": "푸짐한 갈빗대가 들어간 맑고 깊은 탕"}]
    }
]

def _parse_naver_apollo_state(html):
    import json
    marker = "window.__APOLLO_STATE__ = "
    idx = html.find(marker)
    if idx == -1:
        return {}
    start = idx + len(marker)
    end = html.find("window.__PLACE_STATE__", start)
    if end == -1:
        end = html.find("</script>", start)
    raw_json = html[start:end].strip()
    if raw_json.endswith(";"):
        raw_json = raw_json[:-1].strip()
    try:
        return json.loads(raw_json)
    except Exception:
        return {}

def _extract_area_label(full_addr, common_addr="", name=""):
    """Extract clean Korean area label supporting all of South Korea while keeping Seoul-centric neighborhoods."""
    addr = (full_addr or common_addr or "").strip()
    seoul_spots = [
        "을지로", "종로", "성수", "강남", "신사", "압구정", "홍대", "연남", "합정", "망원",
        "용산", "이태원", "한남", "삼각지", "명동", "잠실", "여의도", "약수", "신당", "혜화",
        "청량리", "건대", "신촌", "광화문", "서촌", "북촌", "왕십리", "성북", "서초", "교대",
    ]
    is_seoul = ("서울" in addr) or (not addr)
    if is_seoul:
        for spot in seoul_spots:
            if spot in addr or spot in (name or ""):
                return spot
        m_gu = re.search(r"([가-힣]+구)", addr)
        if m_gu:
            return f"서울 {m_gu.group(1).rstrip('구')}"
        if common_addr:
            parts = common_addr.split()
            return parts[-1] if len(parts) >= 2 else common_addr
        return "서울"

    # Outside Seoul: Metro cities & Provinces (대한민국 전체)
    metro_map = {
        "부산": "부산", "대구": "대구", "인천": "인천",
        "광주": "광주", "대전": "대전", "울산": "울산", "세종": "세종",
    }
    for k, short_m in metro_map.items():
        if addr.startswith(k) or f" {k}" in addr:
            if short_m == "세종":
                return "세종"
            m_sub = re.search(r"(?:광역시|특별시|시)?\s+([가-힣]+(?:구|군))", addr)
            if m_sub:
                sub = re.sub(r"(구|군)$", "", m_sub.group(1))
                return f"{short_m} {sub}" if len(sub) >= 2 else short_m
            return short_m

    prov_map = {
        "경기": "경기", "강원": "강원", "충청북": "충북", "충북": "충북",
        "충청남": "충남", "충남": "충남", "전라북": "전북", "전북": "전북",
        "전라남": "전남", "전남": "전남", "경상북": "경북", "경북": "경북",
        "경상남": "경남", "경남": "경남", "제주": "제주",
    }
    for k, short_p in prov_map.items():
        if k in addr[:10]:
            m_city = re.search(r"\s([가-힣]+(?:시|군))", addr)
            if m_city:
                city = re.sub(r"(시|군)$", "", m_city.group(1))
                if short_p == "제주" and city == "제주":
                    return "제주"
                return f"{short_p} {city}" if len(city) >= 2 else short_p
            return short_p

    if common_addr:
        parts = common_addr.split()
        return parts[-1] if len(parts) >= 2 else common_addr
    return "기타"


def fetch_naver_restaurants(query="음식점", x="126.9910", y="37.5665", bounds=None):
    import urllib.request, urllib.parse
    q = urllib.parse.quote(query if query else "음식점")
    url = f"https://pcmap.place.naver.com/restaurant/list?query={q}&x={x}&y={y}"
    if bounds:
        url += f"&bounds={urllib.parse.quote(bounds)}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9"
    })
    res = urllib.request.urlopen(req, timeout=8)
    html = res.read().decode("utf-8", errors="ignore")
    state = _parse_naver_apollo_state(html)

    results = []
    seen_ids = set()
    existing_db_names = {r.get("name", "").strip(): r.get("id") for r in database.get_all_restaurants() if len(r.get("reviews", [])) > 0}

    for k, v in state.items():
        if not isinstance(v, dict):
            continue
        if k.startswith("RestaurantAdSummary:"):
            continue
        if not (k.startswith("PlaceListBusinessesItem:") or k.startswith("RestaurantListSummary:")):
            continue

        place_id = str(v.get("id", ""))
        name = v.get("name")
        px = v.get("x")
        py = v.get("y")
        if not (place_id and name and px and py) or place_id in seen_ids:
            continue
        seen_ids.add(place_id)

        road_addr = v.get("roadAddress") or v.get("address") or ""
        common_addr = v.get("commonAddress") or ""
        full_addr = v.get("fullAddress") or f"{common_addr} {road_addr}".strip()

        area = _extract_area_label(full_addr, common_addr, name)

        photos = []
        if v.get("imageUrls") and isinstance(v["imageUrls"], list):
            photos.extend([u for u in v["imageUrls"] if u][:5])
        elif v.get("imageUrl"):
            photos.append(v["imageUrl"])

        if v.get("visitorImages") and isinstance(v["visitorImages"], list):
            for vi in v["visitorImages"]:
                if isinstance(vi, dict) and vi.get("imageUrl") and vi["imageUrl"] not in photos:
                    photos.append(vi["imageUrl"])
                if len(photos) >= 8:
                    break

        if not photos:
            photos = ["https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80"]

        bh = v.get("newBusinessHours")
        hours_str = "11:30 ~ 21:30"
        if isinstance(bh, dict):
            status = bh.get("status", "")
            desc = bh.get("description", "")
            hours_str = f"{status} · {desc}" if (status and desc) else (status or desc or hours_str)

        options = v.get("options") or ""
        parking_str = options if options else "매장 문의"

        cat_str = v.get("category") or "음식점"
        micro_rev = v.get("microReview")
        intro_str = micro_rev if micro_rev else f"네이버 지도에 등록된 {area} {cat_str} 전문점 '{name}'입니다."

        results.append({
            "naverId": place_id,
            "name": name,
            "area": area,
            "category": cat_str,
            "address": full_addr,
            "subway": f"{area} 인근",
            "phone": v.get("phone") or v.get("virtualPhone") or "전화번호 미등록",
            "lat": float(py),
            "lng": float(px),
            "hours": hours_str,
            "parking": parking_str,
            "intro": intro_str,
            "photos": photos,
            "naverReviewCount": v.get("visitorReviewCount") or "0",
            "naverScore": v.get("visitorReviewScore") or "",
            "alreadyRegistered": name.strip() in existing_db_names,
            "existingId": existing_db_names.get(name.strip()),
            "source": "naver_map"
        })

    return results[:25]

import time

# In-memory TTL Caches for protecting against Naver rate limits & instant response
PLACE_SEARCH_CACHE = {}  # key -> {"data": [...], "timestamp": float}
PLACE_DETAIL_CACHE = {}  # naver_id -> {"data": {...}, "timestamp": float}
CACHE_SEARCH_TTL = 7200    # 2 hours
CACHE_DETAIL_TTL = 86400   # 24 hours
MAX_CACHE_ENTRIES = 1000

def _get_from_cache(cache_dict, key, ttl):
    item = cache_dict.get(key)
    if not item:
        return None
    if time.time() - item["timestamp"] > ttl:
        return None
    return item["data"]

def _set_cache(cache_dict, key, data):
    if len(cache_dict) > MAX_CACHE_ENTRIES:
        sorted_keys = sorted(cache_dict.keys(), key=lambda k: cache_dict[k]["timestamp"])
        for k in sorted_keys[:200]:
            cache_dict.pop(k, None)
    cache_dict[key] = {
        "data": data,
        "timestamp": time.time()
    }

def _distance_km(lat1, lng1, lat2, lng2):
    """Haversine distance in kilometers between two coordinates."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _sort_places_by_distance(items, ref_lat, ref_lng):
    """Return places sorted closest-first relative to (ref_lat, ref_lng)."""
    try:
        rlat = float(ref_lat)
        rlng = float(ref_lng)
    except Exception:
        return items
    return sorted(
        items,
        key=lambda it: _distance_km(rlat, rlng, float(it.get("lat") or 0), float(it.get("lng") or 0)),
    )


@app.route("/api/search-place", methods=["GET"])
def search_place():
    query = request.args.get("q", "").strip()
    lat = request.args.get("lat", "37.5665").strip()
    lng = request.args.get("lng", "126.9910").strip()
    bounds = request.args.get("bounds", "").strip() or None

    search_keyword = query if query else "음식점"

    # Normalize cache key (round coords to ~100m to maximize cache hit rate when panning slightly)
    try:
        lat_r = round(float(lat), 3)
        lng_r = round(float(lng), 3)
    except Exception:
        lat_r, lng_r = lat, lng

    bounds_key = ""
    if bounds:
        try:
            b_parts = [round(float(p), 3) for p in bounds.split(";")]
            bounds_key = ";".join(map(str, b_parts))
        except Exception:
            bounds_key = bounds

    cache_key = f"{search_keyword.lower()}:{lat_r}:{lng_r}:{bounds_key}"

    # Check cache first
    cached_data = _get_from_cache(PLACE_SEARCH_CACHE, cache_key, CACHE_SEARCH_TTL)
    existing_db_names = {r.get("name", "").strip(): r.get("id") for r in database.get_all_restaurants() if len(r.get("reviews", [])) > 0}

    if cached_data is not None:
        # Refresh alreadyRegistered with current SQLite DB state
        fresh_list = []
        for item in cached_data:
            it = dict(item)
            it["alreadyRegistered"] = it["name"].strip() in existing_db_names
            it["existingId"] = existing_db_names.get(it["name"].strip())
            fresh_list.append(it)
        if not bounds:
            fresh_list = _sort_places_by_distance(fresh_list, lat, lng)
        return jsonify({"success": True, "data": fresh_list, "fromCache": True})

    results = []

    # 1. Live Naver Map Place Search (by keyword OR by map viewport bounds)
    try:
        results = fetch_naver_restaurants(query=search_keyword, x=lng, y=lat, bounds=bounds)
        if not results and bounds:
            results = fetch_naver_restaurants(query=search_keyword, x=lng, y=lat, bounds=None)
        if results:
            _set_cache(PLACE_SEARCH_CACHE, cache_key, results)
    except Exception as e:
        print("Naver Map live search error:", e)
        # Try stale cache if upstream error / rate limit occurred
        stale_entry = PLACE_SEARCH_CACHE.get(cache_key)
        if stale_entry and stale_entry.get("data"):
            print(f"Serving stale cache for {cache_key} due to upstream error")
            fresh_list = []
            for item in stale_entry["data"]:
                it = dict(item)
                it["alreadyRegistered"] = it["name"].strip() in existing_db_names
                it["existingId"] = existing_db_names.get(it["name"].strip())
                fresh_list.append(it)
            return jsonify({"success": True, "data": fresh_list, "fromStaleCache": True})

    # 2. Fallback to built-in curated catalog if offline or empty
    if not results:
        q_lower = "" if search_keyword in ("음식점", "전체", "맛집") else query.lower()
        for p in BUILTIN_PLACES:
            if not q_lower or q_lower in p["name"].lower() or q_lower in p["category"].lower() or q_lower in p["address"].lower():
                p_copy = dict(p)
                p_copy["source"] = "curated_map"
                p_copy["alreadyRegistered"] = p_copy["name"].strip() in existing_db_names
                p_copy["existingId"] = existing_db_names.get(p_copy["name"].strip())
                results.append(p_copy)

    if not bounds and results:
        results = _sort_places_by_distance(results, lat, lng)

    return jsonify({"success": True, "data": results})

@app.route("/api/naver-place/<naver_id>", methods=["GET"])
def get_naver_place_detail(naver_id):
    # Check detail cache first
    cached_detail = _get_from_cache(PLACE_DETAIL_CACHE, naver_id, CACHE_DETAIL_TTL)
    if cached_detail is not None:
        return jsonify({"success": True, "data": cached_detail, "fromCache": True})

    try:
        import urllib.request
        url = f"https://pcmap.place.naver.com/restaurant/{naver_id}/home"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9"
        })
        res = urllib.request.urlopen(req, timeout=8)
        html = res.read().decode("utf-8", errors="ignore")
        state = _parse_naver_apollo_state(html)

        menus = []
        subways = []
        photos = []
        base_info = {}

        for k, v in state.items():
            if not isinstance(v, dict):
                continue
            if k.startswith("PlaceDetailBase:"):
                base_info = v
            elif k.startswith("PlaceMenuItem:"):
                m_name = v.get("name")
                price_obj = v.get("price")
                m_price = price_obj.get("displayText") if isinstance(price_obj, dict) else ""
                m_desc = v.get("description") or "매장 대표 메뉴"
                if m_name:
                    menus.append({
                        "name": m_name,
                        "price": m_price or "변동",
                        "desc": m_desc
                    })
            elif k.startswith("SubwayStationInfo:"):
                st_name = v.get("displayName") or v.get("name")
                exit_no = v.get("nearestExit")
                walk_min = v.get("walkTime")
                walk_dist = v.get("walkingDistance")
                if st_name:
                    exit_str = f" {exit_no}번 출구" if exit_no else ""
                    walk_str = f" 도보 {walk_min}분 ({walk_dist}m)" if walk_min else ""
                    subways.append((walk_dist or 9999, f"{st_name}{exit_str}{walk_str}"))
            elif k.startswith("PlaceDetailTopPhotoItem:"):
                p_url = v.get("originalUrl") or v.get("thumbnailUrl")
                if p_url and p_url not in photos:
                    photos.append(p_url)

        subways.sort(key=lambda x: x[0])
        nearest_subway = subways[0][1] if subways else ""

        conveniences = base_info.get("conveniences") or []
        parking_info = ", ".join(conveniences) if conveniences else ""
        micro_reviews = base_info.get("microReviews") or []
        intro = micro_reviews[0] if micro_reviews else ""
        road_address = base_info.get("roadAddress") or base_info.get("address") or ""
        phone = base_info.get("phone") or base_info.get("virtualPhone") or ""

        detail_data = {
            "naverId": naver_id,
            "subway": nearest_subway,
            "menus": menus[:8],
            "photos": photos[:8],
            "parking": parking_info,
            "intro": intro,
            "roadAddress": road_address,
            "phone": phone
        }

        # Cache valid detail
        _set_cache(PLACE_DETAIL_CACHE, naver_id, detail_data)

        return jsonify({
            "success": True,
            "data": detail_data
        })
    except Exception as e:
        # Check if stale detail cache exists
        stale_detail = PLACE_DETAIL_CACHE.get(naver_id)
        if stale_detail and stale_detail.get("data"):
            return jsonify({"success": True, "data": stale_detail["data"], "fromStaleCache": True})
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/reverse-geocode", methods=["GET"])
def reverse_geocode():
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    if not lat or not lng:
        return jsonify({"success": False, "error": "lat and lng required"}), 400

    kakao_key = request.headers.get("X-Kakao-Key") or os.environ.get("KAKAO_REST_API_KEY", "")
    address = ""

    if kakao_key:
        try:
            import urllib.request, json
            url = f"https://dapi.kakao.com/v2/local/geo/coord2address.json?x={lng}&y={lat}"
            req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {kakao_key}"})
            res = urllib.request.urlopen(req, timeout=3)
            data = json.loads(res.read().decode("utf-8"))
            if data.get("documents"):
                doc = data["documents"][0]
                road = doc.get("road_address")
                address = road.get("address_name") if road else doc.get("address", {}).get("address_name", "")
        except Exception as e:
            print("Kakao reverse geocode failed:", e)

    if not address:
        try:
            import urllib.request, json
            url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json"
            req = urllib.request.Request(url, headers={"User-Agent": "HongchelinApp/1.0 (hongchelin@gmail.com)"})
            res = urllib.request.urlopen(req, timeout=4)
            data = json.loads(res.read().decode("utf-8"))
            addr_data = data.get("display_name", "")
            parts = [p.strip() for p in addr_data.split(",")]
            kor_parts = [p for p in parts if any('\uac00' <= c <= '\ud7a3' for c in p)]
            if kor_parts:
                address = " ".join(reversed(kor_parts[-4:]))
            else:
                address = addr_data
        except Exception as e:
            print("Nominatim reverse failed:", e)
            address = f"서울시 (위도 {lat}, 경도 {lng})"

    area = "서울"
    for a_cand in ["을지로", "종로", "성수", "강남", "신사", "압구정", "홍대", "연남", "용산", "이태원", "명동", "잠실", "여의도", "마포", "서초"]:
        if a_cand in address:
            area = a_cand
            break

    return jsonify({
        "success": True,
        "address": address,
        "area": area,
        "lat": float(lat),
        "lng": float(lng)
    })

@app.route("/api/reset", methods=["POST"])
def reset_db():
    admin_user, err_resp = _require_admin()
    if err_resp:
        return err_resp
    try:
        database.reset_database()
        return jsonify({"success": True, "message": "데이터베이스가 초기화되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"홍슐랭 가이드 Flask REST API 서버 가동: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

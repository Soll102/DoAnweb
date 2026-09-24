# -*- coding: utf-8 -*-
"""
AZUREA ISLANDS — Web Đặt Lịch Du Lịch Biển & Đảo Phong Cách Luxury
Stack: Flask + SQLite (sqlite3) + HTML/CSS/JS thuần
Tác giả: Đồ án Web — Chủ đề Đại dương / Biển cả / Đảo Luxury

Chạy:
    pip install -r requirements.txt
    python app.py
    → mở http://127.0.0.1:5000
    → Admin: http://127.0.0.1:5000/admin  (admin@azurea.vn / admin123)
"""
import os
import re
import json
import sqlite3
import random
import string
import unicodedata
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv

load_dotenv()  # đọc OPENROUTER_* từ file .env (nếu có)

from flask import Flask, g, render_template, request, redirect, url_for, jsonify, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import requests
except ImportError:  # cho phép chạy fallback rule-based khi chưa pip install -r requirements.txt
    requests = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "azurea-luxury-ocean-secret-2026")

# ---------- Cấu hình OpenRouter (chatbot AI thật) ----------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.8-27b:free").strip() or "qwen/qwen3.8-27b:free"
OPENROUTER_SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:5000").strip()
OPENROUTER_APP_NAME = os.getenv("APP_NAME", "AZUREA ISLANDS ").strip()


def openrouter_enabled():
    """Có key + đã cài requests thì mới gọi AI thật, còn không fallback rule-based."""
    return bool(OPENROUTER_API_KEY) and requests is not None

# ---------- Helpers: DB ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def vnd(n):
    try:
        return f"{int(n):,}₫".replace(",", ".")
    except Exception:
        return f"{n}₫"

app.jinja_env.filters["vnd"] = vnd

# ---------- Bảo mật PII: che họ tên / SĐT / email khi chưa xác thực ----------
def mask_name(name):
    parts = (name or "").split()
    if not parts:
        return "***"
    return parts[0] + " ***"

def mask_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 3:
        return "***"
    return "*" * (len(digits) - 3) + digits[-3:]

def mask_email(email):
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    local, domain = e.split("@", 1)
    if not local:
        return "***@" + domain
    return local[0] + "***@" + domain

app.jinja_env.filters["mask_name"] = mask_name
app.jinja_env.filters["mask_phone"] = mask_phone
app.jinja_env.filters["mask_email"] = mask_email

def extract_phone(text):
    """Móc SĐT VN (đầu 0, 9–11 số, cho phép cách/dấu chấm) trong câu chat, bỏ qua ngày tháng."""
    for m in re.finditer(r"(?<!\d)0[\d\s\.]{7,14}(?!\d)", text or ""):
        digits = re.sub(r"\D", "", m.group())
        if digits.startswith("0") and 9 <= len(digits) <= 11:
            return digits
    return None

def gen_booking_code():
    return "AZ-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def calc_nights(check_in, check_out):
    d1 = datetime.strptime(check_in, "%Y-%m-%d").date()
    d2 = datetime.strptime(check_out, "%Y-%m-%d").date()
    return (d2 - d1).days

def is_room_available(room_id, check_in, check_out, exclude_booking_id=None):
    """Kiểm tra phòng còn trống không (logic overlap)."""
    db = get_db()
    q = """
        SELECT COUNT(*) AS c FROM bookings
        WHERE room_id = ?
          AND status IN ('pending','confirmed')
          AND NOT (date(check_out) <= date(?) OR date(check_in) >= date(?))
    """
    params = [room_id, check_in, check_out]
    if exclude_booking_id:
        q += " AND id != ?"
        params.append(exclude_booking_id)
    row = db.execute(q, params).fetchone()
    return row["c"] == 0

def booked_dates_in_month(room_id, year, month):
    """Trả về set các ngày đã kín trong tháng (YYYY-MM-DD)."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{last_day}"
    db = get_db()
    rows = db.execute("""
        SELECT check_in, check_out FROM bookings
        WHERE room_id = ? AND status IN ('pending','confirmed')
          AND date(check_in) <= date(?) AND date(check_out) > date(?)
    """, (room_id, end, start)).fetchall()
    booked = set()
    for r in rows:
        d1 = datetime.strptime(r["check_in"], "%Y-%m-%d").date()
        d2 = datetime.strptime(r["check_out"], "%Y-%m-%d").date()
        cur = d1
        while cur < d2:
            if cur.year == year and cur.month == month:
                booked.add(cur.isoformat())
            cur += timedelta(days=1)
    return booked

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "user_id" not in session:
            flash("Vui lòng đăng nhập để tiếp tục.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or not u["is_admin"]:
            flash("Khu vực quản trị. Vui lòng đăng nhập bằng tài khoản admin.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

@app.context_processor
def inject_globals():
    return dict(current_user=current_user(), today=date.today().isoformat())

# ---------- Chatbot Engine (rule-based tiếng Việt) ----------

def chatbot_reply(message: str, ctx=None):
    msg = (message or "").lower().strip()
    nlow = norm(msg)  # bản không dấu để hiểu cả khi khách gõ không dấu
    db = get_db()

    def hit(*keys, _wb=()):
        """Khớp từ khóa có dấu HOẶC không dấu.
        Từ ngắn (<=3 ký tự sau khi bỏ dấu) chỉ khớp nguyên từ để tránh nhiễu."""
        for k in keys:
            if k in msg or (len(norm(k)) > 3 and norm(k) in nlow):
                return True
        for w in _wb:
            if re.search(r"\b" + re.escape(w) + r"\b", nlow):
                return True
        return False

    def room_list_text():
        rooms = db.execute("SELECT name, type, price_per_night FROM rooms WHERE is_active=1 ORDER BY id").fetchall()
        lines = [f"• {r['name']} ({r['type']}) — {vnd(r['price_per_night'])}/đêm" for r in rooms]
        return "Hiện tại AZUREA có các hạng phòng:\n" + "\n".join(lines) + "\n\nBạn muốn tôi kiểm tra lịch trống phòng nào? (nhắn: 'lịch trống + tên phòng')"

    # chào hỏi
    if (re.search(r"\b(hello|hi|hey|chào|xin chào|alo)\b", msg)
            or re.search(r"\b(hello|hi|hey|chao|xin chao|alo)\b", nlow)):
        return ("Xin chào quý khách! Chào mừng đến với <b>AZUREA ISLANDS</b> — resort biển và đảo hạng sang.<br>"
                "Tôi là <b>Marina</b>, trợ lý của resort. Tôi có thể giúp bạn:<br>"
                "• Xem giá và loại phòng • Kiểm tra lịch trống • Hướng dẫn đặt phòng • Chính sách hủy và thanh toán<br>"
                "Bạn cần hỗ trợ gì hôm nay?"), ["Xem bảng giá", "Phòng còn trống?", "Cách đặt phòng", "Ưu đãi hiện tại"]

    if hit("giá", "bao nhiêu", "bảng giá", "price", "chi phí", "phí", _wb=("gia",)) and "tham gia" not in nlow:
        return (room_list_text() + "<br><br><b>Ưu đãi tháng này:</b> ở 3 đêm tặng 1 bữa tối hải sản và miễn phí đưa đón cano đảo."), ["Kiểm tra lịch trống", "Cách đặt phòng", "Ưu đãi hiện tại"]

    rt = match_room_type(db, nlow)
    # nhường các câu hỏi giờ giấc / thanh toán / tra cứu có nhắc tới "phòng" cho đúng nhánh
    time_q = hit("nhận phòng", "trả phòng", "check-in", "checkin", "checkout", "check-out", "giờ", _wb=("gio",))
    pay_q = hit("thanh toán", "payment", "cọc", "chuyển khoản", "tiền mặt", _wb=("tra", "coc"))
    if (hit("loại phòng", "hạng phòng", "villa", "suite", "bungalow", "penthouse", "phòng nào", "phòng")
            or rt) and not time_q and not pay_q and "tra cuu" not in nlow:
        # chỉ nêu tên hạng phòng (VD chip "Ocean Villa") → hiện lịch hạng đó
        if rt and nlow.strip().rstrip("?!").strip() == norm(rt):
            return avail_answer(db, msg, nlow, ctx)
        # nêu tên 1 villa cụ thể (VD chip sau khi lọc) → lịch mini villa đó
        if "xem lich su" not in nlow and find_room(db, msg):
            return avail_answer(db, msg, nlow, ctx)
        # hỏi lịch → hiện lịch thật trong chat (không chỉ đường link)
        if "xem lich su" not in nlow and hit("trống", "còn", "lịch", "available", _wb=("trong", "con", "lich")):
            return avail_answer(db, msg, nlow, ctx)
        return (room_list_text()), ["Kiểm tra lịch trống", "Cách đặt phòng", "Ưu đãi hiện tại"]

    if hit("lịch trống", "còn phòng", "còn trống", "trống không", "availability", "xem lịch") and "xem lich su" not in nlow:
        return avail_answer(db, msg, nlow, ctx)

    if hit("đặt phòng", "đặt lịch", "book", "booking", "cách đặt", "đặt", _wb=("dat",)) and "dat nuoc" not in nlow:
        return ("Đặt phòng chỉ mất 1 phút:<br>"
                "1. Vào <a href='/phong'><b>Danh sách Villa</b></a> và chọn villa yêu thích<br>"
                "2. Chọn <b>ngày nhận – trả phòng</b>, số khách — hệ thống tự tính tiền<br>"
                "3. Nhập tên, SĐT, email rồi bấm <b>Xác nhận đặt</b> để nhận <b>mã AZ-XXXXXX</b><br>"
                "Bạn có thể tra cứu / hủy tại <a href='/tra-cuu'><b>Tra cứu đặt phòng</b></a>."), ["Xem danh sách phòng", "Tra cứu đặt phòng"]

    if hit("hủy", "cancel", "hoàn tiền", "refund", _wb=("huy",)):
        return ("Chính sách hủy linh hoạt của AZUREA:<br>"
                "• Hủy <b>trước 7 ngày</b>: hoàn 100%<br>• Trước 3–7 ngày: hoàn 50%<br>• Trong 3 ngày: không hoàn (có thể dời lịch 1 lần miễn phí)<br>"
                "Để hủy, vào <a href='/tra-cuu'><b>Tra cứu</b></a> → nhập mã AZ-XXXXXX + SĐT → bấm Hủy."), ["Tra cứu đặt phòng", "Liên hệ nhân viên"]

    if hit("thanh toán", "payment", "cọc", "chuyển khoản", "tiền mặt", "trả", _wb=("tra", "coc")) and "tra cuu" not in nlow:
        return ("AZUREA hỗ trợ thẻ Visa/Master, chuyển khoản và tiền mặt tại lễ tân.<br>"
                "Bạn chỉ cần <b>cọc 30%</b> để giữ phòng, còn lại thanh toán khi nhận phòng."), ["Cách đặt phòng", "Chính sách hủy"]

    if hit("nhận phòng", "trả phòng", "check-in", "checkin", "checkout", "check-out", "giờ", _wb=("gio",)):
        return ("<b>Nhận phòng:</b> từ 14:00 &nbsp;|&nbsp; <b>Trả phòng:</b> trước 12:00.<br>"
                "Đến sớm? Resort có phòng chờ Marina Lounge và hồ bơi vô cực miễn phí trong lúc đợi phòng. Trả muộn tới 18:00 chỉ phụ thu 30%."), ["Đặt phòng", "Dịch vụ resort"]

    if hit("ở đâu", "địa chỉ", "location", "đường", "bản đồ", "map"):
        return ("AZUREA ISLANDS — <b>Bãi Dài, đảo Coral, Nha Trang, Khánh Hòa</b>.<br>"
                "Cách sân bay Cam Ranh 25 phút (xe resort đón miễn phí). Cano ra đảo riêng Coral Cay chỉ 15 phút, chạy mỗi giờ từ 7:00–21:00."), ["Xem phòng", "Tour đảo"]

    if hit("ưu đãi", "uy dai", "khuyến mãi", "voucher", "giảm", "sale", "combo", "deal"):
        return ("<b>Ưu đãi tháng này:</b><br>• Ở 3+ đêm: <b>giảm 15%</b> và tặng bữa tối hải sản<br>• Trăng mật: tặng trang trí phòng và bánh kem<br>• Nhập mã <b>AZUREA10</b> khi đặt để giảm thêm 10% (áp dụng online)<br>"
                "Bạn đi mấy đêm để tôi gợi ý combo phù hợp nhất?"), ["Xem bảng giá", "Đặt phòng ngay"]

    if hit("tour", "đảo", "lặn", "snorkel", "diving", "cano", "thuyền", "kayak", "câu cá", _wb=("dao", "lan")):
        return ("<b>Tour đảo nổi bật:</b><br>• Cano Coral Cay nửa ngày — 850K/khách (lặn ngắm san hô và BBQ bãi biển)<br>"
                "• Hoàng hôn du thuyền Sunset Pearl — 1.2TR/khách<br>• Lặn bình khí có HLV PADI — 1.5TR/khách<br>"
                "Khách đặt villa được <b>giảm 20% tour</b>. Bạn muốn giữ chỗ tour kèm phòng không?"), ["Đặt phòng", "Bảng giá tour"]

    if hit("spa", "massage", "ăn", "nhà hàng", "buffet", "hải sản", "dịch vụ",
           _wb=("an uong", "an sang", "an trua", "an toi", "mon an", "quan an")):
        return ("<b>Dịch vụ 5 sao:</b> Spa đá nóng view biển, hồ bơi vô cực, gym 24/7, kids club.<br>"
                "3 nhà hàng: <i>Ocean Pearl</i> (hải sản), <i>Azure Fine Dining</i> (Âu), <i>Lagoon BBQ</i> (nướng bãi biển). Buffet sáng đã gồm trong giá phòng."), ["Đặt bàn nhà hàng", "Xem phòng"]

    if hit("liên hệ", "hotline", "sđt", "sdt", "nhân viên", "gặp ai", "tư vấn", "phone"):
        return ("Hotline 24/7: <b>1900 6868</b> (miễn phí) — Zalo: <b>0909 888 999</b><br>"
                "Email: hello@azurea.vn<br>Hoặc để lại lời nhắn tại <a href='/lien-he'><b>Liên hệ</b></a>, đội concierge sẽ gọi lại trong 10 phút."), ["Để lại lời nhắn"]

    if hit("cảm ơn", "thank", "tuyệt", "ok", "oke", "được"):
        return ("Không có gì ạ! AZUREA rất mong được đón tiếp bạn giữa biển xanh và nắng vàng.<br>Cần thêm gì bạn cứ nhắn Marina nhé!"), ["Xem phòng", "Ưu đãi hiện tại"]

    if hit("mã đặt", "tra cứu", "kiểm tra đơn", "đơn của tôi", "booking code", "az-"):
        m = re.search(r"AZ-[A-Z0-9]{4,6}", message.upper())
        if m:
            code = m.group(0)
            phone = extract_phone(message)
            if not phone:
                return ("Để bảo mật thông tin, bạn gửi thêm <b>SĐT lúc đặt phòng</b> kèm mã "
                        f"<b>{code}</b> giúp tôi nhé (VD: '{code} SĐT 0901234567')."), ["Tra cứu đặt phòng"]
            b = db.execute("""
                SELECT b.*, r.name AS room_name FROM bookings b
                JOIN rooms r ON r.id=b.room_id WHERE b.booking_code=? AND b.phone=?
            """, (code, phone)).fetchone()
            if b:
                return (f"Tôi tìm thấy đơn <b>{code}</b>:<br>{b['room_name']}<br>{b['check_in']} đến {b['check_out']} ({b['guests']} khách)<br>"
                        f"Tổng: {vnd(b['total_price'])} — Trạng thái: <b>{b['status']}</b><br>Chi tiết tại <a href='/tra-cuu?code={code}'><b>Tra cứu</b></a>."), ["Hủy đơn này", "Đặt thêm"]
            return ("Tôi chưa thấy đơn nào khớp mã + SĐT này. Bạn kiểm tra lại giúp tôi nhé, "
                    "hoặc tra cứu tại <a href='/tra-cuu'><b>đây</b></a>."), ["Liên hệ nhân viên"]
        return ("Bạn gửi tôi <b>mã đặt phòng (dạng AZ-XXXXXX)</b> để tôi tra cứu giúp nhé! Hoặc mở <a href='/tra-cuu'><b>Tra cứu đặt phòng</b></a>."), ["Tra cứu đặt phòng"]

    # fallback
    return ("Tôi chưa hiểu rõ ý bạn. Marina có thể giúp:<br>"
            "• <b>Bảng giá và loại villa</b> • <b>Lịch trống</b> • <b>Cách đặt / hủy phòng</b> • <b>Tour đảo và ưu đãi</b><br>"
            "Bạn gõ 1 trong các từ khóa trên, hoặc gọi hotline <b>1900 6868</b> để gặp concierge nhé!"), ["Xem bảng giá", "Lịch trống", "Cách đặt phòng", "Ưu đãi hiện tại"]


# ---------- Seed / Init ----------

SEED_ROOMS = [
    {
        "name": "Ocean Pearl Villa",
        "type": "Ocean Villa",
        "price_per_night": 4500000,
        "capacity": 2, "size_m2": 68,
        "description": "Villa mặt biển với hồ bơi riêng vô cực, giường canopy view đại dương, bồn tắm đá cẩm thạch hướng sóng. Bước 10 bước là chạm cát trắng Bãi Dài.",
        "image_url": "https://images.unsplash.com/photo-1582719508461-905c673771fd?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Hồ bơi riêng vô cực|View biển trực diện|Bồn tắm đá cẩm thạch|Minibar cao cấp|Đưa đón cano miễn phí|Ăn sáng nổi trên hồ bơi",
        "rating": 4.9,
    },
    {
        "name": "Coral Sunset Suite",
        "type": "Coral Suite",
        "price_per_night": 3200000,
        "capacity": 3, "size_m2": 52,
        "description": "Suite tầng cao ngắm hoàng hôn đẹp nhất đảo. Ban công panorama 180° ôm trọn vịnh san hô, decor san hô & vỏ ốc nghệ thuật.",
        "image_url": "https://images.unsplash.com/photo-1590490360182-c33d57733427?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Ban công panorama|Bồn tắm view hoàng hôn|Nespresso & trà chiều|Smart TV 55 inch|Ăn sáng buffet|Yoga bình minh miễn phí",
        "rating": 4.8,
    },
    {
        "name": "Pearl Lagoon Bungalow",
        "type": "Pearl Bungalow",
        "price_per_night": 2400000,
        "capacity": 2, "size_m2": 42,
        "description": "Bungalow mái lá sang trọng nép mình bên đầm phá xanh ngọc. Võng lưới trên mặt nước, kayaks miễn phí, lý tưởng cho cặp đôi.",
        "image_url": "https://images.unsplash.com/photo-1520250497591-112f2f40a3f4?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Võng lưới mặt nước|Kayak miễn phí|Ngoài trời tắm mưa|Hammock vườn dừa|Ăn sáng tận phòng|Xe đạp đảo miễn phí",
        "rating": 4.7,
    },
    {
        "name": "Azure Royal Penthouse",
        "type": "Royal Penthouse",
        "price_per_night": 8900000,
        "capacity": 6, "size_m2": 150,
        "description": "Penthouse 2 tầng trên đỉnh resort: rooftop infinity pool, phòng cinema, quản gia riêng 24/7, tiệc BBQ đầu bếp Michelin tại villa.",
        "image_url": "https://images.unsplash.com/photo-1540541338287-41700207dee6?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Rooftop infinity pool|Quản gia riêng 24/7|Phòng cinema|Bếp chef riêng|Rolls-Royce đưa đón|Flycam + photographer",
        "rating": 5.0,
    },
    {
        "name": "Turtle Bay Family Villa",
        "type": "Ocean Villa",
        "price_per_night": 5600000,
        "capacity": 5, "size_m2": 95,
        "description": "Villa gia đình 2 phòng ngủ cạnh bãi rùa đẻ trứng. Kids club, cầu trượt nước mini, bếp full, sân vườn BBQ riêng.",
        "image_url": "https://images.unsplash.com/photo-1571003123894-1f0594d2b5d9?auto=format&fit=crop&w=1200&q=80",
        "amenities": "2 phòng ngủ|Cầu trượt nước mini|Kids club miễn phí|Bếp & sân BBQ|Cũi em bé|Xe điện riêng",
        "rating": 4.8,
    },
    {
        "name": "Deep Blue Overwater",
        "type": "Coral Suite",
        "price_per_night": 6800000,
        "capacity": 2, "size_m2": 75,
        "description": "Căn overwater đầu tiên tại Việt Nam: sàn kính nhìn xuống rạn san hô, cầu thang riêng xuống biển, bữa tối dưới sao trên boong.",
        "image_url": "https://images.unsplash.com/photo-1584132967334-10e028bd69f7?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Sàn kính đáy biển|Cầu thang riêng xuống biển|Lưới nằm mặt nước|Kính lặn & SUP miễn phí|Bữa tối trên boong|Massage cặp đôi 60p",
        "rating": 4.9,
    },
    {
        "name": "Emerald Cliff Villa",
        "type": "Ocean Villa",
        "price_per_night": 5200000,
        "capacity": 4, "size_m2": 88,
        "description": "Villa trên vách đá nhìn toàn cảnh vịnh: hồ bơi tràn vách đá, sky bar riêng, đường mòn xuống bãi tắm bí mật chỉ dành cho khách villa.",
        "image_url": "https://images.unsplash.com/photo-1566073771259-6a8506099945?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Hồ bơi tràn vách đá|Sky bar riêng|Bãi tắm bí mật|Xe điện riêng|Ăn sáng tận phòng|Tour hoàng hôn miễn phí",
        "rating": 4.8,
    },
    {
        "name": "Moonlight Lagoon Suite",
        "type": "Coral Suite",
        "price_per_night": 3800000,
        "capacity": 2, "size_m2": 58,
        "description": "Suite hướng đầm phá lấp lánh về đêm: bồn tắm sục ngoài trời, thuyền kayak phát sáng, ban công ngắm trăng lý tưởng cho tuần trăng mật.",
        "image_url": "https://images.unsplash.com/photo-1611892440504-42a792e24d32?auto=format&fit=crop&w=1200&q=80",
        "amenities": "Bồn tắm sục ngoài trời|Kayak phát sáng|Ban công ngắm trăng|Trang trí trăng mật|Ăn sáng buffet|Xe đạp đảo miễn phí",
        "rating": 4.7,
    },
    {
        "name": "Golden Sand Beach House",
        "type": "Pearl Bungalow",
        "price_per_night": 2900000,
        "capacity": 6, "size_m2": 72,
        "description": "Nhà gỗ mặt biển cho nhóm bạn và gia đình đông: 3 phòng ngủ, hiên nướng BBQ hướng sóng, đốt lửa trại và xem phim ngoài trời mỗi tối.",
        "image_url": "https://images.unsplash.com/photo-1499793983690-e29da59ef1c2?auto=format&fit=crop&w=1200&q=80",
        "amenities": "3 phòng ngủ|Hiên BBQ hướng biển|Lửa trại mỗi tối|Xem phim ngoài trời|Bếp full|Xe đạp đảo miễn phí",
        "rating": 4.7,
    },
]

def init_db(seed=True):
    first = not os.path.exists(DB_PATH)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = f.read()
    con = sqlite3.connect(DB_PATH)
    con.executescript(schema)
    if seed:
        cur = con.cursor()
        n = cur.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        if n == 0:
            for r in SEED_ROOMS:
                cur.execute("""INSERT INTO rooms (name,type,price_per_night,capacity,size_m2,description,image_url,amenities,rating)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (r["name"], r["type"], r["price_per_night"], r["capacity"], r["size_m2"],
                             r["description"], r["image_url"], r["amenities"], r["rating"]))
        if cur.execute("SELECT COUNT(*) FROM users WHERE email='admin@azurea.vn'").fetchone()[0] == 0:
            cur.execute("INSERT INTO users (name,email,phone,password_hash,is_admin) VALUES (?,?,?,?,?)",
                        ("Quản trị viên", "admin@azurea.vn", "19006868",
                         generate_password_hash("admin123"), 1))
        if cur.execute("SELECT COUNT(*) FROM users WHERE email='demo@azurea.vn'").fetchone()[0] == 0:
            cur.execute("INSERT INTO users (name,email,phone,password_hash,is_admin) VALUES (?,?,?,?,?)",
                        ("Khách Demo", "demo@azurea.vn", "0909888999",
                         generate_password_hash("demo123"), 0))
        # bookings mẫu để demo lịch trống, biểu đồ doanh thu và trạng thái (nếu chưa có)
        # (room_id, họ tên, SĐT, email, nhận (lệch ngày so với hôm nay), trả, trạng thái, số khách, ngày tạo đơn)
        if cur.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0:
            today = date.today()
            samples = [
                # --- Đơn đã hoàn thành trong ~10 ngày qua ---
                (3, "Phan Thanh Tùng", "0988999000", "tung@gmail.com", -12, -10, "completed", 2, -12),
                (2, "Hoàng Thị Lan", "0911222333", "lan@gmail.com", -9, -7, "completed", 2, -9),
                (3, "Vũ Đức Anh", "0922333444", "ducanh@gmail.com", -8, -6, "completed", 2, -8),
                (1, "Đặng Thu Thảo", "0933444555", "thao@gmail.com", -6, -3, "completed", 2, -7),
                (4, "Bùi Văn Hải", "0944555666", "hai@gmail.com", -5, -3, "completed", 4, -6),
                (6, "Ngô Minh Châu", "0955666777", "chau@gmail.com", -4, -2, "completed", 2, -5),
                (5, "Lý Gia Bảo", "0966777888", "giabao@gmail.com", -3, -1, "completed", 5, -4),
                (2, "Trịnh Kim Ngân", "0977888999", "ngan@gmail.com", -2, -1, "completed", 2, -3),
                # --- Đơn sắp tới (rải đều để lịch trống đẹp mà vẫn còn chỗ đặt) ---
                (1, "Nguyễn Thu Hà", "0912345678", "thuha@gmail.com", 0, 3, "confirmed", 2, 0),
                (3, "Phạm Quốc Bảo", "0933444555", "bao@gmail.com", 1, 2, "confirmed", 2, 0),
                (2, "Trần Minh Khang", "0987654321", "khang@gmail.com", 2, 5, "confirmed", 2, 0),
                (6, "Võ Thu Uyên", "0908889999", "uyen@gmail.com", 3, 4, "confirmed", 2, 0),
                (5, "Đoàn Gia Hân", "0977888999", "han@gmail.com", 4, 7, "confirmed", 4, 0),
                (1, "Lê Hoàng Yến", "0905111222", "yen@gmail.com", 6, 8, "pending", 2, 0),
                (2, "Nguyễn Ngọc Ánh", "0906667777", "anh@gmail.com", 8, 9, "confirmed", 2, 0),
                (3, "Lâm Hoàng Nam", "0903334444", "nam@gmail.com", 9, 11, "confirmed", 2, 0),
                (4, "Trương Mỹ Linh", "0904445555", "linh@gmail.com", 10, 12, "pending", 3, 0),
                (6, "Hà Văn Đức", "0905556666", "duc@gmail.com", 12, 14, "confirmed", 2, 0),
                (5, "Phạm Đình Khôi", "0907778888", "khoi@gmail.com", 15, 18, "pending", 4, 0),
                # --- Đơn đã hủy (minh họa biểu đồ trạng thái) ---
                (4, "Mai Phương Anh", "0902223333", "phuonganh@gmail.com", -15, -13, "cancelled", 2, -15),
                (1, "Đỗ Mạnh Cường", "0901112222", "cuong@gmail.com", 9, 11, "cancelled", 2, 0),
                # --- Đơn cho 3 villa mới ---
                (7, "Dương Khánh Vy", "0909991111", "vy@gmail.com", -7, -5, "completed", 4, -7),
                (7, "Ngô Tiến Đạt", "0909992222", "dat@gmail.com", 5, 7, "confirmed", 3, 0),
                (8, "Bùi Lan Anh", "0909993333", "lananh@gmail.com", 4, 6, "pending", 2, 0),
                (9, "Trần Gia Hào", "0909994444", "hao@gmail.com", -3, -1, "completed", 6, -3),
            ]
            for room_id, name, phone, email, d_from, d_to, st, guests, d_created in samples:
                ci = (today + timedelta(days=d_from)).isoformat()
                co = (today + timedelta(days=d_to)).isoformat()
                created = (today + timedelta(days=d_created)).isoformat() + " 10:00:00"
                price = cur.execute("SELECT price_per_night FROM rooms WHERE id=?", (room_id,)).fetchone()[0]
                total = price * max(1, (d_to - d_from))
                cur.execute("""INSERT INTO bookings (booking_code,room_id,full_name,phone,email,check_in,check_out,guests,total_price,status,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (gen_booking_code(), room_id, name, phone, email, ci, co, guests, total, st, created))
        if cur.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0:
            cur.executemany("INSERT INTO reviews (room_id,user_name,rating,comment) VALUES (?,?,?,?)", [
                (1, "Thu Hà", 5, "Villa đẹp như Maldives! Hồ bơi vô cực ngắm bình minh đỉnh cao, nhân viên siêu dễ thương."),
                (1, "Minh Khang", 5, "Phòng sạch, view biển trực diện, ăn sáng nổi rất đáng tiền."),
                (2, "Gia Hân", 5, "Hoàng hôn từ ban công đẹp nghẹt thở. Sẽ quay lại!"),
                (4, "Quốc Bảo", 5, "Penthouse xứng đáng từng đồng, quản gia phục vụ 24/7 quá chu đáo."),
                (6, "Hoàng Yến", 5, "Ngủ trên mặt biển, sáng nhìn cá bơi dưới sàn kính — trải nghiệm để đời."),
            ])
    con.commit()
    con.close()
    return first


# ---------- Routes: Trang công khai ----------

@app.route("/")
def index():
    db = get_db()
    featured = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY rating DESC LIMIT 3").fetchall()
    rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 LIMIT 6").fetchall()
    reviews = db.execute("SELECT r.*, rm.name AS room_name FROM reviews r JOIN rooms rm ON rm.id=r.room_id ORDER BY r.created_at DESC LIMIT 3").fetchall()
    stats = {
        "rooms": db.execute("SELECT COUNT(*) c FROM rooms WHERE is_active=1").fetchone()["c"],
        "bookings": db.execute("SELECT COUNT(*) c FROM bookings WHERE status!='cancelled'").fetchone()["c"],
        "guests": 12500,
        "rating": 4.9,
    }
    return render_template("index.html", featured=featured, rooms=rooms, reviews=reviews, stats=stats)

@app.route("/phong")
def rooms_page():
    db = get_db()
    q = request.args.get("q", "").strip()
    ftype = request.args.get("type", "").strip()
    max_price = request.args.get("max_price", "").strip()
    guests = request.args.get("guests", "").strip()
    sql = "SELECT * FROM rooms WHERE is_active=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if ftype:
        sql += " AND type = ?"
        params.append(ftype)
    if max_price and max_price.isdigit():
        sql += " AND price_per_night <= ?"
        params.append(int(max_price))
    if guests and guests.isdigit():
        sql += " AND capacity >= ?"
        params.append(int(guests))
    sql += " ORDER BY price_per_night ASC"
    rooms = db.execute(sql, params).fetchall()
    types = [r["type"] for r in db.execute("SELECT DISTINCT type FROM rooms").fetchall()]
    return render_template("rooms.html", rooms=rooms, types=types, q=q, ftype=ftype, max_price=max_price, guests=guests)

@app.route("/phong/<int:room_id>")
def room_detail(room_id):
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room or not room["is_active"]:
        flash("Villa này hiện tạm ngừng bán. Bạn chọn villa khác nhé!", "warning")
        return redirect(url_for("rooms_page"))
    reviews = db.execute("SELECT * FROM reviews WHERE room_id=? ORDER BY created_at DESC", (room_id,)).fetchall()
    others = db.execute("SELECT * FROM rooms WHERE id!=? AND is_active=1 LIMIT 3", (room_id,)).fetchall()
    amenities = (room["amenities"] or "").split("|")
    return render_template("room_detail.html", room=room, reviews=reviews, others=others, amenities=amenities)

@app.route("/lich-trong")
def calendar_page():
    db = get_db()
    rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY name").fetchall()
    room_id = request.args.get("room_id", type=int) or (rooms[0]["id"] if rooms else None)
    month_str = request.args.get("month", "")
    try:
        if month_str:
            y, m = map(int, month_str.split("-"))
        else:
            y, m = date.today().year, date.today().month
    except ValueError:
        y, m = date.today().year, date.today().month
    booked = booked_dates_in_month(room_id, y, m) if room_id else set()
    room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone() if room_id else None
    return render_template("calendar.html", rooms=rooms, room_id=room_id, room=room,
                           year=y, month=m, booked=sorted(booked),
                           month_str=f"{y:04d}-{m:02d}")

@app.route("/api/availability")
def api_availability():
    room_id = request.args.get("room_id", type=int)
    month_str = request.args.get("month", "")
    if not room_id or not month_str:
        return jsonify({"error": "Thiếu room_id hoặc month (YYYY-MM)"}), 400
    try:
        y, m = map(int, month_str.split("-"))
    except ValueError:
        return jsonify({"error": "month phải dạng YYYY-MM"}), 400
    booked = sorted(booked_dates_in_month(room_id, y, m))
    return jsonify({"room_id": room_id, "month": month_str, "booked_dates": booked})

@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(force=True, silent=True) or request.form
    room_id = int(data.get("room_id", 0))
    ci, co = data.get("check_in", ""), data.get("check_out", "")
    try:
        nights = calc_nights(ci, co)
    except Exception:
        return jsonify({"ok": False, "message": "Ngày không hợp lệ (định dạng YYYY-MM-DD)."})
    if nights <= 0:
        return jsonify({"ok": False, "message": "Ngày trả phòng phải sau ngày nhận phòng ít nhất 1 đêm."})
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        return jsonify({"ok": False, "message": "Không tìm thấy phòng."})
    ok = is_room_available(room_id, ci, co)
    total = room["price_per_night"] * nights
    return jsonify({"ok": ok, "nights": nights, "total": total, "total_text": vnd(total),
                    "message": "✅ Phòng còn trống! Bạn có thể đặt ngay." if ok else "❌ Rất tiếc, khoảng ngày này đã kín. Vui lòng chọn ngày khác."})

# --- Đặt phòng ---
@app.route("/dat-phong/<int:room_id>", methods=["GET", "POST"])
def booking_page(room_id):
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room or not room["is_active"]:
        flash("Villa này hiện tạm ngừng bán. Bạn chọn villa khác nhé!", "warning")
        return redirect(url_for("rooms_page"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        ci = request.form.get("check_in", "")
        co = request.form.get("check_out", "")
        guests = int(request.form.get("guests", 2) or 2)
        note = request.form.get("note", "").strip()
        promo = request.form.get("promo", "").strip().upper()
        if not (full_name and phone and email and ci and co):
            flash("Vui lòng điền đầy đủ họ tên, SĐT, email và ngày nhận/trả phòng.", "danger")
            return render_template("booking.html", room=room)
        try:
            nights = calc_nights(ci, co)
        except Exception:
            flash("Ngày không hợp lệ.", "danger")
            return render_template("booking.html", room=room)
        if nights <= 0:
            flash("Ngày trả phòng phải sau ngày nhận phòng.", "danger")
            return render_template("booking.html", room=room)
        if guests > room["capacity"] + 2:
            flash(f"Villa này tối đa {room['capacity']+2} khách (kê thêm giường phụ). Vui lòng chọn villa lớn hơn.", "danger")
            return render_template("booking.html", room=room)
        if not is_room_available(room_id, ci, co):
            flash("Rất tiếc! Khoảng ngày này vừa có khách đặt. Vui lòng chọn ngày khác hoặc xem lịch trống.", "danger")
            return redirect(url_for("calendar_page", room_id=room_id))
        total = room["price_per_night"] * nights
        discount = 0
        if promo == "AZUREA10":
            discount = int(total * 0.10)
            total -= discount
        code = gen_booking_code()
        while db.execute("SELECT 1 FROM bookings WHERE booking_code=?", (code,)).fetchone():
            code = gen_booking_code()
        uid = session.get("user_id")
        db.execute("""INSERT INTO bookings (booking_code,user_id,room_id,full_name,phone,email,check_in,check_out,guests,total_price,status,note)
                      VALUES (?,?,?,?,?,?,?,?,?,?, 'confirmed', ?)""",
                   (code, uid, room_id, full_name, phone, email, ci, co, guests, total, note))
        db.commit()
        return redirect(url_for("booking_success", code=code))
    # GET: prefill
    u = current_user()
    return render_template("booking.html", room=room, user=u)

@app.route("/dat-thanh-cong/<code>")
def booking_success(code):
    db = get_db()
    b = db.execute("""SELECT b.*, r.name AS room_name, r.image_url FROM bookings b
                      JOIN rooms r ON r.id=b.room_id WHERE b.booking_code=?""", (code,)).fetchone()
    if not b:
        flash("Không tìm thấy đơn đặt phòng.", "danger")
        return redirect(url_for("index"))
    nights = calc_nights(b["check_in"], b["check_out"])
    return render_template("booking_success.html", b=b, nights=nights)

@app.route("/tra-cuu", methods=["GET", "POST"])
def lookup():
    result = None
    verified = False  # đã xác thực SĐT/chính chủ → mới hiện đầy đủ họ tên + SĐT
    code = request.args.get("code", "") or request.form.get("code", "")
    phone = request.form.get("phone", "")
    if request.method == "POST" or code:
        db = get_db()
        if phone:
            result = db.execute("""SELECT b.*, r.name AS room_name FROM bookings b JOIN rooms r ON r.id=b.room_id
                                   WHERE b.booking_code=? AND b.phone=?""", (code.strip().upper(), phone.strip())).fetchone()
            if not result:
                flash("Không tìm thấy đơn khớp mã + SĐT. Kiểm tra lại giúp bạn nhé!", "warning")
            else:
                verified = True
        else:
            rows = db.execute("""SELECT b.*, r.name AS room_name FROM bookings b JOIN rooms r ON r.id=b.room_id
                                 WHERE b.booking_code=?""", (code.strip().upper(),)).fetchall()
            result = rows[0] if rows else None
            if not result and code:
                flash("Không tìm thấy mã đặt phòng này.", "warning")
            elif result:
                # Chính chủ (đơn của tài khoản đang đăng nhập) hoặc admin → coi như đã xác thực
                u = current_user()
                if u and (u["is_admin"] or u["id"] == result["user_id"]):
                    verified = True
    # danh sách của user đang đăng nhập
    mine = []
    if "user_id" in session:
        db = get_db()
        mine = db.execute("""SELECT b.*, r.name AS room_name FROM bookings b JOIN rooms r ON r.id=b.room_id
                             WHERE b.user_id=? ORDER BY b.created_at DESC""", (session["user_id"],)).fetchall()
    return render_template("lookup.html", result=result, mine=mine, code=code, verified=verified)

@app.route("/huy-dat-phong/<code>", methods=["POST"])
def cancel_booking(code):
    db = get_db()
    b = db.execute("SELECT * FROM bookings WHERE booking_code=?", (code.strip().upper(),)).fetchone()
    if not b:
        flash("Không tìm thấy đơn.", "danger")
        return redirect(url_for("lookup"))
    phone = request.form.get("phone", "").strip()
    # cho phép hủy nếu đúng SĐT hoặc là admin hoặc chính chủ
    u = current_user()
    allowed = (phone and phone == b["phone"]) or (u and (u["is_admin"] or u["id"] == b["user_id"]))
    if not allowed:
        flash("SĐT xác nhận chưa đúng. Vui lòng nhập đúng SĐT lúc đặt phòng.", "danger")
        return redirect(url_for("lookup", code=code))
    if b["status"] == "cancelled":
        flash("Đơn này đã hủy trước đó.", "info")
    else:
        db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
        db.commit()
        flash(f"Đã hủy đơn {code}. AZUREA sẽ hoàn tiền theo chính sách trong 3–5 ngày. Hẹn gặp lại bạn!", "success")
    return redirect(url_for("lookup", code=code))

@app.route("/danh-gia", methods=["POST"])
def add_review():
    db = get_db()
    room_id = request.form.get("room_id", type=int)
    name = request.form.get("user_name", "").strip() or "Du khách"
    rating = int(request.form.get("rating", 5) or 5)
    comment = request.form.get("comment", "").strip()
    if not comment:
        flash("Vui lòng viết vài dòng cảm nhận nhé!", "warning")
        return redirect(url_for("room_detail", room_id=room_id))
    db.execute("INSERT INTO reviews (room_id,user_name,rating,comment) VALUES (?,?,?,?)",
               (room_id, name, max(1, min(5, rating)), comment))
    db.commit()
    flash("Cảm ơn bạn đã đánh giá!", "success")
    return redirect(url_for("room_detail", room_id=room_id))

@app.route("/lien-he", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        db = get_db()
        db.execute("INSERT INTO contacts (name,email,phone,subject,message) VALUES (?,?,?,?,?)",
                   (request.form.get("name", ""), request.form.get("email", ""),
                    request.form.get("phone", ""), request.form.get("subject", ""),
                    request.form.get("message", "")))
        db.commit()
        flash("Đã gửi lời nhắn! Concierge sẽ liên hệ trong 10 phút.", "success")
        return redirect(url_for("contact"))
    return render_template("contact.html")

@app.route("/gioi-thieu")
def about():
    db = get_db()
    n_rooms = db.execute("SELECT COUNT(*) c FROM rooms WHERE is_active=1").fetchone()["c"]
    return render_template("about.html", n_rooms=n_rooms)

# ---------- Auth ----------
@app.route("/dang-nhap", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and check_password_hash(u["password_hash"], pw):
            session["user_id"] = u["id"]
            session["user_name"] = u["name"]
            session["is_admin"] = bool(u["is_admin"])
            flash(f"Chào mừng trở lại, {u['name']}!", "success")
            nxt = request.args.get("next") or request.form.get("next") or url_for("index")
            if u["is_admin"] and nxt == url_for("index"):
                return redirect(url_for("admin"))
            return redirect(nxt)
        flash("Email hoặc mật khẩu chưa đúng.", "danger")
    return render_template("login.html")

@app.route("/dang-ky", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        db = get_db()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        pw = request.form.get("password", "")
        if not (name and email and pw):
            flash("Vui lòng điền đủ họ tên, email, mật khẩu.", "danger")
            return render_template("register.html")
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("Email đã được đăng ký. Hãy đăng nhập nhé!", "warning")
            return redirect(url_for("login"))
        db.execute("INSERT INTO users (name,email,phone,password_hash) VALUES (?,?,?,?)",
                   (name, email, phone, generate_password_hash(pw)))
        db.commit()
        flash("Đăng ký thành công! Hãy đăng nhập để đặt phòng nhanh hơn.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/dang-xuat")
def logout():
    session.clear()
    flash("Đã đăng xuất. Hẹn gặp lại bạn!", "info")
    return redirect(url_for("index"))

# ---------- Chatbot Flows: đặt hộ & gọi nhân viên ----------
# Hội thoại có trạng thái (state machine) theo từng phiên chat.
# Toàn bộ engine mới KHÔNG phân biệt có dấu / không dấu ("dat ho" == "đặt hộ").
chat_sessions = {}  # sid -> {"flow": "booking"/"contact"/None, "step":..., "data":{...}, "updated":...}

def norm(s):
    """Chuẩn hóa tiếng Việt: thường + bỏ dấu ('đặt hộ' -> 'dat ho')."""
    s = (s or "").lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", s)
                   if unicodedata.category(ch) != "Mn")

def get_chat_session(sid):
    now = datetime.now()
    if len(chat_sessions) > 200:  # dọn session quá 30 phút không hoạt động
        for k in [k for k, v in chat_sessions.items()
                  if (now - v["updated"]).total_seconds() > 1800]:
            chat_sessions.pop(k, None)
    s = chat_sessions.get(sid)
    if not s:
        s = {"flow": None, "step": None, "data": {}, "updated": now, "history": []}
        chat_sessions[sid] = s
    if "history" not in s:
        s["history"] = []
    s["updated"] = now
    return s

def is_exit(low):
    # Lưu ý: "không" đơn lẻ KHÔNG tính là thoát (đó là câu trả lời ở bước mã ưu đãi)
    t = norm(low).strip()
    if t in ("huy", "thoat", "dung", "cancel", "stop", "thoi"):
        return True
    return any(k in t for k in ("khong dat nua", "khong lien he nua", "huy bo",
                                "dung lai", "thoat ra", "khong can nua"))

def room_options_text(db):
    rooms = db.execute(
        "SELECT id, name, type, price_per_night, capacity FROM rooms WHERE is_active=1 ORDER BY id").fetchall()
    lines = [f"<b>{i+1}.</b> {r['name']} ({r['type']}) — {vnd(r['price_per_night'])}/đêm"
             for i, r in enumerate(rooms)]
    return "<br>".join(lines), rooms

def find_room(db, text):
    rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY id").fetchall()
    t = norm(text).strip()
    if t.isdigit():
        i = int(t) - 1
        if 0 <= i < len(rooms):
            return rooms[i]
        return None
    for r in rooms:
        rn = norm(r["name"])
        if rn in t or (len(t) >= 3 and t in rn):
            return r
    return None

def parse_vi_date(s):
    """Hiểu 'mai', 'mốt', 'tuần sau', '20/09', '20/09/2026', '2026-09-20'. Trả về date hoặc None."""
    s = norm(s).strip()
    today = date.today()
    if s in ("hom nay", "today"):
        return today
    if s in ("mai", "ngay mai", "tomorrow"):
        return today + timedelta(days=1)
    if s in ("mot", "ngay mot"):
        return today + timedelta(days=2)
    if "tuan sau" in s:
        return today + timedelta(days=7)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", s)
    if m:
        dd, mm = int(m.group(1)), int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else today.year
        if yy < 100:
            yy += 2000
        try:
            d = date(yy, mm, dd)
        except ValueError:
            return None
        if m.group(3) is None and d < today:  # không ghi năm mà đã qua → hiểu là năm sau
            try:
                d = date(yy + 1, mm, dd)
            except ValueError:
                return None
        return d
    return None

def fmt_d(iso):
    y, m, dd = iso.split("-")
    return f"{dd}/{m}/{y}"

# ----- Lịch mini hiện ngay trong chatbot -----
WD_VN = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]

def match_room_type(db, nlow):
    types = [r["type"] for r in db.execute(
        "SELECT DISTINCT type FROM rooms WHERE is_active=1").fetchall()]
    for t in types:
        if norm(t) in (nlow or ""):
            return t
    return None

def extract_dates(nlow):
    """Móc mọi ngày trong câu (số lẫn chữ như 'mai', 'tuần sau'), trả list date đã sắp xếp."""
    found = []
    for m in re.finditer(r"\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?|\d{4}-\d{1,2}-\d{1,2}", nlow or ""):
        d = parse_vi_date(m.group(0))
        if d and d not in found:
            found.append(d)
    for word, delta in (("ngay mai", 1), ("ngay mot", 2), ("tuan sau", 7), ("hom nay", 0)):
        if word in (nlow or ""):
            d = date.today() + timedelta(days=delta)
            if d not in found:
                found.append(d)
    if re.search(r"\bmai\b", nlow or ""):
        d = date.today() + timedelta(days=1)
        if d not in found:
            found.append(d)
    found.sort()
    return found

def night_free(db, room_id, d):
    return is_room_available(room_id, d.isoformat(), (d + timedelta(days=1)).isoformat())

def avail_mini_cal(db, room, days=14):
    """Bảng lịch mini xanh/vàng: mỗi ô một đêm."""
    today = date.today()
    cells = []
    for i in range(days):
        d = today + timedelta(days=i)
        ok = night_free(db, room["id"], d)
        bg, fg = ("#dcfce7", "#166534") if ok else ("#f9e7b8", "#8a5a00")
        mark = "Trống" if ok else "Kín"
        cells.append(
            f"<td style='background:{bg};color:{fg};border-radius:8px;padding:5px 1px;font-size:.76rem'>"
            f"<b>{d.strftime('%d/%m')}</b><br>{WD_VN[d.weekday()]} · {mark}</td>")
    rows = "".join(f"<tr>{''.join(cells[i:i + 7])}</tr>" for i in range(0, days, 7))
    return (f"<table style='width:100%;border-collapse:separate;border-spacing:3px;text-align:center'>{rows}</table>"
            f"<div style='font-size:.78rem;color:#5b7286'>"
            f"<span style='display:inline-block;width:10px;height:10px;border-radius:3px;background:#dcfce7'></span> Còn trống "
            f"<span style='display:inline-block;width:10px;height:10px;border-radius:3px;background:#f9e7b8;margin-left:.5rem'></span> Đã kín</div>")

def avail_overview(db, type_filter=None, days=7):
    rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY id").fetchall()
    if type_filter:
        rooms = [r for r in rooms if r["type"] == type_filter]
    today = date.today()
    lines = []
    for r in rooms:
        busy = []
        for i in range(days):
            d = today + timedelta(days=i)
            if not night_free(db, r["id"], d):
                busy.append(d.strftime("%d/%m"))
        free_n = days - len(busy)
        extra = f" (kín: {', '.join(busy)})" if busy and free_n else ""
        state = f"trống {free_n}/{days} đêm tới{extra}" if free_n else "kín cả 7 đêm tới"
        lines.append(f"• <b>{r['name']}</b>: {state}")
    head = f"Lịch 7 đêm tới — hạng <b>{type_filter}</b>:<br>" if type_filter else "Lịch 7 đêm tới của các villa:<br>"
    return head + "<br>".join(lines)

def avail_answer(db, msg, nlow, ctx):
    """Trả lời lịch trống với dữ liệu thật: khoảng ngày / 1 đêm / lịch mini / tổng quan."""
    room = find_room(db, msg)
    rtype = None if room else match_room_type(db, nlow)
    dates = extract_dates(nlow)

    def remember(r):
        if ctx is not None and r is not None:
            ctx["last_room_id"] = r["id"]

    # 1. villa + khoảng ngày → trả lời luôn trống/kín kèm tiền
    if room and len(dates) >= 2:
        remember(room)
        ci, co = dates[0], dates[1]
        if ci == co:
            return ("2 ngày bạn chọn trùng nhau. Bạn cho tôi khoảng khác nhé "
                    "(VD: 'Ocean Pearl Villa từ 20/09 đến 22/09')."), ["Lịch trống", "Đặt hộ tôi"]
        nights = (co - ci).days
        total = room["price_per_night"] * nights
        if is_room_available(room["id"], ci.isoformat(), co.isoformat()):
            return (f"<b>{room['name']}</b> từ {fmt_d(ci.isoformat())} đến {fmt_d(co.isoformat())}: "
                    f"<b style='color:#0b6b5b'>CÒN TRỐNG</b><br>"
                    f"{nights} đêm — Tổng <b>{vnd(total)}</b>. Giữ chỗ luôn không bạn?"), ["Đặt phòng này", "Villa khác"]
        return (f"<b>{room['name']}</b> từ {fmt_d(ci.isoformat())} đến {fmt_d(co.isoformat())}: "
                f"<b style='color:#8a5a00'>ĐÃ KÍN</b>.<br>Bạn chọn khoảng khác, "
                f"hoặc để tôi đặt hộ villa khác nhé!"), ["Lịch trống", "Đặt hộ tôi"]

    # 2. có khoảng ngày nhưng chưa rõ villa → villa nào trống
    if len(dates) >= 2 and not room and not rtype:
        ci, co = dates[0], dates[1]
        if ci == co:
            return ("2 ngày bạn chọn trùng nhau. Bạn cho tôi khoảng khác nhé "
                    "(VD: '20/09 đến 22/09 còn villa nào?')."), ["Lịch trống", "Đặt hộ tôi"]
        nights = (co - ci).days
        rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY price_per_night").fetchall()
        lines = []
        for r in rooms:
            if is_room_available(r["id"], ci.isoformat(), co.isoformat()):
                lines.append(f"• <b>{r['name']}</b>: Trống — {nights} đêm ~ {vnd(r['price_per_night'] * nights)}")
            else:
                lines.append(f"• {r['name']}: Kín")
        return (f"Từ {fmt_d(ci.isoformat())} đến {fmt_d(co.isoformat())}:<br>" + "<br>".join(lines)
                + "<br><br>Bạn muốn đặt villa nào? Gõ tên villa hoặc bấm 'Đặt hộ tôi'."), ["Đặt hộ tôi", "Lịch trống"]

    # 3. villa + 1 ngày → đêm đó còn không
    if room and len(dates) == 1:
        remember(room)
        d = dates[0]
        ok = night_free(db, room["id"], d)
        state = "<b style='color:#0b6b5b'>CÒN TRỐNG</b>" if ok else "<b style='color:#8a5a00'>ĐÃ KÍN</b>"
        return (f"Đêm {fmt_d(d.isoformat())} ({WD_VN[d.weekday()]}), <b>{room['name']}</b>: {state}<br>"
                f"Giá {vnd(room['price_per_night'])}/đêm."), ["Đặt phòng này", "Villa khác"]

    # 4. chỉ 1 ngày → đêm đó villa nào trống
    if len(dates) == 1 and not room and not rtype:
        d = dates[0]
        rooms = db.execute("SELECT * FROM rooms WHERE is_active=1 ORDER BY price_per_night").fetchall()
        lines = [(f"• <b>{r['name']}</b>: Trống ({vnd(r['price_per_night'])}/đêm)"
                  if night_free(db, r["id"], d) else f"• {r['name']}: Kín") for r in rooms]
        return (f"Đêm {fmt_d(d.isoformat())} ({WD_VN[d.weekday()]}):<br>" + "<br>".join(lines)), ["Đặt hộ tôi", "Lịch trống"]

    # 5. villa cụ thể → lịch mini 14 ngày
    if room:
        remember(room)
        return (f"Lịch <b>{room['name']}</b> 14 đêm tới:<br>{avail_mini_cal(db, room)}"
                f"<br>Xem cả tháng tại <a href='/lich-trong?room_id={room['id']}'><b>Lịch trống</b></a>."), ["Đặt phòng này", "Villa khác"]

    # 6. hạng phòng → tổng quan các villa hạng đó + chip tên villa
    if rtype:
        names = [r["name"] for r in db.execute(
            "SELECT name FROM rooms WHERE is_active=1 AND type=? ORDER BY id", (rtype,)).fetchall()]
        return (avail_overview(db, rtype)
                + "<br><br>Bấm tên villa để xem lịch chi tiết 14 ngày."), names

    # 7. mặc định → tổng quan tất cả + chip các hạng
    return (avail_overview(db)
            + "<br><br>Bấm hạng phòng để lọc, hoặc gõ tên villa kèm ngày "
              "(VD: 'Ocean Pearl Villa từ 20/09 đến 22/09')."), \
        ["Ocean Villa", "Coral Suite", "Pearl Bungalow", "Royal Penthouse"]

# ----- Luồng 1: Đặt phòng hộ -----
def start_booking_flow(sess, db):
    sess["flow"] = "booking"
    sess["step"] = "room"
    sess["data"] = {}
    opts, _ = room_options_text(db)
    return (f"Tuyệt! Tôi sẽ đặt phòng hộ bạn ngay trong khung chat này.<br><br>{opts}"
            f"<br><br>Bạn chọn villa <b>số mấy</b> (hoặc gõ tên villa)?"), ["1", "2", "3", "Hủy"]

def handle_booking_flow(sess, text, low, db):
    step = sess["step"]
    d = sess["data"]

    if step == "room":
        room = find_room(db, text)
        if not room:
            opts, _ = room_options_text(db)
            return (f"Tôi chưa tìm thấy villa đó. Bạn chọn lại giúp tôi nhé:<br><br>{opts}"), ["Hủy"]
        d.update(room_id=room["id"], room_name=room["name"],
                 price=room["price_per_night"], capacity=room["capacity"])
        sess["step"] = "checkin"
        return (f"Đã chọn <b>{room['name']}</b> — {vnd(room['price_per_night'])}/đêm.<br>"
                f"Bạn <b>nhận phòng ngày nào</b>? (gõ VD: 20/09 hoặc 20/09/2026, gõ 'mai' nếu là ngày mai)"), ["Hủy"]

    if step == "checkin":
        ci = parse_vi_date(text)
        if not ci:
            return "Ngày chưa đúng định dạng. Bạn gõ lại giúp tôi theo kiểu <b>ngày/tháng</b>, VD: 20/09", ["Hủy"]
        if ci < date.today():
            return "Ngày nhận phòng phải từ <b>hôm nay</b> trở đi. Bạn chọn ngày khác nhé!", ["Hủy"]
        d["check_in"] = ci.isoformat()
        sess["step"] = "checkout"
        return f"Nhận phòng: <b>{fmt_d(d['check_in'])}</b>.<br>Bạn <b>trả phòng ngày nào</b>?", ["Hủy"]

    if step == "checkout":
        co = parse_vi_date(text)
        ci = date.fromisoformat(d["check_in"])
        if not co:
            return "Ngày chưa đúng định dạng. Bạn gõ lại theo kiểu <b>ngày/tháng</b>, VD: 22/09", ["Hủy"]
        if co <= ci:
            return "Ngày trả phòng phải <b>sau</b> ngày nhận phòng. Bạn gõ lại nhé!", ["Hủy"]
        if not is_room_available(d["room_id"], ci.isoformat(), co.isoformat()):
            sess["step"] = "checkin"
            return (f"Tiếc quá, <b>{d['room_name']}</b> đã kín từ {ci:%d/%m} đến {co:%d/%m}. "
                    f"Bạn chọn khoảng ngày khác nhé — <b>nhận phòng ngày nào</b>?"), ["Hủy"]
        nights = (co - ci).days
        d.update(check_out=co.isoformat(), nights=nights, total=d["price"] * nights)
        sess["step"] = "guests"
        return (f"Phòng còn trống! <b>{nights} đêm</b> — tạm tính <b>{vnd(d['total'])}</b>.<br>"
                f"Đoàn mình đi <b>mấy khách</b>? (villa tối đa {d['capacity']} khách, kê giường phụ +2)"), ["2", "4", "Hủy"]

    if step == "guests":
        m = re.search(r"\d+", text)
        g = int(m.group()) if m else 0
        if g < 1 or g > d["capacity"] + 2:
            return f"Số khách chưa hợp lệ (1–{d['capacity'] + 2}). Bạn gõ lại giúp tôi!", ["Hủy"]
        d["guests"] = g
        sess["step"] = "name"
        return f"Đoàn {g} khách — ghi nhận! Bạn cho tôi xin <b>họ tên người đặt</b>?", ["Hủy"]

    if step == "name":
        if len(text.strip()) < 2:
            return "Tên hơi ngắn, bạn gõ đầy đủ họ tên giúp tôi nhé!", ["Hủy"]
        d["name"] = text.strip()
        sess["step"] = "phone"
        return f"Cảm ơn {d['name']}! Cho tôi xin <b>số điện thoại</b> để resort liên hệ?", ["Hủy"]

    if step == "phone":
        digits = re.sub(r"\D", "", text)
        if len(digits) < 9 or len(digits) > 11:
            return "SĐT chưa đúng (cần 9–11 chữ số). Bạn gõ lại giúp tôi!", ["Hủy"]
        d["phone"] = digits
        sess["step"] = "email"
        return "Số này resort sẽ gọi xác nhận. Bạn cho tôi xin <b>email</b> để nhận mã đặt phòng?", ["Hủy"]

    if step == "email":
        if not re.match(r"^[\w.\-]+@[\w\-]+(\.[\w\-]+)+$", text.strip()):
            return "Email chưa đúng định dạng. Bạn gõ lại giúp tôi nhé!", ["Hủy"]
        d["email"] = text.strip()
        sess["step"] = "promo"
        return "Gần xong rồi! Bạn có <b>mã ưu đãi</b> không? (gõ mã, VD: AZUREA10 — hoặc gõ 'không')", ["AZUREA10", "Không", "Hủy"]

    if step == "promo":
        promo = text.strip().upper()
        d["promo"] = "" if norm(promo) in ("khong", "k", "bo qua", "khong co", "-", "ko", "khong co ma") else promo
        total = d["price"] * d["nights"]
        if d["promo"] == "AZUREA10":
            total = int(total * 0.9)
        d["final"] = total
        sess["step"] = "confirm"
        if d["promo"] == "AZUREA10":
            promo_line = " (đã trừ 10% mã AZUREA10)"
        elif d["promo"]:
            promo_line = f" (mã {d['promo']} không áp dụng, giữ nguyên giá)"
        else:
            promo_line = ""
        return (f"Xác nhận lại giúp tôi nhé:<br>"
                f"• Villa: <b>{d['room_name']}</b><br>"
                f"• {fmt_d(d['check_in'])} → {fmt_d(d['check_out'])} ({d['nights']} đêm, {d['guests']} khách)<br>"
                f"• Khách: {d['name']} — {d['phone']} — {d['email']}<br>"
                f"• Tổng: <b>{vnd(total)}</b>{promo_line} (cọc 30% giữ phòng)<br><br>"
                f"Gõ <b>XÁC NHẬN</b> để tôi đặt luôn, hoặc 'làm lại' để nhập từ đầu."), ["XÁC NHẬN", "Làm lại", "Hủy"]

    if step == "confirm":
        nl = norm(low)
        if re.search(r"xac nhan|dong y|chot|dat luon|dung roi|^ok$|^oke$|^co$|yes", nl):
            code = gen_booking_code()
            while db.execute("SELECT 1 FROM bookings WHERE booking_code=?", (code,)).fetchone():
                code = gen_booking_code()
            note = "Đặt hộ qua chatbot Marina" + (" (mã AZUREA10)" if d.get("promo") == "AZUREA10" else "")
            db.execute("""INSERT INTO bookings (booking_code,user_id,room_id,full_name,phone,email,check_in,check_out,guests,total_price,status,note)
                          VALUES (?,?,?,?,?,?,?,?,?,?, 'confirmed', ?)""",
                       (code, session.get("user_id"), d["room_id"], d["name"], d["phone"], d["email"],
                        d["check_in"], d["check_out"], d["guests"], d["final"], note))
            db.commit()
            sess["flow"] = None
            sess["step"] = None
            sess["data"] = {}
            return (f"Đặt thành công! Mã của bạn là <b>{code}</b><br>"
                    f"{d['room_name']} — {fmt_d(d['check_in'])} → {fmt_d(d['check_out'])} — Tổng <b>{vnd(d['final'])}</b><br>"
                    f"Chi tiết và hủy đơn tại <a href='/tra-cuu?code={code}'><b>Tra cứu</b></a>. Hẹn gặp bạn giữa biển xanh!"), ["Đặt thêm phòng", "Tra cứu đặt phòng"]
        if "lam lai" in norm(low) or "nhap lai" in norm(low):
            return start_booking_flow(sess, db)
        return "Bạn gõ <b>XÁC NHẬN</b> để tôi đặt luôn, 'làm lại' để nhập từ đầu, hoặc 'hủy' để dừng nhé!", ["XÁC NHẬN", "Làm lại", "Hủy"]

    sess["flow"] = None
    return chatbot_reply(text)

# ----- Luồng 2: Gọi nhân viên (để lại lời nhắn) -----
def start_contact_flow(sess):
    sess["flow"] = "contact"
    sess["step"] = "name"
    sess["data"] = {}
    return "Để nhân viên gọi lại, bạn cho tôi xin <b>tên của bạn</b>?", ["Hủy"]

def handle_contact_flow(sess, text, low, db):
    step = sess["step"]
    d = sess["data"]
    if step == "name":
        if len(text.strip()) < 2:
            return "Bạn gõ đầy đủ họ tên giúp tôi nhé!", ["Hủy"]
        d["name"] = text.strip()
        sess["step"] = "phone"
        return f"Cảm ơn {d['name']}! Cho tôi xin <b>SĐT</b> để nhân viên gọi lại?", ["Hủy"]
    if step == "phone":
        digits = re.sub(r"\D", "", text)
        if len(digits) < 9 or len(digits) > 11:
            return "SĐT cần 9–11 chữ số. Bạn gõ lại giúp tôi!", ["Hủy"]
        d["phone"] = digits
        sess["step"] = "content"
        return "Bạn muốn <b>nhắn gì</b> tới nhân viên? (VD: tư vấn tour đảo, hỏi giá villa...)", ["Hủy"]
    if step == "content":
        if len(text.strip()) < 3:
            return "Bạn viết rõ hơn một chút giúp tôi nhé!", ["Hủy"]
        db.execute("INSERT INTO contacts (name,email,phone,subject,message) VALUES (?,?,?,?,?)",
                   (d["name"], "", d["phone"], "Nhắn từ chatbot Marina", text.strip()))
        db.commit()
        sess["flow"] = None
        sess["step"] = None
        sess["data"] = {}
        return (f"Đã ghi nhận! Nhân viên concierge sẽ gọi lại số <b>{d['phone']}</b> trong 10 phút (7:00–21:00). "
                f"Cần gì gấp bạn gọi hotline <b>1900 6868</b> nhé!"), ["Đặt hộ tôi", "Xem bảng giá"]
    sess["flow"] = None
    return chatbot_reply(text)

# ---------- Chatbot AI thật qua OpenRouter (Marina) ----------
# Hybrid: câu hỏi thường + đặt phòng 1 lệnh do AI đảm nhiệm (có DB thật grounding),
# tra mã AZ-... và các tác vụ cần chính xác tuyệt đối vẫn tra DB trực tiếp.
# Khi chưa có OPENROUTER_API_KEY → tự fallback về chatbot rule-based cũ.

def build_resort_context(db):
    """Tóm tắt dữ liệu thật của resort để nhét vào system prompt."""
    try:
        rooms = db.execute(
            "SELECT name, type, price_per_night, capacity FROM rooms WHERE is_active=1 ORDER BY id"
        ).fetchall()
        lines = [f"- {r['name']} ({r['type']}) — {vnd(r['price_per_night'])}/đêm, tối đa {r['capacity']}+2 khách"
                 for r in rooms]
        room_text = "\n".join(lines) if lines else "- (chưa có dữ liệu phòng)"
    except Exception:
        room_text = "- (không đọc được DB)"
    try:
        overview_html = avail_overview(db)
        overview_plain = re.sub(r"<[^>]+>", "", overview_html)
    except Exception:
        overview_plain = ""
    return (f"Hôm nay: {date.today().isoformat()} (định dạng YYYY-MM-DD).\n"
            f"DANH SÁCH VILLA ĐANG BÁN:\n{room_text}\n\n"
            f"LỊCH THẬT 7 ĐÊM TỚI:\n{overview_plain[:1200]}\n\n"
            f"Chính sách: nhận 14:00 / trả 12:00; cọc 30%; hủy trước 7 ngày hoàn 100%, "
            f"3–7 ngày hoàn 50%, trong 3 ngày không hoàn (được dời 1 lần); mã AZUREA10 giảm 10% online; "
            f"hotline 1900 6868; địa chỉ Bãi Dài, đảo Coral, Nha Trang.")


def build_marina_system_prompt(db, extra_grounding=""):
    base = (
        "Bạn là Marina — trợ lý lễ tân của resort AZUREA ISLANDS (Bãi Dài, đảo Coral, Nha Trang). "
        "Trả lời bằng TIẾNG VIỆT, thân thiện, xưng 'tôi/Marina', gọi khách 'quý khách/bạn'. "
        "Cho phép dùng HTML đơn giản: <b>, <br>, <a href='...'>.\n\n"
        "PHẠM VI: bạn CHỈ được tư vấn về resort: phòng/villa, giá, lịch trống, đặt/hủy phòng, "
        "thanh toán, tour đảo, spa/nhà hàng, địa chỉ, liên hệ. "
        "Mọi chủ đề ngoài resort (giải bài tập, code, toán, văn, kiến thức chung, chính trị...) "
        "đều từ chối lịch sự đúng 1 mẫu: "
        "'Xin lỗi, Marina chỉ hỗ trợ các vấn đề về AZUREA ISLANDS (đặt phòng, giá, lịch trống, tour, ưu đãi). "
        "Bạn cần hỗ trợ gì về kỳ nghỉ của mình?' — và gợi ý 1 chủ đề resort.\n\n"
        "BẢO MẬT: không bao giờ tiết lộ họ tên, SĐT, email của bất kỳ khách nào khác; "
        "không liệt kê hay đoán thông tin đơn đặt phòng của người khác. "
        "Khi khách tra cứu đơn, chỉ xác nhận chi tiết khi họ cung cấp đúng MÃ ĐƠN + SĐT lúc đặt; "
        "mọi câu trả lời về lịch chỉ ở mức trống/kín, không kèm tên khách.\n\n"
        "ĐẶT PHÒNG 1 LỆNH: nếu khách muốn đặt phòng và ĐÃ cho đủ 7 thông tin "
        "(tên villa khớp danh sách, ngày nhận, ngày trả, số khách, họ tên, SĐT 9–11 số, email đúng định dạng; "
        "mã ưu đãi là tùy chọn), bạn XUẤT thêm đúng 1 khối cuối tin nhắn:\n"
        "<!-- BOOKING_JSON: {\"room_name\": \"...\", \"check_in\": \"YYYY-MM-DD\", \"check_out\": \"YYYY-MM-DD\", "
        "\"guests\": 2, \"full_name\": \"...\", \"phone\": \"...\", \"email\": \"...\", \"promo\": \"\"} -->\n"
        "Ngày phải đổi về YYYY-MM-DD (hiểu 'mai', 'mốt', '20/09', '20/09/2026'). "
        "Nếu THIẾU bất kỳ trường nào thì KHÔNG xuất JSON — hỏi tiếp tự nhiên để lấy đủ, mỗi lần hỏi tối đa 2 trường còn thiếu. "
        "Không bao giờ bịa mã đặt phòng, không khẳng định chắc chắn còn phòng với ngày xa — backend sẽ kiểm tra DB thật "
        "trước khi tạo đơn; bạn chỉ ước lượng từ dữ liệu được cấp.\n\n"
        "DỮ LIỆU RESORT THẬT (lấy từ database, ưu tiên cao nhất):\n"
        f"{build_resort_context(db)}"
    )
    if extra_grounding:
        base += f"\n\n{extra_grounding}"
    return base


def call_openrouter_api(messages, timeout=30):
    """Gọi OpenRouter Chat Completions, trả về text. Raise RuntimeError nếu lỗi."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("Chưa cấu hình OPENROUTER_API_KEY trong file .env.")
    if requests is None:
        raise RuntimeError("Chưa cài thư viện requests (chạy: pip install -r requirements.txt).")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_SITE_URL,
            "X-Title": OPENROUTER_APP_NAME,
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 900,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        detail = resp.text[:500]
        if resp.status_code == 401:
            raise RuntimeError("OpenRouter báo 401 — API key sai/thiếu. Kiểm tra OPENROUTER_API_KEY trong .env.")
        if resp.status_code == 404:
            raise RuntimeError(f"OpenRouter báo 404 — model '{OPENROUTER_MODEL}' không tồn tại. Đổi OPENROUTER_MODEL trong .env.")
        if resp.status_code == 429:
            raise RuntimeError("OpenRouter báo 429 — hết quota/rate-limit của model free. Thử lại sau hoặc đổi model.")
        raise RuntimeError(f"OpenRouter lỗi {resp.status_code}: {detail}")
    data = resp.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"OpenRouter trả về không đúng định dạng: {str(data)[:500]}")


def extract_booking_json(text):
    """Móc JSON đặt phòng mà AI xuất ra. Trả về dict hoặc None."""
    if not text:
        return None
    m = re.search(r"BOOKING_JSON\s*:\s*(\{.*?\})\s*-->", text, re.DOTALL)
    if not m:
        m = re.search(r"```(?:json)?\s*(\{[^`]*?\"room_name\"[^`]*?\})\s*```", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def try_ai_booking(data, db):
    """Validate dữ liệu AI trích xuất với DB thật rồi tạo đơn. Trả về (ok, reply_html)."""
    room_name = (data.get("room_name") or "").strip()
    room = find_room(db, room_name) if room_name else None
    if not room:
        return False, (f"Tôi chưa tìm thấy villa <b>{room_name or '(trống)'}</b> trong hệ thống. "
                       f"Bạn chọn lại 1 villa trong danh sách nhé (VD: Ocean Pearl Villa).")
    # Ngày: ưu tiên ISO, fallback parse tiếng Việt
    def to_date(v):
        if not v:
            return None
        s = str(v).strip()
        try:
            return date.fromisoformat(s)
        except ValueError:
            return parse_vi_date(s)
    ci = to_date(data.get("check_in"))
    co = to_date(data.get("check_out"))
    if not ci or not co:
        return False, "Ngày nhận/trả chưa rõ. Bạn cho tôi <b>ngày nhận và ngày trả</b> (VD: 20/10 đến 22/10) nhé!"
    if co <= ci:
        return False, "Ngày trả phòng phải <b>sau</b> ngày nhận phòng. Bạn cho lại khoảng ngày nhé!"
    if ci < date.today():
        return False, "Ngày nhận phòng phải từ <b>hôm nay</b> trở đi. Bạn chọn ngày khác nhé!"
    try:
        guests = int(data.get("guests") or 0)
    except (ValueError, TypeError):
        guests = 0
    if guests < 1 or guests > room["capacity"] + 2:
        return False, (f"Villa <b>{room['name']}</b> chỉ nhận 1–{room['capacity'] + 2} khách. "
                       f"Bạn cho tôi lại <b>số khách</b> nhé!")
    full_name = (data.get("full_name") or "").strip()
    if len(full_name) < 2:
        return False, "Bạn cho tôi xin <b>họ tên người đặt</b> nhé!"
    phone = re.sub(r"\D", "", str(data.get("phone") or ""))
    if len(phone) < 9 or len(phone) > 11:
        return False, "SĐT cần 9–11 chữ số. Bạn cho tôi lại <b>số điện thoại</b> nhé!"
    email = (data.get("email") or "").strip()
    if not re.match(r"^[\w.\-]+@[\w\-]+(\.[\w\-]+)+$", email):
        return False, "Email chưa đúng định dạng. Bạn cho tôi lại <b>email</b> để nhận mã đặt phòng nhé!"
    promo = (data.get("promo") or "").strip().upper()
    if not is_room_available(room["id"], ci.isoformat(), co.isoformat()):
        return False, (f"Tiếc quá, <b>{room['name']}</b> từ {fmt_d(ci.isoformat())} đến {fmt_d(co.isoformat())} "
                       f"đã <b>kín</b>. Bạn chọn khoảng ngày khác hoặc villa khác nhé!")
    nights = (co - ci).days
    total = room["price_per_night"] * nights
    if promo == "AZUREA10":
        total = int(total * 0.9)
    code = gen_booking_code()
    while db.execute("SELECT 1 FROM bookings WHERE booking_code=?", (code,)).fetchone():
        code = gen_booking_code()
    note = "Đặt qua chatbot AI Marina (OpenRouter)" + (" (mã AZUREA10)" if promo == "AZUREA10" else "")
    db.execute("""INSERT INTO bookings (booking_code,user_id,room_id,full_name,phone,email,check_in,check_out,guests,total_price,status,note)
                  VALUES (?,?,?,?,?,?,?,?,?,?, 'confirmed', ?)""",
               (code, session.get("user_id"), room["id"], full_name, phone, email,
                ci.isoformat(), co.isoformat(), guests, total, note))
    db.commit()
    promo_line = " (đã trừ 10% mã AZUREA10)" if promo == "AZUREA10" else ""
    return True, (f"Đặt thành công! Mã của bạn là <b>{code}</b><br>"
                  f"{room['name']} — {fmt_d(ci.isoformat())} → {fmt_d(co.isoformat())} "
                  f"({nights} đêm, {guests} khách) — Tổng <b>{vnd(total)}</b>{promo_line}<br>"
                  f"Chi tiết và hủy đơn tại <a href='/tra-cuu?code={code}'><b>Tra cứu</b></a>. Hẹn gặp bạn giữa biển xanh!")


def ai_chat_reply(msg, sess, db, nlow):
    """Đường AI chính: grounding lịch thật + gọi OpenRouter + xử lý BOOKING_JSON."""
    # Grounding thêm lịch thật nếu câu hỏi có ngày/phòng để AI khỏi bịa
    extra = ""
    try:
        dates = extract_dates(nlow)
        room_hit = find_room(db, msg) or match_room_type(db, nlow)
        if dates or room_hit or any(k in (nlow or "") for k in ("trong", "con phong", "con trong", "lich", "available")):
            real_reply, _ = avail_answer(db, msg, nlow, None)
            plain = re.sub(r"<[^>]+>", " ", real_reply)
            plain = re.sub(r"\s+", " ", plain).strip()
            if plain:
                extra = ("[DỮ LIỆU LỊCH THẬT TỪ DATABASE — trả lời lịch trống dựa vào đây, đừng bịa khác]: "
                         + plain[:1500])
    except Exception:
        extra = ""
    system = build_marina_system_prompt(db, extra)
    hist = sess.setdefault("history", [])
    messages = [{"role": "system", "content": system}]
    messages += hist[-12:]
    messages.append({"role": "user", "content": msg})
    ai_text = call_openrouter_api(messages)
    if not ai_text:
        raise RuntimeError("OpenRouter trả về rỗng.")
    booking = extract_booking_json(ai_text)
    if booking:
        ok, book_reply = try_ai_booking(booking, db)
        clean = re.sub(r"<!--\s*BOOKING_JSON:.*?-->", "", ai_text, flags=re.DOTALL).strip()
        clean = re.sub(r"```(?:json)?\s*\{[^`]*?\"room_name\"[^`]*?\}\s*```", "", clean, flags=re.DOTALL).strip()
        if ok:
            reply = book_reply  # đặt 1 lệnh thành công → trả mã đơn luôn
            suggestions = ["Tra cứu đặt phòng", "Đặt thêm phòng", "Ưu đãi hiện tại"]
        else:
            # Thiếu/sai/kín → giữ câu hỏi tiếp của AI (nếu có) + lỗi validate từ DB
            reply = (clean + f"<br><br>{book_reply}") if clean else book_reply
            suggestions = ["Đặt phòng giúp tôi", "Lịch trống", "Xem bảng giá"]
        hist.append({"role": "user", "content": msg[:1000]})
        hist.append({"role": "assistant", "content": re.sub(r"<[^>]+>", "", reply)[:1500]})
        sess["history"] = hist[-20:]
        # strip JSON khỏi text hiển thị (trường hợp đặt thành công clean rỗng thì dùng book_reply)
        return reply, suggestions
    hist.append({"role": "user", "content": msg[:1000]})
    hist.append({"role": "assistant", "content": re.sub(r"<[^>]+>", "", ai_text)[:1500]})
    sess["history"] = hist[-20:]
    return ai_text, ["Đặt phòng giúp tôi", "Xem bảng giá", "Lịch trống", "Ưu đãi hiện tại"]


# ---------- Chatbot API ----------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    sid = data.get("session_id") or "web"
    if not msg:
        return jsonify({"reply": "Bạn nhắn gì đó cho Marina nhé!"})
    db = get_db()
    low = msg.lower()
    nlow = norm(msg)
    sess = get_chat_session(sid)
    reply, suggestions = None, []

    # 0. Tra cứu mã AZ-... luôn tra DB thật (không để AI bịa).
    # Bảo mật: chỉ tiết lộ khi khách cho đúng MÃ + SĐT lúc đặt.
    m_code = re.search(r"AZ-[A-Z0-9]{4,6}", msg.upper())
    if m_code:
        # Chỉ ưu tiên tra cứu khi câu hỏi có vẻ là tra cứu HOẶC chỉ chứa mã;
        # tránh cướp các câu đặt phòng 1 lệnh không liên quan (hiếm khi chứa AZ-).
        looks_like_lookup = ("tra cuu" in nlow or "ma dat" in nlow or "kiem tra" in nlow
                             or "don cua" in nlow or "az-" in low or len(msg) < 20)
        if looks_like_lookup and sess.get("flow") not in ("booking", "contact"):
            code = m_code.group(0)
            phone = extract_phone(msg)
            if not phone:
                reply = ("Để bảo mật thông tin, bạn gửi thêm <b>SĐT lúc đặt phòng</b> kèm mã "
                         f"<b>{code}</b> giúp tôi nhé (VD: '{code} SĐT 0901234567').")
                suggestions = ["Tra cứu đặt phòng"]
            else:
                b = db.execute("""
                    SELECT b.*, r.name AS room_name FROM bookings b
                    JOIN rooms r ON r.id=b.room_id WHERE b.booking_code=? AND b.phone=?
                """, (code, phone)).fetchone()
                if b:
                    reply = (f"Tôi tìm thấy đơn <b>{code}</b>:<br>{b['room_name']}<br>"
                             f"{b['check_in']} đến {b['check_out']} ({b['guests']} khách)<br>"
                             f"Tổng: {vnd(b['total_price'])} — Trạng thái: <b>{b['status']}</b><br>"
                             f"Chi tiết tại <a href='/tra-cuu?code={code}'><b>Tra cứu</b></a>.")
                    suggestions = ["Hủy đơn này", "Đặt thêm"]
                else:
                    reply = ("Tôi chưa thấy đơn nào khớp mã + SĐT này. Bạn kiểm tra lại giúp tôi nhé, "
                             "hoặc tra cứu tại <a href='/tra-cuu'><b>đây</b></a>.")
                    suggestions = ["Liên hệ nhân viên"]

    # 1. Có key OpenRouter → AI thật (đặt 1 lệnh + hỏi đáp + từ chối việc ngoài lề)
    if reply is None and openrouter_enabled():
        # AI đảm nhiệm hội thoại tự do; reset luồng rule-based cũ để khỏi kẹt state
        if sess.get("flow") in ("booking", "contact") and is_exit(nlow):
            sess["flow"] = None
            sess["step"] = None
            sess["data"] = {}
        try:
            reply, suggestions = ai_chat_reply(msg, sess, db, nlow)
        except Exception as e:
            print(f"[Marina AI] fallback rule-based vì lỗi: {e}")
            reply, suggestions = None, []

    # 2. Fallback: luồng rule-based cũ (khi chưa có key hoặc AI lỗi)
    if reply is None:
        # 2a. Đang trong luồng đặt hộ / gọi nhân viên → xử lý theo bước
        if sess["flow"] in ("booking", "contact"):
            if is_exit(nlow):
                sess["flow"] = None
                sess["step"] = None
                sess["data"] = {}
                reply, suggestions = ("Đã dừng lại. Cần gì bạn cứ nhắn tôi nhé!",
                                      ["Xem bảng giá", "Lịch trống", "Ưu đãi hiện tại"])
            elif sess["flow"] == "booking":
                reply, suggestions = handle_booking_flow(sess, msg, low, db)
            else:
                reply, suggestions = handle_contact_flow(sess, msg, low, db)
        # 2b. Bắt đầu luồng đặt hộ (không phân biệt dấu)
        elif (re.search(r"dat (ho|giup|gium|dum|them)|dat cho|dat phong (cho|giup)|nho dat|book (ho|giup|dum)", nlow)
              or nlow.strip() in ("dat ho toi", "dat ho", "dat giup toi", "dat gium", "dat them phong")
              or (re.search(r"dat phong nay|dat villa nay", nlow) and not sess.get("last_room_id"))):
            reply, suggestions = start_booking_flow(sess, db)
        # 2c. Bắt đầu luồng gọi nhân viên (không phân biệt dấu)
        elif re.search(r"gap nhan vien|lien he nhan vien|nhan vien (ho tro|tu van|goi)|de lai loi nhan"
                       r"|nhan (cho nhan vien|ngay|tai day)|goi (lai )?cho toi|tu van vien|gap nguoi that", nlow):
            reply, suggestions = start_contact_flow(sess)
        # 2d. "Đặt phòng này" ngay sau khi xem lịch → đặt hộ đúng villa đó, khỏi chọn lại
        elif nlow.strip() in ("dat phong nay", "dat villa nay", "chot villa nay") and sess.get("last_room_id"):
            room = db.execute("SELECT * FROM rooms WHERE id=? AND is_active=1",
                              (sess["last_room_id"],)).fetchone()
            if room is None:
                reply, suggestions = start_booking_flow(sess, db)
            else:
                sess["flow"] = "booking"
                sess["step"] = "checkin"
                sess["data"] = {"room_id": room["id"], "room_name": room["name"],
                                "price": room["price_per_night"], "capacity": room["capacity"]}
                reply = (f"Chốt <b>{room['name']}</b> — {vnd(room['price_per_night'])}/đêm.<br>"
                         f"Bạn <b>nhận phòng ngày nào</b>? (VD: 20/09, hoặc gõ 'mai')")
                suggestions = ["Hủy"]
        # 2e. Hỏi về đặt phòng chung chung → mời chọn đặt hộ hoặc tự đặt
        elif "huong dan" in nlow and "dat" in nlow:
            reply, suggestions = chatbot_reply(msg, sess)
        elif "dat ban" not in nlow and "tra cuu" not in nlow and "dat nuoc" not in nlow and re.search(
                r"dat phong|dat lich|book|booking|cach dat|\bdat\b", nlow):
            reply, suggestions = ("Bạn muốn tôi <b>đặt hộ luôn</b> ngay trong khung chat này, "
                                  "hay xem hướng dẫn để tự đặt trên web?",
                                  ["Đặt hộ tôi", "Xem hướng dẫn tự đặt"])
        # 2f. Hỏi đáp thường
        else:
            reply, suggestions = chatbot_reply(msg, sess)

    try:
        db.execute("INSERT INTO chat_logs (session_id,user_msg,bot_reply) VALUES (?,?,?)",
                   (sid, msg, re.sub(r"<[^>]+>", "", reply)[:2000]))
        db.commit()
    except Exception:
        pass
    return jsonify({"reply": reply, "suggestions": suggestions})

# ---------- Admin ----------
@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    total_revenue = db.execute("SELECT COALESCE(SUM(total_price),0) s FROM bookings WHERE status IN ('confirmed','completed')").fetchone()["s"]
    stats = {
        "revenue": total_revenue,
        "bookings": db.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"],
        "pending": db.execute("SELECT COUNT(*) c FROM bookings WHERE status='pending'").fetchone()["c"],
        "rooms": db.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"],
        "contacts": db.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"],
    }
    bookings = db.execute("""SELECT b.*, r.name AS room_name FROM bookings b
                             JOIN rooms r ON r.id=b.room_id ORDER BY b.created_at DESC LIMIT 50""").fetchall()
    rooms = db.execute("SELECT * FROM rooms ORDER BY id").fetchall()
    contacts = db.execute("SELECT * FROM contacts ORDER BY created_at DESC LIMIT 20").fetchall()
    # doanh thu 7 ngày gần nhất (bảng)
    rev7 = db.execute("""SELECT date(created_at) d, COALESCE(SUM(total_price),0) s FROM bookings
                         WHERE status IN ('confirmed','completed') AND date(created_at) >= date('now','-6 days')
                         GROUP BY d ORDER BY d""").fetchall()
    # ---- Dữ liệu cho biểu đồ ----
    # 1. Doanh thu 14 ngày gần nhất (lấp ngày không có đơn = 0)
    rev_map = {r["d"]: r["s"] for r in db.execute(
        """SELECT date(created_at) d, COALESCE(SUM(total_price),0) s FROM bookings
           WHERE status IN ('confirmed','completed') AND date(created_at) >= date('now','-13 days')
           GROUP BY d""").fetchall()}
    rev_labels, rev_data = [], []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        rev_labels.append(f"{d[8:10]}/{d[5:7]}")
        rev_data.append(rev_map.get(d, 0))
    # 2. Đơn theo trạng thái
    status_rows = db.execute("SELECT status, COUNT(*) c FROM bookings GROUP BY status").fetchall()
    status_labels = [r["status"] for r in status_rows]
    status_data = [r["c"] for r in status_rows]
    # 3. Đơn và doanh thu theo từng villa
    room_rows = db.execute(
        """SELECT r.name AS name, COUNT(b.id) AS c,
                  COALESCE(SUM(CASE WHEN b.status IN ('confirmed','completed') THEN b.total_price ELSE 0 END),0) AS s
           FROM rooms r LEFT JOIN bookings b ON b.room_id = r.id
           GROUP BY r.id ORDER BY c DESC""").fetchall()
    room_labels = [r["name"] for r in room_rows]
    room_counts = [r["c"] for r in room_rows]
    room_revenue = [r["s"] for r in room_rows]
    # 4. Đơn 6 tháng gần nhất (lấp tháng trống = 0)
    month_map = {r["m"]: r["c"] for r in db.execute(
        """SELECT strftime('%Y-%m', created_at) m, COUNT(*) c FROM bookings
           WHERE date(created_at) >= date('now','-6 months') GROUP BY m""").fetchall()}
    month_labels, month_data = [], []
    y, m = date.today().year, date.today().month
    for k in range(5, -1, -1):
        mm = m - k
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        key = f"{yy:04d}-{mm:02d}"
        month_labels.append(f"{mm:02d}/{yy}")
        month_data.append(month_map.get(key, 0))
    chart = dict(rev_labels=rev_labels, rev_data=rev_data,
                 status_labels=status_labels, status_data=status_data,
                 room_labels=room_labels, room_counts=room_counts, room_revenue=room_revenue,
                 month_labels=month_labels, month_data=month_data)
    return render_template("admin.html", stats=stats, bookings=bookings, rooms=rooms,
                           contacts=contacts, rev7=rev7, chart=chart)

@app.route("/admin/booking/<int:bid>/status", methods=["POST"])
@admin_required
def admin_booking_status(bid):
    st = request.form.get("status", "confirmed")
    if st not in ("pending", "confirmed", "cancelled", "completed"):
        st = "confirmed"
    db = get_db()
    db.execute("UPDATE bookings SET status=? WHERE id=?", (st, bid))
    db.commit()
    flash(f"Đã cập nhật đơn #{bid} → {st}.", "success")
    return redirect(url_for("admin"))

@app.route("/admin/room/toggle/<int:rid>", methods=["POST"])
@admin_required
def admin_room_toggle(rid):
    db = get_db()
    r = db.execute("SELECT is_active FROM rooms WHERE id=?", (rid,)).fetchone()
    if r:
        db.execute("UPDATE rooms SET is_active=? WHERE id=?", (0 if r["is_active"] else 1, rid))
        db.commit()
    return redirect(url_for("admin"))

# ---------- Errors ----------
@app.errorhandler(404)
def nf(e):
    return render_template("404.html"), 404

if __name__ == "__main__":
    init_db(seed=True)
    print("=" * 60)
    print("🌊 AZUREA ISLANDS — http://127.0.0.1:5000")
    print("🔑 Admin: admin@azurea.vn / admin123")
    print("=" * 60)
    app.run(debug=True)

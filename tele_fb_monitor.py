# tele_fb_monitor.py
import os, re, sqlite3, time, html, threading
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters
)

# ================== CONFIG ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "fbwatch.db"
CHECK_INTERVAL_SEC = 300  # chu kỳ kiểm tra định kỳ (giây)

# ===== Phân quyền =====
# Thay bằng user_id Telegram của bạn (có thể nhiều id)
OWNER_IDS = {
    6886,  # ví dụ
}

def is_admin(user_id: int) -> bool:
    return user_id in OWNER_IDS

# UA & headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Các cụm chỉ ra "không tồn tại/không khả dụng". Không coi login wall là DIE.
DEAD_PHRASES = [
    # EN
    "this content isn't available right now",
    "this page isn't available",
    "the link may be broken",
    "content isn't available",
    "page not found",
    "the page you requested cannot be displayed right now",
    # VI
    "trang bạn yêu cầu không thể hiển thị",
    "liên kết có thể đã bị hỏng",
    "bạn hiện không thể xem nội dung này",
    "nội dung này hiện không khả dụng",
    "rất tiếc, nội dung này hiện không khả dụng",
]

# Conversation states
ADD_UID, ADD_TYPE, ADD_NOTE, ADD_CUSTOMER = range(1, 5)

# ================== DB & UTILS ==================
def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def db():
    con = sqlite3.connect(DB_PATH)
    # profiles
    con.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
        uid TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        name TEXT,
        last_status TEXT CHECK(last_status IN ('LIVE','DIE'))
    )
    """)
    # subscriptions
    con.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions(
        chat_id INTEGER NOT NULL,
        uid TEXT NOT NULL,
        note TEXT,
        customer TEXT,
        kind TEXT,
        PRIMARY KEY(chat_id, uid),
        FOREIGN KEY(uid) REFERENCES profiles(uid) ON DELETE CASCADE
    )
    """)
    # whitelist người dùng
    con.execute("""
    CREATE TABLE IF NOT EXISTS allowed_users(
        user_id INTEGER PRIMARY KEY,
        added_at TEXT
    )
    """)
    # migrate columns cho subscriptions (idempotent)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(subscriptions)").fetchall()]
        if "note" not in cols:
            con.execute("ALTER TABLE subscriptions ADD COLUMN note TEXT")
        if "customer" not in cols:
            con.execute("ALTER TABLE subscriptions ADD COLUMN customer TEXT")
        if "kind" not in cols:
            con.execute("ALTER TABLE subscriptions ADD COLUMN kind TEXT")
    except Exception:
        pass
    return con

def is_allowed_user(user_id: int) -> bool:
    if is_admin(user_id):  # owner luôn có quyền
        return True
    con = db()
    row = con.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row is not None

def add_allowed_user(user_id: int):
    con = db()
    con.execute("INSERT OR IGNORE INTO allowed_users(user_id, added_at) VALUES(?,?)",
                (user_id, now_iso()))
    con.commit(); con.close()

def remove_allowed_user(user_id: int):
    con = db()
    con.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
    con.commit(); con.close()

def list_allowed_users():
    con = db()
    rows = con.execute("SELECT user_id, added_at FROM allowed_users ORDER BY added_at DESC").fetchall()
    con.close(); return rows

def add_subscription(chat_id:int, uid:str, url:str, note:str|None=None, customer:str|None=None, kind:str|None="profile"):
    con = db()
    con.execute("INSERT OR IGNORE INTO profiles(uid,url) VALUES(?,?)", (uid,url))
    con.execute("""
        INSERT OR IGNORE INTO subscriptions(chat_id,uid,note,customer,kind)
        VALUES(?,?,?,?,?)
    """, (chat_id, uid, note, customer, kind))
    if note is not None:
        con.execute("UPDATE subscriptions SET note=? WHERE chat_id=? AND uid=?", (note, chat_id, uid))
    if customer is not None:
        con.execute("UPDATE subscriptions SET customer=? WHERE chat_id=? AND uid=?", (customer, chat_id, uid))
    if kind is not None:
        con.execute("UPDATE subscriptions SET kind=? WHERE chat_id=? AND uid=?", (kind, chat_id, uid))
    con.commit(); con.close()

def set_profile_status(uid:str, name:str|None, status:str):
    con = db()
    con.execute("UPDATE profiles SET name=COALESCE(?,name), last_status=? WHERE uid=?",
                (name, status, uid))
    con.commit(); con.close()

def list_subs(chat_id:int):
    con = db()
    rows = con.execute("""
        SELECT p.uid, COALESCE(p.name,''), COALESCE(p.last_status,''), p.url,
               COALESCE(s.note,''), COALESCE(s.customer,''), COALESCE(s.kind,'profile')
        FROM subscriptions s JOIN profiles p ON s.uid=p.uid
        WHERE s.chat_id=? ORDER BY p.uid
    """,(chat_id,)).fetchall()
    con.close(); return rows

def remove_subscription(chat_id:int, uid:str):
    con = db()
    con.execute("DELETE FROM subscriptions WHERE chat_id=? AND uid=?", (chat_id,uid))
    con.commit(); con.close()

def get_all_uids():
    con = db()
    rows = con.execute("SELECT uid, url, COALESCE(last_status,'') FROM profiles").fetchall()
    con.close(); return rows

def subscribers_of(uid:str):
    con = db()
    rows = [r[0] for r in con.execute("SELECT chat_id FROM subscriptions WHERE uid=?", (uid,)).fetchall()]
    con.close(); return rows

UID_RE = re.compile(r'^\d{5,}$')

def normalize_target(s: str):
    """
    Nhận UID hoặc URL Facebook cho profile/page/group. Trả về (uid_or_slug, mbasic_url).
    """
    s = s.strip()
    if s.startswith("http"):
        u = urlparse(s)
        if "facebook.com" not in u.netloc:
            raise ValueError("Đây không phải link Facebook hợp lệ.")
        qs = parse_qs(u.query)
        if "id" in qs and qs["id"][0].isdigit():
            uid = qs["id"][0]
            url = f"https://mbasic.facebook.com/profile.php?id={uid}"
            return uid, url
        slug = u.path.strip("/").split("/")[0]
        if not slug:
            raise ValueError("Không lấy được UID/username từ link.")
        uid = slug
        url = f"https://mbasic.facebook.com/{slug}"
        return uid, url
    else:
        uid = s
        if not re.match(r'^[A-Za-z0-9\.]+$', uid):
            raise ValueError("UID/username không hợp lệ.")
        if UID_RE.match(uid):
            url = f"https://mbasic.facebook.com/profile.php?id={uid}"
        else:
            url = f"https://mbasic.facebook.com/{uid}"
        return uid, url

# --------------- fetch helpers ---------------
def _try_fetch(url: str, headers: dict, timeout: int) -> tuple[str|None, str|None, str]:
    """
    Trả về (status, name, final_url) hoặc (None, None, final_url) khi lỗi mạng.
    Strict DIE: nếu chứa DEAD_PHRASES, hoặc HTTP 404/410.
    Nếu không xác định rõ DIE -> coi là LIVE (login wall/private).
    """
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        final = r.url.lower()

        if r.status_code in (404, 410):
            return "DIE", None, final

        text_lower = r.text.lower()
        if any(phrase in text_lower for phrase in DEAD_PHRASES):
            return "DIE", None, final

        soup = BeautifulSoup(r.text, "html.parser")
        name = None
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            name = og["content"].strip()
        if not name and soup.title and soup.title.text:
            t = soup.title.text.strip()
            low = t.lower()
            if all(k not in low for k in ["facebook", "log in", "đăng nhập"]):
                name = t

        return "LIVE", name, final

    except Exception:
        return None, None, url

def fetch_status_and_name(url: str, timeout: int = 20):
    """
    Thử nhiều biến thể: mbasic -> m.facebook -> www.facebook với UA crawler.
    Kết luận DIE chỉ khi thấy tín hiệu DIE rõ ràng (DEAD_PHRASES / 404/410).
    """
    # 1) mbasic
    status, name, _ = _try_fetch(url, HEADERS, timeout)
    if status is not None:
        return status, name

    # 2) m.facebook hoặc ngược lại
    if "mbasic.facebook" in url:
        alt = url.replace("mbasic.facebook", "m.facebook")
    else:
        alt = url.replace("m.facebook", "mbasic.facebook")
    status, name, _ = _try_fetch(alt, HEADERS, timeout)
    if status is not None:
        return status, name

    # 3) www.facebook + UA crawler
    crawler_headers = {
        **HEADERS,
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    }
    alt2 = alt.replace("m.facebook", "www.facebook").replace("mbasic.facebook", "www.facebook")
    status, name, _ = _try_fetch(alt2, crawler_headers, timeout)
    if status is not None:
        return status, name

    return None, None  # lỗi mạng/cấm IP…

# ================== UI / MSG ==================
HELP = (
    "✨ *FB Watch Bot*\n"
    "/them – Thêm từng bước (UID → Loại → Ghi chú → Tên KH)\n"
    "/them <uid/url> | <ghi chú> | <tên KH> | <profile|group> – Thêm nhanh 1 dòng\n"
    "/danhsach – Xem UID đang theo dõi (kiểm tra realtime)\n"
    "/xoa <uid> – Bỏ theo dõi\n"
    "/trogiup – Hướng dẫn\n"
    "\n"
    "👮 Quyền truy cập: chỉ user được admin cấp quyền mới dùng được.\n"
    "Admin: /myid, /allow <user_id>, /deny <user_id>, /allowed"
)

def line_box():
    return "____________________________"

def card_added(uid, note, customer, kind, added_when, status, url):
    status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
    note_display = note or "—"
    customer_display = customer or "—"
    kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
    return (
        "🆕 *Đã thêm UID mới!*\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📂 *Loại*: {kind_display}\n"
        f"📝 *Ghi chú*: {html.escape(note_display)}\n"
        f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
        f"📌 *Ngày thêm*: {added_when}\n"
        f"📟 *Trạng thái hiện tại*: {status_icon}\n"
        f"{line_box()}"
    )

def card_alert(uid, note, customer, url, old, new):
    arrow = "🔴 DIE → 🟢 LIVE" if new=="LIVE" else "🟢 LIVE → 🔴 DIE"
    note_display = note or "—"
    customer_display = customer or "—"
    return (
        f"{'🚀 *UID đã LIVE trở lại!*' if new=='LIVE' else '☠️ *UID đã DIE!*'}\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📝 *Ghi chú*: {html.escape(note_display)}\n"
        f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
        f"📟 *Trạng thái*: {arrow}\n"
        f"⏰ *Thời gian*: {now_iso()}\n"
        f"{line_box()}"
    )

# ================== GUARD (quyền) ==================
async def guard(update: Update) -> bool:
    user = update.effective_user
    if user and is_allowed_user(user.id):
        return True
    if user:
        await update.effective_message.reply_text(
            "❌ Bạn chưa được cấp quyền dùng bot này.\n"
            "Liên hệ admin để được mở quyền."
        )
    return False

# ================== COMMANDS ==================
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"👤 user_id của bạn: `{user.id}`",
            parse_mode=ParseMode.MARKDOWN
        )

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("❌ Chỉ admin mới dùng được lệnh này.")
        return
    if not context.args:
        await update.effective_message.reply_text("Dùng: /allow <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id phải là số.")
        return
    add_allowed_user(uid)
    await update.effective_message.reply_text(f"✅ Đã cấp quyền cho user_id: {uid}")

async def deny_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("❌ Chỉ admin mới dùng được lệnh này.")
        return
    if not context.args:
        await update.effective_message.reply_text("Dùng: /deny <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id phải là số.")
        return
    remove_allowed_user(uid)
    await update.effective_message.reply_text(f"🗑️ Đã thu hồi quyền của user_id: {uid}")

async def allowed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("❌ Chỉ admin mới dùng được lệnh này.")
        return
    rows = list_allowed_users()
    if not rows:
        await update.effective_message.reply_text("Danh sách rỗng.")
        return
    text = "👥 *Danh sách user được cấp quyền:*\n" + "\n".join(
        [f"- `{uid}` (từ {ts})" for uid, ts in rows]
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text("Chào bạn!\n"+HELP, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

def parse_inline_add(text: str):
    """
    <uid/url> | <note> | <customer> | <profile|group>
    """
    parts = [p.strip() for p in text.split("|")]
    target = parts[0]
    note = parts[1] if len(parts) > 1 and parts[1] else None
    customer = parts[2] if len(parts) > 2 and parts[2] else None
    kind = parts[3].lower() if len(parts) > 3 and parts[3] else "profile"
    if kind not in ("profile", "group"):
        kind = "profile"
    return target, note, customer, kind

async def them_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    # nếu gõ kèm tham số -> thêm nhanh
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            status, name = fetch_status_and_name(url)
            if status is None:
                status = "DIE"
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            set_profile_status(uid, name, status)

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
                [
                    InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                    InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
                ]
            ])
            await update.effective_message.reply_text(
                card_added(uid, note, customer, kind, now_iso(), status, url),
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    # Wizard
    await update.effective_message.reply_text(
        "➕ *Vui lòng nhập UID hoặc URL bạn muốn theo dõi:*",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["add"] = {}
    return ADD_UID

async def them_got_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    try:
        uid, url = normalize_target(text)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ {e}\nVui lòng nhập lại UID/URL.")
        return ADD_UID

    context.user_data["add"]["uid"] = uid
    context.user_data["add"]["url"] = url

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 Profile/Page", callback_data="type:profile"),
        InlineKeyboardButton("👥 Group", callback_data="type:group")
    ]])
    await update.effective_message.reply_text(
        f"📌 *Chọn loại UID cho* `{uid}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    return ADD_TYPE

async def them_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard(update): 
        return ConversationHandler.END
    data = query.data or ""
    kind = "profile"
    if data == "type:group":
        kind = "group"
        await query.message.reply_text("✅ *Đã chọn loại:* Group", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.message.reply_text("✅ *Đã chọn loại:* Profile/Page", parse_mode=ParseMode.MARKDOWN)

    context.user_data["add"]["kind"] = kind
    uid = context.user_data["add"].get("uid")
    await query.message.reply_text(
        f"✍️ *Nhập ghi chú cho UID* `{uid}`\n_Ví dụ: Dame 282, unlock 282_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_NOTE

async def them_got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return ConversationHandler.END
    context.user_data["add"]["note"] = (update.effective_message.text or "").strip()
    uid = context.user_data["add"].get("uid")
    await update.effective_message.reply_text(
        f"📝 *Nhập tên cho UID* `{uid}`\n_Ví dụ: Tran Tang_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CUSTOMER

async def them_got_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return ConversationHandler.END
    context.user_data["add"]["customer"] = (update.effective_message.text or "").strip()

    info = context.user_data.get("add", {})
    uid, url = info.get("uid"), info.get("url")
    note, customer = info.get("note"), info.get("customer")
    kind = info.get("kind", "profile")

    status, name = fetch_status_and_name(url)
    if status is None:
        status = "DIE"

    add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
    set_profile_status(uid, name, status)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
        [
            InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
            InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
        ]
    ])
    await update.effective_message.reply_text(
        card_added(uid, note, customer, kind, now_iso(), status, url),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
    )
    context.user_data.pop("add", None)
    return ConversationHandler.END

async def them_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add", None)
    await update.effective_message.reply_text("Đã hủy.")
    return ConversationHandler.END

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    rows = list_subs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Chưa có UID nào. Dùng /them để bắt đầu.")
        return

    for uid, _, prev_status, url, note, customer, kind in rows:
        status, name = fetch_status_and_name(url)
        if status is None:
            status = prev_status if prev_status else "DIE"
        if name:
            set_profile_status(uid, name, status)
        else:
            set_profile_status(uid, None, status)

        status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
        note_display = note or "—"
        customer_display = customer or "—"
        kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"

        text = (
            f"{line_box()}\n"
            f"🪪 *UID*: [{uid}]({url})\n"
            f"📂 *Loại*: {kind_display}\n"
            f"📝 *Ghi chú*: {html.escape(note_display)}\n"
            f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
            f"📟 *Trạng thái*: {status_icon}\n"
            f"{line_box()}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
            [
                InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
            ]
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
        )
        time.sleep(0.4)

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Dùng: /xoa <uid>")
        return
    uid = context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Đã bỏ theo dõi {uid}")

# =============== BUTTONS & POLLER ===============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        return
    data = (query.data or "")
    chat_id = query.message.chat.id if query.message else None

    if data.startswith("stop:"):
        uid = data.split(":",1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛑 Đã dừng theo dõi UID {uid}")

    elif data.startswith("keep:"):
        # chỉ đóng inline buttons để gọn
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Vẫn tiếp tục theo dõi.")

def poll_once(application: Application):
    # Dò toàn DB, phát cảnh báo khi đổi trạng thái
    for uid, url, prev in get_all_uids():
        status, name = fetch_status_and_name(url)
        if status is None:
            continue  # mạng lỗi -> bỏ qua
        if prev != status:
            set_profile_status(uid, name, status)
            for chat_id in subscribers_of(uid):
                con = db()
                row = con.execute("""
                    SELECT COALESCE(note,''), COALESCE(customer,'')
                    FROM subscriptions WHERE chat_id=? AND uid=?
                """, (chat_id, uid)).fetchone()
                con.close()
                note, customer = (row or ("",""))
                text = card_alert(uid, note, customer, url, prev if prev else "Unknown", status)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
                    [
                        InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                        InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
                    ]
                ])
                application.create_task(
                    application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                        reply_markup=keyboard
                    )
                )
        else:
            if name:
                set_profile_status(uid, name, status)
        time.sleep(0.6)

# =============== Health-check HTTP ===============
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404); self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[health] listening on :{port}")

# ================== MAIN ==================
def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN không hợp lệ hoặc không nạp được từ .env")

    start_health_server()  # cho Render

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation /them
    conv_them = ConversationHandler(
        entry_points=[CommandHandler("them", them_entry)],
        states={
            ADD_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_uid)],
            ADD_TYPE: [CallbackQueryHandler(them_pick_type, pattern=r"^type:")],
            ADD_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_note)],
            ADD_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_customer)],
        },
        fallbacks=[CommandHandler("cancel", them_cancel)],
        allow_reentry=True,
    )

    # public
    application.add_handler(CommandHandler("myid", myid))
    # admin
    application.add_handler(CommandHandler("allow", allow_cmd))
    application.add_handler(CommandHandler("deny", deny_cmd))
    application.add_handler(CommandHandler("allowed", allowed_cmd))

    application.add_handler(CommandHandler(["start","trogiup","menu"], help_cmd))
    application.add_handler(conv_them)
    application.add_handler(CommandHandler(["danhsach"], list_cmd))
    application.add_handler(CommandHandler(["xoa"], remove_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Scheduler background poll
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(lambda: poll_once(application), "interval",
                      seconds=CHECK_INTERVAL_SEC, max_instances=1)
    scheduler.start()

    print("Bot is running...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    db()  # đảm bảo bảng sẵn sàng
    main()

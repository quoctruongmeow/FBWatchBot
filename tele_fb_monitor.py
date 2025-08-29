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

# Hardcode dự phòng (nếu không set OWNER_IDS env). Điền ID của bạn vào đây nếu muốn.
OWNER_IDS_DEFAULT = { }  # ví dụ: {123456789}

# Chủ động đọc từ biến môi trường: "12345,67890"
_env_owners = {
    int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}
OWNER_IDS = _env_owners or OWNER_IDS_DEFAULT

DB_PATH = "fbwatch.db"
CHECK_INTERVAL_SEC = 300  # chu kỳ kiểm tra định kỳ (giây)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Những cụm thể hiện "không tồn tại/không khả dụng"
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

# ================== AUTH HELPERS ==================
def is_allowed(user_id: int) -> bool:
    """Chỉ cho phép user_id trong OWNER_IDS."""
    return user_id in OWNER_IDS

async def guard(update: Update) -> bool:
    """Gửi thông báo thiếu quyền nếu user không thuộc OWNER_IDS."""
    user = update.effective_user
    if user and is_allowed(user.id):
        return True
    msg = (
        "⛔ Bạn chưa được cấp quyền sử dụng bot này.\n"
        "Gõ /myid để lấy User ID và gửi cho admin để được cấp quyền."
    )
    if update.effective_message:
        await update.effective_message.reply_text(msg)
    return False

# ================== UTILS/DB ==================
def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def db():
    conn = sqlite3.connect(DB_PATH)
    # profiles
    conn.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
        uid TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        name TEXT,
        last_status TEXT CHECK(last_status IN ('LIVE','DIE'))
    )
    """)
    # subscriptions
    conn.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions(
        chat_id INTEGER NOT NULL,
        uid TEXT NOT NULL,
        note TEXT,
        customer TEXT,
        kind TEXT,                 -- 'profile' | 'group'
        PRIMARY KEY(chat_id, uid),
        FOREIGN KEY(uid) REFERENCES profiles(uid) ON DELETE CASCADE
    )
    """)
    # migrate
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
        if "note" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN note TEXT")
        if "customer" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN customer TEXT")
        if "kind" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN kind TEXT")
    except Exception:
        pass
    return conn

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
    Nhận UID hoặc URL Facebook cho profile, page, group.
    Trả về (uid_or_slug, mbasic_url).
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

# -------- fetch helpers --------
def _try_fetch(url: str, headers: dict, timeout: int) -> tuple[str|None, str|None, str]:
    """Trả về (status, name, final_url) hoặc (None, None, final_url) khi lỗi mạng."""
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        final = r.url.lower()

        # Lỗi HTTP 404/410 ⇒ chết rõ ràng
        if r.status_code in (404, 410):
            return "DIE", None, final

        text_lower = r.text.lower()
        if any(phrase in text_lower for phrase in DEAD_PHRASES):
            return "DIE", None, final

        # login wall/private: vẫn coi là LIVE (tồn tại)
        soup = BeautifulSoup(r.text, "html.parser")
        name = None
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            name = og["content"].strip()
        if not name and soup.title and soup.title.text:
            t = soup.title.text.strip()
            low = t.lower()
            if all(k not in low for k in ["facebook", "log in"]):
                name = t
        return "LIVE", name, final
    except Exception:
        return None, None, url

def fetch_status_and_name(url: str, timeout: int = 20):
    """
    Nới điều kiện: login wall vẫn coi là LIVE.
    Thử lần lượt: mbasic → m.facebook → www.facebook với UA khác nhau.
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

    # lỗi mạng/cấm IP…: không kết luận được
    return None, None

# ================== UI / TEMPLATES ==================
HELP = (
"✨ *FB Watch Bot*\n"
"/them – Thêm từng bước (UID → Loại → Ghi chú → Tên KH)\n"
"/them <uid/url> | <ghi chú> | <tên KH> | <profile|group> – Thêm nhanh 1 dòng\n"
"/danhsach – Xem UID đang theo dõi (kiểm tra realtime)\n"
"/xoa <uid> – Bỏ theo dõi\n"
"/myid – Xem user_id của bạn\n"
"/trogiup – Hướng dẫn\n"
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

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Chào bạn!\n"+HELP, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await update.effective_message.reply_text("Không lấy được User ID.")
        return
    await update.effective_message.reply_text(
        f"🆔 User ID của bạn là: `{user_id}`\n"
        f"👉 Gửi ID này cho admin để được cấp quyền.",
        parse_mode="Markdown"
    )

# ---- /them: hỗ trợ nhanh + wizard ----
def parse_inline_add(text: str):
    """
    <uid/url> | <note> | <customer> | <profile|group>
    (có thể thiếu 2-3 phần cuối; kind mặc định profile)
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
    if not await guard(update):
        return ConversationHandler.END

    # nếu gõ kèm tham số -> thêm nhanh
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            # kiểm tra trạng thái thật
            status, name = fetch_status_and_name(url)
            if status is None:
                status = "DIE"  # không kết luận được → coi như DIE tạm thời
            # lưu
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

    # wizard bước 1: hỏi UID/URL
    await update.effective_message.reply_text("➕ *Vui lòng nhập UID hoặc URL bạn muốn theo dõi:*", parse_mode=ParseMode.MARKDOWN)
    context.user_data["add"] = {}
    return ADD_UID

async def them_got_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END

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
    if not is_allowed(update.effective_user.id):
        await update.callback_query.answer("Bạn chưa được cấp quyền", show_alert=True)
        return

    query = update.callback_query
    await query.answer()
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
    if not await guard(update):
        return ConversationHandler.END

    context.user_data["add"]["note"] = (update.effective_message.text or "").strip()
    uid = context.user_data["add"].get("uid")
    await update.effective_message.reply_text(
        f"📝 *Nhập tên cho UID* `{uid}`\n_Ví dụ: Tran Tang_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CUSTOMER

async def them_got_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END

    context.user_data["add"]["customer"] = (update.effective_message.text or "").strip()

    info = context.user_data.get("add", {})
    uid, url = info.get("uid"), info.get("url")
    note, customer = info.get("note"), info.get("customer")
    kind = info.get("kind", "profile")

    # Kiểm tra trạng thái thật trước khi lưu
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

# ================== LIST / REMOVE ==================
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    rows = list_subs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Chưa có UID nào. Dùng /them để bắt đầu.")
        return

    for uid, _, prev_status, url, note, customer, kind in rows:
        # Kiểm tra realtime
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
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Dùng: /xoa <uid>")
        return
    uid = context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Đã bỏ theo dõi {uid}")

# ================== BUTTONS & POLLER ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await query.answer("Bạn chưa được cấp quyền", show_alert=True)
        return

    await query.answer()
    data = (query.data or "")
    chat_id = query.message.chat.id if query.message else None

    if data.startswith("stop:"):
        uid = data.split(":",1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛑 Đã dừng theo dõi UID {uid}")

    elif data.startswith("keep:"):
        uid = data.split(":",1)[1]
        await query.answer("Tiếp tục theo dõi UID này", show_alert=False)

def poll_once(application: Application):
    for uid, url, prev in get_all_uids():
        status, name = fetch_status_and_name(url)
        if status is None:
            continue
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

# ================== HEALTH CHECK HTTP ==================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

def serve_health():
    try:
        server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
        server.serve_forever()
    except Exception:
        pass

# ================== MAIN ==================
def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN không hợp lệ hoặc không nạp được từ .env")

    # HTTP healthcheck server
    threading.Thread(target=serve_health, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("trogiup", help_cmd))
    application.add_handler(CommandHandler("myid", myid))        # 👈 thêm /myid
    application.add_handler(conv_them)
    application.add_handler(CommandHandler("danhsach", list_cmd))
    application.add_handler(CommandHandler("xoa", remove_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(lambda: poll_once(application), "interval",
                      seconds=CHECK_INTERVAL_SEC, max_instances=1)
    scheduler.start()

    print("Bot is running...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    db()
    main()

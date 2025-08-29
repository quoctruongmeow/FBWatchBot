# tele_fb_monitor.py
import os, re, sqlite3, time, html, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
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

# =============== CONFIG ===============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "fbwatch.db"
CHECK_INTERVAL_SEC = 300  # chu kỳ kiểm tra (giây)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

DEAD_PHRASES = [
    "content isn't available right now",
    "this page isn't available",
    "the link may be broken",
    "trang bạn yêu cầu không thể hiển thị",
    "liên kết có thể đã bị hỏng",
    "bạn hiện không thể xem nội dung này",
]

# Conversation states
ADD_UID, ADD_TYPE, ADD_NOTE, ADD_CUSTOMER = range(1, 5)

# =============== UTILS/DB ===============
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
    # migrate DB cũ
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

def list_subs_rows(chat_id:int):
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

def fetch_status_and_name(url:str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        text = r.text.lower()
        for phrase in DEAD_PHRASES:
            if phrase in text:
                return "DIE", None
        soup = BeautifulSoup(r.text, "html.parser")
        og_title = soup.find("meta", attrs={"property": "og:title"})
        name = og_title["content"].strip() if og_title and og_title.get("content") else None
        if name and name.lower() != "facebook":
            return "LIVE", name
        if soup.title and soup.title.text.strip():
            t = soup.title.text.strip()
            if t.lower() not in ("facebook", "log in to facebook"):
                return "LIVE", t
        if any(k in text for k in ["add friend", "message", "theo dõi", "bạn bè", "tham gia nhóm", "nhóm"]):
            return "LIVE", name
        return "DIE", None
    except Exception:
        return None, None

# =============== UI / TEMPLATES ===============
HELP = (
"✨ *FB Watch Bot*\n"
"/them – Thêm từng bước (UID → Loại → Ghi chú → Tên KH)\n"
"/them <uid/url> | <ghi chú> | <tên KH> | <profile|group> – Thêm nhanh 1 dòng\n"
"/themhng <danh sách> – Thêm hàng loạt (mỗi dòng 1 URL/UID, cho phép kèm | ghi chú | tên KH | loại)\n"
"/danhsach – Xem UID đang theo dõi (tự kiểm tra LIVE/DIE lúc hiển thị)\n"
"/xoa <uid> – Bỏ theo dõi\n"
"/trogiup – Hướng dẫn\n"
)

def line_box():
    return "____________________________"

def card_added(uid, name, note, customer, kind, added_when, status, url):
    status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
    note_display = note or "—"
    customer_display = customer or "—"
    kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
    return (
        "🆕 *Đã thêm UID mới!*\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📂 *Loại*: {kind_display}\n"
        # Bỏ dòng Tên theo yêu cầu
        f"📝 *Ghi chú*: {html.escape(note_display)}\n"
        f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
        f"📌 *Ngày thêm*: {added_when}\n"
        f"📟 *Trạng thái hiện tại*: {status_icon}\n"
        f"{line_box()}"
    )

def card_alert(uid, name, note, customer, url, old, new):
    arrow = "🔴 DIE → 🟢 LIVE" if new=="LIVE" else "🟢 LIVE → 🔴 DIE"
    title = "🚀 *UID đã LIVE trở lại!*" if new=="LIVE" else "☠️ *UID đã DIE!*"
    name_display = name or "—"
    note_display = note or "—"
    customer_display = customer or "—"
    return (
        f"{title}\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"👤 *Tên*: {html.escape(name_display)}\n"
        f"📝 *Ghi chú*: {html.escape(note_display)}\n"
        f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
        f"📟 *Trạng thái*: {arrow}\n"
        f"⏰ *Thời gian*: {now_iso()}\n"
        f"{line_box()}"
    )

# =============== COMMANDS ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Chào bạn!\n"+HELP, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

# ---- /them: hỗ trợ nhanh + wizard ----
def parse_inline_add(text: str):
    """
    <uid/url> | <note> | <customer> | <profile|group>
    (có thể thiếu các phần cuối; kind mặc định profile)
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
    # nếu gõ kèm tham số -> thêm nhanh
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                    InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
                ]
            ])
            # kiểm tra nhanh trạng thái hiện tại để hiển thị thẻ thêm đẹp hơn
            st, _nm = fetch_status_and_name(url)
            st = st or "DIE"
            await update.effective_message.reply_text(
                card_added(uid, None, note, customer, kind, now_iso(), st, url),
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    # wizard bước 1: hỏi UID/URL
    await update.effective_message.reply_text("➕ *Vui lòng nhập UID bạn muốn theo dõi:*", parse_mode=ParseMode.MARKDOWN)
    context.user_data["add"] = {}
    return ADD_UID

async def them_got_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    try:
        uid, url = normalize_target(text)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ {e}\nVui lòng nhập lại UID/URL.")
        return ADD_UID

    context.user_data["add"]["uid"] = uid
    context.user_data["add"]["url"] = url

    # wizard bước 2: chọn loại (đã bật Group)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Profile/Page", callback_data="type:profile"),
            InlineKeyboardButton("👥 Group", callback_data="type:group")
        ]
    ])
    await update.effective_message.reply_text(
        f"📌 *Chọn loại UID cho* `{uid}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    return ADD_TYPE

async def them_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "type:profile":
        context.user_data["add"]["kind"] = "profile"
        await query.message.reply_text("✅ *Đã chọn loại:* Profile/Page", parse_mode=ParseMode.MARKDOWN)
    elif data == "type:group":
        context.user_data["add"]["kind"] = "group"
        await query.message.reply_text("✅ *Đã chọn loại:* Group", parse_mode=ParseMode.MARKDOWN)
    else:
        context.user_data["add"]["kind"] = "profile"

    uid = context.user_data["add"].get("uid")
    await query.message.reply_text(
        f"✍️ *Nhập ghi chú cho UID* `{uid}`\n_Ví dụ: Dame 282, unlock 282_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_NOTE

async def them_got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["note"] = (update.effective_message.text or "").strip()
    uid = context.user_data["add"].get("uid")
    await update.effective_message.reply_text(
        f"📝 *Nhập tên cho UID* `{uid}`\n_Ví dụ: Tran Tang_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CUSTOMER

async def them_got_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["customer"] = (update.effective_message.text or "").strip()

    # Lưu và trả thẻ
    info = context.user_data.get("add", {})
    uid, url = info.get("uid"), info.get("url")
    note, customer = info.get("note"), info.get("customer")
    kind = info.get("kind", "profile")

    add_subscription(update.effective_chat.id, uid, url, note, customer, kind)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
            InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
        ]
    ])
    st, _nm = fetch_status_and_name(url)
    st = st or "DIE"
    await update.effective_message.reply_text(
        card_added(uid, None, note, customer, kind, now_iso(), st, url),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
    )
    context.user_data.pop("add", None)
    return ConversationHandler.END

async def them_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add", None)
    await update.effective_message.reply_text("Đã hủy.")
    return ConversationHandler.END

# ---- /themhng: thêm hàng loạt ----
async def add_bulk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Dùng: /themhng <danh sách>\n"
            "• Mỗi dòng một mục; có thể dạng:\n"
            "`<url|uid>` hoặc `<url|uid> | <ghi chú> | <tên KH> | <profile|group>`",
            parse_mode=ParseMode.MARKDOWN
        ); return
    text = " ".join(context.args)
    n_ok, n_fail = 0, 0
    for raw_line in re.split(r'[\n]+', text):
        line = raw_line.strip()
        if not line:
            continue
        try:
            target, note, customer, kind = parse_inline_add(line)
            uid, url = normalize_target(target)
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            n_ok += 1
        except Exception:
            n_fail += 1
    await update.effective_message.reply_text(f"✅ Thêm thành công: {n_ok} | ❌ Lỗi: {n_fail}")

# =============== LIST / REMOVE ===============
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_subs_rows(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Chưa có UID nào. Dùng /them để bắt đầu.")
        return
    for uid, name, cached_status, url, note, customer, kind in rows:
        # Kiểm tra lại trạng thái ngay lúc hiển thị và cập nhật DB
        fresh_status, fresh_name = fetch_status_and_name(url)
        if fresh_status:
            set_profile_status(uid, fresh_name, fresh_status)
            status_for_view = fresh_status
            name_for_view = fresh_name or name
        else:
            status_for_view = cached_status or "DIE"
            name_for_view = name

        status_icon = "🟢 LIVE" if status_for_view=="LIVE" else ("🔴 DIE" if status_for_view=="DIE" else "⚪ Unknown")
        note_display = note or "—"
        customer_display = customer or "—"
        kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
        text = (
            f"{line_box()}\n"
            f"🪪 *UID*: [{uid}]({url})\n"
            f"📂 *Loại*: {kind_display}\n"
            f"👤 *Tên*: {html.escape(name_for_view or '—')}\n"
            f"📝 *Ghi chú*: {html.escape(note_display)}\n"
            f"🙍 *Khách hàng*: {html.escape(customer_display)}\n"
            f"📟 *Trạng thái*: {status_icon}\n"
            f"{line_box()}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
            [InlineKeyboardButton("🗑️ Xóa UID này", callback_data=f"del:{uid}")]
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
        )

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Dùng: /xoa <uid>"); return
    uid = context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Đã bỏ theo dõi {uid}")

# =============== BUTTONS & POLLER ===============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "")
    chat_id = query.message.chat.id if query.message else None

    if data.startswith("stop:"):
        uid = data.split(":",1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛑 Đã dừng theo dõi UID {uid}")

    elif data.startswith("del:"):
        uid = data.split(":",1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🗑️ Đã xóa UID {uid} khỏi danh sách")

    elif data.startswith("keep:"):
        # chỉ ẩn nút (tiếp tục theo dõi)
        await query.edit_message_reply_markup(reply_markup=None)

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
                text = card_alert(uid, name, note, customer, url, prev if prev else "Unknown", status)
                keyboard = InlineKeyboardMarkup([
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
        time.sleep(0.7)

# =============== HEALTH CHECK HTTP SERVER ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path.startswith('/health'):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health server running on port {port}")

# =============== MAIN ===============
def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN không hợp lệ hoặc không nạp được từ .env")

    start_health_server()

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

    application.add_handler(CommandHandler(["start","trogiup","menu"], help_cmd))
    application.add_handler(conv_them)
    application.add_handler(CommandHandler(["danhsach"], list_cmd))
    application.add_handler(CommandHandler(["xoa"], remove_cmd))
    application.add_handler(CommandHandler(["themhng","themhangloat"], add_bulk_cmd))
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

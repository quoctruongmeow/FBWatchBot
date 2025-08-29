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

# =============== CONFIG ===============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "fbwatch.db"

# kiểm tra mỗi 5 phút
CHECK_INTERVAL_SEC = 300

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

# metrics (cho /metrics health-check)
LAST_POLL_AT = "-"
def _set_last_poll():
    global LAST_POLL_AT
    LAST_POLL_AT = now_iso()

# =============== UTILS/DB ===============
def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
        uid TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        name TEXT,
        last_status TEXT CHECK(last_status IN ('LIVE','DIE'))
    )
    """)
    conn.execute("""
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

# =============== UID helpers ===============
UID_RE = re.compile(r'^\d{5,}$')

def _extract_numeric_id_from_html(html_text: str) -> str|None:
    """
    Cố gắng trích entity_id/profile_id/page_id/... từ HTML.
    """
    m = re.search(r'entity_id["\']\s*:\s*["\'](\d+)["\']', html_text)
    if m: return m.group(1)
    m = re.search(r'"profile_id"\s*:\s*"(\d+)"', html_text)
    if m: return m.group(1)
    m = re.search(r'"pageID"\s*:\s*"(\d+)"', html_text)
    if m: return m.group(1)
    m = re.search(r'"group_id"\s*:\s*"(\d+)"', html_text)
    if m: return m.group(1)
    m = re.search(r'"id"\s*:\s*"(\d{5,})"', html_text)
    if m: return m.group(1)
    return None

def normalize_target(raw: str):
    """
    Nhận UID/username/URL Facebook cho profile/page/group.
    - Nếu URL có ?id=... -> dùng luôn.
    - Nếu slug/username -> fetch 1 lần để cố gắng lấy numeric id (entity_id).
    Trả về (uid_or_slug (có thể là số), mbasic_url).
    """
    s = raw.strip()
    if not s:
        raise ValueError("Chuỗi rỗng.")

    # URL
    if s.startswith("http"):
        u = urlparse(s)
        if "facebook.com" not in u.netloc:
            raise ValueError("Đây không phải link Facebook hợp lệ.")
        qs = parse_qs(u.query)
        if "id" in qs and qs["id"][0].isdigit():
            uid = qs["id"][0]
            url = f"https://mbasic.facebook.com/profile.php?id={uid}"
            return uid, url
        # chưa có id -> lấy slug, rồi cố gắng chuyển numeric
        slug = u.path.strip("/").split("/")[0]
        if not slug:
            raise ValueError("Không lấy được UID/username từ link.")
        # thử fetch trang thường (m.basic cũng ok)
        try:
            r = requests.get(f"https://mbasic.facebook.com/{slug}", headers=HEADERS, timeout=15)
            nuid = _extract_numeric_id_from_html(r.text)
            if nuid:
                return nuid, f"https://mbasic.facebook.com/profile.php?id={nuid}"
        except Exception:
            pass
        # fallback: dùng slug
        return slug, f"https://mbasic.facebook.com/{slug}"

    # Không phải URL -> UID/username
    uid = s
    if not re.match(r'^[A-Za-z0-9\.\-_]+$', uid):
        raise ValueError("UID/username không hợp lệ.")
    if UID_RE.match(uid):
        return uid, f"https://mbasic.facebook.com/profile.php?id={uid}"
    # username -> thử resolve
    try:
        r = requests.get(f"https://mbasic.facebook.com/{uid}", headers=HEADERS, timeout=15)
        nuid = _extract_numeric_id_from_html(r.text)
        if nuid:
            return nuid, f"https://mbasic.facebook.com/profile.php?id={nuid}"
    except Exception:
        pass
    return uid, f"https://mbasic.facebook.com/{uid}"

def fetch_status_and_name(url:str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        text_low = r.text.lower()
        for phrase in DEAD_PHRASES:
            if phrase in text_low:
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
        if any(k in text_low for k in ["add friend", "message", "theo dõi", "bạn bè", "tham gia nhóm", "nhóm"]):
            return "LIVE", name
        return "DIE", None
    except Exception:
        return None, None

# =============== UI / TEMPLATES ===============
HELP = (
"✨ *FB Watch Bot*\n"
"/them – Thêm từng bước (UID → Loại → Ghi chú → Tên KH)\n"
"/themhng – Thêm UID hàng loạt (mỗi dòng một mục)\n"
"   • Dòng đơn giản: `<uid/url>`\n"
"   • Dòng đầy đủ: `<uid/url> | <ghi chú> | <tên KH> | <profile|group>`\n"
"/danhsach – Kiểm tra lại và hiển thị trạng thái LIVE/DIE hiện tại\n"
"/xoa <uid> – Bỏ theo dõi\n"
"/getuid <url/slug> – Trả về UID số nếu tìm được\n"
"/trogiup – Hướng dẫn\n"
)

def line_box():
    return "____________________________"

def card_added(uid, name, note, customer, kind, added_when, status, url):
    status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
    name_display = name or "—"
    note_display = note or "—"
    customer_display = customer or "—"
    kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
    return (
        "🆕 *Đã thêm UID mới!*\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📂 *Loại*: {kind_display}\n"
        f"👤 *Tên*: {html.escape(name_display)}\n"
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

def parse_inline_add(text: str):
    parts = [p.strip() for p in text.split("|")]
    target = parts[0]
    note = parts[1] if len(parts) > 1 and parts[1] else None
    customer = parts[2] if len(parts) > 2 and parts[2] else None
    kind = parts[3].lower() if len(parts) > 3 and parts[3] else "profile"
    if kind not in ("profile", "group"):
        kind = "profile"
    return target, note, customer, kind

async def them_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
                [InlineKeyboardButton("🗑️ Bỏ theo dõi", callback_data=f"stop:{uid}")]
            ])
            # kiểm tra ngay trạng thái lúc thêm
            st, nm = fetch_status_and_name(url)
            st = st or "DIE"
            if nm: set_profile_status(uid, nm, st)
            await update.effective_message.reply_text(
                card_added(uid, nm, note, customer, kind, now_iso(), st, url),
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")
        return ConversationHandler.END

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

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 Profile/Page", callback_data="type:profile"),
        InlineKeyboardButton("👥 Group", callback_data="type:group")
    ]])
    await update.effective_message.reply_text(
        f"📌 *Chọn loại UID cho* `{uid}`:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    return ADD_TYPE

async def them_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "type:group":
        context.user_data["add"]["kind"] = "group"
        await query.message.reply_text("✅ *Đã chọn loại:* Group", parse_mode=ParseMode.MARKDOWN)
    else:
        context.user_data["add"]["kind"] = "profile"
        await query.message.reply_text("✅ *Đã chọn loại:* Profile/Page", parse_mode=ParseMode.MARKDOWN)

    uid = context.user_data["add"].get("uid")
    await query.message.reply_text(
        f"✍️ *Nhập ghi chú cho UID* `{uid}`\n_Ví dụ: Dame 282, unlock 282_", parse_mode=ParseMode.MARKDOWN
    )
    return ADD_NOTE

async def them_got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["note"] = (update.effective_message.text or "").strip()
    uid = context.user_data["add"].get("uid")
    await update.effective_message.reply_text(
        f"📝 *Nhập tên cho UID* `{uid}`\n_Ví dụ: Tran Tang_", parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CUSTOMER

async def them_got_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["customer"] = (update.effective_message.text or "").strip()
    info = context.user_data.get("add", {})
    uid, url = info.get("uid"), info.get("url")
    note, customer = info.get("note"), info.get("customer")
    kind = info.get("kind", "profile")
    add_subscription(update.effective_chat.id, uid, url, note, customer, kind)

    st, nm = fetch_status_and_name(url)
    st = st or "DIE"
    if nm: set_profile_status(uid, nm, st)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Mở Facebook", url=url)],
        [InlineKeyboardButton("🗑️ Bỏ theo dõi", callback_data=f"stop:{uid}")]
    ])
    await update.effective_message.reply_text(
        card_added(uid, nm, note, customer, kind, now_iso(), st, url),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
    )
    context.user_data.pop("add", None)
    return ConversationHandler.END

async def them_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add", None)
    await update.effective_message.reply_text("Đã hủy.")
    return ConversationHandler.END

# -------- /themhng (thêm hàng loạt) ----------
async def add_bulk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📥 Gửi *danh sách* (mỗi dòng 1 mục):\n"
        "`<uid/url>` *hoặc* `<uid/url> | <ghi chú> | <tên KH> | <profile|group>`",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["await_bulk"] = True

async def handle_bulk_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_bulk"):
        return
    context.user_data["await_bulk"] = False

    raw = update.effective_message.text or ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    ok, fail = 0, 0
    for line in lines:
        try:
            if "|" in line:
                target, note, customer, kind = parse_inline_add(line)
            else:
                target, note, customer, kind = line, None, None, "profile"

            uid, url = normalize_target(target)
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)

            # kiểm tra tức thời
            st, nm = fetch_status_and_name(url)
            st = st or "DIE"
            if nm: set_profile_status(uid, nm, st)
            ok += 1
            time.sleep(0.6)
        except Exception:
            fail += 1
    await update.effective_message.reply_text(f"✅ Đã thêm: {ok} | ❌ lỗi: {fail}")

# =============== LIST / REMOVE ===============
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_subs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Chưa có UID nào. Dùng /them để bắt đầu.")
        return
    for uid, name, _, url, note, customer, kind in rows:
        # kiểm tra lại live/die ngay lúc xem danh sách
        status, nm = fetch_status_and_name(url)
        if status is None:  # lỗi mạng -> giữ nguyên
            status = "DIE"
        if nm: set_profile_status(uid, nm, status)

        status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
        name_display = nm or name or "—"
        note_display = note or "—"
        customer_display = customer or "—"
        kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
        text = (
            f"{line_box()}\n"
            f"🪪 *UID*: [{uid}]({url})\n"
            f"📂 *Loại*: {kind_display}\n"
            f"👤 *Tên*: {html.escape(name_display)}\n"
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
        time.sleep(0.5)

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Dùng: /xoa <uid>")
        return
    uid = context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Đã bỏ theo dõi {uid}")

# -------- /getuid ----------
async def getuid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Dùng: /getuid <url hoặc slug>")
        return
    target = " ".join(context.args)
    try:
        uid, url = normalize_target(target)
        await update.effective_message.reply_text(f"🔎 UID: *{uid}*\n🔗 {url}",
                                                  parse_mode=ParseMode.MARKDOWN,
                                                  disable_web_page_preview=True)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ {e}")

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

def poll_once(application: Application):
    _set_last_poll()
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
        time.sleep(0.5)

# =============== HEALTH-CHECK HTTP SERVER ===============
class _HCHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/health" or self.path == "/hc":
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path == "/metrics":
            con = db()
            n1 = con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            n2 = con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
            con.close()
            out = f"profiles={n1}\nsubs={n2}\nlast_poll=\"{LAST_POLL_AT}\"\n"
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(out.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    def _run():
        try:
            httpd = HTTPServer(("", 8080), _HCHandler)
            httpd.serve_forever()
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()

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
    application.add_handler(CommandHandler("themhng", add_bulk_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_text))
    application.add_handler(CommandHandler("danhsach", list_cmd))
    application.add_handler(CommandHandler("xoa", remove_cmd))
    application.add_handler(CommandHandler("getuid", getuid_cmd))
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

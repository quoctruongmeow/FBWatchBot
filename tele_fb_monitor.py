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

# =================== CONFIG & ROLES ===================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "fbwatch.db"
CHECK_INTERVAL_SEC = 300  # chu kỳ quét định kỳ

def _parse_ids(key:str):
    raw = os.getenv(key, "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}

OWNER_IDS  = _parse_ids("OWNER_IDS")   # toàn quyền
EDITOR_IDS = _parse_ids("EDITOR_IDS")  # thêm/xóa + xem
VIEWER_IDS = _parse_ids("VIEWER_IDS")  # chỉ xem

# map quyền -> tập ID
ROLE = {
    "owner": OWNER_IDS,
    "editor": EDITOR_IDS,
    "viewer": VIEWER_IDS,
}

def role_of(user_id:int) -> str:
    if user_id in OWNER_IDS:
        return "owner"
    if user_id in EDITOR_IDS:
        return "editor"
    if user_id in VIEWER_IDS:
        return "viewer"
    return "unauthorized"

def ensure_role(allowed: set[str]):
    """Decorator cho handler: chặn nếu user không có quyền."""
    def deco(func):
        async def wrapper(update:Update, context:ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            uid = update.effective_user.id if update.effective_user else None
            r = role_of(uid) if uid else "unauthorized"
            if r not in allowed:
                await update.effective_message.reply_text(
                    "❌ Bạn chưa được cấp quyền dùng bot này.\n"
                    "👉 Gõ /myid để lấy ID rồi gửi cho admin mở quyền."
                )
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return deco

# =================== Crawler & Detect ===================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

# chỉ coi là DIE khi thấy các dấu hiệu "không tồn tại/không sẵn sàng"
DEAD_PHRASES = [
    "this content isn't available right now",
    "this page isn't available",
    "the link may be broken",
    "content isn't available",
    "page not found",
    "the page you requested cannot be displayed right now",
    "trang bạn yêu cầu không thể hiển thị",
    "liên kết có thể đã bị hỏng",
    "bạn hiện không thể xem nội dung này",
    "nội dung này hiện không khả dụng",
    "rất tiếc, nội dung này hiện không khả dụng",
]

UID_RE = re.compile(r'^\d{5,}$')

def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def normalize_target(s: str):
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
        return slug, f"https://mbasic.facebook.com/{slug}"
    # chỉ uid/username
    uid = s
    if not re.match(r'^[A-Za-z0-9\.]+$', uid):
        raise ValueError("UID/username không hợp lệ.")
    if UID_RE.match(uid):
        return uid, f"https://mbasic.facebook.com/profile.php?id={uid}"
    return uid, f"https://mbasic.facebook.com/{uid}"

def _try_fetch(url: str, headers: dict, timeout: int) -> tuple[str|None, str|None]:
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code in (404, 410):
            return "DIE", None
        text_lower = r.text.lower()
        if any(p in text_lower for p in DEAD_PHRASES):
            return "DIE", None
        # qua đây là tồn tại (kể cả private/login wall)
        soup = BeautifulSoup(r.text, "html.parser")
        name = None
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            name = og["content"].strip()
        if not name and soup.title and soup.title.text:
            t = soup.title.text.strip()
            if all(k not in t.lower() for k in ["facebook", "log in"]):
                name = t
        return "LIVE", name
    except Exception:
        return None, None

def fetch_status_and_name(url:str, timeout:int=20):
    status, name = _try_fetch(url, HEADERS, timeout)
    if status is not None: return status, name
    alt = url.replace("mbasic.facebook", "m.facebook") if "mbasic.facebook" in url else url.replace("m.facebook","mbasic.facebook")
    status, name = _try_fetch(alt, HEADERS, timeout)
    if status is not None: return status, name
    headers2 = {**HEADERS, "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"}
    alt2 = alt.replace("m.facebook","www.facebook").replace("mbasic.facebook","www.facebook")
    status, name = _try_fetch(alt2, headers2, timeout)
    return (status, name) if status is not None else (None, None)

# =================== DB ===================
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
        uid TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        name TEXT,
        last_status TEXT CHECK(last_status IN ('LIVE','DIE'))
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions(
        chat_id INTEGER NOT NULL,
        uid TEXT NOT NULL,
        note TEXT,
        customer TEXT,
        kind TEXT,
        PRIMARY KEY(chat_id, uid),
        FOREIGN KEY(uid) REFERENCES profiles(uid) ON DELETE CASCADE
    )""")
    return con

def add_subscription(chat_id, uid, url, note=None, customer=None, kind="profile"):
    con = db()
    con.execute("INSERT OR IGNORE INTO profiles(uid,url) VALUES(?,?)",(uid,url))
    con.execute("""INSERT OR IGNORE INTO subscriptions(chat_id,uid,note,customer,kind)
                   VALUES(?,?,?,?,?)""",(chat_id,uid,note,customer,kind))
    if note is not None:
        con.execute("UPDATE subscriptions SET note=? WHERE chat_id=? AND uid=?",(note,chat_id,uid))
    if customer is not None:
        con.execute("UPDATE subscriptions SET customer=? WHERE chat_id=? AND uid=?",(customer,chat_id,uid))
    if kind is not None:
        con.execute("UPDATE subscriptions SET kind=? WHERE chat_id=? AND uid=?",(kind,chat_id,uid))
    con.commit(); con.close()

def set_profile_status(uid, name, status):
    con = db()
    con.execute("UPDATE profiles SET name=COALESCE(?,name), last_status=? WHERE uid=?",(name,status,uid))
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
    con.execute("DELETE FROM subscriptions WHERE chat_id=? AND uid=?",(chat_id,uid))
    con.commit(); con.close()

def get_all_uids():
    con = db()
    rows = con.execute("SELECT uid, url, COALESCE(last_status,'') FROM profiles").fetchall()
    con.close(); return rows

def subscribers_of(uid:str):
    con = db()
    rows = [r[0] for r in con.execute("SELECT chat_id FROM subscriptions WHERE uid=?",(uid,)).fetchall()]
    con.close(); return rows

# =================== UI ===================
ADD_UID, ADD_TYPE, ADD_NOTE, ADD_CUSTOMER = range(1,5)

HELP = (
"✨ *FB Watch Bot*\n"
"/myid – Lấy ID của bạn (gửi cho admin để mở quyền)\n"
"/them – Thêm từng bước (UID → Loại → Ghi chú → Tên KH) *(EDITOR/OWNER)*\n"
"/them <uid/url> | <ghi chú> | <tên KH> | <profile|group> – Thêm nhanh *(EDITOR/OWNER)*\n"
"/danhsach – Xem UID đang theo dõi *(VIEWER/EDITOR/OWNER)*\n"
"/xoa <uid> – Bỏ theo dõi *(EDITOR/OWNER)*\n"
"/trogiup – Hướng dẫn\n"
)

def line_box(): return "____________________________"

def card_added(uid, note, customer, kind, added_when, status, url):
    status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
    kind_display = "Profile/Page" if (kind or "profile")=="profile" else "Group"
    return (
        "🆕 *Đã thêm UID mới!*\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📂 *Loại*: {kind_display}\n"
        f"📝 *Ghi chú*: {html.escape(note or '—')}\n"
        f"🙍 *Khách hàng*: {html.escape(customer or '—')}\n"
        f"📌 *Ngày thêm*: {added_when}\n"
        f"📟 *Trạng thái hiện tại*: {status_icon}\n"
        f"{line_box()}"
    )

def card_alert(uid, note, customer, url, old, new):
    arrow = "🔴 DIE → 🟢 LIVE" if new=="LIVE" else "🟢 LIVE → 🔴 DIE"
    title = "🚀 *UID đã LIVE trở lại!*" if new=="LIVE" else "☠️ *UID đã DIE!*"
    return (
        f"{title}\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📝 *Ghi chú*: {html.escape(note or '—')}\n"
        f"🙍 *Khách hàng*: {html.escape(customer or '—')}\n"
        f"📟 *Trạng thái*: {arrow}\n"
        f"⏰ *Thời gian*: {now_iso()}\n"
        f"{line_box()}"
    )

# =================== COMMANDS (with roles) ===================
async def myid_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    role = role_of(uid)
    role_vn = {"owner":"OWNER","editor":"EDITOR","viewer":"VIEWER","unauthorized":"Chưa cấp quyền"}[role]
    await update.effective_message.reply_text(
        f"🆔 *ID của bạn:* `{uid}`\n🔑 *Quyền hiện tại:* {role_vn}",
        parse_mode=ParseMode.MARKDOWN
    )

async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Chào bạn!\n"+HELP, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

def parse_inline_add(text:str):
    parts=[p.strip() for p in text.split("|")]
    target = parts[0]; note = parts[1] if len(parts)>1 and parts[1] else None
    customer = parts[2] if len(parts)>2 and parts[2] else None
    kind = (parts[3].lower() if len(parts)>3 and parts[3] else "profile")
    if kind not in ("profile","group"): kind="profile"
    return target, note, customer, kind

@ensure_role({"owner","editor"})
async def them_entry(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            status, name = fetch_status_and_name(url)
            if status is None: status = "DIE"
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            set_profile_status(uid, name, status)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 Mở Facebook", url=url),
            ],[
                InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
            ]])
            await update.effective_message.reply_text(
                card_added(uid, note, customer, kind, now_iso(), status, url),
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    await update.effective_message.reply_text("➕ *Vui lòng nhập UID hoặc URL bạn muốn theo dõi:*", parse_mode=ParseMode.MARKDOWN)
    context.user_data["add"]={}
    return ADD_UID

@ensure_role({"owner","editor"})
async def them_got_uid(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text=(update.effective_message.text or "").strip()
    try:
        uid,url=normalize_target(text)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ {e}\nVui lòng nhập lại UID/URL.")
        return ADD_UID
    context.user_data["add"]["uid"]=uid
    context.user_data["add"]["url"]=url
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 Profile/Page","type:profile"),
                                InlineKeyboardButton("👥 Group","type:group")]])
    await update.effective_message.reply_text(
        f"📌 *Chọn loại UID cho* `{uid}`:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    return ADD_TYPE

@ensure_role({"owner","editor"})
async def them_pick_type(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    kind="profile" if q.data!="type:group" else "group"
    context.user_data["add"]["kind"]=kind
    await q.message.reply_text("✍️ *Nhập ghi chú (nếu có)*:", parse_mode=ParseMode.MARKDOWN)
    return ADD_NOTE

@ensure_role({"owner","editor"})
async def them_got_note(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["note"]=(update.effective_message.text or "").strip()
    await update.effective_message.reply_text("📝 *Nhập tên khách hàng (nếu có)*:", parse_mode=ParseMode.MARKDOWN)
    return ADD_CUSTOMER

@ensure_role({"owner","editor"})
async def them_got_customer(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["customer"]=(update.effective_message.text or "").strip()
    info=context.user_data.get("add",{})
    uid,url=info.get("uid"),info.get("url")
    note,customer=info.get("note"),info.get("customer")
    kind=info.get("kind","profile")
    status,name=fetch_status_and_name(url)
    if status is None: status="DIE"
    add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
    set_profile_status(uid, name, status)
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Mở Facebook", url=url),
    ],[
        InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
        InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
    ]])
    await update.effective_message.reply_text(
        card_added(uid, note, customer, kind, now_iso(), status, url),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
    )
    context.user_data.pop("add",None)
    return ConversationHandler.END

@ensure_role({"owner","editor","viewer"})
async def list_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    rows=list_subs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Chưa có UID nào. Dùng /them để bắt đầu.")
        return
    for uid,_,prev,url,note,customer,kind in rows:
        status,name=fetch_status_and_name(url)
        if status is None: status=prev if prev else "DIE"
        set_profile_status(uid, name, status if status else prev)
        status_icon = "🟢 LIVE" if status=="LIVE" else "🔴 DIE"
        kind_display = "Profile/Page" if (kind or "profile")=="profile" else "Group"
        text=(f"{line_box()}\n"
              f"🪪 *UID*: [{uid}]({url})\n"
              f"📂 *Loại*: {kind_display}\n"
              f"📝 *Ghi chú*: {html.escape(note or '—')}\n"
              f"🙍 *Khách hàng*: {html.escape(customer or '—')}\n"
              f"📟 *Trạng thái*: {status_icon}\n"
              f"{line_box()}")
        kb=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Mở Facebook", url=url),
        ],[
            InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
            InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
        ]])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
        )
        time.sleep(0.4)

@ensure_role({"owner","editor"})
async def remove_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Dùng: /xoa <uid>")
        return
    uid=context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Đã bỏ theo dõi {uid}")

async def button_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    data=query.data or ""
    chat_id=query.message.chat.id if query.message else None

    if data.startswith("stop:"):
        # chỉ editor/owner mới được dừng theo dõi
        if role_of(update.effective_user.id) not in {"owner","editor"}:
            await query.message.reply_text("❌ Bạn không có quyền dừng theo dõi.")
            return
        uid=data.split(":",1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛑 Đã dừng theo dõi UID {uid}")

    elif data.startswith("keep:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Tiếp tục theo dõi UID này.")

# =================== POLLER & HEALTHCHECK ===================
def poll_once(application:Application):
    for uid, url, prev in get_all_uids():
        status, name = fetch_status_and_name(url)
        if status is None:
            continue
        if prev != status:
            set_profile_status(uid, name, status)
            for chat_id in subscribers_of(uid):
                con=db()
                row=con.execute("SELECT COALESCE(note,''), COALESCE(customer,'') FROM subscriptions WHERE chat_id=? AND uid=?",(chat_id,uid)).fetchone()
                con.close()
                note,customer=(row or ("",""))
                text=card_alert(uid, note, customer, url, prev if prev else "Unknown", status)
                keyboard=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Mở Facebook", url=url),
                ],[
                    InlineKeyboardButton("✅ Tiếp tục theo dõi", callback_data=f"keep:{uid}"),
                    InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}")
                ]])
                application.create_task(
                    application.bot.send_message(chat_id=chat_id, text=text,
                                                 parse_mode=ParseMode.MARKDOWN,
                                                 disable_web_page_preview=True,
                                                 reply_markup=keyboard)
                )
        else:
            if name:
                set_profile_status(uid, name, status)
        time.sleep(0.6)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args, **kwargs): return

def run_health_server():
    server=HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

# =================== MAIN ===================
def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN không hợp lệ hoặc không nạp được từ .env")

    # bật HTTP health-check (Render free giữ process)
    threading.Thread(target=run_health_server, daemon=True).start()

    application=Application.builder().token(BOT_TOKEN).build()

    conv_them=ConversationHandler(
        entry_points=[CommandHandler("them", them_entry)],
        states={
            ADD_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_uid)],
            ADD_TYPE: [CallbackQueryHandler(them_pick_type, pattern=r"^type:")],
            ADD_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_note)],
            ADD_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, them_got_customer)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("myid",   myid_cmd))
    application.add_handler(CommandHandler(["start","trogiup","menu"], start_cmd))
    application.add_handler(conv_them)
    application.add_handler(CommandHandler("danhsach", list_cmd))
    application.add_handler(CommandHandler("xoa",      remove_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    # scheduler check định kỳ
    scheduler=BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(lambda: poll_once(application), "interval", seconds=CHECK_INTERVAL_SEC, max_instances=1)
    scheduler.start()

    print("Bot is running with roles…")
    application.run_polling(close_loop=False)

if __name__=="__main__":
    db()  # đảm bảo schema
    main()

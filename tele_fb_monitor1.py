# file: live_bot.py
import os
import re
import time
import json
from datetime import datetime
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from check_live_sync import check_live

BOT_TOKEN = os.getenv("BOT_TOKEN")  # export BOT_TOKEN=xxx
if not BOT_TOKEN:
    raise SystemExit("Vui lòng set BOT_TOKEN trong biến môi trường BOT_TOKEN.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Lưu tạm trong RAM (có thể thay bằng DB/Redis khi triển khai thật)
# tracking: uid -> {"note": str, "customer": str, "added": ts, "following": bool}
tracking = {}

GREEN = "🟢"
RED = "🔴"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

MENU_TEXT = (
    "<b>Lệnh hỗ trợ</b>\n\n"
    "start - Khởi động bot\n"
    "them - Thêm UID mới\n"
    "themnhg - Thêm UID hàng loạt\n"
    "xoa - Bỏ theo dõi UID\n"
    "danhsach - Xem UID đang theo dõi\n"
    "trogiup - Hướng dẫn sử dụng\n"
    "menu - Hiện menu lệnh\n"
    "getuid - Lấy UID từ link Facebook\n\n"
    "<i>Gợi ý:</i>\n"
    "• /them <code>&lt;uid&gt; [ghi_chu] [khach_hang]</code>\n"
    "• /themnhg: gửi kèm nhiều dòng, mỗi dòng: <code>uid[,ghi_chu[,khach_hang]]</code>\n"
    "• /xoa <code>&lt;uid&gt;</code>\n"
    "• /getuid <code>&lt;link_facebook&gt;</code>\n"
)

def build_card(uid: str, note: str = "unlock", customer: str = "T") -> tuple[str, InlineKeyboardMarkup]:
    status = check_live(uid)
    dot = GREEN if status == "live" else RED
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = (
        "🆕 <b>Đã thêm/ cập nhật UID!</b>\n\n"
        f"🆔 <b>UID:</b> <a href=\"https://facebook.com/{uid}\">{uid}</a>\n"
        "📄 <b>Loại:</b> Profile/Page\n"
        f"📝 <b>Ghi chú:</b> {note}\n"
        f"👤 <b>Khách hàng:</b> {customer}\n"
        f"📅 <b>Ngày thêm:</b> {now}\n"
        f"✅ <b>Trạng thái hiện tại:</b> {dot} {status.upper()}"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🌐 Mở Facebook", url=f"https://facebook.com/{uid}"))
    following = tracking.get(uid, {}).get("following", True)
    if following:
        kb.add(
            InlineKeyboardButton("🟢 Tiếp tục theo dõi", callback_data=f"noop:{uid}"),
            InlineKeyboardButton("🛑 Dừng theo dõi UID này", callback_data=f"stop:{uid}"),
        )
    else:
        kb.add(InlineKeyboardButton("✅ Bắt đầu theo dõi lại", callback_data=f"start:{uid}"))
    return text, kb

def extract_uid_from_link(link: str, timeout: float = 10.0) -> str | None:
    """
    Trả về UID (chuỗi số) nếu tìm được từ link Facebook. Heuristic:
    1) Nếu link có tham số id=123... => lấy số.
    2) Nếu chứa dãy số dài (>=7) trong path => lấy số đó.
    3) Nếu là username (chữ), thử gọi graph.facebook.com/<username>?fields=id (không token).
       Nếu trả JSON có 'id' => lấy id; ngược lại trả None.
    """
    # 1) id=...
    m = re.search(r"[?&]id=(\d{5,})", link)
    if m:
        return m.group(1)

    # 2) dãy số trong path
    m2 = re.search(r"facebook\.com/(?:profile\.php\?id=)?(\d{7,})", link)
    if m2:
        return m2.group(1)

    # 3) username -> thử graph
    m3 = re.search(r"facebook\.com/([A-Za-z0-9.\-_]+)/?", link)
    if m3:
        username = m3.group(1)
        if username.lower() in {"profile.php", "people", "pages"}:
            return None
        try:
            headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive", "Accept": "*/*"}
            url = f"https://graph.facebook.com/{username}"
            resp = requests.get(url, params={"fields": "id"}, headers=headers, timeout=timeout)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            uid = str(data.get("id")) if isinstance(data, dict) else None
            if uid and uid.isdigit():
                return uid
        except Exception:
            pass

    return None

def ensure_tracked(uid: str, note="unlock", customer="T"):
    if uid not in tracking:
        tracking[uid] = {"note": note, "customer": customer, "added": int(time.time()), "following": True}
    else:
        tracking[uid].update({"note": note or tracking[uid]["note"], "customer": customer or tracking[uid]["customer"]})

# ---------- Command Handlers ----------

@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Xin chào! Mình đã sẵn sàng.\n\n" + MENU_TEXT)

@bot.message_handler(commands=["trogiup"])
def cmd_help(m):
    bot.reply_to(m, MENU_TEXT)

@bot.message_handler(commands=["menu"])
def cmd_menu(m):
    bot.reply_to(m, MENU_TEXT)

@bot.message_handler(commands=["them"])
def cmd_them(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Cú pháp: <code>/them &lt;uid&gt; [ghi_chu] [khach_hang]</code>")
        return
    uid = parts[1]
    note = parts[2] if len(parts) >= 3 else "unlock"
    customer = parts[3] if len(parts) >= 4 else "T"

    ensure_tracked(uid, note, customer)
    text, kb = build_card(uid, tracking[uid]["note"], tracking[uid]["customer"])
    bot.send_message(m.chat.id, text, reply_markup=kb, disable_web_page_preview=True)

@bot.message_handler(commands=["themnhg"])
def cmd_themnhg(m):
    """
    Nội dung sau lệnh có thể nằm ở cùng dòng hoặc dòng kế tiếp.
    Mỗi dòng: uid[,ghi_chu[,khach_hang]]
    """
    payload = m.text.split(maxsplit=1)
    tail = payload[1] if len(payload) > 1 else ""
    # Nếu user reply bằng văn bản nhiều dòng, ưu tiên caption/next messages thì dùng message.reply_to_message?
    lines = (tail.strip() or "").splitlines()
    if not lines and m.reply_to_message and m.reply_to_message.text:
        lines = m.reply_to_message.text.strip().splitlines()

    if not lines:
        bot.reply_to(m, "Gửi danh sách theo dạng:\n<code>/themnhg</code>\n<code>uid1,note1,KH1</code>\n<code>uid2</code>\n...")
        return

    results = []
    for raw in lines:
        if not raw.strip():
            continue
        parts = [p.strip() for p in raw.split(",")]
        uid = parts[0]
        note = parts[1] if len(parts) >= 2 and parts[1] else "unlock"
        customer = parts[2] if len(parts) >= 3 and parts[2] else "T"
        ensure_tracked(uid, note, customer)
        status = check_live(uid)
        results.append({"uid": uid, "status": status, "note": note, "customer": customer})

    # Tóm tắt + gửi từng card
    summary = "\n".join([f"{r['uid']}: {r['status']}" for r in results])
    bot.reply_to(m, "<b>Đã thêm hàng loạt:</b>\n" + "<code>" + summary + "</code>")
    for r in results[:20]:  # tránh spam quá nhiều tin
        text, kb = build_card(r["uid"], r["note"], r["customer"])
        bot.send_message(m.chat.id, text, reply_markup=kb, disable_web_page_preview=True)

@bot.message_handler(commands=["xoa"])
def cmd_xoa(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Cú pháp: <code>/xoa &lt;uid&gt;</code>")
        return
    uid = parts[1]
    if tracking.pop(uid, None) is None:
        bot.reply_to(m, f"UID <code>{uid}</code> không tồn tại trong danh sách.")
    else:
        bot.reply_to(m, f"Đã xóa UID <code>{uid}</code> khỏi danh sách theo dõi.")

@bot.message_handler(commands=["danhsach"])
def cmd_danhsach(m):
    if not tracking:
        bot.reply_to(m, "Danh sách trống.")
        return
    lines = []
    for i, (uid, info) in enumerate(list(tracking.items())[:50], start=1):
        status = check_live(uid)
        dot = GREEN if status == "live" else RED
        lines.append(f"{i}. {uid} {dot} {status.upper()} | {info['note']} | {info['customer']}")
    text = "<b>UID đang theo dõi (tối đa 50):</b>\n" + "\n".join(lines)
    bot.reply_to(m, text)

@bot.message_handler(commands=["getuid"])
def cmd_getuid(m):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, "Cú pháp: <code>/getuid &lt;link_facebook&gt;</code>")
        return
    link = parts[1].strip()
    uid = extract_uid_from_link(link)
    if uid:
        bot.reply_to(m, f"UID lấy được: <code>{uid}</code>")
    else:
        bot.reply_to(m, "Không thể trích xuất UID từ link. "
                        "Thử link dạng <code>profile.php?id=...</code> hoặc username công khai.")

# ---------- Callback Buttons ----------

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    try:
        action, uid = c.data.split(":", 1)
    except ValueError:
        bot.answer_callback_query(c.id); return

    if action == "stop":
        if uid in tracking:
            tracking[uid]["following"] = False
        bot.answer_callback_query(c.id, "Đã dừng theo dõi.")
    elif action == "start":
        if uid in tracking:
            tracking[uid]["following"] = True
        bot.answer_callback_query(c.id, "Đã tiếp tục theo dõi.")
    else:
        bot.answer_callback_query(c.id)

    note = tracking.get(uid, {}).get("note", "unlock")
    customer = tracking.get(uid, {}).get("customer", "T")
    text, kb = build_card(uid, note, customer)
    try:
        bot.edit_message_text(chat_id=c.message.chat.id, message_id=c.message.message_id,
                              text=text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)

# ---------- Run ----------
if __name__ == "__main__":
    print("Bot đang chạy…")
    bot.infinity_polling(skip_pending=True, timeout=60)

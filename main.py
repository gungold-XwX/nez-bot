import os
import sqlite3
import random
import time
from typing import List, Tuple, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================== ENV ==================
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "10000"))

# локальный сдвиг времени (МСК = +3)
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "3"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nez.db"

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        callsign TEXT,
        points INTEGER DEFAULT 0,
        created_at INTEGER
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        kind TEXT,
        payload TEXT,
        created_at INTEGER,
        fixed_at INTEGER DEFAULT 0,
        decrypted_at INTEGER DEFAULT 0,
        status TEXT
    )
    """)
    conn.commit()
    return conn

def upsert_user(conn, user_id: int, callsign: str):
    cur = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        conn.execute(
            "INSERT INTO users (user_id, callsign, points, created_at) VALUES (?, ?, 0, ?)",
            (user_id, callsign[:32], int(time.time()))
        )
        conn.commit()

def get_user(conn, user_id: int):
    cur = conn.execute(
        "SELECT user_id, callsign, points, created_at FROM users WHERE user_id=?",
        (user_id,)
    )
    return cur.fetchone()

def add_points(conn, user_id: int, delta: int):
    conn.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (delta, user_id)
    )
    conn.commit()

def leaderboard(conn, limit=10):
    cur = conn.execute(
        "SELECT callsign, points FROM users ORDER BY points DESC, created_at ASC LIMIT ?",
        (limit,)
    )
    return cur.fetchall()

def all_users(conn):
    cur = conn.execute("SELECT user_id, callsign, points FROM users")
    return cur.fetchall()

def queue_position(conn, user_id: int) -> Tuple[int, int]:
    cur = conn.execute(
        "SELECT user_id FROM users ORDER BY points DESC, created_at ASC"
    )
    ids = [r[0] for r in cur.fetchall()]
    total = len(ids)
    if user_id not in ids:
        return total + 1, total
    return ids.index(user_id) + 1, total

def neighbors(conn, user_id: int, window=2):
    cur = conn.execute(
        "SELECT user_id, callsign, points FROM users ORDER BY points DESC, created_at ASC"
    )
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if user_id not in ids:
        return [], []
    idx = ids.index(user_id)
    return rows[max(0, idx-window):idx], rows[idx+1:idx+1+window]

# ================== ANOMALIES ==================
NOCLASS_PAYLOADS = [
    "Шумовой пакет. Семантика не обнаружена.",
    "Интерференция среды. Отражение нестабильно.",
    "След третьего измерения рассеялся.",
    "Данные фрагментированы. Класс не присвоен."
]

def s_payload(track_id: int) -> str:
    return f"[CLASS S]\nARCHIVE FRAGMENT NEZ-S-{track_id:02d}\n…signal continues…"

def create_anomaly(conn, user_id: int, kind: str, payload: str):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, created_at, status)
    VALUES (?, ?, ?, ?, 'SENT')
    """, (user_id, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, user_id: int):
    cur = conn.execute("""
    SELECT id, kind, payload, created_at, fixed_at, status
    FROM anomalies
    WHERE user_id=? AND status IN ('SENT','FIXED')
    ORDER BY created_at DESC
    LIMIT 1
    """, (user_id,))
    return cur.fetchone()

def fix_anomaly(conn, anomaly_id: int):
    conn.execute("""
    UPDATE anomalies SET fixed_at=?, status='FIXED'
    WHERE id=? AND status='SENT'
    """, (int(time.time()), anomaly_id))
    conn.commit()

def decrypt_anomaly(conn, anomaly_id: int):
    conn.execute("""
    UPDATE anomalies SET decrypted_at=?, status='DECRYPTED'
    WHERE id=? AND status='FIXED'
    """, (int(time.time()), anomaly_id))
    conn.commit()

# ================== UI ==================
def hdr():
    return "NEZ PROJECT × GOV // EDEN QUEUE\n"

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Очередь", callback_data="Q")],
        [InlineKeyboardButton("⚠️ Аномалия", callback_data="A")],
        [InlineKeyboardButton("🏆 Топ", callback_data="TOP")],
        [InlineKeyboardButton("ℹ️ Справка", callback_data="HELP")],
    ])

def fix_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Зафиксировать", callback_data=f"FIX:{aid}")]
    ])

def decrypt_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Расшифровать", callback_data=f"DEC:{aid}")]
    ])

# ================== TIME ==================
def now():
    return int(time.time())

def local_hour(ts: int):
    return ((ts + TZ_OFFSET*3600) % 86400) // 3600

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    uid = update.effective_user.id
    name = update.effective_user.username or update.effective_user.first_name or "observer"
    upsert_user(conn, uid, name)
    pos, total = queue_position(conn, uid)

    text = (
        hdr() +
        f"Путешественник: {name}\n"
        f"Позиция в очереди: {pos}/{total}\n\n"
        "Это цифровая очередь в Нулевой Эдем.\n"
        "Следи за аномалиями и продвигайся вверх."
    )
    await update.message.reply_text(text, reply_markup=menu())

async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    conn = db()
    uid = update.effective_user.id
    u = get_user(conn, uid)
    pos, total = queue_position(conn, uid)
    up, down = neighbors(conn, uid)

    text = hdr() + f"Очередь: {pos}/{total}\nОчки: {u[2]}\n\n"
    if up:
        text += "↑ Выше:\n" + "\n".join([f"{r[1]} ({r[2]})" for r in up]) + "\n"
    if down:
        text += "↓ Ниже:\n" + "\n".join([f"{r[1]} ({r[2]})" for r in down])

    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=menu())
    else:
        await update.message.reply_text(text, reply_markup=menu())

async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    conn = db()
    rows = leaderboard(conn)
    text = hdr() + "🏆 ТОП\n\n"
    for i, (cs, pts) in enumerate(rows, 1):
        text += f"{i}. {cs} — {pts}\n"
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=menu())
    else:
        await update.message.reply_text(text, reply_markup=menu())

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    text = (
        hdr() +
        "• Очередь обновляется автоматически\n"
        "• Аномалии появляются 3 раза в день\n"
        "• Быстрая фиксация = больше очков\n"
        "• CLASS S — фрагменты архива\n\n"
        "Игроки с верхних позиций будут отмечены на концерте."
    )
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=menu())
    else:
        await update.message.reply_text(text, reply_markup=menu())

async def check_anomaly(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    conn = db()
    uid = update.effective_user.id
    a = get_active_anomaly(conn, uid)
    if not a:
        text = hdr() + "Аномалий нет. Ожидайте следующего окна."
        if edit:
            await update.callback_query.edit_message_text(text, reply_markup=menu())
        else:
            await update.message.reply_text(text, reply_markup=menu())
        return

    aid, kind, payload, created, fixed_at, status = a
    if status == "SENT":
        text = hdr() + "⚠️ Обнаружена аномалия.\nТребуется фиксация."
        kb = fix_kb(aid)
    else:
        wait = now() - fixed_at
        if wait < 600:
            text = hdr() + f"Фиксация принята.\nОжидайте {600-wait} сек."
            kb = menu()
        else:
            text = hdr() + "Аномалия готова к расшифровке."
            kb = decrypt_kb(aid)

    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

# ================== CALLBACKS ==================
async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    if d == "Q":
        await show_queue(update, context, edit=True)
    elif d == "A":
        await check_anomaly(update, context, edit=True)
    elif d == "TOP":
        await show_top(update, context, edit=True)
    elif d == "HELP":
        await show_help(update, context, edit=True)

async def on_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    aid = int(q.data.split(":")[1])
    conn = db()
    fix_anomaly(conn, aid)
    add_points(conn, update.effective_user.id, 2)
    await q.edit_message_text(
        hdr() + "Фиксация принята. Ожидайте 10 минут.",
        reply_markup=menu()
    )

async def on_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    aid = int(q.data.split(":")[1])
    conn = db()
    cur = conn.execute(
        "SELECT kind, payload FROM anomalies WHERE id=?",
        (aid,)
    )
    kind, payload = cur.fetchone()
    decrypt_anomaly(conn, aid)
    reward = 5 if kind == "S" else 3
    add_points(conn, update.effective_user.id, reward)
    await q.edit_message_text(
        hdr() + f"🔎 Расшифровка\n\n{payload}\n\n+{reward} pts",
        reply_markup=menu()
    )

# ================== JOBS ==================
async def job_queue_push(context: ContextTypes.DEFAULT_TYPE):
    h = local_hour(now())
    if h not in (10, 16, 22):
        return
    conn = db()
    for uid, cs, pts in all_users(conn):
        pos, total = queue_position(conn, uid)
        try:
            await context.bot.send_message(
                uid,
                hdr() + f"Справка\nПозиция: {pos}/{total}\nОчки: {pts}",
                reply_markup=menu()
            )
        except:
            pass

async def job_spawn_anomalies(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = all_users(conn)
    for uid, cs, pts in users:
        pos, total = queue_position(conn, uid)
        chance = 0.1 + (1 - pos/max(1,total)) * 0.6
        if random.random() < chance:
            payload = s_payload(random.randint(1, 12))
            kind = "S"
        else:
            payload = random.choice(NOCLASS_PAYLOADS)
            kind = "NOCLASS"
        create_anomaly(conn, uid, kind, payload)
        try:
            await context.bot.send_message(
                uid,
                hdr() + "⚠️ Обнаружена аномалия",
                reply_markup=menu()
            )
        except:
            pass

# ================== APP ==================
def build_app():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_fix, pattern=r"^FIX:"))
    app.add_handler(CallbackQueryHandler(on_decrypt, pattern=r"^DEC:"))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_handler(MessageHandler(filters.TEXT, lambda u, c: u.message.reply_text("Открой меню", reply_markup=menu())))

    return app

if __name__ == "__main__":
    application = build_app()

    application.job_queue.run_repeating(job_queue_push, interval=3600, first=60)
    application.job_queue.run_repeating(job_spawn_anomalies, interval=8*3600, first=120)

    if BASE_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{BASE_URL}/telegram",
            drop_pending_updates=True
        )
    else:
        application.run_polling(drop_pending_updates=True)

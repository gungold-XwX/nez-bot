import os
import sqlite3
import random
import time
import re
from typing import Tuple, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# ================== ENV ==================
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nez.db"

# ================== STYLE ==================
def hdr():
    return (
        "🔴 NEZ PROJECT × GOV\n"
        "▶ DIGITAL ACCESS QUEUE / EDEN-0\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        points INTEGER DEFAULT 0,
        created_at INTEGER
    )""")
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
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS s_audio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        file_id TEXT
    )""")
    conn.commit()
    return conn

# ================== USERS ==================
def get_user(conn, uid):
    return conn.execute(
        "SELECT user_id, username, points FROM users WHERE user_id=?",
        (uid,)
    ).fetchone()

def create_user(conn, uid, name):
    conn.execute(
        "INSERT INTO users VALUES (?, ?, 0, ?)",
        (uid, name, int(time.time()))
    )
    conn.commit()

def add_points(conn, uid, pts):
    conn.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (pts, uid)
    )
    conn.commit()

def all_users(conn):
    return conn.execute(
        "SELECT user_id, username, points FROM users"
    ).fetchall()

def queue_position(conn, uid) -> Tuple[int, int]:
    ids = [r[0] for r in conn.execute(
        "SELECT user_id FROM users ORDER BY points DESC, created_at ASC"
    )]
    total = len(ids)
    return (ids.index(uid) + 1, total) if uid in ids else (total + 1, total)

# ================== VALIDATION ==================
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,20}$")

# ================== S AUDIO ==================
def add_s_audio(conn, title, fid):
    conn.execute(
        "INSERT INTO s_audio (title, file_id) VALUES (?, ?)",
        (title[:60], fid)
    )
    conn.commit()

def random_s_audio(conn):
    return conn.execute(
        "SELECT title, file_id FROM s_audio ORDER BY RANDOM() LIMIT 1"
    ).fetchone()

# ================== ANOMALIES ==================
NOCLASS = [
    "▒▒▒ СОДЕРЖИМОЕ УТЕРЯНО ▒▒▒",
    "РАСШИФРОВКА ПРЕРВАНА",
    "ДАННЫЕ ПОВРЕЖДЕНЫ",
    "▒░▒▒░▒░▒▒░▒░▒▒▒░▒░",
]

def create_anomaly(conn, uid, kind, payload):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, created_at, status)
    VALUES (?, ?, ?, ?, 'SENT')
    """, (uid, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, uid):
    return conn.execute("""
    SELECT id, kind, payload, fixed_at, status
    FROM anomalies
    WHERE user_id=? AND status IN ('SENT','FIXED')
    ORDER BY created_at DESC LIMIT 1
    """, (uid,)).fetchone()

# ================== BULLETIN ==================
def build_bulletin(conn):
    today = time.strftime("%d.%m.%Y")
    rows = conn.execute(
        "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
    ).fetchall()

    text = (
        "🔴 NEZ PROJECT × GOV\n"
        "▶ OFFICIAL BULLETIN / EDEN-0\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"ДАТА: {today}\n\n"
        "СОСТОЯНИЕ СИСТЕМЫ:\n"
        "— активность третьего измерения повышена\n"
        "— очередь нестабильна\n"
        "— зафиксированы новые аномалии\n\n"
        "РЕЙТИНГ ДОСТУПА:\n"
    )

    for i, (name, pts) in enumerate(rows, 1):
        if i <= 3:
            text += f"{i:02d}. {name} — {pts} pts  [CANDIDATE]\n"
        else:
            text += f"{i:02d}. {name} — {pts} pts\n"

    text += "\n▶ Следующий бюллетень через 24 часа"
    return text

async def send_daily_bulletin(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    bulletin = build_bulletin(conn)
    for uid, _, _ in all_users(conn):
        try:
            await context.bot.send_message(uid, bulletin)
        except:
            pass

# ================== UI ==================
def menu(uid):
    rows = [
        [InlineKeyboardButton("▶ СТАТУС ОЧЕРЕДИ", callback_data="Q")],
        [InlineKeyboardButton("🔴 АКТИВНАЯ АНОМАЛИЯ", callback_data="A")],
        [InlineKeyboardButton("🏛 РЕЙТИНГ", callback_data="TOP")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("🔴 ЗАПУСТИТЬ АНОМАЛИЮ", callback_data="ADMIN_ANOM")])
        rows.append([InlineKeyboardButton("➕ ДОБАВИТЬ S-СИГНАЛ", callback_data="ADD_S")])
    return InlineKeyboardMarkup(rows)

def fix_kb(aid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶ ЗАФИКСИРОВАТЬ", callback_data=f"FIX:{aid}")]
    ])

def decrypt_kb(aid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶ РАСШИФРОВАТЬ", callback_data=f"DEC:{aid}")]
    ])

WAITING_USERNAME = set()
WAITING_AUDIO = set()

# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    uid = update.effective_user.id
    u = get_user(conn, uid)

    if u:
        pos, total = queue_position(conn, uid)
        await update.message.reply_text(
            hdr() +
            f"🟢 ДОСТУП АКТИВЕН\n\n"
            f"ID: {u[1]}\n"
            f"Позиция: {pos} / {total}",
            reply_markup=menu(uid)
        )
        return

    WAITING_USERNAME.add(uid)
    await update.message.reply_text(
        hdr() +
        "▶ РЕГИСТРАЦИЯ ДОСТУПА\n\n"
        "Первые позиции очереди будут публично отмечены.\n"
        "Поздравление проводит глава NEZ на специальном мероприятии.\n\n"
        "▶ Требования к ID:\n"
        "латиница / цифры / . _ -\n"
        "длина: 3–20\n\n"
        "Введите идентификатор:"
    )

# ================== TEXT ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in WAITING_USERNAME:
        return

    name = update.message.text.strip()
    if not USERNAME_RE.match(name):
        await update.message.reply_text(
            hdr() + "⛔ НЕКОРРЕКТНЫЙ ИДЕНТИФИКАТОР\nПопробуйте снова."
        )
        return

    conn = db()
    create_user(conn, uid, name)
    WAITING_USERNAME.remove(uid)

    pos, total = queue_position(conn, uid)
    await update.message.reply_text(
        hdr() +
        "🟢 РЕГИСТРАЦИЯ ЗАВЕРШЕНА\n\n"
        f"ID: {name}\n"
        f"Позиция: {pos} / {total}",
        reply_markup=menu(uid)
    )

# ================== CALLBACKS ==================
async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    conn = db()

    if q.data == "Q":
        pos, total = queue_position(conn, uid)
        await q.edit_message_text(
            hdr() + f"🔵 СТАТУС ОЧЕРЕДИ\n\nПозиция: {pos} / {total}",
            reply_markup=menu(uid)
        )

    elif q.data == "A":
        a = get_active_anomaly(conn, uid)
        if not a:
            await q.edit_message_text(
                hdr() + "🟢 АКТИВНЫХ АНОМАЛИЙ НЕТ",
                reply_markup=menu(uid)
            )
            return
        aid, kind, payload, fixed_at, status = a
        if status == "SENT":
            await q.edit_message_text(
                hdr() + "🔴 АНОМАЛИЯ ОБНАРУЖЕНА\n▶ Требуется фиксация",
                reply_markup=fix_kb(aid)
            )
        else:
            if time.time() - fixed_at < 600:
                await q.edit_message_text(
                    hdr() + "🟠 ФИКСАЦИЯ ПРИНЯТА\n⏳ Ожидание 10 минут",
                    reply_markup=menu(uid)
                )
            else:
                await q.edit_message_text(
                    hdr() + "▶ ГОТОВО К РАСШИФРОВКЕ",
                    reply_markup=decrypt_kb(aid)
                )

    elif q.data == "TOP":
        rows = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()
        txt = hdr() + "🏛 РЕЙТИНГ ДОСТУПА\n\n"
        for i, (n, p) in enumerate(rows, 1):
            txt += f"{i}. {n} — {p}\n"
        await q.edit_message_text(txt, reply_markup=menu(uid))

    elif q.data == "ADD_S" and uid == ADMIN_ID:
        WAITING_AUDIO.add(uid)
        await q.edit_message_text(
            hdr() + "▶ ДОБАВЛЕНИЕ S-СИГНАЛА\nОтправьте аудио-файл."
        )

    elif q.data == "ADMIN_ANOM" and uid == ADMIN_ID:
        await admin_spawn(context)

# ================== ADMIN ANOMALY ==================
async def admin_spawn(context):
    conn = db()
    for uid, _, _ in all_users(conn):
        pos, total = queue_position(conn, uid)
        chance = 0.15 + (1 - pos / max(1, total)) * 0.6
        if random.random() < chance:
            row = random_s_audio(conn)
            if row:
                _, fid = row
                kind, payload = "S_AUDIO", fid
            else:
                kind, payload = "NOCLASS", random.choice(NOCLASS)
        else:
            kind, payload = "NOCLASS", random.choice(NOCLASS)

        create_anomaly(conn, uid, kind, payload)
        try:
            await context.bot.send_message(
                uid,
                hdr() + "🔴 ОБНАРУЖЕНА АНОМАЛИЯ",
                reply_markup=menu(uid)
            )
        except:
            pass

# ================== FIX / DECRYPT ==================
async def on_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    aid = int(q.data.split(":")[1])
    conn = db()
    conn.execute(
        "UPDATE anomalies SET fixed_at=?, status='FIXED' WHERE id=?",
        (int(time.time()), aid)
    )
    conn.commit()
    add_points(conn, q.from_user.id, 2)
    await q.edit_message_text(
        hdr() + "🟢 ФИКСАЦИЯ ПРИНЯТА\n⏳ Ожидание 10 минут",
        reply_markup=menu(q.from_user.id)
    )

async def on_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    aid = int(q.data.split(":")[1])
    conn = db()
    kind, payload = conn.execute(
        "SELECT kind, payload FROM anomalies WHERE id=?",
        (aid,)
    ).fetchone()

    if kind == "S_AUDIO":
        await context.bot.send_audio(
            q.from_user.id,
            payload,
            caption=hdr() + "🟥 CLASS S // ARCHIVE SIGNAL"
        )
        reward = 5
    else:
        await context.bot.send_message(
            q.from_user.id,
            hdr() + payload
        )
        reward = 3

    conn.execute(
        "UPDATE anomalies SET status='DECRYPTED' WHERE id=?",
        (aid,)
    )
    conn.commit()
    add_points(conn, q.from_user.id, reward)

    await q.edit_message_text(
        hdr() + f"🟢 РАСШИФРОВКА ЗАВЕРШЕНА\n▶ +{reward} ОЧКОВ",
        reply_markup=menu(q.from_user.id)
    )

# ================== AUDIO ==================
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in WAITING_AUDIO:
        return

    if update.message.audio:
        fid = update.message.audio.file_id
        title = update.message.audio.title or "S_SIGNAL"
    elif update.message.voice:
        fid = update.message.voice.file_id
        title = "S_SIGNAL_VOICE"
    else:
        return

    conn = db()
    add_s_audio(conn, title, fid)
    WAITING_AUDIO.remove(uid)

    await update.message.reply_text(
        hdr() + "🟢 S-СИГНАЛ ДОБАВЛЕН",
        reply_markup=menu(uid)
    )

# ================== APP ==================
def build_app():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_fix, pattern=r"^FIX"))
    app.add_handler(CallbackQueryHandler(on_decrypt, pattern=r"^DEC"))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, on_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

if __name__ == "__main__":
    application = build_app()

    # 🔔 ежедневный официальный бюллетень
    application.job_queue.run_repeating(
        send_daily_bulletin,
        interval=24 * 3600,
        first=300
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{BASE_URL}/telegram",
        drop_pending_updates=True
    )

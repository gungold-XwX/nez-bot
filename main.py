import os
import sqlite3
import random
import time
import re
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================== CONFIG ==================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "10000"))

DB_PATH = os.environ.get("DB_PATH", "/var/data/nez.db")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ================== STYLE ==================
def hdr():
    return "● NEZ PROJECT — EDEN-0 ACCESS\n"

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT UNIQUE,
        points INTEGER DEFAULT 0,
        created_at INTEGER
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        kind TEXT,
        payload TEXT,
        status TEXT,
        created_at INTEGER,
        fixed_at INTEGER
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS s_audio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT
    )""")
    conn.commit()
    return conn

# ================== USERS ==================
def get_user(conn, uid):
    return conn.execute(
        "SELECT user_id, username, points, created_at FROM users WHERE user_id=?",
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

def ordered_users(conn):
    return conn.execute(
        "SELECT user_id, username, points FROM users ORDER BY points DESC, created_at ASC"
    ).fetchall()

def queue_position(conn, uid) -> Tuple[int, int]:
    ids = [r[0] for r in ordered_users(conn)]
    total = len(ids)
    return (ids.index(uid) + 1, total) if uid in ids else (total + 1, total)

# ================== S AUDIO ==================
def add_s_audio(conn, fid):
    conn.execute("INSERT INTO s_audio (file_id) VALUES (?)", (fid,))
    conn.commit()

def random_s_audio(conn) -> Optional[str]:
    row = conn.execute(
        "SELECT file_id FROM s_audio ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    return row[0] if row else None

# ================== ANOMALIES ==================
NOCLASS_TEXT = [
    "данные повреждены",
    "содержимое утеряно",
    "шум сигнала",
]

def create_anomaly(conn, uid, kind, payload):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, status, created_at)
    VALUES (?, ?, ?, 'NEW', ?)
    """, (uid, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, uid):
    return conn.execute("""
    SELECT id, kind, payload, status, fixed_at
    FROM anomalies
    WHERE user_id=? AND status IN ('NEW','FIXED')
    ORDER BY created_at DESC
    LIMIT 1
    """, (uid,)).fetchone()

# ================== UI ==================
def menu(uid):
    rows = [
        [InlineKeyboardButton("Очередь", callback_data="Q")],
        [InlineKeyboardButton("Активный пакет", callback_data="A")],
        [InlineKeyboardButton("Рейтинг", callback_data="TOP")],
        [InlineKeyboardButton("Помощь", callback_data="HELP")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("＋ Добавить S", callback_data="ADD_S")])
        rows.append([InlineKeyboardButton("⚠ Запустить пакет", callback_data="ADMIN_PUSH")])
    return InlineKeyboardMarkup(rows)

# ================== STATES ==================
WAIT_USERNAME = set()
S_MODE = set()

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,20}$")

# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    uid = update.effective_user.id
    user = get_user(conn, uid)

    if user:
        pos, total = queue_position(conn, uid)
        await update.message.reply_text(
            hdr() +
            f"ID: {user[1]}\n"
            f"Позиция: {pos}/{total}\n"
            f"Индекс допуска: {user[2]}",
            reply_markup=menu(uid)
        )
        return

    WAIT_USERNAME.add(uid)
    await update.message.reply_text(
        hdr() +
        "Регистрация доступа.\n\n"
        "Введите ID (латиница, 3–20):"
    )

# ================== TEXT ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in WAIT_USERNAME:
        name = update.message.text.strip()
        if not USERNAME_RE.match(name):
            await update.message.reply_text("Неверный формат. Попробуйте снова.")
            return

        conn = db()
        if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            await update.message.reply_text("ID уже занят.")
            return

        create_user(conn, uid, name)
        WAIT_USERNAME.remove(uid)

        pos, total = queue_position(conn, uid)
        await update.message.reply_text(
            hdr() +
            f"Доступ активирован.\n"
            f"ID: {name}\n"
            f"Позиция: {pos}/{total}",
            reply_markup=menu(uid)
        )

# ================== CALLBACKS ==================
async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    conn = db()

    if q.data == "HELP":
        await q.edit_message_text(
            hdr() +
            "Вы зарегистрированы в цифровой очереди в Нулевой Эдем (EDEN-0).\n\n"
            "• Несколько раз за день NEZ Project отправляет на ваш терминал пакеты данных\n"
            "• Подтверждение и расшифровка пакетов данных повышают ваш индекс допуска\n"
            "• Чем быстрее вы подтвердите и расшифруете присланный пакет данных, тем сильнее повысится ваш индекс допуска\n"
            "• Чем выше индекс допуска — тем выше ваша позиция в очереди\n"
            "• Обладатели первых трех позиций в очереди будут отмечены публично",
            reply_markup=menu(uid)
        )

    elif q.data == "Q":
        user = get_user(conn, uid)
        pos, total = queue_position(conn, uid)
        await q.edit_message_text(
            hdr() +
            f"ID: {user[1]}\n"
            f"Позиция: {pos}/{total}\n"
            f"Индекс допуска: {user[2]}",
            reply_markup=menu(uid)
        )

    elif q.data == "TOP":
        rows = ordered_users(conn)[:10]
        text = hdr() + "Топ доступа:\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{i}. {r[1]} — {r[2]}\n"
        await q.edit_message_text(text, reply_markup=menu(uid))

    elif q.data == "A":
        a = get_active_anomaly(conn, uid)
        if not a:
            await q.edit_message_text("Активных пакетов нет.", reply_markup=menu(uid))
            return

        aid, kind, payload, status, fixed_at = a

        if status == "NEW":
            conn.execute(
                "UPDATE anomalies SET status='FIXED', fixed_at=? WHERE id=?",
                (int(time.time()), aid)
            )
            conn.commit()
            add_points(conn, uid, 1)
            await q.edit_message_text(
                "Пакет подтверждён.\nОжидайте 10 минут.",
                reply_markup=menu(uid)
            )
        else:
            if time.time() - fixed_at < 600:
                await q.edit_message_text("Стабилизация…", reply_markup=menu(uid))
            else:
                if kind == "S":
                    await context.bot.send_audio(uid, payload)
                    add_points(conn, uid, 4)
                else:
                    await context.bot.send_message(uid, payload)
                    add_points(conn, uid, 2)

                conn.execute(
                    "UPDATE anomalies SET status='DONE' WHERE id=?",
                    (aid,)
                )
                conn.commit()

                await q.edit_message_text(
                    "Пакет расшифрован.",
                    reply_markup=menu(uid)
                )

    elif q.data == "ADD_S" and uid == ADMIN_ID:
        S_MODE.add(uid)
        await q.edit_message_text(
            "Режим добавления S активен.\nОтправляйте аудио.",
            reply_markup=menu(uid)
        )

    elif q.data == "ADMIN_PUSH" and uid == ADMIN_ID:
        await spawn_anomalies(context)
        await q.edit_message_text("Пакеты отправлены.", reply_markup=menu(uid))

# ================== AUDIO ==================
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in S_MODE:
        return

    fid = update.message.audio.file_id if update.message.audio else update.message.voice.file_id
    conn = db()
    add_s_audio(conn, fid)

    await update.message.reply_text("S добавлен.")

# ================== SPAWN ==================
async def spawn_anomalies(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = ordered_users(conn)

    for uid, _, _ in users:
        if random.random() < 0.25:
            fid = random_s_audio(conn)
            if fid:
                create_anomaly(conn, uid, "S", fid)
                continue
        create_anomaly(conn, uid, "N", random.choice(NOCLASS_TEXT))

        try:
            await context.bot.send_message(uid, "Новый пакет доступен.")
        except:
            pass

# ================== APP ==================
def build_app():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, on_audio))
    return app

if __name__ == "__main__":
    application = build_app()

    if BASE_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{BASE_URL.rstrip('/')}/telegram"
        )
    else:
        application.run_polling()


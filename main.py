import os
import sqlite3
import random
import time
import re
from typing import Tuple, Optional

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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nez.db"

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
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
    conn.execute("""
    CREATE TABLE IF NOT EXISTS s_audio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        file_id TEXT
    )
    """)
    conn.commit()
    return conn

# ================== USERS ==================
def get_user(conn, user_id: int):
    return conn.execute(
        "SELECT user_id, username, points FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

def create_user(conn, user_id: int, username: str):
    conn.execute(
        "INSERT INTO users VALUES (?, ?, 0, ?)",
        (user_id, username, int(time.time()))
    )
    conn.commit()

def add_points(conn, user_id: int, delta: int):
    conn.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (delta, user_id)
    )
    conn.commit()

def all_users(conn):
    return conn.execute("SELECT user_id, username, points FROM users").fetchall()

def queue_position(conn, user_id: int) -> Tuple[int, int]:
    ids = [r[0] for r in conn.execute(
        "SELECT user_id FROM users ORDER BY points DESC, created_at ASC"
    ).fetchall()]
    total = len(ids)
    return (ids.index(user_id) + 1, total) if user_id in ids else (total + 1, total)

# ================== VALIDATION ==================
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,20}$")

def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))

# ================== S AUDIO ==================
def add_s_audio(conn, title: str, file_id: str):
    conn.execute(
        "INSERT INTO s_audio (title, file_id) VALUES (?, ?)",
        (title[:60], file_id)
    )
    conn.commit()

def random_s_audio(conn) -> Optional[Tuple[str, str]]:
    return conn.execute(
        "SELECT title, file_id FROM s_audio ORDER BY RANDOM() LIMIT 1"
    ).fetchone()

# ================== ANOMALIES ==================
NOCLASS = [
    "…ещё один выстрел в моей груди — выцвел ░▒▒",
    "я уже отмечен кровью / слезами звёзд ✶⋯",
    "вновь-и-вновь ░ молиться звёздам ▒▒▒",
    "я молил о жизни — вечность… вечность… ⌁",
    "приходи меня убить ░ завтра в 05:00",
    "softscars ▒▒▒",
    "⛧ ░▒▒░ ▒░░░ ░▒ ▒▒░░ ⛧",
    "▒░▒▒░░▒▒▒░▒░░▒▒░▒░▒░░▒▒▒░░▒▒░",
    "СОДЕРЖИМОЕ УТЕРЯНО",
    "d i a b l o s ⛧░",
    "РАСШИФРОВКА ПРЕРВАНА ░ данные не подлежат восстановлению",
    "Данные повреждены. Класс не присвоен.",
]

def create_anomaly(conn, user_id: int, kind: str, payload: str):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, created_at, status)
    VALUES (?, ?, ?, ?, 'SENT')
    """, (user_id, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, user_id: int):
    return conn.execute("""
    SELECT id, kind, payload, fixed_at, status
    FROM anomalies
    WHERE user_id=? AND status IN ('SENT','FIXED')
    ORDER BY created_at DESC LIMIT 1
    """, (user_id,)).fetchone()

def fix_anomaly(conn, aid: int):
    conn.execute(
        "UPDATE anomalies SET fixed_at=?, status='FIXED' WHERE id=?",
        (int(time.time()), aid)
    )
    conn.commit()

def decrypt_anomaly(conn, aid: int):
    conn.execute(
        "UPDATE anomalies SET decrypted_at=?, status='DECRYPTED' WHERE id=?",
        (int(time.time()), aid)
    )
    conn.commit()

# ================== UI ==================
def hdr():
    return "NEZ PROJECT × GOV\nDIGITAL ACCESS QUEUE\n\n"

def menu(uid: int):
    rows = [
        [InlineKeyboardButton("СТАТУС ОЧЕРЕДИ", callback_data="Q")],
        [InlineKeyboardButton("АКТИВНАЯ АНОМАЛИЯ", callback_data="A")],
        [InlineKeyboardButton("РЕЙТИНГ", callback_data="TOP")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("ДОБАВИТЬ S-СИГНАЛ", callback_data="ADD_S")])
    return InlineKeyboardMarkup(rows)

def fix_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ЗАФИКСИРОВАТЬ АНОМАЛИЮ", callback_data=f"FIX:{aid}")]
    ])

def decrypt_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("РАСШИФРОВАТЬ ДАННЫЕ", callback_data=f"DEC:{aid}")]
    ])

# ================== STATE ==================
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
            f"Идентификатор: {u[1]}\n"
            f"Позиция в очереди: {pos}/{total}\n\n"
            "Доступ активен.",
            reply_markup=menu(uid)
        )
        return

    WAITING_USERNAME.add(uid)
    await update.message.reply_text(
        hdr() +
        "РЕГИСТРАЦИЯ ДОСТУПА\n\n"
        "Вы регистрируетесь в цифровой очереди Нулевого Эдема.\n\n"
        "ВНИМАНИЕ:\n"
        "• пользователи на первых позициях будут публично отмечены\n"
        "• поздравление проводит глава NEZ на специальном мероприятии\n"
        "• допускаются только корректные идентификаторы\n"
        "• идентификатор не подлежит изменению\n\n"
        "Требования к идентификатору:\n"
        "латиница, цифры, . _ -\n"
        "длина: 3–20 символов\n\n"
        "Введите идентификатор:",
    )

# ================== USERNAME INPUT ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in WAITING_USERNAME:
        name = update.message.text.strip()

        if not valid_username(name):
            await update.message.reply_text(
                hdr() +
                "ОШИБКА ВАЛИДАЦИИ\n"
                "Идентификатор не соответствует требованиям.\n"
                "Попробуйте снова."
            )
            return

        conn = db()
        create_user(conn, uid, name)
        WAITING_USERNAME.remove(uid)

        pos, total = queue_position(conn, uid)
        await update.message.reply_text(
            hdr() +
            "РЕГИСТРАЦИЯ ЗАВЕРШЕНА\n\n"
            f"Идентификатор: {name}\n"
            f"Позиция в очереди: {pos}/{total}\n\n"
            "Доступ активирован.",
            reply_markup=menu(uid)
        )
        return

# ================== CALLBACKS ==================
async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    conn = db()

    if q.data == "Q":
        pos, total = queue_position(conn, uid)
        await q.edit_message_text(
            hdr() + f"ПОЗИЦИЯ В ОЧЕРЕДИ\n\n{pos} / {total}",
            reply_markup=menu(uid)
        )

    elif q.data == "A":
        a = get_active_anomaly(conn, uid)
        if not a:
            await q.edit_message_text(
                hdr() + "АКТИВНЫЕ АНОМАЛИИ ОТСУТСТВУЮТ",
                reply_markup=menu(uid)
            )
            return

        aid, kind, payload, fixed_at, status = a
        if status == "SENT":
            await q.edit_message_text(
                hdr() + "ОБНАРУЖЕНА АНОМАЛИЯ\nТребуется фиксация.",
                reply_markup=fix_kb(aid)
            )
        else:
            if time.time() - fixed_at < 600:
                await q.edit_message_text(
                    hdr() + "АНОМАЛИЯ ЗАФИКСИРОВАНА\nОжидание перед расшифровкой.",
                    reply_markup=menu(uid)
                )
            else:
                await q.edit_message_text(
                    hdr() + "АНОМАЛИЯ ГОТОВА К РАСШИФРОВКЕ",
                    reply_markup=decrypt_kb(aid)
                )

    elif q.data == "TOP":
        rows = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()
        text = hdr() + "РЕЙТИНГ ДОСТУПА\n\n"
        for i, (name, pts) in enumerate(rows, 1):
            text += f"{i}. {name} — {pts}\n"
        await q.edit_message_text(text, reply_markup=menu(uid))

    elif q.data == "ADD_S" and uid == ADMIN_ID:
        WAITING_AUDIO.add(uid)
        await q.edit_message_text(
            hdr() +
            "РЕЖИМ ДОБАВЛЕНИЯ S-СИГНАЛА\n\n"
            "Пришлите аудио-файл.\n"
            "Следующий сигнал будет сохранён."
        )

# ================== FIX / DECRYPT ==================
async def on_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    aid = int(q.data.split(":")[1])
    conn = db()
    fix_anomaly(conn, aid)
    add_points(conn, q.from_user.id, 2)
    await q.edit_message_text(
        hdr() + "ФИКСАЦИЯ ПРИНЯТА\nОжидайте 10 минут.",
        reply_markup=menu(q.from_user.id)
    )

async def on_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    aid = int(q.data.split(":")[1])
    conn = db()

    kind, payload = conn.execute(
        "SELECT kind, payload FROM anomalies WHERE id=?",
        (aid,)
    ).fetchone()

    decrypt_anomaly(conn, aid)

    if kind == "S_AUDIO":
        await context.bot.send_audio(
            chat_id=q.from_user.id,
            audio=payload,
            caption=hdr() + "CLASS S\nАРХИВНЫЙ СИГНАЛ"
        )
        reward = 5
    else:
        await context.bot.send_message(
            q.from_user.id,
            hdr() + payload
        )
        reward = 3

    add_points(conn, q.from_user.id, reward)
    await q.edit_message_text(
        hdr() + f"РАСШИФРОВКА ЗАВЕРШЕНА\n+{reward} ОЧКОВ",
        reply_markup=menu(q.from_user.id)
    )

# ================== AUDIO UPLOAD ==================
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
        hdr() + "S-СИГНАЛ СОХРАНЁН",
        reply_markup=menu(uid)
    )

# ================== JOB ==================
async def spawn_anomalies(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    for uid, _, pts in all_users(conn):
        pos, total = queue_position(conn, uid)
        chance = 0.15 + (1 - pos / max(1, total)) * 0.6

        if random.random() < chance:
            row = random_s_audio(conn)
            if row:
                title, fid = row
                kind, payload = "S_AUDIO", fid
            else:
                kind, payload = "NOCLASS", random.choice(NOCLASS)
        else:
            kind, payload = "NOCLASS", random.choice(NOCLASS)

        create_anomaly(conn, uid, kind, payload)
        try:
            await context.bot.send_message(
                uid,
                hdr() + "ЗАФИКСИРОВАНА АНОМАЛЬНАЯ АКТИВНОСТЬ",
                reply_markup=menu(uid)
            )
        except:
            pass

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
    application.job_queue.run_repeating(spawn_anomalies, interval=8 * 3600, first=120)

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

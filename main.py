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

# ================== STYLE ==================
LINE = "━━━━━━━━━━━━━━━━━━"

def hdr():
    return (
        "🔴 NEZ PROJECT × GOV\n"
        "▶ EDEN-0 ACCESS QUEUE TERMINAL\n"
        f"{LINE}\n"
    )

def footer_hint():
    return "\n▶ Используйте меню ниже."

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
        "SELECT user_id, username, points, created_at FROM users WHERE user_id=?",
        (uid,)
    ).fetchone()

def create_user(conn, uid, name):
    conn.execute(
        "INSERT INTO users (user_id, username, points, created_at) VALUES (?, ?, 0, ?)",
        (uid, name, int(time.time()))
    )
    conn.commit()

def add_points(conn, uid, pts):
    conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (pts, uid))
    conn.commit()

def all_users(conn):
    return conn.execute("SELECT user_id, username, points FROM users").fetchall()

def leaderboard(conn, limit=10):
    return conn.execute(
        "SELECT username, points FROM users ORDER BY points DESC, created_at ASC LIMIT ?",
        (limit,)
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
    conn.execute("INSERT INTO s_audio (title, file_id) VALUES (?, ?)", (title[:60], fid))
    conn.commit()

def random_s_audio(conn) -> Optional[Tuple[str, str]]:
    return conn.execute(
        "SELECT title, file_id FROM s_audio ORDER BY RANDOM() LIMIT 1"
    ).fetchone()

# ================== ANOMALIES ==================
NOCLASS = [
    "▒▒▒ СОДЕРЖИМОЕ УТЕРЯНО ▒▒▒",
    "РАСШИФРОВКА ПРЕРВАНА ░ ДАННЫЕ НЕВОССТАНОВИМЫ",
    "ДАННЫЕ ПОВРЕЖДЕНЫ. КЛАСС НЕ ПРИСВОЕН.",
    "⛧ ░▒▒░ ▒░░░ ░▒ ▒▒░░ ⛧",
    "▒░▒▒░░▒▒▒░▒░░▒▒░▒░▒░░▒▒▒░░▒▒░",
]

def create_anomaly(conn, uid, kind, payload):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, created_at, status)
    VALUES (?, ?, ?, ?, 'SENT')
    """, (uid, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, uid):
    return conn.execute("""
    SELECT id, kind, payload, created_at, fixed_at, status
    FROM anomalies
    WHERE user_id=? AND status IN ('SENT','FIXED')
    ORDER BY created_at DESC LIMIT 1
    """, (uid,)).fetchone()

# ================== BULLETIN ==================
def build_bulletin(conn):
    today = time.strftime("%d.%m.%Y")
    rows = leaderboard(conn, limit=10)

    text = (
        "🔴 NEZ PROJECT × GOV\n"
        "▶ OFFICIAL BULLETIN / EDEN-0\n"
        f"{LINE}\n"
        f"ДАТА: {today}\n\n"
        "СОСТОЯНИЕ СИСТЕМЫ:\n"
        "— активность третьего измерения повышена\n"
        "— очередь динамична (перерасчёт допуска)\n"
        "— зафиксированы новые пакеты данных\n\n"
        "ТОП ДОПУСКА:\n"
    )
    for i, (name, pts) in enumerate(rows, 1):
        tag = "  [CANDIDATE]" if i <= 3 else ""
        text += f"{i:02d}. {name} — {pts} IDx{tag}\n"

    text += "\n▶ Примечание: кандидаты TOP-3 будут отмечены на спец. мероприятии."
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
def menu(uid: int):
    rows = [
        [InlineKeyboardButton("🔵 ВАША ПОЗИЦИЯ В ОЧЕРЕДИ", callback_data="Q")],
        [InlineKeyboardButton("🔴 АКТИВНЫЙ ПАКЕТ", callback_data="A")],
        [InlineKeyboardButton("🏛 РЕЙТИНГ", callback_data="TOP")],
        #[InlineKeyboardButton("ℹ️ ПОМОЩЬ / ПРОТОКОЛ", callback_data="HELP")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("🔴 (ADMIN) ЗАПУСТИТЬ ПАКЕТ", callback_data="ADMIN_ANOM")])
        rows.append([InlineKeyboardButton("➕ (ADMIN) ДОБАВИТЬ S-СИГНАЛ", callback_data="ADD_S")])
    return InlineKeyboardMarkup(rows)

def confirm_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ПОДТВЕРДИТЬ ПОЛУЧЕНИЕ", callback_data=f"ACK:{aid}")]
    ])

def decrypt_kb(aid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 РАСШИФРОВАТЬ ПАКЕТ", callback_data=f"DEC:{aid}")]
    ])

WAITING_USERNAME = set()
WAITING_AUDIO = set()

# ================== HELP TEXT ==================
def help_text():
    return (
        hdr() +
        "ℹ️ ПРОТОКОЛ УЧАСТИЯ\n"
        f"{LINE}\n\n"
        "Что это:\n"
        "— цифровая очередь доступа к объекту EDEN-0\n"
        "— система ведёт ранжирование наблюдателей\n\n"
        "Почему вы продвигаетесь:\n"
        "— NEZ не выдаёт доступ всем сразу\n"
        "— очередь пересчитывается по Индексу допуска (IDx)\n"
        "— IDx растёт, когда вы быстро и корректно подтверждаете пакеты и проходите расшифровку\n\n"
        "Как действовать:\n"
        "1) появляется 🔴 АКТИВНЫЙ ПАКЕТ\n"
        "2) нажмите ✅ ПОДТВЕРДИТЬ ПОЛУЧЕНИЕ (это регистрация реакции наблюдателя)\n"
        "3) выдержите интервал ⏳ 10 минут\n"
        "4) нажмите 🔎 РАСШИФРОВАТЬ ПАКЕТ\n\n"
        "Классы:\n"
        "— NOCLASS: шум/обрывки\n"
        "— CLASS S: архивный сигнал (аудио). Чем выше очередь — тем выше шанс.\n\n"
        "Важно:\n"
        "— TOP позиции будут публично отмечены\n"
        "— поздравление проводит глава NEZ на спец. мероприятии\n"
    )

# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    uid = update.effective_user.id
    u = get_user(conn, uid)

    if u:
        pos, total = queue_position(conn, uid)
        await update.message.reply_text(
            hdr() +
            "🟢 ДОСТУП АКТИВЕН\n\n"
            f"ID: {u[1]}\n"
            f"Позиция: {pos} / {total}\n"
            f"Индекс допуска (IDx): {u[2]}\n"
            + footer_hint(),
            reply_markup=menu(uid)
        )
        return

    WAITING_USERNAME.add(uid)
    await update.message.reply_text(
        hdr() +
        "▶ ВЫ СОБИРАЕТЕСЬ ЗАРЕГИСТРИРОВАТЬСЯ В ЦИФРОВОЙ ОЧЕРЕДИ В НУЛЕВОЙ ЭДЕМ\n\n"
        "Обладатели первых позиций в очереди будут публично отмечены на закрытой конференции NEZ Project 24.01.2026.\n\n"
        "Требования к ID:\n"
        "— латиница / цифры / . _ -\n"
        "— длина 3–20\n"
        "— ID невозможно изменить после регистрации\n\n"
        "Введите ID (пример: metaego):"
    )

# ================== TEXT INPUT ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in WAITING_USERNAME:
        return

    name = update.message.text.strip()
    if not USERNAME_RE.match(name):
        await update.message.reply_text(
            hdr() +
            "⛔ ОТКАЗ В РЕГИСТРАЦИИ\n"
            f"{LINE}\n"
            "ID не соответствует формату.\n"
            "Разрешено: a-z A-Z 0-9 _ . -\n"
            "Длина: 3–20\n\n"
            "Введите ID снова:"
        )
        return

    conn = db()
    # защита от повторов (чтобы не было одинаковых ников)
    exists = conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone()
    if exists:
        await update.message.reply_text(
            hdr() +
            "⛔ ОТКАЗ В РЕГИСТРАЦИИ\n"
            f"{LINE}\n"
            "ID уже занят.\n"
            "Введите другой:"
        )
        return

    create_user(conn, uid, name)
    WAITING_USERNAME.remove(uid)

    pos, total = queue_position(conn, uid)
    await update.message.reply_text(
        hdr() +
        "🟢 РЕГИСТРАЦИЯ ПРИНЯТА\n"
        f"{LINE}\n\n"
        f"ID: {name}\n"
        f"Позиция: {pos} / {total}\n"
        "Индекс допуска (IDx): 0\n\n"
        "▶ Рекомендуется открыть ℹ️ ПОМОЩЬ / ПРОТОКОЛ.",
        reply_markup=menu(uid)
    )

# ================== CALLBACKS ==================
async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    conn = db()

    if q.data == "HELP":
        await q.edit_message_text(help_text(), reply_markup=menu(uid))
        return

    if q.data == "Q":
        u = get_user(conn, uid)
        if not u:
            await q.edit_message_text(hdr() + "⛔ Требуется регистрация. Нажмите /start")
            return
        pos, total = queue_position(conn, uid)
        await q.edit_message_text(
            hdr() +
            "🔵 СТАТУС ОЧЕРЕДИ\n"
            f"{LINE}\n\n"
            f"ID: {u[1]}\n"
            f"Позиция: {pos} / {total}\n"
            f"Индекс допуска (IDx): {u[2]}\n\n"
            "▶ Чем выше IDx — тем выше шанс CLASS S.\n"
            + footer_hint(),
            reply_markup=menu(uid)
        )
        return

    if q.data == "TOP":
        rows = leaderboard(conn, limit=10)
        txt = hdr() + "🏛 РЕЙТИНГ ДОПУСКА\n" + f"{LINE}\n\n"
        for i, (n, p) in enumerate(rows, 1):
            mark = "  🟥CANDIDATE" if i <= 3 else ""
            txt += f"{i:02d}. {n} — {p} IDx{mark}\n"
        txt += "\n▶ TOP-3 отмечаются публично."
        await q.edit_message_text(txt, reply_markup=menu(uid))
        return

    if q.data == "A":
        a = get_active_anomaly(conn, uid)
        if not a:
            await q.edit_message_text(
                hdr() +
                "🟢 АКТИВНЫХ ПАКЕТОВ НЕТ\n"
                f"{LINE}\n"
                "Ожидайте следующее окно.",
                reply_markup=menu(uid)
            )
            return

        aid, kind, payload, created_at, fixed_at, status = a
        if status == "SENT":
            await q.edit_message_text(
                hdr() +
                "🔴 АКТИВНЫЙ ПАКЕТ ОБНАРУЖЕН\n"
                f"{LINE}\n\n"
                "▶ Действие: подтвердите получение.\n"
                "Это фиксирует вашу реакцию как наблюдателя.",
                reply_markup=confirm_kb(aid)
            )
            return

        # FIXED
        waited = int(time.time()) - int(fixed_at or 0)
        remaining = max(0, 600 - waited)
        if remaining > 0:
            await q.edit_message_text(
                hdr() +
                "🟠 ПОЛУЧЕНИЕ ПОДТВЕРЖДЕНО\n"
                f"{LINE}\n\n"
                f"⏳ Интервал стабилизации: {remaining//60} мин {remaining%60} сек\n"
                "▶ После истечения интервала станет доступна расшифровка.",
                reply_markup=menu(uid)
            )
        else:
            await q.edit_message_text(
                hdr() +
                "🔎 ПАКЕТ ГОТОВ К РАСШИФРОВКЕ\n"
                f"{LINE}\n\n"
                "▶ Действие: открыть содержимое пакета.",
                reply_markup=decrypt_kb(aid)
            )
        return

    if q.data == "ADD_S" and uid == ADMIN_ID:
        WAITING_AUDIO.add(uid)
        await q.edit_message_text(
            hdr() +
            "➕ (ADMIN) ДОБАВЛЕНИЕ CLASS S\n"
            f"{LINE}\n\n"
            "Отправьте аудио (mp3/voice).\n"
            "Следующее аудио будет сохранено как архивный сигнал."
        )
        return

    if q.data == "ADMIN_ANOM" and uid == ADMIN_ID:
        await admin_spawn(context)
        # мягкое подтверждение администратору
        await q.edit_message_text(
            hdr() +
            "🟢 (ADMIN) РАССЫЛКА ПАКЕТА ВЫПОЛНЕНА\n"
            f"{LINE}\n"
            "Пакеты отправлены всем активным наблюдателям.",
            reply_markup=menu(uid)
        )
        return

# ================== ACK / DECRYPT ==================
async def on_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    aid = int(q.data.split(":")[1])

    conn = db()
    # подтверждаем только если ещё SENT
    conn.execute(
        "UPDATE anomalies SET fixed_at=?, status='FIXED' WHERE id=? AND status='SENT'",
        (int(time.time()), aid)
    )
    conn.commit()

    # награда за подтверждение (минимальная)
    add_points(conn, uid, 2)

    await q.edit_message_text(
        hdr() +
        "🟠 ПОЛУЧЕНИЕ ПОДТВЕРЖДЕНО\n"
        f"{LINE}\n\n"
        "⏳ Требуется интервал стабилизации: 10 минут\n"
        "▶ Затем: РАСШИФРОВАТЬ ПАКЕТ\n\n"
        "✓ Индекс допуска: +2",
        reply_markup=menu(uid)
    )

async def on_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    aid = int(q.data.split(":")[1])
    conn = db()

    row = conn.execute(
        "SELECT kind, payload, fixed_at, status FROM anomalies WHERE id=? AND user_id=?",
        (aid, uid)
    ).fetchone()
    if not row:
        await q.edit_message_text(hdr() + "⛔ Пакет не найден.", reply_markup=menu(uid))
        return

    kind, payload, fixed_at, status = row
    if status != "FIXED":
        await q.edit_message_text(hdr() + "⛔ Расшифровка недоступна.", reply_markup=menu(uid))
        return

    waited = int(time.time()) - int(fixed_at or 0)
    if waited < 600:
        remaining = 600 - waited
        await q.edit_message_text(
            hdr() +
            "🟠 РЕЖИМ СТАБИЛИЗАЦИИ\n"
            f"{LINE}\n\n"
            f"⏳ Осталось: {remaining//60} мин {remaining%60} сек\n"
            "▶ Повторите попытку после истечения интервала.",
            reply_markup=menu(uid)
        )
        return

    # помечаем как DECRYPTED
    conn.execute(
        "UPDATE anomalies SET decrypted_at=?, status='DECRYPTED' WHERE id=?",
        (int(time.time()), aid)
    )
    conn.commit()

    if kind == "S_AUDIO":
        await context.bot.send_audio(
            chat_id=uid,
            audio=payload,
            caption=hdr() + "🟥 CLASS S // ARCHIVE SIGNAL\n" + f"{LINE}\n✓ Содержимое выдано отдельным пакетом."
        )
        reward = 5
        result_text = (
            hdr() +
            "🟥 CLASS S ПОДТВЕРЖДЁН\n"
            f"{LINE}\n\n"
            "✓ Архивный сигнал выдан отдельным сообщением.\n"
            f"✓ Индекс допуска: +{reward}"
        )
    else:
        reward = 3
        result_text = (
            hdr() +
            "🟢 РАСШИФРОВКА ВЫПОЛНЕНА\n"
            f"{LINE}\n\n"
            f"{payload}\n\n"
            f"✓ Индекс допуска: +{reward}"
        )

    add_points(conn, uid, reward)
    await q.edit_message_text(result_text, reply_markup=menu(uid))

# ================== ADMIN / SPAWN ==================
async def admin_spawn(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = all_users(conn)
    for uid, _, _ in users:
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
                hdr() +
                "🔴 НОВЫЙ ПАКЕТ ДАННЫХ\n"
                f"{LINE}\n\n"
                "▶ Откройте: 🔴 АКТИВНЫЙ ПАКЕТ\n"
                "▶ Затем подтвердите получение.",
                reply_markup=menu(uid)
            )
        except:
            pass

# ================== AUDIO UPLOAD ==================
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in WAITING_AUDIO:
        return

    if update.message.audio:
        fid = update.message.audio.file_id
        title = update.message.audio.title or (update.message.audio.file_name or "S_SIGNAL")
    elif update.message.voice:
        fid = update.message.voice.file_id
        title = "S_SIGNAL_VOICE"
    else:
        return

    conn = db()
    add_s_audio(conn, title, fid)
    WAITING_AUDIO.remove(uid)

    await update.message.reply_text(
        hdr() +
        "🟢 CLASS S ДОБАВЛЕН\n"
        f"{LINE}\n"
        f"Название: {title}\n"
        "✓ Сигнал сохранён в архив.",
        reply_markup=menu(uid)
    )

# ================== APP ==================
def build_app():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(on_ack, pattern=r"^ACK:"))
    app.add_handler(CallbackQueryHandler(on_decrypt, pattern=r"^DEC:"))
    app.add_handler(CallbackQueryHandler(on_click))

    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, on_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

if __name__ == "__main__":
    application = build_app()

    # 🔔 ежедневный официальный бюллетень (1 раз / 24ч)
    application.job_queue.run_repeating(
        send_daily_bulletin,
        interval=24 * 3600,
        first=300
    )

    if BASE_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{BASE_URL.rstrip('/')}/telegram",
            drop_pending_updates=True
        )
    else:
        application.run_polling(drop_pending_updates=True)

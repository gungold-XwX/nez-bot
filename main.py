import os
import sqlite3
import random
import time
import re
from typing import Optional, Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

# Scheduling
TZ = ZoneInfo("Europe/Amsterdam")
PACKETS_PER_DAY = 3
SCHEDULE_ANCHOR_HOUR = 0      # schedule daily shortly after midnight
SCHEDULE_ANCHOR_MINUTE = 5

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
    # file_id UNIQUE to avoid duplicates
    conn.execute("""
    CREATE TABLE IF NOT EXISTS s_audio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT UNIQUE
    )""")
    # store last day we scheduled random packet jobs (so restarts won't duplicate)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scheduler_meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )""")
    conn.commit()
    return conn

def get_meta(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT v FROM scheduler_meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None

def set_meta(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO scheduler_meta (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value)
    )
    conn.commit()

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

def queue_neighbors(conn, uid, window: int = 2):
    rows = ordered_users(conn)  # (user_id, username, points)
    ids = [r[0] for r in rows]
    if uid not in ids:
        return [], []
    i = ids.index(uid)
    above = rows[max(0, i - window): i]
    below = rows[i + 1: i + 1 + window]
    return above, below

# ================== S AUDIO ==================
def add_s_audio(conn, fid: str) -> bool:
    """
    Returns True if inserted, False if duplicate.
    """
    try:
        conn.execute("INSERT INTO s_audio (file_id) VALUES (?)", (fid,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def count_s_audio(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM s_audio").fetchone()
    return int(row[0]) if row else 0

def random_s_audio(conn) -> Optional[str]:
    row = conn.execute(
        "SELECT file_id FROM s_audio ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    return row[0] if row else None

# ================== ANOMALIES ==================
NOCLASS_TEXT = [
    "Пакет расшифрован: данные повреждены",
    "Пакет расшифрован: содержимое утеряно",
]

LORE_SNIPPETS = [
    "01 — EVENT\n…мир пережил событие X, приведшее к необратимым изменениям глобального порядка…",
    "02 — SCARCITY\n…ресурсов земли стало не хватать для обеспечения населения…",
    "03 — DISASTER\n…усилились частота и масштаб природных катастроф…",
    "04 — COLLAPSE\n…на фоне кризиса возникла фаза затяжных вооружённых конфликтов и анархии…",
    "05 — INTERIM\n…после периода хаоса было сформировано временное правительство…",
    "06 — STABILITY\n…глобально ситуация стабилизировалась на минимально допустимом уровне…",
    "07 — NEZ\n…в рамках антикризисных мер была учреждена корпорация NEZ project…",
    "08 — MISSION\n…основная задача: сбор, анализ и систематизация данных о феномене «нулевой эдем» и активности так называемого третьего измерения…",
    "09 — EDEN\n…нулевой эдем — отдельное измерение, представляющее собой структурный двойник земли. доступ осуществляется через пространственно-временной разлом. изначально нулевой эдем рассматривался как «обнулённая» версия мира, потенциально пригодная для переселения человечества…",
    "10 — QUEUE\n…согласно ранним данным, нулевой эдем являлся чистой, восстановленной формой земли. после определения даты стабильного открытия разлома NEZ project инициировал создание цифровой очереди для населения с целью контролируемого доступа в новое измерение…",
]

def create_anomaly(conn, uid, kind, payload):
    conn.execute("""
    INSERT INTO anomalies (user_id, kind, payload, status, created_at)
    VALUES (?, ?, ?, 'NEW', ?)
    """, (uid, kind, payload, int(time.time())))
    conn.commit()

def get_active_anomaly(conn, uid):
    return conn.execute("""
    SELECT id, kind, payload, status, fixed_at, created_at
    FROM anomalies
    WHERE user_id=? AND status IN ('NEW','FIXED')
    ORDER BY created_at DESC
    LIMIT 1
    """, (uid,)).fetchone()

# always keep only 1 active packet per user
def expire_active_anomalies(conn, uid):
    conn.execute(
        "UPDATE anomalies SET status='EXPIRED' WHERE user_id=? AND status IN ('NEW','FIXED')",
        (uid,)
    )
    conn.commit()

# ================== SCORE (FAST CONFIRM) ==================
def confirm_points(elapsed_sec: int) -> int:
    if elapsed_sec <= 5:
        return 8
    if elapsed_sec <= 10:
        return 7
    if elapsed_sec <= 20:
        return 6
    if elapsed_sec <= 30:
        return 5
    if elapsed_sec <= 45:
        return 4
    if elapsed_sec <= 60:
        return 3
    if elapsed_sec <= 120:
        return 2
    return 1

# ================== UI ==================
def menu(uid):
    rows = [
        [InlineKeyboardButton("Позиция в очереди", callback_data="Q")],
        [InlineKeyboardButton("Активный пакет данных", callback_data="A")],
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
        "Вы регистрируетесь в цифровой очереди в Нулевой Эдем (EDEN-0).\n\n"
        "ID обладателей первых трех позиций в очереди будут публично отмечены на специальной конференции NEZ Project 24.01.26.\n\n"
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
            "• Обладатели первых трех позиций в очереди будут отмечены публично на специальной конференции NEZ Project 24.01.26. metaego-asterasounds2401.ticketscloud.org",
            reply_markup=menu(uid)
        )

    elif q.data == "Q":
        user = get_user(conn, uid)
        pos, total = queue_position(conn, uid)

        above, below = queue_neighbors(conn, uid, window=2)
        neigh = ""
        if above or below:
            neigh += "\n\nСоседи:\n"
            if above:
                for r in above:
                    neigh += f"▲ {r[1]} — {r[2]}\n"
            if below:
                for r in below:
                    neigh += f"▼ {r[1]} — {r[2]}\n"

        await q.edit_message_text(
            hdr() +
            f"ID: {user[1]}\n"
            f"Позиция: {pos}/{total}\n"
            f"Индекс допуска: {user[2]}"
            + neigh,
            reply_markup=menu(uid)
        )

    elif q.data == "TOP":
        rows = ordered_users(conn)[:10]
        text = hdr() + "Обладатели первых позиций в очереди:\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{i}. {r[1]} — {r[2]}\n"
        await q.edit_message_text(text, reply_markup=menu(uid))

    elif q.data == "A":
        a = get_active_anomaly(conn, uid)
        if not a:
            await q.edit_message_text("Вы еще не получили новый пакет данных от NEZ Project.", reply_markup=menu(uid))
            return

        aid, kind, payload, status, fixed_at, created_at = a

        if status == "NEW":
            now = int(time.time())
            elapsed = max(0, now - int(created_at or now))
            pts = confirm_points(elapsed)

            conn.execute(
                "UPDATE anomalies SET status='FIXED', fixed_at=? WHERE id=?",
                (now, aid)
            )
            conn.commit()

            add_points(conn, uid, pts)

            await q.edit_message_text(
                "Вы подтвердили получение нового пакета данных от NEZ Project.\nРасшифровка пакета займет 1 минуту.",
                reply_markup=menu(uid)
            )
        else:
            if time.time() - fixed_at < 60:
                await q.edit_message_text("Происходит расшифровка пакета данных… Пожалуйста, подождите.", reply_markup=menu(uid))
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
        total_s = count_s_audio(conn)
        await q.edit_message_text(
            "Режим добавления S активен.\nОтправляйте аудио.",
            reply_markup=menu(uid)
        )
        # не меняем UI-экран, но админ получит цифру отдельным сообщением
        try:
            await context.bot.send_message(uid, f"Всего S: {total_s}")
        except:
            pass

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
    inserted = add_s_audio(conn, fid)
    total_s = count_s_audio(conn)

    if inserted:
        await update.message.reply_text(f"S добавлен.\nВсего S: {total_s}")
    else:
        await update.message.reply_text(f"S уже существует.\nВсего S: {total_s}")

# ================== SPAWN ==================
async def spawn_anomalies(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = ordered_users(conn)

    for uid, _, _ in users:
        expire_active_anomalies(conn, uid)

        # 25% try S (if there is any S)
        if random.random() < 0.25:
            fid = random_s_audio(conn)
            if fid:
                create_anomaly(conn, uid, "S", fid)
                try:
                    await context.bot.send_message(uid, "Новый пакет данных от NEZ Project доступен.")
                except:
                    pass
                continue

        # Not S -> 50% NOCLASS, 50% LORE
        if random.random() < 0.5:
            payload = random.choice(NOCLASS_TEXT)
        else:
            payload = random.choice(LORE_SNIPPETS)

        create_anomaly(conn, uid, "N", payload)

        try:
            await context.bot.send_message(uid, "Новый пакет данных от NEZ Project доступен.")
        except:
            pass

# ================== AUTO SCHEDULING (3 random times/day) ==================
def _today_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def _pick_random_times_for_date(date_local: datetime, n: int) -> list[datetime]:
    minutes = random.sample(range(0, 24 * 60), k=n)
    minutes.sort()
    out = []
    for m in minutes:
        hh = m // 60
        mm = m % 60
        out.append(date_local.replace(hour=hh, minute=mm, second=0, microsecond=0))
    return out

def schedule_packets_for_today(app: Application):
    conn = db()
    now_local = datetime.now(TZ)
    key = _today_key(now_local)
    last = get_meta(conn, "last_scheduled_day")

    if last == key:
        return  # already scheduled today

    set_meta(conn, "last_scheduled_day", key)

    date_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    targets = _pick_random_times_for_date(date_local, PACKETS_PER_DAY)

    for i, target_local in enumerate(targets, 1):
        delay = (target_local - now_local).total_seconds()
        if delay <= 0:
            continue
        app.job_queue.run_once(
            callback=spawn_anomalies,
            when=delay,
            name=f"daily_packets_{key}_{i}"
        )

def seconds_until_next_anchor(now_local: datetime) -> float:
    anchor_today = now_local.replace(
        hour=SCHEDULE_ANCHOR_HOUR,
        minute=SCHEDULE_ANCHOR_MINUTE,
        second=0,
        microsecond=0
    )
    if now_local < anchor_today:
        return (anchor_today - now_local).total_seconds()

    anchor_next = anchor_today + timedelta(days=1)
    return (anchor_next - now_local).total_seconds()

async def daily_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    schedule_packets_for_today(app)

    now_local = datetime.now(TZ)
    delay = seconds_until_next_anchor(now_local)
    app.job_queue.run_once(daily_scheduler_job, when=delay, name="daily_scheduler")

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

    # schedule packets for today on boot (if not already)
    schedule_packets_for_today(application)

    # schedule daily scheduler (runs every day ~00:05 Amsterdam)
    now_local = datetime.now(TZ)
    first_delay = seconds_until_next_anchor(now_local)
    application.job_queue.run_once(daily_scheduler_job, when=first_delay, name="daily_scheduler")

    if BASE_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{BASE_URL.rstrip('/')}/telegram"
        )
    else:
        application.run_polling()

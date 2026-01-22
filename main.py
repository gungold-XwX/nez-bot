import os
import sqlite3
import random
import time
import re
import math
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
SCHEDULE_ANCHOR_HOUR = 0
SCHEDULE_ANCHOR_MINUTE = 5

# Activity ranking (decay)
ACTIVITY_HALF_LIFE_DAYS = 3  # активность “вдвое” тухнет за 3 дня

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ================== STYLE ==================
def hdr():
    return "● NEZ PROJECT — EDEN-0 ACCESS\n"

def access_level(points: int) -> str:
    if points > 500:
        return "SSS"
    if points >= 400:
        return "SS"
    if points >= 300:
        return "A"
    if points >= 200:
        return "B"
    if points >= 100:
        return "C"
    if points >= 50:
        return "D"
    return "E"

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
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
        file_id TEXT UNIQUE
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scheduler_meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS username_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        old_username TEXT,
        new_username TEXT,
        status TEXT,
        created_at INTEGER
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_limits (
        user_id INTEGER PRIMARY KEY,
        username_change_used INTEGER DEFAULT 0
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_activity (
        user_id INTEGER PRIMARY KEY,
        score REAL DEFAULT 0,
        updated_at INTEGER
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

# ================== FREEZE (QUEUE LOCK) ==================
FREEZE_KEY = "queue_frozen"          # "1" / "0"
FREEZE_TS_KEY = "queue_frozen_ts"    # unix ts

def is_frozen(conn) -> Tuple[bool, Optional[int]]:
    v = get_meta(conn, FREEZE_KEY)
    if v == "1":
        ts = get_meta(conn, FREEZE_TS_KEY)
        try:
            return True, int(ts) if ts else None
        except:
            return True, None
    return False, None

def set_frozen(conn, frozen: bool):
    if frozen:
        set_meta(conn, FREEZE_KEY, "1")
        set_meta(conn, FREEZE_TS_KEY, str(int(time.time())))
    else:
        set_meta(conn, FREEZE_KEY, "0")
        set_meta(conn, FREEZE_TS_KEY, "")

def freeze_banner(conn) -> str:
    frozen, ts = is_frozen(conn)
    if not frozen:
        return ""
    stamp = ""
    if ts:
        try:
            stamp = datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M:%S %Z")
        except:
            stamp = ""
    if stamp:
        return f"\n\n[СТАТУС] ОЧЕРЕДЬ ЗАМОРОЖЕНА\nФиксация: {stamp}"
    return "\n\n[СТАТУС] ОЧЕРЕДЬ ЗАМОРОЖЕНА"

# ================== ACTIVITY ==================
def ensure_activity_row(conn, uid: int):
    conn.execute(
        "INSERT OR IGNORE INTO user_activity (user_id, score, updated_at) VALUES (?, 0, ?)",
        (uid, int(time.time()))
    )
    conn.commit()

def get_activity(conn, uid: int) -> Tuple[float, int]:
    ensure_activity_row(conn, uid)
    row = conn.execute(
        "SELECT score, updated_at FROM user_activity WHERE user_id=?",
        (uid,)
    ).fetchone()
    if not row:
        return 0.0, int(time.time())
    return float(row[0] or 0.0), int(row[1] or int(time.time()))

def _decay_multiplier(dt_sec: int) -> float:
    half_life_sec = ACTIVITY_HALF_LIFE_DAYS * 24 * 3600
    if half_life_sec <= 0:
        return 0.0
    return 0.5 ** (dt_sec / half_life_sec)

def get_sync_now(conn, uid: int, now_ts: Optional[int] = None) -> float:
    if now_ts is None:
        now_ts = int(time.time())
    score, last_ts = get_activity(conn, uid)
    dt = max(0, now_ts - last_ts)
    decay = _decay_multiplier(dt)
    return float(score) * decay

def update_activity(conn, uid: int, pts: int, now_ts: int):
    score, last_ts = get_activity(conn, uid)
    dt = max(0, now_ts - last_ts)

    decay = _decay_multiplier(dt)
    new_score = score * decay + float(pts)

    conn.execute(
        "UPDATE user_activity SET score=?, updated_at=? WHERE user_id=?",
        (new_score, now_ts, uid)
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
    conn.execute(
        "INSERT OR IGNORE INTO user_limits (user_id, username_change_used) VALUES (?, 0)",
        (uid,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_activity (user_id, score, updated_at) VALUES (?, 0, ?)",
        (uid, int(time.time()))
    )
    conn.commit()

def add_points(conn, uid, pts):
    frozen, _ = is_frozen(conn)
    if frozen:
        return  # заморозка: никаких изменений очков/активности

    now_ts = int(time.time())
    conn.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (pts, uid)
    )
    conn.commit()
    update_activity(conn, uid, pts, now_ts)

def ordered_users(conn):
    # если заморожено — фиксируем "сейчас" на момент фиксации
    frozen, fts = is_frozen(conn)
    now_ts = int(fts) if (frozen and fts) else int(time.time())

    rows = conn.execute("""
        SELECT u.user_id, u.username, u.points, u.created_at,
               COALESCE(a.score, 0) AS activity_score,
               COALESCE(a.updated_at, ?) AS activity_updated_at
        FROM users u
        LEFT JOIN user_activity a ON a.user_id = u.user_id
    """, (now_ts,)).fetchall()

    if not rows:
        return []

    eff_sync = []
    for r in rows:
        score = float(r[4] or 0.0)
        last_ts = int(r[5] or now_ts)
        dt = max(0, now_ts - last_ts)
        decay = _decay_multiplier(dt)
        eff_sync.append(score * decay)

    points_logs = [math.log1p(max(0, int(r[2]))) for r in rows]
    act_logs = [math.log1p(max(0.0, float(s))) for s in eff_sync]

    max_p = max(points_logs) if points_logs else 1.0
    max_a = max(act_logs) if act_logs else 1.0

    if max_p <= 0:
        max_p = 1.0
    if max_a <= 0:
        max_a = 1.0

    scored = []
    for idx, r in enumerate(rows):
        uid, username, points, created_at, _, _ = r
        sync_now = float(eff_sync[idx])

        p_norm = math.log1p(max(0, int(points))) / max_p
        a_norm = math.log1p(max(0.0, sync_now)) / max_a

        blended = 0.5 * p_norm + 0.5 * a_norm

        pri_int = int(round(blended * 1000))
        scored.append((uid, username, int(points), int(created_at), sync_now, blended, pri_int))

    scored.sort(key=lambda x: (-x[5], -x[2], x[3]))
    return [(s[0], s[1], s[6]) for s in scored]

def pri_of_user(conn, uid: int) -> int:
    rows = ordered_users(conn)
    for r in rows:
        if r[0] == uid:
            return int(r[2])
    return 0

def queue_position(conn, uid) -> Tuple[int, int]:
    ids = [r[0] for r in ordered_users(conn)]
    total = len(ids)
    return (ids.index(uid) + 1, total) if uid in ids else (total + 1, total)

def queue_neighbors(conn, uid, window: int = 2):
    rows = ordered_users(conn)
    ids = [r[0] for r in rows]
    if uid not in ids:
        return [], []
    i = ids.index(uid)
    above = rows[max(0, i - window): i]
    below = rows[i + 1: i + 1 + window]
    return above, below

# ================== S AUDIO ==================
def add_s_audio(conn, fid: str) -> bool:
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
    "Пакет расшифрован: Получен фрагмент №01 типа PROTOCOL — EVENT\n…мир пережил событие X, приведшее к необратимым изменениям глобального порядка…",
    "Пакет расшифрован: Получен фрагмент №02 типа PROTOCOL — SCARCITY\n…ресурсов земли стало не хватать для обеспечения населения…",
    "Пакет расшифрован: Получен фрагмент №03 типа PROTOCOL — DISASTER\n…усилились частота и масштаб природных катастроф…",
    "Пакет расшифрован: Получен фрагмент №04 типа PROTOCOL — COLLAPSE\n…на фоне кризиса возникла фаза затяжных вооружённых конфликтов и анархии…",
    "Пакет расшифрован: Получен фрагмент №05 типа PROTOCOL — INTERIM\n…после периода хаоса было сформировано временное правительство…",
    "Пакет расшифрован: Получен фрагмент №06 типа PROTOCOL — STABILITY\n…глобально ситуация стабилизировалась на минимально допустимом уровне…",
    "Пакет расшифрован: Получен фрагмент №07 типа PROTOCOL — NEZ\n…в рамках антикризисных мер была учреждена корпорация NEZ project…",
    "Пакет расшифрован: Получен фрагмент №08 типа PROTOCOL — MISSION\n…основная задача: сбор, анализ и систематизация данных о феномене «нулевой эдем» и активности так называемого третьего измерения…",
    "Пакет расшифрован: Получен фрагмент №09 типа PROTOCOL — EDEN\n…нулевой эдем — отдельное измерение, представляющее собой структурный двойник земли. доступ осуществляется через пространственно-временной разлом. изначально нулевой эдем рассматривался как «обнулённая» версия мира, потенциально пригодная для переселения человечества…",
    "Пакет расшифрован: Получен фрагмент №10 типа PROTOCOL — QUEUE\n…согласно ранним данным, нулевой эдем являлся чистой, восстановленной формой земли. после определения даты стабильного открытия разлома NEZ project инициировал создание цифровой очереди для населения с целью контролируемого доступа в новое измерение…",
]

FRAGMENT_SNIPPETS = [
    "Пакет расшифрован: Получен фрагмент №01 типа FRAGMENT\nЕщё один выстрел —\nв моей груди выцвел.\nЗнакомые лица…\nМне всё это просто снится...",
    "Пакет расшифрован: Получен фрагмент №02 типа FRAGMENT\nОстанови меня\ngлазами своими синими.\nВ снега топи меня\nи к небесам возноси меня...",
    "Пакет расшифрован: Получен фрагмент №03 типа FRAGMENT\nУ меня в запасе вечность.\nЕсли хочешь — стреляй, стреляй, стреляй.\nЕсли хочешь развлечься,\nесли хочешь огня, огня, огня...",
    "Пакет расшифрован: Получен фрагмент №04 типа FRAGMENT\nТы так слаба на вид, но\nвсе твои шрамы видно.\nИ ты впиваешься в шею —\nмоя Фемида...",
    "Пакет расшифрован: Получен фрагмент №05 типа FRAGMENT\nНочью под белым пламенем\nлежим убиты, ранены.\nПоцелуй на прощание —\nтвои слёзы — моя вина...",
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

# ================== USERNAME CHANGE ==================
USERNAME_RE_REG = re.compile(r"^[a-zA-Z0-9_.-]{3,20}$")
USERNAME_RE_CHANGE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 _\.\-]{3,20}$")

WAIT_USERNAME = set()
WAIT_RENAME = set()
WAIT_BROADCAST = set()
S_MODE = set()

def ensure_limits_row(conn, uid: int):
    conn.execute(
        "INSERT OR IGNORE INTO user_limits (user_id, username_change_used) VALUES (?, 0)",
        (uid,)
    )
    conn.commit()

def username_change_used(conn, uid: int) -> int:
    ensure_limits_row(conn, uid)
    row = conn.execute(
        "SELECT username_change_used FROM user_limits WHERE user_id=?",
        (uid,)
    ).fetchone()
    return int(row[0]) if row else 0

def inc_username_change_used(conn, uid: int):
    ensure_limits_row(conn, uid)
    conn.execute(
        "UPDATE user_limits SET username_change_used = username_change_used + 1 WHERE user_id=?",
        (uid,)
    )
    conn.commit()

def create_rename_request(conn, uid: int, old_name: str, new_name: str) -> int:
    cur = conn.execute(
        "INSERT INTO username_changes (user_id, old_username, new_username, status, created_at) "
        "VALUES (?, ?, ?, 'PENDING', ?)",
        (uid, old_name, new_name, int(time.time()))
    )
    conn.commit()
    return int(cur.lastrowid)

def get_rename_request(conn, rid: int):
    return conn.execute(
        "SELECT id, user_id, old_username, new_username, status FROM username_changes WHERE id=?",
        (rid,)
    ).fetchone()

def set_rename_status(conn, rid: int, status: str):
    conn.execute("UPDATE username_changes SET status=? WHERE id=?", (status, rid))
    conn.commit()

def rename_kb(req_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Подтвердить", callback_data=f"RENAME_OK:{req_id}"),
            InlineKeyboardButton("Отклонить", callback_data=f"RENAME_NO:{req_id}"),
        ]
    ])

# ================== UI ==================
def menu(uid):
    if uid in WAIT_RENAME:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="RENAME_CANCEL")]
        ])

    if uid in WAIT_BROADCAST and uid == ADMIN_ID:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="ADMIN_BROADCAST_CANCEL")]
        ])

    rows = [
        [InlineKeyboardButton("Позиция в очереди", callback_data="Q")],
        [InlineKeyboardButton("Активный пакет данных", callback_data="A")],
        [InlineKeyboardButton("Рейтинг", callback_data="TOP")],
        [InlineKeyboardButton("Смена ID", callback_data="RENAME")],
        [InlineKeyboardButton("Помощь", callback_data="HELP")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("📣 Рассылка", callback_data="ADMIN_BROADCAST")])
        rows.append([InlineKeyboardButton("🧊 Заморозка очереди", callback_data="ADMIN_FREEZE_TOGGLE")])
        rows.append([InlineKeyboardButton("＋ Добавить S", callback_data="ADD_S")])
        rows.append([InlineKeyboardButton("⚠ Запустить пакет", callback_data="ADMIN_PUSH")])
    return InlineKeyboardMarkup(rows)

# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    uid = update.effective_user.id
    user = get_user(conn, uid)

    if user:
        pos, total = queue_position(conn, uid)
        pri = pri_of_user(conn, uid)

        await update.message.reply_text(
            hdr() +
            f"ID: {user[1]}\n"
            f"Позиция: {pos}/{total}\n"
            f"Индекс допуска: {pri}\n"
            f"Уровень доступа: {access_level(int(user[2]))}"
            + freeze_banner(conn),
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
    txt = (update.message.text or "").strip()
    conn = db()

    # ===== admin broadcast flow =====
    if uid == ADMIN_ID and uid in WAIT_BROADCAST:
        WAIT_BROADCAST.discard(uid)

        rows = conn.execute("SELECT user_id FROM users").fetchall()
        user_ids = [int(r[0]) for r in rows]

        sent = 0
        failed = 0

        for to_uid in user_ids:
            try:
                await context.bot.send_message(chat_id=to_uid, text=txt)
                sent += 1
            except:
                failed += 1

        await update.message.reply_text(
            f"Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}",
            reply_markup=menu(uid)
        )
        return

    # ===== registration ID (latin only) =====
    if uid in WAIT_USERNAME:
        name = txt
        if not USERNAME_RE_REG.match(name):
            await update.message.reply_text("Неверный формат. Попробуйте снова.")
            return

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
            f"Позиция: {pos}/{total}"
            + freeze_banner(conn),
            reply_markup=menu(uid)
        )
        return

    # ===== rename flow (allowed even in freeze) =====
    if uid in WAIT_RENAME:
        user = get_user(conn, uid)
        if not user:
            WAIT_RENAME.discard(uid)
            await update.message.reply_text("Вы еще не зарегистрированы.", reply_markup=menu(uid))
            return

        used = username_change_used(conn, uid)
        if used >= 3:
            WAIT_RENAME.discard(uid)
            await update.message.reply_text("Лимит смены ID исчерпан.", reply_markup=menu(uid))
            return

        new_name = txt

        if not USERNAME_RE_CHANGE.match(new_name):
            await update.message.reply_text("Неверный формат. Попробуйте снова.")
            return

        if conn.execute("SELECT 1 FROM users WHERE username=?", (new_name,)).fetchone():
            await update.message.reply_text("ID уже занят.")
            return

        old_name = user[1]
        inc_username_change_used(conn, uid)
        used_after = username_change_used(conn, uid)

        rid = create_rename_request(conn, uid, old_name, new_name)
        WAIT_RENAME.discard(uid)

        await update.message.reply_text(
            "Запрос отправлен на проверку.",
            reply_markup=menu(uid)
        )

        if ADMIN_ID != 0:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "Запрос на смену ID\n\n"
                        f"Пользователь: {uid}\n"
                        f"Текущий ID: {old_name}\n"
                        f"Новый ID: {new_name}\n"
                        f"Попытка: {used_after}/3"
                    ),
                    reply_markup=rename_kb(rid)
                )
            except:
                pass
        return

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
            "• Обладатели первых трех позиций в очереди будут отмечены публично на специальной конференции NEZ Project 24.01.26.\n"
            "metaego-asterasounds2401.ticketscloud.org"
            + freeze_banner(conn),
            reply_markup=menu(uid)
        )

    elif q.data == "Q":
        user = get_user(conn, uid)
        pos, total = queue_position(conn, uid)
        pri = pri_of_user(conn, uid)

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
            f"Индекс допуска: {pri}\n"
            f"Уровень доступа: {access_level(int(user[2]))}"
            + neigh
            + freeze_banner(conn),
            reply_markup=menu(uid)
        )

    elif q.data == "TOP":
        rows = ordered_users(conn)[:10]
        text = hdr() + "Обладатели первых позиций в очереди:\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{i}. {r[1]} — {r[2]}\n"
        text += freeze_banner(conn)
        await q.edit_message_text(text, reply_markup=menu(uid))

    elif q.data == "A":
        frozen, _ = is_frozen(conn)
        if frozen:
            await q.edit_message_text(
                hdr() +
                "Очередь переведена в режим фиксации.\n"
                "Выдача и обработка пакетов данных временно приостановлены.\n\n"
                "Подробности будут опубликованы дополнительно."
                + freeze_banner(conn),
                reply_markup=menu(uid)
            )
            return

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
                    await context.bot.send_message(uid, "Пакет расшифрован: Получен фрагмент типа INTERCEPT")
                    await context.bot.send_audio(uid, payload)
                    add_points(conn, uid, 4)
                else:
                    await context.bot.send_message(uid, payload)
                    add_points(conn, uid, 2)

                conn.execute("UPDATE anomalies SET status='DONE' WHERE id=?", (aid,))
                conn.commit()

                await q.edit_message_text(
                    "Пакет расшифрован.",
                    reply_markup=menu(uid)
                )

    elif q.data == "RENAME":
        user = get_user(conn, uid)
        if not user:
            await q.edit_message_text("Вы еще не зарегистрированы.", reply_markup=menu(uid))
            return

        used = username_change_used(conn, uid)
        if used >= 3:
            await q.edit_message_text("Лимит смены ID исчерпан.", reply_markup=menu(uid))
            return

        left = 3 - used
        WAIT_RENAME.add(uid)
        await q.edit_message_text(
            f"Введите новый ID.\nОсталось попыток: {left}/3",
            reply_markup=menu(uid)
        )

    elif q.data == "RENAME_CANCEL":
        if uid in WAIT_RENAME:
            WAIT_RENAME.discard(uid)
        await q.edit_message_text("Отменено.", reply_markup=menu(uid))

    # ================== ADMIN: BROADCAST ==================
    elif q.data == "ADMIN_BROADCAST" and uid == ADMIN_ID:
        WAIT_BROADCAST.add(uid)
        await q.edit_message_text(
            hdr() +
            "Режим рассылки активирован.\n\n"
            "Отправьте одним сообщением текст, который необходимо разослать всем пользователям.\n"
            "Для отмены нажмите «Отмена».",
            reply_markup=menu(uid)
        )

    elif q.data == "ADMIN_BROADCAST_CANCEL" and uid == ADMIN_ID:
        WAIT_BROADCAST.discard(uid)
        await q.edit_message_text("Отменено.", reply_markup=menu(uid))

    # ================== ADMIN: FREEZE TOGGLE ==================
    elif q.data == "ADMIN_FREEZE_TOGGLE" and uid == ADMIN_ID:
        frozen, _ = is_frozen(conn)
        set_frozen(conn, not frozen)

        frozen2, ts2 = is_frozen(conn)
        if frozen2:
            stamp = datetime.fromtimestamp(ts2 or int(time.time()), TZ).strftime("%d.%m.%Y %H:%M:%S %Z")
            msg = (
                hdr() +
                "Очередь переведена в режим фиксации.\n"
                f"Время фиксации: {stamp}\n\n"
                "Выдача пакетов данных, перерасчет рейтингов и начисления приостановлены."
            )
        else:
            msg = (
                hdr() +
                "Режим фиксации отключён.\n\n"
                "Очередь, выдача пакетов и начисления возобновлены."
            )

        await q.edit_message_text(msg, reply_markup=menu(uid))

    # ================== ADMIN MODERATION (RENAME) ==================
    elif q.data.startswith("RENAME_OK:") and uid == ADMIN_ID:
        rid = int(q.data.split(":", 1)[1])
        req = get_rename_request(conn, rid)
        if not req:
            await q.edit_message_text("Запрос не найден.", reply_markup=menu(uid))
            return
        _, target_uid, old_name, new_name, status = req
        if status != "PENDING":
            await q.edit_message_text("Запрос уже обработан.", reply_markup=menu(uid))
            return

        if conn.execute("SELECT 1 FROM users WHERE username=?", (new_name,)).fetchone():
            set_rename_status(conn, rid, "DECLINED")
            await q.edit_message_text("Отклонено: ID уже занят.", reply_markup=menu(uid))
            try:
                await context.bot.send_message(chat_id=target_uid, text="Запрос отклонён.")
            except:
                pass
            return

        conn.execute("UPDATE users SET username=? WHERE user_id=?", (new_name, target_uid))
        conn.commit()
        set_rename_status(conn, rid, "APPROVED")

        await q.edit_message_text("Подтверждено.", reply_markup=menu(uid))
        try:
            await context.bot.send_message(chat_id=target_uid, text="Запрос подтвержден.")
        except:
            pass

    elif q.data.startswith("RENAME_NO:") and uid == ADMIN_ID:
        rid = int(q.data.split(":", 1)[1])
        req = get_rename_request(conn, rid)
        if not req:
            await q.edit_message_text("Запрос не найден.", reply_markup=menu(uid))
            return
        _, target_uid, old_name, new_name, status = req
        if status != "PENDING":
            await q.edit_message_text("Запрос уже обработан.", reply_markup=menu(uid))
            return

        set_rename_status(conn, rid, "DECLINED")
        await q.edit_message_text("Отклонено.", reply_markup=menu(uid))
        try:
            await context.bot.send_message(chat_id=target_uid, text="Запрос отклонён.")
        except:
            pass

    # ================== ADMIN: S AUDIO ==================
    elif q.data == "ADD_S" and uid == ADMIN_ID:
        S_MODE.add(uid)
        total_s = count_s_audio(conn)
        await q.edit_message_text(
            "Режим добавления S активен.\nОтправляйте аудио.",
            reply_markup=menu(uid)
        )
        try:
            await context.bot.send_message(uid, f"Всего S: {total_s}")
        except:
            pass

    elif q.data == "ADMIN_PUSH" and uid == ADMIN_ID:
        frozen, _ = is_frozen(conn)
        if frozen:
            await q.edit_message_text("Очередь заморожена. Выдача пакетов приостановлена.", reply_markup=menu(uid))
            return
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

    frozen, _ = is_frozen(conn)
    if frozen:
        return  # заморозка: пакеты не выдаём

    users = ordered_users(conn)

    for uid, _, _ in users:
        expire_active_anomalies(conn, uid)

        r = random.random()

        if r < 0.40:
            fid = random_s_audio(conn)
            if fid:
                create_anomaly(conn, uid, "S", fid)
                try:
                    await context.bot.send_message(uid, "Новый пакет данных от NEZ Project доступен.")
                except:
                    pass
                continue

        if r < 0.60:
            payload = random.choice(FRAGMENT_SNIPPETS)
        elif r < 0.80:
            payload = random.choice(LORE_SNIPPETS)
        else:
            payload = random.choice(NOCLASS_TEXT)

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

    frozen, _ = is_frozen(conn)
    if frozen:
        return  # заморозка: даже не планируем на сегодня

    now_local = datetime.now(TZ)
    key = _today_key(now_local)
    last = get_meta(conn, "last_scheduled_day")

    if last == key:
        return

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

    # schedule daily scheduler (~00:05 Amsterdam)
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

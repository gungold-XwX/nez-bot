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

# Duel settings
DUEL_REQUEST_TTL_SEC = 10 * 60           # 10 minutes to accept/decline
DUEL_COOLDOWN_MIN_SEC = 6 * 3600         # 6h
DUEL_COOLDOWN_MAX_SEC = 12 * 3600        # 12h

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

    # Duel meta (cooldowns)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_meta (
        user_id INTEGER PRIMARY KEY,
        duel_cooldown_until INTEGER DEFAULT 0
    )""")

    # Duel requests
    conn.execute("""
    CREATE TABLE IF NOT EXISTS duels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        challenger_id INTEGER,
        target_id INTEGER,
        created_at INTEGER,
        status TEXT,
        winner_id INTEGER DEFAULT 0,
        resolved_at INTEGER DEFAULT 0
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

def add_points(conn, uid, pts_delta: int):
    # clamp at 0 so index doesn't go negative
    conn.execute(
        "UPDATE users SET points = CASE WHEN points + ? < 0 THEN 0 ELSE points + ? END WHERE user_id=?",
        (pts_delta, pts_delta, uid)
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

def neighbor_above(conn, uid) -> Optional[Tuple[int, str, int]]:
    rows = ordered_users(conn)
    ids = [r[0] for r in rows]
    if uid not in ids:
        return None
    i = ids.index(uid)
    if i == 0:
        return None
    return rows[i - 1]  # (user_id, username, points)

# ================== USER META (COOLDOWN) ==================
def ensure_user_meta(conn, uid: int):
    conn.execute(
        "INSERT OR IGNORE INTO user_meta (user_id, duel_cooldown_until) VALUES (?, 0)",
        (uid,)
    )
    conn.commit()

def get_duel_cooldown_until(conn, uid: int) -> int:
    ensure_user_meta(conn, uid)
    row = conn.execute(
        "SELECT duel_cooldown_until FROM user_meta WHERE user_id=?",
        (uid,)
    ).fetchone()
    return int(row[0]) if row else 0

def set_duel_cooldown(conn, uid: int):
    ensure_user_meta(conn, uid)
    now = int(time.time())
    cd = random.randint(DUEL_COOLDOWN_MIN_SEC, DUEL_COOLDOWN_MAX_SEC)
    conn.execute(
        "UPDATE user_meta SET duel_cooldown_until=? WHERE user_id=?",
        (now + cd, uid)
    )
    conn.commit()

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
    "Пакет расшифрован: данные повреждены",
    "Пакет расшифрован: содержимое утеряно",
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

# ================== DUELS (Quota Shift) ==================
def create_duel(conn, challenger_id: int, target_id: int) -> int:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO duels (challenger_id, target_id, created_at, status) VALUES (?, ?, ?, 'PENDING')",
        (challenger_id, target_id, now)
    )
    conn.commit()
    return int(cur.lastrowid)

def get_duel(conn, duel_id: int):
    return conn.execute(
        "SELECT id, challenger_id, target_id, created_at, status, winner_id FROM duels WHERE id=?",
        (duel_id,)
    ).fetchone()

def set_duel_status(conn, duel_id: int, status: str, winner_id: int = 0):
    now = int(time.time())
    conn.execute(
        "UPDATE duels SET status=?, winner_id=?, resolved_at=? WHERE id=?",
        (status, winner_id, now if status in ("DONE", "DECLINED", "EXPIRED") else 0, duel_id)
    )
    conn.commit()

def mark_expired_if_ttl(conn, duel_row) -> bool:
    # returns True if expired now
    duel_id, challenger_id, target_id, created_at, status, winner_id = duel_row
    if status != "PENDING":
        return False
    if int(time.time()) - int(created_at) > DUEL_REQUEST_TTL_SEC:
        set_duel_status(conn, duel_id, "EXPIRED", 0)
        return True
    return False

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

def duel_request_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Запросить сдвиг квоты (сосед выше)", callback_data="DUEL_REQ")]
    ])

def duel_invite_kb(duel_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Принять", callback_data=f"DUEL_ACCEPT:{duel_id}"),
            InlineKeyboardButton("Отклонить", callback_data=f"DUEL_DECLINE:{duel_id}")
        ]
    ])

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

    # ====== DUEL: request (quota shift) ======
    if q.data == "DUEL_REQ":
        # cooldown check
        now = int(time.time())
        until = get_duel_cooldown_until(conn, uid)
        if now < until:
            await q.edit_message_text(
                "Окно пересчёта квоты недоступно. Повторите позже.",
                reply_markup=menu(uid)
            )
            return

        above = neighbor_above(conn, uid)
        if not above:
            await q.edit_message_text(
                "Запрос невозможен: вышестоящий слот отсутствует.",
                reply_markup=menu(uid)
            )
            return

        target_id, target_name, _ = above
        duel_id = create_duel(conn, uid, target_id)

        # Put both on cooldown to prevent farming (request counted)
        set_duel_cooldown(conn, uid)
        set_duel_cooldown(conn, target_id)

        # notify challenger
        await q.edit_message_text(
            "Запрос на сдвиг квоты сформирован.\n"
            "Ожидание подтверждения адресата.",
            reply_markup=menu(uid)
        )

        # send invite to target
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Получен входящий запрос на сдвиг квоты.\n"
                     "Участие добровольное.\n"
                     "Подтвердите участие для запуска пересчёта.",
                reply_markup=duel_invite_kb(duel_id)
            )
        except:
            # if target can't be reached, expire request and notify challenger
            set_duel_status(conn, duel_id, "EXPIRED", 0)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="Адресат недоступен. Пересчёт не выполнен."
                )
            except:
                pass
        return

    # ====== DUEL: accept/decline ======
    if q.data.startswith("DUEL_ACCEPT:") or q.data.startswith("DUEL_DECLINE:"):
        action, duel_id_s = q.data.split(":")
        duel_id = int(duel_id_s)

        duel_row = get_duel(conn, duel_id)
        if not duel_row:
            await q.edit_message_text("Запрос недействителен.", reply_markup=menu(uid))
            return

        duel_id_db, challenger_id, target_id, created_at, status, winner_id = duel_row

        # only target can respond
        if uid != target_id:
            await q.edit_message_text("Недостаточно прав доступа.", reply_markup=menu(uid))
            return

        # expire if too old
        if mark_expired_if_ttl(conn, duel_row):
            await q.edit_message_text("Запрос истёк. Пересчёт не выполнен.", reply_markup=menu(uid))
            try:
                await context.bot.send_message(
                    chat_id=challenger_id,
                    text="Запрос истёк. Пересчёт не выполнен."
                )
            except:
                pass
            return

        if status != "PENDING":
            await q.edit_message_text("Запрос уже обработан.", reply_markup=menu(uid))
            return

        if action == "DUEL_DECLINE":
            set_duel_status(conn, duel_id, "DECLINED", 0)
            await q.edit_message_text("Запрос отклонён. Пересчёт не выполнен.", reply_markup=menu(uid))
            try:
                await context.bot.send_message(
                    chat_id=challenger_id,
                    text="Запрос отклонён адресатом. Пересчёт не выполнен."
                )
            except:
                pass
            return

        # ACCEPT: random resolution
        # winner: either challenger or target
        winner = challenger_id if random.random() < 0.5 else target_id
        set_duel_status(conn, duel_id, "DONE", winner)

        # rule: if invited wins -> invited +1, challenger -1; else opposite
        if winner == target_id:
            add_points(conn, target_id, +1)
            add_points(conn, challenger_id, -1)
        else:
            add_points(conn, challenger_id, +1)
            add_points(conn, target_id, -1)

        await q.edit_message_text(
            "Пересчёт квоты выполнен.\nРезультат: квота перераспределена.",
            reply_markup=menu(uid)
        )

        # notify challenger
        try:
            await context.bot.send_message(
                chat_id=challenger_id,
                text="Пересчёт квоты выполнен.\nРезультат: квота перераспределена."
            )
        except:
            pass

        return

    # ====== EXISTING FLOWS ======
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

        # Add duel button here (separate message UI) without changing existing text.
        await q.edit_message_text(
            hdr() +
            f"ID: {user[1]}\n"
            f"Позиция: {pos}/{total}\n"
            f"Индекс допуска: {user[2]}"
            + neigh,
            reply_markup=InlineKeyboardMarkup(
                (menu(uid).inline_keyboard + duel_request_kb().inline_keyboard)
            )
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
        # always keep only 1 active packet per user
        expire_active_anomalies(conn, uid)

        if random.random() < 0.25:
            fid = random_s_audio(conn)
            if fid:
                create_anomaly(conn, uid, "S", fid)
                continue
        create_anomaly(conn, uid, "N", random.choice(NOCLASS_TEXT))

        try:
            await context.bot.send_message(uid, "Новый пакет данных от NEZ Project доступен.")
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

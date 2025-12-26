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

def ordered_users(conn):
    return conn.execute(
        "SELECT user_id, username, points, created_at FROM users ORDER BY points DESC, created_at ASC"
    ).fetchall()

def queue_position(conn, uid) -> Tuple[int, int]:
    rows = ordered_users(conn)
    ids = [r[0] for r in rows]
    total = len(ids)
    return (ids.index(uid) + 1, total) if uid in ids else (total + 1, total)

def queue_neighbors(conn, uid, window: int = 2):
    rows = ordered_users(conn)  # (user_id, username, points, created_at)
    ids = [r[0] for r in rows]
    if uid not in ids:
        return [], []
    i = ids.index(uid)
    above = rows[max(0, i - window): i]
    below = rows[i + 1: i + 1 + window]
    return above, below

def neighbor_above(conn, uid) -> Optional[Tuple[int, str, int, int]]:
    rows = ordered_users(conn)
    ids = [r[0] for r in rows]
    if uid not in ids:
        return None
    i = ids.index(uid)
    if i == 0:
        return None
    return rows[i - 1]  # (user_id, username, points, created_at)

def set_points(conn, uid: int, value: int):
    if value < 0:
        value = 0
    conn.execute("UPDATE users SET points=? WHERE user_id=?", (value, uid))
    conn.commit()

def set_created_at(conn, uid: int, value: int):
    conn.execute("UPDATE users SET created_at=? WHERE user_id=?", (value, uid))
    conn.commit()

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
    duel_id, challenger_id, target_id, created_at, status, winner_id = duel_row
    if status != "PENDING":
        return False
    if int(time.time()) - int(created_at) > DUEL_REQUEST_TTL_SEC:
        set_duel_status(conn, duel_id, "EXPIRED", 0)
        return True
    return False

def swap_positions_one_step(conn, winner_id: int, loser_id: int):
    """
    Duel is always with neighbor above (loser_id may be above or below depending on winner),
    but we enforce "swap order between these two" (i.e., winner +1 position, loser -1).
    We do it by adjusting points minimally, and fallback to swapping created_at if points==0 edge case.
    """
    w = get_user(conn, winner_id)  # (id, username, points, created_at)
    l = get_user(conn, loser_id)

    if not w or not l:
        return

    w_pts, w_ct = int(w[2]), int(w[3])
    l_pts, l_ct = int(l[2]), int(l[3])

    # If loser has >0 points, winner gets loser's points, loser gets -1 from that level.
    # This makes winner just above loser without jumping over tie-group above (created_at keeps it stable).
    if l_pts > 0:
        set_points(conn, winner_id, l_pts)
        set_points(conn, loser_id, l_pts - 1)
        return

    # Edge: loser has 0 points (both likely near bottom). Swap created_at to flip their relative order.
    # Keep points unchanged.
    set_created_at(conn, winner_id, l_ct)
    set_created_at(conn, loser_id, w_ct)

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

def duel_confirm_kb(target_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подтвердить запрос", callback_data=f"DUEL_REQ_OK:{target_id}")],
        [InlineKeyboardButton("Отмена", callback_data="DUEL_REQ_CANCEL")]
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

    # ====== DUEL: step 1 — show confirmation window ======
    if q.data == "DUEL_REQ":
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

        target_id, target_name, _, _ = above

        await q.edit_message_text(
            "Запрос на сдвиг квоты будет отправлен наблюдателю выше по очереди.\n\n"
            "Если адресат подтвердит участие, NEZ выполнит пересчёт квоты.\n"
            "По результату пересчёта позиции в очереди будут скорректированы:\n"
            "• один участник поднимется на 1 место\n"
            "• второй участник опустится на 1 место\n\n"
            "Адресат может отклонить запрос без последствий.\n"
            "Частота пересчётов ограничена.",
            reply_markup=duel_confirm_kb(target_id)
        )
        return

    if q.data == "DUEL_REQ_CANCEL":
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
            reply_markup=InlineKeyboardMarkup(
                (menu(uid).inline_keyboard + duel_request_kb().inline_keyboard)
            )
        )
        return

    # ====== DUEL: step 2 — actually send request after confirm ======
    if q.data.startswith("DUEL_REQ_OK:"):
        try:
            target_id = int(q.data.split(":", 1)[1])
        except:
            await q.edit_message_text("Запрос недействителен.", reply_markup=menu(uid))
            return

        now = int(time.time())
        until = get_duel_cooldown_until(conn, uid)
        if now < until:
            await q.edit_message_text(
                "Окно пересчёта квоты недоступно. Повторите позже.",
                reply_markup=menu(uid)
            )
            return

        above = neighbor_above(conn, uid)
        if not above or above[0] != target_id:
            await q.edit_message_text(
                "Запрос невозможен: вышестоящий слот изменился.",
                reply_markup=menu(uid)
            )
            return

        duel_id = create_duel(conn, uid, target_id)

        # cooldown for both (as in your current logic)
        set_duel_cooldown(conn, uid)
        set_duel_cooldown(conn, target_id)

        await q.edit_message_text(
            "Запрос на сдвиг квоты сформирован.\n"
            "Ожидание подтверждения адресата.",
            reply_markup=menu(uid)
        )

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Получен входящий запрос на сдвиг квоты.\n"
                     "Участие добровольное.\n"
                     "Подтвердите участие для запуска пересчёта.",
                reply_markup=duel_invite_kb(duel_id)
            )
        except:
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

        if uid != target_id:
            await q.edit_message_text("Недостаточно прав доступа.", reply_markup=menu(uid))
            return

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
        winner = challenger_id if random.random() < 0.5 else target_id
        loser = target_id if winner == challenger_id else challenger_id

        # capture old positions
        old_pos_w, total = queue_position(conn, winner)
        old_pos_l, _ = queue_position(conn, loser)

        set_duel_status(conn, duel_id, "DONE", winner)

        # swap by one position (winner up, loser down)
        swap_positions_one_step(conn, winner, loser)

        # new positions
        new_pos_w, total2 = queue_position(conn, winner)
        new_pos_l, _ = queue_position(conn, loser)

        w_user = get_user(conn, winner)
        l_user = get_user(conn, loser)
        w_name = w_user[1] if w_user else "UNKNOWN"
        l_name = l_user[1] if l_user else "UNKNOWN"

        # result text (shown to both)
        result_text = (
            "Пересчёт квоты выполнен.\n"
            "Результат: квота перераспределена.\n\n"
            f"Поднялся: {w_name} ({old_pos_w} → {new_pos_w})\n"
            f"Опустился: {l_name} ({old_pos_l} → {new_pos_l})"
        )

        await q.edit_message_text(result_text, reply_markup=menu(uid))

        # notify challenger too
        try:
            await context.bot.send_message(
                chat_id=challenger_id,
                text=result_text
            )
        except:
            pass

        # notify target too (in case callback edit is not seen later)
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=result_text
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

            # keep your original behavior: fast confirm adds points (index)
            conn.execute(
                "UPDATE users SET points = points + ? WHERE user_id=?",
                (pts, uid)
            )
            conn.commit()

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
                    conn.execute("UPDATE users SET points = points + 4 WHERE user_id=?", (uid,))
                    conn.commit()
                else:
                    await context.bot.send_message(uid, payload)
                    conn.execute("UPDATE users SET points = points + 2 WHERE user_id=?", (uid,))
                    conn.commit()

                conn.execute("UPDATE anomalies SET status='DONE' WHERE id=?", (aid,))
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

    for uid, _, _, _ in users:
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

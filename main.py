import os
import sqlite3
import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ========= ENV =========
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "10000"))

# локальное "дневное" расписание через смещение часов (по умолчанию МСК +3)
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "3"))  # часы к UTC

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nez.db"

# ========= DB =========
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        callsign TEXT,
        points INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT 0,
        last_queue_push INTEGER DEFAULT 0
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,              -- "NOCLASS" / "S"
        payload TEXT NOT NULL,           -- текст расшифровки
        created_at INTEGER NOT NULL,
        fixed_at INTEGER DEFAULT 0,
        decrypted_at INTEGER DEFAULT 0,
        status TEXT NOT NULL             -- "SENT" / "FIXED" / "DECRYPTED"
    )
    """)
    conn.commit()
    return conn

def upsert_user(conn, user_id: int, callsign: str):
    cur = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO users (user_id, callsign, points, created_at) VALUES (?, ?, ?, ?)",
            (user_id, callsign[:32], 0, int(time.time()))
        )
    conn.commit()

def get_user(conn, user_id: int):
    cur = conn.execute("SELECT user_id, callsign, points, created_at FROM users WHERE user_id=?", (user_id,))
    return cur.fetchone()

def add_points(conn, user_id: int, delta: int):
    conn.execute("UPDATE users SET points = COALESCE(points,0) + ? WHERE user_id=?", (delta, user_id))
    conn.commit()

def all_users(conn) -> List[Tuple[int, str, int]]:
    cur = conn.execute("SELECT user_id, callsign, points FROM users")
    return cur.fetchall()

def leaderboard(conn, limit=10):
    cur = conn.execute("SELECT callsign, points FROM users ORDER BY points DESC, created_at ASC LIMIT ?", (limit,))
    return cur.fetchall()

def queue_position(conn, user_id: int) -> Tuple[int, int]:
    """
    Возвращает (позиция, всего).
    Позиция 1 = лучший (больше points), tie-breaker = раньше зарегистрирован.
    """
    cur = conn.execute("""
        SELECT user_id
        FROM users
        ORDER BY points DESC, created_at ASC
    """)
    ids = [r[0] for r in cur.fetchall()]
    total = len(ids)
    if user_id not in ids:
        return (total + 1, total)
    return (ids.index(user_id) + 1, total)

def neighbors(conn, user_id: int, window=2):
    cur = conn.execute("""
        SELECT user_id, callsign, points
        FROM users
        ORDER BY points DESC, created_at ASC
    """)
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if user_id not in ids:
        return [], []
    idx = ids.index(user_id)
    above = rows[max(0, idx - window): idx]
    below = rows[idx + 1: idx + 1 + window]
    return above, below

def create_anomaly(conn, user_id: int, kind: str, payload: str) -> int:
    now = int(time.time())
    conn.execute("""
        INSERT INTO anomalies (user_id, kind, payload, created_at, status)
        VALUES (?, ?, ?, ?, 'SENT')
    """, (user_id, kind, payload, now))
    conn.commit()
    cur = conn.execute("SELECT last_insert_rowid()")
    return int(cur.fetchone()[0])

def get_active_anomaly(conn, user_id: int) -> Optional[Tuple]:
    cur = conn.execute("""
        SELECT id, kind, payload, created_at, fixed_at, decrypted_at, status
        FROM anomalies
        WHERE user_id=? AND status IN ('SENT','FIXED')
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))
    return cur.fetchone()

def set_fixed(conn, anomaly_id: int):
    conn.execute("""
        UPDATE anomalies SET fixed_at=?, status='FIXED'
        WHERE id=? AND status='SENT'
    """, (int(time.time()), anomaly_id))
    conn.commit()

def set_decrypted(conn, anomaly_id: int):
    conn.execute("""
        UPDATE anomalies SET decrypted_at=?, status='DECRYPTED'
        WHERE id=? AND status='FIXED'
    """, (int(time.time()), anomaly_id))
    conn.commit()

# ========= LORE / CONTENT =========
def hdr():
    return "NEZ PROJECT × GOV // EDEN QUEUE TERMINAL\n"

def s_payload_stub(track_id: int) -> str:
    # сюда позже вставим реальные отрывки/ID треков
    return f"[CLASS S] ARCHIVE SIGNAL\nFRAG: NEZ-S-{track_id:02d}\nCONTENT: (sanitized excerpt)\n…"

NOCLASS_PAYLOADS = [
    "Данные шумовые. Семантика не выделена. [NOCLASS]",
    "Интерференция среды. Резонанс ложный. [NOCLASS]",
    "Слой третьего измерения проявился кратковременно. Трассировка невозможна. [NOCLASS]",
    "Пакет данных неполный. Маркер «отражение» не подтверждён. [NOCLASS]",
]

# ========= UI =========
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Справка (очередь)", callback_data="Q")],
        [InlineKeyboardButton("⚠️ Проверить аномалию", callback_data="A")],
        [InlineKeyboardButton("🏆 Топ", callback_data="TOP")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="HELP")],
    ])

def anomaly_fix_kb(anomaly_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Зафиксировать", callback_data=f"FIX:{anomaly_id}")]
    ])

def anomaly_decrypt_kb(anomaly_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Расшифровать", callback_data=f"DEC:{anomaly_id}")]
    ])

# ========= CORE LOGIC =========
def now_utc() -> int:
    return int(time.time())

def local_hour(utc_ts: int) -> int:
    # "локальные" часы по TZ_OFFSET
    return int((utc_ts + TZ_OFFSET * 3600) % 86400) // 3600

def daytime_six_hour_slots() -> List[int]:
    """
    6-часовые слоты ДНЁМ.
    Выберем 10:00, 16:00, 22:00 (локально) — 3 справки.
    Если хочешь строго "днём" без 22:00 — скажи, сделаю 9/15/21 или 10/16/20.
    """
    return [10, 16, 22]

def is_daytime_for_queue(utc_ts: int) -> bool:
    h = local_hour(utc_ts)
    return h in daytime_six_hour_slots()

def chance_class_s(pos: int, total: int) -> float:
    # чем ближе к 1 месту, тем выше шанс S
    if total <= 1:
        return 0.6
    # нормируем: топ-1 ~0.65, середина ~0.20, низ ~0.08
    x = (total - pos) / (total - 1)  # 0..1
    return 0.08 + x * 0.57

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    name = (update.effective_user.username or update.effective_user.first_name or "observer")
    upsert_user(conn, user_id, name)

    u = get_user(conn, user_id)
    pos, total = queue_position(conn, user_id)

    text = (
        hdr()
        + f"Добро пожаловать в цифровую очередь Нулевого Эдема.\n"
        + f"Путешественник: {u[1]}\n"
        + f"Текущая позиция: {pos}/{total}\n\n"
        + "Система присылает справку о позиции каждые 6 часов днём.\n"
        + "3 раза в день появляются аномалии: нужно быстро фиксировать и расшифровывать.\n\n"
        + "Открой меню:"
    )
    await update.message.reply_text(text, reply_markup=main_menu())

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_queue_card(update, context)

async def send_queue_card(update: Update, context: ContextTypes.DEFAULT_TYPE, as_message: bool=True):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        if as_message:
            await update.message.reply_text("Нажми /start", reply_markup=main_menu())
        else:
            await update.callback_query.edit_message_text("Нажми /start", reply_markup=main_menu())
        return

    pos, total = queue_position(conn, user_id)
    above, below = neighbors(conn, user_id, window=2)

    def fmt_row(r):
        _uid, cs, pts = r
        return f"{cs} — {pts} pts"

    neighbors_text = ""
    if above:
        neighbors_text += "Соседи выше:\n" + "\n".join(["↑ " + fmt_row(r) for r in above]) + "\n"
    if below:
        neighbors_text += "Соседи ниже:\n" + "\n".join(["↓ " + fmt_row(r) for r in below]) + "\n"
    if not neighbors_text:
        neighbors_text = "Соседи: недостаточно данных.\n"

    text = (
        hdr()
        + "СПРАВКА О ПОЗИЦИИ\n"
        + f"Путешественник: {u[1]}\n"
        + f"Позиция в очереди: {pos}/{total}\n"
        + f"Очки (репутация): {u[2]}\n\n"
        + neighbors_text
        + "\nПодсказка: ловля аномалий ускоряет рост очереди."
    )

    if as_message:
        await update.message.reply_text(text, reply_markup=main_menu())
    else:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu())

async def send_help(update: Update, context: ContextTypes.DEFAULT_TYPE, as_message=True):
    text = (
        hdr()
        + "КАК ЭТО РАБОТАЕТ\n\n"
        + "• Очередь: чем выше — тем ближе к «окну».\n"
        + "• Аномалии: 3 раза в день появляются данные.\n"
        + "  1) нажми «Зафиксировать» как можно быстрее\n"
        + "  2) подожди 10 минут\n"
        + "  3) нажми «Расшифровать»\n\n"
        + "• Класс S (редко): это сигналы архива (фрагменты альбома).\n"
        + "  Чем выше позиция — тем выше шанс получить S.\n\n"
        + "Награды:\n"
        + "• фиксация: +2 pts\n"
        + "• расшифровка: +3 pts (S даёт +5)\n"
        + "\nМеню ниже."
    )
    if as_message:
        await update.message.reply_text(text, reply_markup=main_menu())
    else:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu())

async def send_top(update: Update, context: ContextTypes.DEFAULT_TYPE, as_message=True):
    conn = db()
    rows = leaderboard(conn, limit=10)
    if not rows:
        text = hdr() + "Топ пуст. Нужны путешественники."
    else:
        lines = [hdr() + "🏆 ТОП ПУТЕШЕСТВЕННИКОВ\n"]
        for i, (cs, pts) in enumerate(rows, start=1):
            lines.append(f"{i:02d}. {cs} — {pts} pts")
        text = "\n".join(lines)

    if as_message:
        await update.message.reply_text(text, reply_markup=main_menu())
    else:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu())

async def send_or_check_anomaly(update: Update, context: ContextTypes.DEFAULT_TYPE, as_message=True):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        if as_message:
            await update.message.reply_text("Нажми /start", reply_markup=main_menu())
        else:
            await update.callback_query.edit_message_text("Нажми /start", reply_markup=main_menu())
        return

    a = get_active_anomaly(conn, user_id)
    if not a:
        text = hdr() + "Аномалий нет.\nОжидайте следующего окна (3 раза в день)."
        if as_message:
            await update.message.reply_text(text, reply_markup=main_menu())
        else:
            await update.callback_query.edit_message_text(text, reply_markup=main_menu())
        return

    anomaly_id, kind, payload, created_at, fixed_at, decrypted_at, status = a
    age_min = max(0, (now_utc() - created_at) // 60)

    if status == "SENT":
        text = (
            hdr()
            + "⚠️ ОБНАРУЖЕНА АНОМАЛИЯ\n"
            + f"Время обнаружения: {age_min} мин назад\n"
            + "Действие: требуется фиксация.\n"
        )
        kb = anomaly_fix_kb(anomaly_id)
    else:
        # FIXED
        waited = now_utc() - int(fixed_at or 0)
        remaining = max(0, 600 - waited)  # 10 минут
        if remaining > 0:
            text = (
                hdr()
                + "АНОМАЛИЯ ЗАФИКСИРОВАНА\n"
                + f"Ожидание перед расшифровкой: ещё {remaining//60} мин {remaining%60} сек\n"
                + "Протокол: выдержать интервал, затем расшифровать."
            )
            kb = main_menu()
        else:
            text = (
                hdr()
                + "АНОМАЛИЯ ГОТОВА К РАСШИФРОВКЕ\n"
                + "Действие: расшифровать пакет данных."
            )
            kb = anomaly_decrypt_kb(anomaly_id)

    if as_message:
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb)

# ========= CALLBACKS =========
async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "Q":
        await send_queue_card(update, context, as_message=False)
    elif data == "A":
        await send_or_check_anomaly(update, context, as_message=False)
    elif data == "TOP":
        await send_top(update, context, as_message=False)
    elif data == "HELP":
        await send_help(update, context, as_message=False)

async def on_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    user_id = update.effective_user.id

    parts = q.data.split(":")
    anomaly_id = int(parts[1])

    # фиксируем
    set_fixed(conn, anomaly_id)
    add_points(conn, user_id, 2)

    text = (
        hdr()
        + "📌 ФИКСАЦИЯ ПРИНЯТА\n"
        + "Протокол: выдержать интервал 10 минут.\n"
        + "Затем появится расшифровка.\n\n"
        + "Подсказка: кнопку «Проверить аномалию» можно нажать позже."
        + "\nНаграда: +2 pts"
    )
    await q.edit_message_text(text, reply_markup=main_menu())

async def on_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    user_id = update.effective_user.id

    anomaly_id = int(q.data.split(":")[1])

    # проверяем таймер 10 минут
    cur = conn.execute("SELECT fixed_at, kind, payload, status FROM anomalies WHERE id=? AND user_id=?", (anomaly_id, user_id))
    row = cur.fetchone()
    if not row:
        await q.edit_message_text(hdr() + "Аномалия не найдена.", reply_markup=main_menu())
        return

    fixed_at, kind, payload, status = row
    if status != "FIXED":
        await q.edit_message_text(hdr() + "Расшифровка недоступна.", reply_markup=main_menu())
        return

    waited = now_utc() - int(fixed_at or 0)
    if waited < 600:
        remaining = 600 - waited
        await q.edit_message_text(
            hdr() + f"Слишком рано.\nОжидайте ещё {remaining//60} мин {remaining%60} сек.",
            reply_markup=main_menu()
        )
        return

    # расшифровка
    set_decrypted(conn, anomaly_id)

    # награда
    reward = 5 if kind == "S" else 3
    add_points(conn, user_id, reward)

    text = (
        hdr()
        + "🔎 РАСШИФРОВКА\n"
        + f"Класс: {kind}\n\n"
        + payload
        + f"\n\nНаграда: +{reward} pts"
    )
    await q.edit_message_text(text, reply_markup=main_menu())

# ========= SCHEDULER JOBS =========
async def job_push_queue(context: ContextTypes.DEFAULT_TYPE):
    """
    Каждые 6 часов ДНЁМ — рассылка справки о позиции.
    Мы запускаем job каждый час, но отправляем только в нужные локальные часы.
    """
    utc_ts = now_utc()
    if not is_daytime_for_queue(utc_ts):
        return

    conn = db()
    users = all_users(conn)
    for user_id, callsign, pts in users:
        pos, total = queue_position(conn, user_id)
        text = (
            hdr()
            + "СПРАВКА (AUTO)\n"
            + f"Позиция: {pos}/{total}\n"
            + f"Очки: {pts}\n"
            + "Доступ контролируется NEZ Project.\n"
        )
        try:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=main_menu())
        except Exception:
            # если юзер заблокировал бота — просто пропускаем
            pass

async def job_spawn_anomalies(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускать 3 раза в сутки в случайные часы (днём).
    Эта job рассылает аномалию всем пользователям.
    """
    conn = db()
    users = all_users(conn)
    if not users:
        return

    # для каждого пользователя решаем класс по позиции
    for user_id, callsign, pts in users:
        pos, total = queue_position(conn, user_id)
        pS = chance_class_s(pos, total)
        is_s = random.random() < pS

        if is_s:
            track_id = random.randint(1, 12)
            payload = s_payload_stub(track_id)
            kind = "S"
        else:
            payload = random.choice(NOCLASS_PAYLOADS)
            kind = "NOCLASS"

        anomaly_id = create_anomaly(conn, user_id, kind, payload)

        text = (
            hdr()
            + "⚠️ ОБНАРУЖЕНА АНОМАЛИЯ\n"
            + "Действие: требуется срочная фиксация.\n"
            + "Чем быстрее фиксация — тем выше приоритет в очереди."
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=anomaly_fix_kb(anomaly_id)
            )
        except Exception:
            pass

def seconds_until_local_hour(target_hour: int) -> int:
    """
    Через сколько секунд наступит ближайший target_hour (локально, по TZ_OFFSET).
    """
    utc = now_utc()
    local = utc + TZ_OFFSET * 3600
    lt = time.gmtime(local)  # используем UTC как "локальный" после сдвига
    current_sec = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    target_sec = target_hour * 3600
    if target_sec <= current_sec:
        # завтра
        return (24 * 3600 - current_sec) + target_sec
    return target_sec - current_sec

def random_day_anomaly_hours() -> List[int]:
    # 3 раза в день, рандомно, "днём": 11..22
    hours = random.sample(range(11, 23), 3)
    hours.sort()
    return hours

async def schedule_daily_anomalies(app: Application):
    """
    Планируем 3 аномалии на ближайшие 24 часа.
    """
    hours = random_day_anomaly_hours()
    for h in hours:
        delay = seconds_until_local_hour(h)
        app.job_queue.run_once(job_spawn_anomalies, when=delay, name=f"anomaly@{h:02d}")

# ========= APP =========
def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))

    app.add_handler(CallbackQueryHandler(on_fix, pattern=r"^FIX:\d+$"))
    app.add_handler(CallbackQueryHandler(on_decrypt, pattern=r"^DEC:\d+$"))
    app.add_handler(CallbackQueryHandler(on_menu_click, pattern=r"^(Q|A|TOP|HELP)$"))

    # Если человек пишет текст — просто показываем меню (чтобы не терялся)
    async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(hdr() + "Открой меню:", reply_markup=main_menu())
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    return app

if __name__ == "__main__":
    application = build_app()

    # 1) очередь: проверяем каждый час, но отправляем только в 10/16/22 локально
    application.job_queue.run_repeating(job_push_queue, interval=3600, first=30)

    # 2) аномалии: планируем 3 окна на сутки, и перепланируем раз в 24ч
    #    (первое расписание сразу при старте)
    async def bootstrap_jobs(app: Application):
        await schedule_daily_anomalies(app)

        async def reschedule(context: ContextTypes.DEFAULT_TYPE):
            await schedule_daily_anomalies(application)

        # перепланирование каждые 24 часа
        application.job_queue.run_repeating(lambda ctx: application.create_task(reschedule(ctx)),
                                            interval=24*3600, first=24*3600)

    application.create_task(bootstrap_jobs(application))

    # запуск webhook/polling
    if BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/telegram"
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=webhook_url,
            drop_pending_updates=True
        )
    else:
        application.run_polling(drop_pending_updates=True)

import os
import sqlite3
import random
import time
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ====== CONFIG ======
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL")  # например: https://your-service.onrender.com
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Нет переменной окружения BOT_TOKEN")

DB_PATH = "nez.db"

OBSERVER_TYPES = [
    "СЕНСОР", "КАРТОГРАФ", "ПРОТОКОЛИСТ", "ИНТЕРПРЕТАТОР", "ОПЕРАТОР",
    "КОРРЕЛЯТОР", "СВИДЕТЕЛЬ", "ИЗОЛЯТОР", "АНАЛИТИК СБОЕВ", "РЕЗОНАТОР"
]

# ====== DB ======
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        callsign TEXT,
        tz TEXT,
        stress INTEGER,
        anomalies TEXT,
        preference TEXT,
        observer_type TEXT,
        clearance INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        last_daily INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    return conn

def get_user(conn, user_id: int):
    cur = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cur.fetchone()

def upsert_user(conn, user_id: int, **fields):
    existing = get_user(conn, user_id)
    if not existing:
        cols = ",".join(["user_id"] + list(fields.keys()))
        qs = ",".join(["?"] * (1 + len(fields)))
        vals = [user_id] + list(fields.values())
        conn.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", vals)
    else:
        sets = ",".join([f"{k}=?" for k in fields.keys()])
        vals = list(fields.values()) + [user_id]
        conn.execute(f"UPDATE users SET {sets} WHERE user_id=?", vals)
    conn.commit()

def add_points(conn, user_id: int, delta: int):
    conn.execute("UPDATE users SET points = COALESCE(points,0) + ? WHERE user_id=?", (delta, user_id))
    conn.commit()

def set_clearance(conn, user_id: int, value: int):
    conn.execute("UPDATE users SET clearance=? WHERE user_id=?", (value, user_id))
    conn.commit()

def top_rank(conn, limit=10):
    cur = conn.execute("SELECT callsign, points, observer_type, clearance FROM users ORDER BY points DESC LIMIT ?", (limit,))
    return cur.fetchall()

# ====== LORE TEXT ======
def header():
    return "NEZ PROJECT // PROGRAM DATA EXCHANGE\nSTATUS: ACTIVE\n"

def sanitized_fragment(clearance: int) -> str:
    fragments = [
        "[ARCHIVE/FRAG-01] САНИТИЗИРОВАНО: зафиксирован след интерференции в речи субъекта. Источник удалён.",
        "[ARCHIVE/FRAG-07] САНИТИЗИРОВАНО: объект «Нулевой Эдем» проявляется через восприятие. Техника теряет сигнал.",
        "[INCIDENT/LOG-03] САНИТИЗИРОВАНО: точка соприкосновения закрыта. Причина: рост когнитивных искажений.",
        "[PROTOCOL/SAFE-02] САНИТИЗИРОВАНО: не пытайтесь «искать вход». Фиксируйте только симптомы и среду.",
        "[SIGNAL/NOISE] …текст разорван… [DATA EXPUNGED] …отражение отвечает…"
    ]
    base = random.choice(fragments)
    if clearance >= 2:
        base += "\n[NOTE] Допуск позволяет видеть корреляции. Следующие 72ч считаются нестабильными."
    if clearance >= 4:
        base += "\n[REDACTION FAILURE] ███ не ошибка. ███ это форма."
    return base

def daily_protocol() -> str:
    lines = [
        "БЮЛЛЕТЕНЬ: уровень фоновой интерференции выше нормы.",
        "БЮЛЛЕТЕНЬ: повторяемость дежавю у наблюдателей выросла.",
        "БЮЛЛЕТЕНЬ: отмечены ложные совпадения времени (ощущение «прыжков»).",
        "БЮЛЛЕТЕНЬ: сегодня предпочтительны кабинетные наблюдения.",
        "БЮЛЛЕТЕНЬ: избегайте обсуждения процедур проникновения. Сбор только санитизированных данных."
    ]
    return random.choice(lines)

# ====== ASSIGN TYPE ======
def assign_observer_type(user_id: int, stress: int, anomalies: str, preference: str) -> str:
    # простая “классификация”: смесь хэша и параметров — чтобы выглядело как система
    seed = (user_id * 31 + stress * 7 + len(anomalies) * 13 + len(preference) * 17) & 0xFFFFFFFF
    rnd = random.Random(seed)
    # чуть сместим вероятности по preference
    pool = OBSERVER_TYPES.copy()
    if "полев" in preference.lower():
        pool += ["КАРТОГРАФ", "ОПЕРАТОР"]
    if "архив" in preference.lower() or "кабин" in preference.lower():
        pool += ["ПРОТОКОЛИСТ", "ИНТЕРПРЕТАТОР", "КОРРЕЛЯТОР"]
    if stress >= 7:
        pool += ["АНАЛИТИК СБОЕВ", "РЕЗОНАТОР"]
    return rnd.choice(pool)

# ====== Conversation States ======
CALLSIGN, TZ, STRESS, ANOMALIES, PREF = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)

    text = (
        header()
        + "ТЕРМИНАЛ ДОСТУПА: @NEZ_PRJ\n"
        + "РЕЖИМ: ВЕРБОВКА\n\n"
        + "Вы подключаетесь к программе обмена данными.\n"
        + "Доступ к материалам ограничен. Плата: фиксированный пакет метрик восприятия.\n\n"
    )

    if u and u[1]:
        text += "Досье найдено. Команды: /daily /archive /profile /rank"
        await update.message.reply_text(text)
        return ConversationHandler.END

    text += "Создать досье наблюдателя? Введите позывной (любое имя/ник, латиница или кириллица)."
    await update.message.reply_text(text)
    return CALLSIGN

async def callsign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["callsign"] = update.message.text.strip()[:32]
    await update.message.reply_text("Часовой пояс? (например: Europe/Amsterdam или MSK/UTC+3)")
    return TZ

async def tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tz"] = update.message.text.strip()[:32]
    await update.message.reply_text("Оцените уровень стресса сейчас (0–10). Введите число.")
    return STRESS

async def stress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = int(update.message.text.strip())
        s = max(0, min(10, s))
    except:
        s = 5
    context.user_data["stress"] = s

    kb = ReplyKeyboardMarkup(
        [["дежавю", "сонный паралич"], ["провалы памяти", "тревога без причины"], ["ничего из списка"]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("Отмечались ли аномальные состояния? (выберите или напишите своё)", reply_markup=kb)
    return ANOMALIES

async def anomalies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["anomalies"] = update.message.text.strip()[:200]
    kb = ReplyKeyboardMarkup(
        [["полевые наблюдения"], ["кабинетные/архив"], ["смешанный режим"]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("Предпочтительный режим?", reply_markup=kb)
    return PREF

async def pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    preference = update.message.text.strip()[:50]
    context.user_data["preference"] = preference

    conn = db()
    user_id = update.effective_user.id
    callsign_ = context.user_data["callsign"]
    tz_ = context.user_data["tz"]
    stress_ = context.user_data["stress"]
    anomalies_ = context.user_data["anomalies"]

    otype = assign_observer_type(user_id, stress_, anomalies_, preference)

    upsert_user(
        conn, user_id,
        callsign=callsign_, tz=tz_, stress=stress_,
        anomalies=anomalies_, preference=preference,
        observer_type=otype, clearance=1, points=3, streak=1, last_daily=0
    )

    msg = (
        header()
        + f"ДОСЬЕ СОЗДАНО\nID: {user_id}\nПОЗЫВНОЙ: {callsign_}\nТИП: {otype}\nДОПУСК: 1\n\n"
        + "ВЫДАН ПЕРВЫЙ САНИТИЗИРОВАННЫЙ ФРАГМЕНТ:\n"
        + sanitized_fragment(1)
        + "\n\nКоманды: /daily /archive /profile /rank"
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Досье не найдено. /start")
        return
    # columns order from table:
    # user_id, callsign, tz, stress, anomalies, preference, observer_type, clearance, points, streak, last_daily
    msg = (
        header()
        + "ДОСЬЕ НАБЛЮДАТЕЛЯ\n"
        + f"ПОЗЫВНОЙ: {u[1]}\nТИП: {u[6]}\nДОПУСК: {u[7]}\nРЕЙТИНГ: {u[8]}\nСЕРИЯ ДНЕЙ: {u[9]}\n"
        + f"TZ: {u[2]}\nСТРЕСС: {u[3]}\nАНОМАЛИИ: {u[4]}\nРЕЖИМ: {u[5]}"
    )
    await update.message.reply_text(msg)

async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = top_rank(conn, limit=10)
    if not rows:
        await update.message.reply_text("Нет данных.")
        return
    lines = ["NEZ PROJECT // RANKING (SANITIZED)\n"]
    for i, (callsign, pts, otype, clr) in enumerate(rows, start=1):
        lines.append(f"{i:02d}. {callsign} — {pts} pts — {otype} — C{clr}")
    await update.message.reply_text("\n".join(lines))

async def archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Досье не найдено. /start")
        return
    clr = int(u[7] or 0)
    add_points(conn, user_id, 1)
    await update.message.reply_text(header() + "ВЫДАЧА АРХИВА:\n" + sanitized_fragment(clr) + "\n\n(+1 к индексу участия)")

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Досье не найдено. /start")
        return

    now = int(time.time())
    last = int(u[10] or 0)
    # 20 часов “суток” для простоты
    if now - last < 20 * 3600:
        await update.message.reply_text(header() + "ПРОТОКОЛ ДНЯ уже выполнен. Ожидайте следующего окна.")
        return

    # начисления и рост допуска
    points = int(u[8] or 0)
    clearance = int(u[7] or 0)
    streak = int(u[9] or 0)

    streak += 1
    gain = 2 + (1 if streak % 3 == 0 else 0)  # бонус за серию
    points += gain

    # каждые 12 очков — допуск вверх (до 5)
    new_clearance = min(5, max(clearance, points // 12))

    conn.execute(
        "UPDATE users SET points=?, clearance=?, streak=?, last_daily=? WHERE user_id=?",
        (points, new_clearance, streak, now, user_id)
    )
    conn.commit()

    msg = (
        header()
        + "ПРОТОКОЛ ДНЯ\n"
        + daily_protocol()
        + "\n\nКАЛИБРОВКА (ответьте одной строкой):\n"
        + "1) Реальность 1–10\n"
        + "2) Было ли ощущение «скачка времени»?\n"
        + "3) Один образ из сна (если был)\n\n"
        + f"НАЧИСЛЕНО: +{gain} pts\n"
        + f"ТЕКУЩИЙ ДОПУСК: {new_clearance}\n\n"
        + "Подсказка: /archive выдаёт фрагменты (и тоже двигает рейтинг)."
    )
    await update.message.reply_text(msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — регистрация\n"
        "/daily — протокол дня\n"
        "/archive — фрагмент архива\n"
        "/profile — досье\n"
        "/rank — рейтинг"
    )

def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CALLSIGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, callsign)],
            TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, tz)],
            STRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, stress)],
            ANOMALIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, anomalies)],
            PREF: [MessageHandler(filters.TEXT & ~filters.COMMAND, pref)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("archive", archive))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("help", help_cmd))
    return app

if __name__ == "__main__":
    application = build_app()

    # Webhook mode (для хостинга)
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
        # fallback: локально на компе
        application.run_polling(drop_pending_updates=True)

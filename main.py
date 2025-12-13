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

TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL")
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Нет переменной окружения BOT_TOKEN")

DB_PATH = "nez.db"

OBSERVER_TYPES = [
    "СЕНСОР", "КАРТОГРАФ", "ПРОТОКОЛИСТ", "ИНТЕРПРЕТАТОР", "ОПЕРАТОР",
    "КОРРЕЛЯТОР", "СВИДЕТЕЛЬ", "ИЗОЛЯТОР", "АНАЛИТИК СБОЕВ", "РЕЗОНАТОР"
]

# ===== UI =====
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📋 Протокол дня", "🧭 Задание"],
        ["📡 Архив", "🗂 Досье"],
        ["🏆 Рейтинг", "ℹ️ Помощь"]
    ],
    resize_keyboard=True
)

# ===== DB =====
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
        last_daily INTEGER DEFAULT 0,
        last_task INTEGER DEFAULT 0,
        pending_task TEXT DEFAULT NULL
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

def top_rank(conn, limit=10):
    cur = conn.execute("SELECT callsign, points, observer_type, clearance FROM users ORDER BY points DESC LIMIT ?", (limit,))
    return cur.fetchall()

# ===== LORE / TEXT =====
def header():
    return "NEZ PROJECT // DATA EXCHANGE TERMINAL\n"

def explain_cycle(u) -> str:
    # u columns: user_id, callsign, tz, stress, anomalies, preference, observer_type, clearance, points, streak, ...
    return (
        "Как пользоваться терминалом:\n"
        "1) 📋 *Протокол дня* — короткая калибровка. Даёт очки.\n"
        "2) 🧭 *Задание* — 1 конкретная проверка/наблюдение. Даёт очки.\n"
        "3) 📡 *Архив* — фрагмент материалов (награда/лут).\n\n"
        "Чем выше очки участия — тем выше *допуск* и тем интереснее архив."
    )

def sanitized_fragment(clearance: int) -> str:
    fragments = [
        "[ARCHIVE/FRAG-01] САНИТИЗИРОВАНО: след интерференции в речи субъекта. Источник удалён.",
        "[ARCHIVE/FRAG-07] САНИТИЗИРОВАНО: объект проявляется через восприятие. Техника теряет сигнал.",
        "[INCIDENT/LOG-03] САНИТИЗИРОВАНО: точка соприкосновения закрыта. Причина: рост когнитивных искажений.",
        "[PROTOCOL/SAFE-02] САНИТИЗИРОВАНО: фиксируйте только симптомы и среду. Не ищите «вход».",
        "[SIGNAL/NOISE] …текст разорван… [DATA EXPUNGED] …отражение отвечает…"
    ]
    base = random.choice(fragments)
    if clearance >= 2:
        base += "\n[NOTE] Допуск: разрешены корреляции. Следующие 72ч нестабильны."
    if clearance >= 4:
        base += "\n[REDACTION FAILURE] ███ это не ошибка. ███ это форма."
    return base

def daily_bulletin() -> str:
    lines = [
        "БЮЛЛЕТЕНЬ: уровень фоновой интерференции выше нормы.",
        "БЮЛЛЕТЕНЬ: повторяемость дежавю у наблюдателей выросла.",
        "БЮЛЛЕТЕНЬ: отмечены ложные совпадения времени (ощущение «прыжков»).",
        "БЮЛЛЕТЕНЬ: сегодня предпочтительны кабинетные наблюдения.",
        "БЮЛЛЕТЕНЬ: минимизируйте шум. Не обсуждайте процедуры проникновения."
    ]
    return random.choice(lines)

# ===== TASKS =====
TASK_POOL = [
    {
        "id": "silence60",
        "title": "ТЕСТ ТИШИНЫ / 60 СЕК",
        "text": "В течение 60 секунд посиди без музыки.\nЗапиши *3 звука*, которые заметил(а) (даже если это «тишина/вентиляция/сердце»).",
        "reward": 3
    },
    {
        "id": "reality_scale",
        "title": "КАЛИБРОВКА РЕАЛЬНОСТИ",
        "text": "Оцени «реальность» по шкале 1–10.\nОдной строкой: `R=7 потому что ...`",
        "reward": 2
    },
    {
        "id": "dejavu_check",
        "title": "ПРОВЕРКА ДЕЖАВЮ",
        "text": "Было ли сегодня ощущение «я это уже видел(а)»?\nОтвет: `да/нет` + 1 короткая деталь (где/когда).",
        "reward": 3
    },
    {
        "id": "text_anomaly",
        "title": "ТЕСТ АНОМАЛИИ ТЕКСТА",
        "text": "Выбери строку, которая «не своя»:\nA) время ровное\nB) тени не отстают\nC) стены запоминают\nD) воздух пустой\nE) голос — мой\nОтветь буквой: A/B/C/D/E",
        "reward": 4
    },
]

def assign_task(seed: int) -> dict:
    rnd = random.Random(seed)
    return rnd.choice(TASK_POOL)

def assign_observer_type(user_id: int, stress: int, anomalies: str, preference: str) -> str:
    seed = (user_id * 31 + stress * 7 + len(anomalies) * 13 + len(preference) * 17) & 0xFFFFFFFF
    rnd = random.Random(seed)
    pool = OBSERVER_TYPES.copy()
    if "полев" in preference.lower():
        pool += ["КАРТОГРАФ", "ОПЕРАТОР"]
    if "архив" in preference.lower() or "кабин" in preference.lower():
        pool += ["ПРОТОКОЛИСТ", "ИНТЕРПРЕТАТОР", "КОРРЕЛЯТОР"]
    if stress >= 7:
        pool += ["АНАЛИТИК СБОЕВ", "РЕЗОНАТОР"]
    return rnd.choice(pool)

# ===== Conversation States (registration) =====
CALLSIGN, TZ, STRESS, ANOMALIES, PREF = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)

    if u and u[1]:
        msg = (
            header()
            + f"ДОБРОВОЛЕЦ: {u[1]} / ТИП: {u[6]} / ДОПУСК: {u[7]} / ОЧКИ: {u[8]}\n\n"
            + explain_cycle(u)
        )
        await update.message.reply_text(msg, reply_markup=MAIN_KB, parse_mode="Markdown")
        return ConversationHandler.END

    msg = (
        header()
        + "РЕЖИМ: ВЕРБОВКА\n"
        + "Вы подключаетесь к программе обмена данными.\n"
        + "Доступ ограничен. Плата — пакет метрик восприятия.\n\n"
        + "Создаём досье.\n"
        + "Введите позывной (любое имя/ник)."
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    return CALLSIGN

async def callsign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["callsign"] = update.message.text.strip()[:32]
    await update.message.reply_text("Часовой пояс? (например: UTC+3)")
    return TZ

async def tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tz"] = update.message.text.strip()[:32]
    await update.message.reply_text("Стресс сейчас 0–10? (числом)")
    return STRESS

async def stress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = int(update.message.text.strip())
        s = max(0, min(10, s))
    except:
        s = 5
    context.user_data["stress"] = s

    kb = ReplyKeyboardMarkup(
        [["дежавю", "сонный паралич"], ["провалы памяти", "тревога"], ["ничего"]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("Аномалии восприятия? (выбери или напиши своё)", reply_markup=kb)
    return ANOMALIES

async def anomalies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["anomalies"] = update.message.text.strip()[:200]
    kb = ReplyKeyboardMarkup(
        [["полевые"], ["кабинетные/архив"], ["смешанные"]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("Режим наблюдения?", reply_markup=kb)
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
        observer_type=otype, clearance=1, points=0, streak=0, last_daily=0, last_task=0, pending_task=None
    )

    msg = (
        header()
        + f"ДОСЬЕ СОЗДАНО\n"
        + f"ПОЗЫВНОЙ: {callsign_}\nТИП: {otype}\nДОПУСК: 1\n\n"
        + "Готово. Теперь всё делается через кнопки снизу.\n\n"
        + explain_cycle((None, callsign_, tz_, stress_, anomalies_, preference, otype, 1, 0, 0))
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB, parse_mode="Markdown")
    return ConversationHandler.END

# ===== Actions =====
def calc_clearance(points: int) -> int:
    # 0..5
    return min(5, points // 12 + 1)  # стартовый допуск 1

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Досье не найдено. Нажми /start", reply_markup=MAIN_KB)
        return
    msg = (
        header()
        + "ДОСЬЕ НАБЛЮДАТЕЛЯ\n"
        + f"ПОЗЫВНОЙ: {u[1]}\n"
        + f"ТИП: {u[6]}\n"
        + f"ДОПУСК: {u[7]}\n"
        + f"ОЧКИ: {u[8]}\n"
        + f"СЕРИЯ ДНЕЙ: {u[9]}\n\n"
        + "Что делать дальше: 📋 Протокол дня → 🧭 Задание → 📡 Архив"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def show_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = top_rank(conn, limit=10)
    if not rows:
        await update.message.reply_text("Пока нет данных.", reply_markup=MAIN_KB)
        return
    lines = ["NEZ PROJECT // RANKING (SANITIZED)\n"]
    for i, (callsign, pts, otype, clr) in enumerate(rows, start=1):
        lines.append(f"{i:02d}. {callsign} — {pts} pts — {otype} — C{clr}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Сначала /start", reply_markup=MAIN_KB)
        return
    clr = int(u[7] or 1)
    add_points(conn, user_id, 1)

    # пересчёт допуска
    u2 = get_user(conn, user_id)
    pts = int(u2[8] or 0)
    new_clr = max(int(u2[7] or 1), calc_clearance(pts))
    upsert_user(conn, user_id, clearance=new_clr)

    msg = (
        header()
        + "ВЫДАЧА АРХИВА\n"
        + sanitized_fragment(new_clr)
        + f"\n\nНаграда: +1 очко\nТекущие очки: {pts}\nДопуск: {new_clr}"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Сначала /start", reply_markup=MAIN_KB)
        return

    now = int(time.time())
    last = int(u[10] or 0)
    if now - last < 20 * 3600:
        await update.message.reply_text(
            header() + "ПРОТОКОЛ ДНЯ уже выполнен.\nПопробуй 🧭 Задание или 📡 Архив.",
            reply_markup=MAIN_KB
        )
        return

    streak = int(u[9] or 0) + 1
    gain = 3  # базовая награда
    if streak % 3 == 0:
        gain += 1  # бонус за серию

    add_points(conn, user_id, gain)
    conn.execute("UPDATE users SET streak=?, last_daily=? WHERE user_id=?", (streak, now, user_id))
    conn.commit()

    u2 = get_user(conn, user_id)
    pts = int(u2[8] or 0)
    new_clr = max(int(u2[7] or 1), calc_clearance(pts))
    upsert_user(conn, user_id, clearance=new_clr)

    msg = (
        header()
        + "📋 ПРОТОКОЛ ДНЯ\n"
        + daily_bulletin()
        + "\n\nОтветь одной строкой (можно коротко):\n"
        + "R=1–10; jump=yes/no; dream=1 образ\n"
        + "пример: `R=7; jump=no; dream=лифт`\n\n"
        + f"Награда: +{gain} очка\nОчки: {pts}\nДопуск: {new_clr}\n\n"
        + "Дальше: нажми 🧭 Задание (конкретная миссия на сегодня)."
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u:
        await update.message.reply_text("Сначала /start", reply_markup=MAIN_KB)
        return

    now = int(time.time())
    last = int(u[11] or 0)
    if now - last < 6 * 3600 and u[12]:
        # уже есть активное задание
        await update.message.reply_text(
            header() + "У тебя уже есть активное задание. Ответь на него обычным сообщением.\n\n"
            + f"АКТИВНО:\n{u[12]}",
            reply_markup=MAIN_KB
        )
        return

    # выдаём новое задание
    seed = (user_id * 101 + int(now // (6 * 3600))) & 0xFFFFFFFF
    t = assign_task(seed)
    pending_text = f"{t['title']}\n{t['text']}\n\nЧтобы сдать: просто напиши ответ сообщением."
    upsert_user(conn, user_id, last_task=now, pending_task=pending_text)

    await update.message.reply_text(
        header() + "🧭 ЗАДАНИЕ ВЫДАНО\n\n" + pending_text,
        reply_markup=MAIN_KB
    )

async def submit_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # принимаем любые сообщения как возможный ответ на активное задание
    conn = db()
    user_id = update.effective_user.id
    u = get_user(conn, user_id)
    if not u or not u[12]:
        # нет активного задания — подсказываем кнопки
        return

    answer = update.message.text.strip()
    pending = u[12]

    # вычислим награду по заголовку (простая привязка)
    reward = 3
    for t in TASK_POOL:
        if t["title"] in pending:
            reward = t["reward"]
            break

    add_points(conn, user_id, reward)
    # закрываем задание
    upsert_user(conn, user_id, pending_task=None)

    u2 = get_user(conn, user_id)
    pts = int(u2[8] or 0)
    new_clr = max(int(u2[7] or 1), calc_clearance(pts))
    upsert_user(conn, user_id, clearance=new_clr)

    msg = (
        header()
        + "ПРИНЯТО.\n"
        + "Данные добавлены в пакет наблюдения (санитизировано).\n\n"
        + f"Награда: +{reward} очка\nОчки: {pts}\nДопуск: {new_clr}\n\n"
        + "Хочешь лут: нажми 📡 Архив."
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        header()
        + "ℹ️ ПОМОЩЬ\n\n"
        + "Кнопки:\n"
        + "📋 Протокол дня — ежедневная калибровка (очки)\n"
        + "🧭 Задание — 1 миссия, сдаёшь ответ сообщением (очки)\n"
        + "📡 Архив — фрагменты материалов (лут)\n"
        + "🗂 Досье — твой статус\n"
        + "🏆 Рейтинг — топ по очкам\n\n"
        + "Команды (если надо): /start /daily /task /archive /profile /rank"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

# ===== Button router =====
async def buttons_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "🗂 Досье":
        await show_profile(update, context)
    elif text == "📋 Протокол дня":
        await daily(update, context)
    elif text == "📡 Архив":
        await show_archive(update, context)
    elif text == "🧭 Задание":
        await task(update, context)
    elif text == "🏆 Рейтинг":
        await show_rank(update, context)
    elif text == "ℹ️ Помощь":
        await help_cmd(update, context)

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

    # команды
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("task", task))
    app.add_handler(CommandHandler("archive", show_archive))
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("rank", show_rank))
    app.add_handler(CommandHandler("help", help_cmd))

    # кнопки меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buttons_router))

    # ответы на задания (любая строка, если есть pending_task)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submit_answer))

    return app

if __name__ == "__main__":
    application = build_app()

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

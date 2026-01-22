import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, time, date
from pathlib import Path
from threading import Thread
from typing import Any
from telegram.error import BadRequest

from flask import Flask
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)

# =======================
# ЛОГИРОВАНИЕ
# =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("school_bot")

# =======================
# ФАЙЛЫ/ПАПКИ
# =======================
DATA_DIR = Path(".")
SETTINGS_FILE = DATA_DIR / "settings.json"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
DUTY_FILE = DATA_DIR / "duty_list.json"
HOMEWORK_FILE = DATA_DIR / "homework.json"
JOKES_FILE = DATA_DIR / "jokes.json"
TMP_UPLOADS = DATA_DIR / "tmp_uploads"

TMP_UPLOADS.mkdir(parents=True, exist_ok=True)

# =======================
# КОНФИГ
# =======================

@dataclass(frozen=True)
class Config:
    token: str
    # можно менять в коде при желании
    homework_ttl_days: int = 14
    duty_reminder_time: time = time(4, 30, 0)
    homework_cleanup_time: time = time(4, 10, 0)

    school_start: date = date(2024, 9, 2)
    holiday_periods: tuple[tuple[date, date], ...] = (
        (date(2024, 6, 1), date(2024, 8, 31)),
    )

# =======================
# JSON ХРАНИЛИЩЕ
# =======================

class JsonStore:
    @staticmethod
    def load(path: Path, default):
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("Файл %s битый JSON. Возвращаю default.", path)
            return default

    @staticmethod
    def save(path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

# =======================
# SETTINGS (chat_id, admins)
# =======================

def settings_default() -> dict[str, Any]:
    return {
        "chat_id": None,          # основной чат для рассылок /test /s /si
        "admins": [],             # ["@username", ...]
    }

def load_settings() -> dict[str, Any]:
    s = JsonStore.load(SETTINGS_FILE, default=settings_default())
    # гарантируем поля
    if "chat_id" not in s:
        s["chat_id"] = None
    if "admins" not in s:
        s["admins"] = []
    return s

def save_settings(s: dict[str, Any]) -> None:
    JsonStore.save(SETTINGS_FILE, s)

# =======================
# ВСПОМОГАТЕЛЬНОЕ
# =======================

DOW_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DOW_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DOW_CANON = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]

RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

def username_tag(update: Update) -> str:
    u = update.effective_user
    if not u or not u.username:
        return ""
    return f"@{u.username}"

def ensure_first_admin_if_empty(update: Update) -> None:
    """
    Чтобы полностью убрать хардкод админов:
    если admins пустой, то первый человек, кто пишет /menu в ЛС, становится админом.
    """
    if not is_private(update):
        return

    s = load_settings()
    if s["admins"]:
        return

    u = username_tag(update)
    if not u:
        return

    s["admins"] = [u]
    save_settings(s)
    logger.warning("Admins list was empty. Set first admin: %s", u)

def is_admin(update: Update) -> bool:
    s = load_settings()
    u = username_tag(update)
    return u in set(s.get("admins", []))

def parse_chat_id(text: str) -> int | None:
    t = text.strip()
    if not t:
        return None
    # разрешаем "-100..." и обычные числа
    if re.fullmatch(r"-?\d{4,20}", t):
        try:
            return int(t)
        except ValueError:
            return None
    return None

def normalize_text_for_send(text: str) -> str:
    """
    - превращает "\\n" в реальный перенос
    - сохраняет обычные переносы
    """
    return text.replace("\\n", "\n")

# =======================
# РАСПИСАНИЕ: алиасы
# =======================

def build_schedule_aliases(day_canon: str) -> list[str]:
    """
    Генерим алиасы для удобства:
    - рус/нижний регистр
    - краткие
    - английские mon/tue/wed/thu/fri + mn (как ты просил)
    - пример опечатки для Понедельник: "Понедельнк"
    """
    day_lower = day_canon.lower()

    mapping = {
        "Понедельник": ["пн", "понедельнк", "mon", "mn"],
        "Вторник": ["вт", "tue", "tu"],
        "Среда": ["ср", "wed", "we"],
        "Четверг": ["чт", "thu", "th"],
        "Пятница": ["пт", "fri", "fr"],
    }
    extra = mapping.get(day_canon, [])
    return [day_canon, day_lower, *extra]

def normalize_day_query(raw: str) -> str:
    """
    Приводим запрос пользователя к ключу, который точно есть в schedule.json.
    Поскольку schedule.json содержит алиасы как отдельные ключи — достаточно .lower/strip.
    """
    return raw.strip().lower()

# =======================
# ДОМАШКА: даты + TTL от даты задания
# =======================

def _parse_numeric_date_token(token: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})", token.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def next_weekday(from_date: date, target_weekday: int) -> date:
    delta = (target_weekday - from_date.weekday() + 7) % 7
    if delta == 0:
        delta = 7
    return from_date + timedelta(days=delta)

def parse_date_and_consumed(args: list[str]) -> tuple[date, int] | None:
    if not args:
        return None

    today = datetime.now().date()
    a0 = args[0].lower().strip()

    if a0 == "завтра":
        return today + timedelta(days=1), 1

    if a0 in DOW_SHORT:
        return next_weekday(today, DOW_SHORT.index(a0)), 1
    if a0 in DOW_FULL:
        return next_weekday(today, DOW_FULL.index(a0)), 1

    nm = _parse_numeric_date_token(a0)
    if nm:
        dd, mm = nm
        return date(today.year, mm, dd), 1

    if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
        dd = int(args[0])
        mm = int(args[1])
        return date(today.year, mm, dd), 2

    if len(args) >= 2 and args[0].isdigit():
        dd = int(args[0])
        mword = args[1].lower().strip()
        if mword in RU_MONTHS:
            return date(today.year, RU_MONTHS[mword], dd), 2

    return None

def expiry_of_homework(d: date, cfg: Config) -> date:
    return d + timedelta(days=cfg.homework_ttl_days)

def cleanup_homework_in_memory(hw: dict, cfg: Config) -> tuple[dict, int]:
    today = datetime.now().date()
    removed = 0
    cleaned = {}
    for k, v in hw.items():
        try:
            d = date.fromisoformat(k)
        except ValueError:
            cleaned[k] = v
            continue
        if today > expiry_of_homework(d, cfg):
            removed += 1
        else:
            cleaned[k] = v
    return cleaned, removed

def load_homework_clean(cfg: Config) -> dict:
    hw = JsonStore.load(HOMEWORK_FILE, default={})
    hw2, removed = cleanup_homework_in_memory(hw, cfg)
    if removed:
        JsonStore.save(HOMEWORK_FILE, hw2)
    return hw2

# =======================
# DUTY LIST: ученики
# =======================

END_WORDS = {"end", "все", "стоп"}

def normalize_username_input(t: str) -> str | None:
    """
    Принимаем "@user" или "user"
    Возвращаем "@user" в нижнем регистре (Telegram username case-insensitive)
    """
    s = t.strip()
    if not s:
        return None
    if s.lower() in END_WORDS:
        return s.lower()
    if s.startswith("@"):
        s = s[1:]
    s = s.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", s):
        return None
    return f"@{s}"

def duty_entry_from_username(u: str) -> str:
    # "<никнейм>, @id" где никнейм = username без @
    nick = u[1:]
    return f"{nick}, {u}"

# =======================
# Меню в ЛС (кнопки)
# =======================

def menu_keyboard(is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📅 Расписание"), KeyboardButton("🧹 Дежурный")],
        [KeyboardButton("📚 Домашка (dz_list)")],
    ]
    if is_admin_user:
        rows += [
            [KeyboardButton("➕ Добавить чат"), KeyboardButton("➕ Добавить администратора")],
            [KeyboardButton("➕ Добавить учеников")],
            [KeyboardButton("📝 Изменить расписание")],
            [KeyboardButton("🧪 Тест в чат")],
            [KeyboardButton("😂 Добавить анекдот")],
        ]
    rows += [[KeyboardButton("❓ Help")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# =======================
# Conversation states
# =======================
(
    ST_MENU,
    ST_SET_CHAT,
    ST_ADD_ADMIN,
    ST_ADD_STUDENTS,
    ST_EDIT_SCHEDULE,
    ST_JOKE_ADD,
) = range(6)

SI_CHAT, SI_PHOTO, SI_TEXT = range(3)

# =======================
# Команды / handlers
# =======================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["cfg"]

    if is_private(update):
        ensure_first_admin_if_empty(update)

    await update.message.reply_text(
        "Привет! Я бот для расписания/домашки/дежурств 😼\n"
        f"🧹 Домашка авто-удаляется через {cfg.homework_ttl_days} дней ОТ ДАТЫ ЗАДАНИЯ.\n\n"
        "В ЛС со мной можно пользоваться командами, не засоряя общий чат.\n"
        "Открой меню кнопками ниже 👇",
        reply_markup=menu_keyboard(is_admin(update)),
    )
    return ST_MENU

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_private(update):
        ensure_first_admin_if_empty(update)

    await update.message.reply_text(
        "Меню открыто 👇",
        reply_markup=menu_keyboard(is_admin(update)),
    )
    return ST_MENU

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    text = (
        "Команды:\n\n"
        "📅 /r [день] — расписание (пример: /r пн)\n"
        "🧹 /d — дежурный сегодня\n\n"
        "📚 /dz <дата> — показать\n"
        "✍️ /dz <дата> <текст> — сохранить\n"
        "🧾 /dz_list [N] — ближайшие N (по умолчанию 10)\n"
        "🛠 /dz_edit <дата> <текст> — (админы)\n"
        "🗑 /dz_del <дата> — удалить (админы)\n\n"
        f"🧹 TTL: {cfg.homework_ttl_days} дней ОТ ДАТЫ ЗАДАНИЯ\n\n"
        "Форматирование при отправке в чат (для админов):\n"
        "• Используй HTML-теги: <b>жирный</b>, <i>курсив</i>, <u>подчёрк</u>\n"
        "• Перенос строки — обычный Enter или напиши \\n\n\n"
        "Админ-команды:\n"
        "• /s <html-текст> — отправить в основной чат\n"
        "• /si — отправить картинку + текст в основной чат\n"
        "• /d_set ... — управление списком дежурных\n"
        "• /joke — случайный анекдот\n"
        "• /joke_add — добавить анекдот\n"
    )
    await update.message.reply_text(text)

# ---------- schedule ----------
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = JsonStore.load(SCHEDULE_FILE, default={})
    footer = (
        "🔄 <i>'//' — чередование</i>\n"
        "<i>'**' — подгруппы</i>"
    )

    if context.args:
        key = normalize_day_query(" ".join(context.args))
        if key in data:
            msg = f"📅 <b>Расписание ({key}):</b>\n{data[key]}\n\n{footer}"
        else:
            msg = "⚠️ Не нашёл такой день. Пример: /r пн"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    # выводим ПН-ПТ если есть
    msg_parts = ["📅 <b>Расписание (Пн–Пт):</b>"]
    for canon in DOW_CANON:
        canon_key = canon.lower()
        if canon_key in data:
            msg_parts.append(f"\n<b>{canon}:</b>\n{data[canon_key]}")
    msg_parts.append(f"\n\n{footer}")
    await update.message.reply_text("\n".join(msg_parts), parse_mode=ParseMode.HTML)

# ---------- duty ----------
def is_weekend(d: date) -> bool:
    return d.weekday() in (5, 6)

def is_holiday(d: date, cfg: Config) -> bool:
    for start, end in cfg.holiday_periods:
        if start <= d <= end:
            return True
    return False

async def cmd_duty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    duty_list = JsonStore.load(DUTY_FILE, default=[])

    if not duty_list:
        await update.message.reply_text("Список дежурных пуст.")
        return

    today = datetime.now().date()
    if today < cfg.school_start:
        await update.message.reply_text("Учебный год ещё не начался.")
        return
    if is_weekend(today) or is_holiday(today, cfg):
        await update.message.reply_text("Сегодня дежурных нет! Отдыхайте 😎")
        return

    # считаем учебные дни от старта до today
    day_counter = 0
    d = cfg.school_start
    while d <= today:
        if not is_weekend(d) and not is_holiday(d, cfg):
            day_counter += 1
        d += timedelta(days=1)

    idx = (day_counter - 1) % len(duty_list)
    await update.message.reply_text(f"Сегодня дежурный: {duty_list[idx]}")

async def duty_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    s = load_settings()
    chat_id = s.get("chat_id")
    if not chat_id:
        return

    duty_list = JsonStore.load(DUTY_FILE, default=[])
    if not duty_list:
        await context.bot.send_message(chat_id=chat_id, text="Список дежурных пуст.")
        return

    today = datetime.now().date()
    if today < cfg.school_start:
        await context.bot.send_message(chat_id=chat_id, text="Учебный год ещё не начался.")
        return
    if is_weekend(today) or is_holiday(today, cfg):
        await context.bot.send_message(chat_id=chat_id, text="Сегодня дежурных нет! Отдыхайте 😎")
        return

    day_counter = 0
    d = cfg.school_start
    while d <= today:
        if not is_weekend(d) and not is_holiday(d, cfg):
            day_counter += 1
        d += timedelta(days=1)

    idx = (day_counter - 1) % len(duty_list)
    await context.bot.send_message(chat_id=chat_id, text=f"Сегодня дежурный: {duty_list[idx]}")

# ---------- homework ----------
async def homework_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    hw = JsonStore.load(HOMEWORK_FILE, default={})
    hw2, removed = cleanup_homework_in_memory(hw, cfg)
    if removed:
        JsonStore.save(HOMEWORK_FILE, hw2)
    logger.info("Homework cleanup job done. Removed=%d", removed)

async def cmd_homework(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    hw = load_homework_clean(cfg)

    if not context.args:
        await update.message.reply_text("Формат: /dz 25.01 | /dz завтра | /dz 25 января")
        return

    parsed = parse_date_and_consumed(context.args)
    if not parsed:
        await update.message.reply_text("Не понял дату. Пример: /dz 25.01")
        return

    target, consumed = parsed
    rest = context.args[consumed:]

    if rest:
        task = " ".join(rest).strip()
        hw[str(target)] = task
        JsonStore.save(HOMEWORK_FILE, hw)
        exp = expiry_of_homework(target, cfg).strftime("%d.%m.%Y")
        await update.message.reply_text(f"Сохранил на {target.strftime('%d.%m.%Y')} ✅\n🧹 Удалится после {exp}")
        return

    task = hw.get(str(target))
    if task:
        exp = expiry_of_homework(target, cfg).strftime("%d.%m.%Y")
        formatted = task.replace(": ", ":\n").replace(";", ";\n")
        await update.message.reply_text(
            f"Домашка на {target.strftime('%d.%m.%Y')}:\n{formatted}\n\n🧹 Удалится после {exp}"
        )
    else:
        exp = expiry_of_homework(target, cfg).strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"На {target.strftime('%d.%m.%Y')} домашки нет.\n"
            f"Добавить: /dz {target.strftime('%d.%m')} <текст>\n"
            f"🧹 Если добавить — удалится после {exp}"
        )

async def cmd_homework_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    hw = load_homework_clean(cfg)

    n = 10
    if context.args:
        try:
            n = int(context.args[0])
            n = max(1, min(n, 50))
        except ValueError:
            await update.message.reply_text("Формат: /dz_list [N], пример: /dz_list 10")
            return

    today = datetime.now().date()
    items: list[tuple[date, str]] = []
    for k, v in hw.items():
        try:
            d = date.fromisoformat(k)
        except ValueError:
            continue
        if d >= today:
            items.append((d, v))
    items.sort(key=lambda x: x[0])
    items = items[:n]

    if not items:
        await update.message.reply_text("Ближайшей домашки нет 🎉")
        return

    lines = ["🧾 Ближайшая домашка:"]
    for d, task in items:
        short = task.strip().replace("\n", " ")
        if len(short) > 120:
            short = short[:117] + "..."
        exp = expiry_of_homework(d, cfg).strftime("%d.%m.%Y")
        lines.append(f"• {d.strftime('%d.%m.%Y')} (до {exp}): {short}")

    await update.message.reply_text("\n".join(lines))

async def cmd_homework_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    ensure_first_admin_if_empty(update)

    if not is_admin(update):
        await update.message.reply_text("Эта команда только для админов.")
        return

    parsed = parse_date_and_consumed(context.args)
    if not parsed:
        await update.message.reply_text("Формат: /dz_edit 25.01 новый текст")
        return

    target, consumed = parsed
    new_task = " ".join(context.args[consumed:]).strip()
    if not new_task:
        await update.message.reply_text("Новый текст пустой.")
        return

    hw = load_homework_clean(cfg)
    if str(target) not in hw:
        await update.message.reply_text("На эту дату домашки нет.")
        return

    hw[str(target)] = new_task
    JsonStore.save(HOMEWORK_FILE, hw)
    await update.message.reply_text("Готово ✅")

async def cmd_homework_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_first_admin_if_empty(update)

    if not is_admin(update):
        await update.message.reply_text("Эта команда только для админов.")
        return

    parsed = parse_date_and_consumed(context.args)
    if not parsed:
        await update.message.reply_text("Формат: /dz_del 25.01")
        return

    target, _ = parsed
    hw = JsonStore.load(HOMEWORK_FILE, default={})
    if str(target) not in hw:
        await update.message.reply_text("На эту дату домашки нет.")
        return

    del hw[str(target)]
    JsonStore.save(HOMEWORK_FILE, hw)
    await update.message.reply_text(f"Удалил домашку на {target.strftime('%d.%m.%Y')} ✅")

# ---------- jokes ----------
async def cmd_joke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jokes = JsonStore.load(JOKES_FILE, default=[])
    if not jokes:
        await update.message.reply_text("jokes.json пуст 😢\nДобавить можно через меню в ЛС: «😂 Добавить анекдот».")
        return

    import random
    await update.message.reply_text(random.choice(jokes))

async def cmd_joke_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_first_admin_if_empty(update)
    if not is_private(update):
        await update.message.reply_text("Добавлять анекдоты можно в ЛС с ботом.")
        return ConversationHandler.END

    if not is_admin(update):
        await update.message.reply_text("Только для админов.")
        return ConversationHandler.END

    await update.message.reply_text("Ок! Пришли текст анекдота одним сообщением.\nОтмена: /cancel")
    return ST_JOKE_ADD

async def st_joke_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    joke = update.message.text.strip()
    jokes = JsonStore.load(JOKES_FILE, default=[])
    jokes.append(joke)
    JsonStore.save(JOKES_FILE, jokes)
    await update.message.reply_text("Добавил ✅", reply_markup=menu_keyboard(True))
    return ST_MENU

# ---------- admin tools (/s форматирование) ----------
async def cmd_send_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_first_admin_if_empty(update)

    if not is_admin(update):
        return

    s = load_settings()
    chat_id = s.get("chat_id")
    if not chat_id:
        await update.message.reply_text("Сначала укажи чат через меню: «➕ Добавить чат».")
        return

    if not context.args:
        await update.message.reply_text("Формат: /s <сообщение в HTML>\nПример: /s <b>Привет</b>\\nВторая строка")
        return

    text = normalize_text_for_send(" ".join(context.args))
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    await update.message.reply_text("Отправлено ✅")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_first_admin_if_empty(update)
    if not is_admin(update):
        return

    s = load_settings()
    chat_id = s.get("chat_id")
    if not chat_id:
        await update.message.reply_text("Сначала укажи чат через меню: «➕ Добавить чат».")
        return

    await context.bot.send_message(chat_id=chat_id, text="Тестовое сообщение ✅")
    await update.message.reply_text("Ок ✅")

# ---------- /si: фото + текст -> в чат ----------
async def cmd_si(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_first_admin_if_empty(update)
    if not is_admin(update):
        return ConversationHandler.END

    if not is_private(update):
        await update.message.reply_text("Команда /si работает в ЛС (чтобы не засорять чат).")
        return ConversationHandler.END

    s = load_settings()
    chat_id = s.get("chat_id")
    if chat_id:
        await update.message.reply_text(
            f"Ок. Основной чат уже задан: {chat_id}\n"
            "Пришли одно фото."
        )
        context.user_data["si_chat_id"] = chat_id
        return SI_PHOTO

    await update.message.reply_text("Сначала пришли chat_id (например: -1001234567890).")
    return SI_CHAT

async def st_si_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cid = parse_chat_id(update.message.text)
    if cid is None:
        await update.message.reply_text("Не похоже на chat_id. Пример: -1001234567890")
        return SI_CHAT

    context.user_data["si_chat_id"] = cid
    await update.message.reply_text("Ок. Теперь пришли одно фото.")
    return SI_PHOTO

async def st_si_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно именно фото (как картинка). Пришли фото одним сообщением.")
        return SI_PHOTO

    chat_id = context.user_data.get("si_chat_id")
    if not chat_id:
        await update.message.reply_text("Не найден chat_id. Начни заново: /si")
        return ConversationHandler.END

    # берём самое большое фото
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    tmp_path = TMP_UPLOADS / f"si_{update.effective_user.id}_{int(datetime.now().timestamp())}.jpg"
    await tg_file.download_to_drive(custom_path=str(tmp_path))
    context.user_data["si_photo_path"] = str(tmp_path)

    # если подпись к фото есть — отправляем сразу (фото+подпись)
    caption = update.message.caption
    if caption and caption.strip():
        caption = normalize_text_for_send(caption.strip())

        try:
            with open(tmp_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption, parse_mode=ParseMode.HTML)
        except BadRequest:
            with open(tmp_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        context.user_data.pop("si_chat_id", None)
        context.user_data.pop("si_photo_path", None)

        await update.message.reply_text("Отправил фото+текст ✅", reply_markup=menu_keyboard(is_admin(update)))
        return ConversationHandler.END

    # ✅ если подписи НЕТ — значит ждём текст отдельным сообщением
    await update.message.reply_text("Фото принято ✅ Теперь пришли текст (отдельным сообщением).")
    return SI_TEXT


async def st_si_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = context.user_data.get("si_chat_id")
    photo_path = context.user_data.get("si_photo_path")

    if not chat_id or not photo_path:
        await update.message.reply_text("Что-то пошло не так. Начни заново: /si")
        return ConversationHandler.END

    caption = normalize_text_for_send((update.message.text or "").strip())

    try:
        with open(photo_path, "rb") as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    except BadRequest:
        # если HTML сломался — отправляем без parse_mode
        with open(photo_path, "rb") as f:
            await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
    finally:
        try:
            os.remove(photo_path)
        except OSError:
            pass

    context.user_data.pop("si_chat_id", None)
    context.user_data.pop("si_photo_path", None)

    await update.message.reply_text("Отправил фото+текст ✅", reply_markup=menu_keyboard(True))
    return ConversationHandler.END


# ---------- /d_set ----------
async def cmd_d_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_first_admin_if_empty(update)
    if not is_admin(update):
        await update.message.reply_text("Только для админов.")
        return

    if not context.args:
        await update.message.reply_text(
            "Форматы:\n"
            "/d_set list\n"
            "/d_set add <@user>\n"
            "/d_set remove <@user>\n"
            "/d_set set <@u1; @u2; @u3>"
        )
        return

    action = context.args[0].lower()
    duty_list = JsonStore.load(DUTY_FILE, default=[])

    if action == "list":
        if not duty_list:
            await update.message.reply_text("Список пуст.")
            return
        await update.message.reply_text("Дежурные:\n" + "\n".join([f"{i+1}) {x}" for i, x in enumerate(duty_list)]))
        return

    if action == "add":
        u = normalize_username_input(" ".join(context.args[1:]))
        if not u or u in END_WORDS:
            await update.message.reply_text("Формат: /d_set add @username")
            return
        entry = duty_entry_from_username(u)
        duty_list.append(entry)
        JsonStore.save(DUTY_FILE, duty_list)
        await update.message.reply_text(f"Добавил ✅ {entry}")
        return

    if action == "remove":
        u = normalize_username_input(" ".join(context.args[1:]))
        if not u or u in END_WORDS:
            await update.message.reply_text("Формат: /d_set remove @username")
            return
        before = len(duty_list)
        duty_list = [x for x in duty_list if not x.endswith(f", {u}")]
        if len(duty_list) == before:
            await update.message.reply_text("Не нашёл такого пользователя в списке.")
            return
        JsonStore.save(DUTY_FILE, duty_list)
        await update.message.reply_text("Удалил ✅")
        return

    if action == "set":
        raw = " ".join(context.args[1:]).strip()
        parts = re.split(r"[;\n,]+", raw)
        new_list = []
        for p in parts:
            u = normalize_username_input(p)
            if u and u not in END_WORDS:
                new_list.append(duty_entry_from_username(u))
        if not new_list:
            await update.message.reply_text("Пусто. Пример: /d_set set @a; @b; @c")
            return
        JsonStore.save(DUTY_FILE, new_list)
        await update.message.reply_text(f"Список обновлён ✅ ({len(new_list)} чел.)")
        return

    await update.message.reply_text("Неизвестная подкоманда. Используй list/add/remove/set")

# =======================
# ЛС-Меню: кнопки -> действия (Conversation)
# =======================

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("MENU ROUTER GOT: %r", update.message.text)
    
    text = (update.message.text or "").strip()

    # Общие кнопки (доступны всем)
    if text == "📅 Расписание":
        await update.message.reply_text("Напиши: /r пн (или /r mon) — либо просто /r для ПН-ПТ.")
        return ST_MENU

    if text == "🧹 Дежурный":
        await cmd_duty(update, context)
        return ST_MENU

    if text == "📚 Домашка (dz_list)":
        await update.message.reply_text("Напиши: /dz_list 10 или /dz 25.01 или /dz завтра")
        return ST_MENU

    if text == "❓ Help":
        await cmd_help(update, context)
        return ST_MENU

    # Админские кнопки
    ensure_first_admin_if_empty(update)
    if not is_admin(update):
        await update.message.reply_text("Ок.")
        return ST_MENU

    if text == "➕ Добавить чат":
        await update.message.reply_text("Пришли chat_id (пример: -1001234567890).")
        return ST_SET_CHAT

    if text == "➕ Добавить администратора":
        await update.message.reply_text("Пришли @username администратора.")
        return ST_ADD_ADMIN

    if text == "➕ Добавить учеников":
        await update.message.reply_text(
            "Ок! Присылай учеников по одному: @username\n"
            "Когда закончишь — напиши: end / все / стоп"
        )
        context.user_data["students_added"] = 0
        return ST_ADD_STUDENTS

    if text == "📝 Изменить расписание":
        context.user_data["schedule_step"] = 0
        context.user_data["schedule_buf"] = {}
        await update.message.reply_text("Введи расписание на Понедельник (одним сообщением).")
        return ST_EDIT_SCHEDULE

    if text == "🧪 Тест в чат":
        await cmd_test(update, context)
        return ST_MENU

    if text == "😂 Добавить анекдот":
        await update.message.reply_text("Пришли текст анекдота одним сообщением.\nОтмена: /cancel")
        return ST_JOKE_ADD

    await update.message.reply_text("Не понял кнопку/сообщение. Попробуй /help")
    return ST_MENU

async def st_set_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cid = parse_chat_id(update.message.text)
    if cid is None:
        await update.message.reply_text("Не похоже на chat_id. Пример: -1001234567890")
        return ST_SET_CHAT

    s = load_settings()
    s["chat_id"] = cid
    save_settings(s)
    await update.message.reply_text(f"Сохранено ✅ chat_id = {cid}", reply_markup=menu_keyboard(True))
    return ST_MENU

async def st_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    u = normalize_username_input(update.message.text)
    if not u or u in END_WORDS:
        await update.message.reply_text("Нужно @username. Пример: @myadmin")
        return ST_ADD_ADMIN

    s = load_settings()
    admins = set(s.get("admins", []))
    admins.add(u)
    s["admins"] = sorted(admins)
    save_settings(s)

    await update.message.reply_text(f"Админ добавлен ✅ {u}", reply_markup=menu_keyboard(True))
    return ST_MENU

async def st_add_students(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.strip()
    u = normalize_username_input(t)
    if not u:
        await update.message.reply_text("Не понял. Присылай @username или end/все/стоп")
        return ST_ADD_STUDENTS

    if u in END_WORDS:
        added = int(context.user_data.get("students_added", 0))
        await update.message.reply_text(f"Готово ✅ Добавлено: {added}", reply_markup=menu_keyboard(True))
        return ST_MENU

    duty_list = JsonStore.load(DUTY_FILE, default=[])
    entry = duty_entry_from_username(u)
    duty_list.append(entry)
    JsonStore.save(DUTY_FILE, duty_list)

    context.user_data["students_added"] = int(context.user_data.get("students_added", 0)) + 1
    await update.message.reply_text(f"Добавил: {entry}\nСледующий? (или end/все/стоп)")
    return ST_ADD_STUDENTS

async def st_edit_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    step = int(context.user_data.get("schedule_step", 0))
    buf = context.user_data.get("schedule_buf", {})

    if step >= len(DOW_CANON):
        # на всякий случай
        return ST_MENU

    day = DOW_CANON[step]
    buf[day] = update.message.text.strip()

    step += 1
    context.user_data["schedule_step"] = step
    context.user_data["schedule_buf"] = buf

    if step < len(DOW_CANON):
        next_day = DOW_CANON[step]
        await update.message.reply_text(f"Теперь введи расписание на {next_day}:")
        return ST_EDIT_SCHEDULE

    # финализируем schedule.json: кладём канон + алиасы как ключи
    schedule_out: dict[str, str] = {}
    for canon_day, value in buf.items():
        aliases = build_schedule_aliases(canon_day)
        for a in aliases:
            schedule_out[a.lower()] = value  # ключи делаем в lower, чтобы нормализовать поиск
        # ещё добавим русскую краткую по первой букве? (не надо, уже есть пн/вт/...)
    JsonStore.save(SCHEDULE_FILE, schedule_out)

    await update.message.reply_text("Расписание сохранено ✅", reply_markup=menu_keyboard(True))
    return ST_MENU

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено ✅", reply_markup=menu_keyboard(is_admin(update)))
    return ST_MENU

# =======================
# FLASK keep-alive
# =======================
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Bot is running"

def keep_alive():
    t = Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8081), daemon=True)
    t.start()

async def st_si_photo_wrong(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Нужно фото одним сообщением 📷. Пришли фото (не файл), или /cancel.")
    return ST_SI_PHOTO

async def st_si_text_wrong(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Теперь нужен текст одним сообщением 📝. Пришли текст, или /cancel.")
    return ST_SI_TEXT

# =======================
# MAIN
# =======================

def load_config() -> Config:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найдена переменная окружения BOT_TOKEN.")
    return Config(token=token)

def build_app(cfg: Config) -> Application:
    app = Application.builder().token(cfg.token).build()
    app.bot_data["cfg"] = cfg

    # 1) /si conversation (создаём ДО add_handler)
    si_conv = ConversationHandler(
    entry_points=[CommandHandler("si", cmd_si)],
    states={
        SI_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_si_chat)],
        SI_PHOTO: [MessageHandler(filters.PHOTO, st_si_photo),
                   MessageHandler(filters.ALL & ~filters.PHOTO, st_si_photo_wrong)],
        SI_TEXT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_si_text),
                   MessageHandler(filters.ALL & ~filters.TEXT, st_si_text_wrong)],
    },
    fallbacks=[CommandHandler("cancel", cmd_cancel)],
    name="si_conv",
    persistent=False,
    )

    # 2) меню conversation (создаём ДО add_handler)
    menu_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("menu", cmd_menu),
        ],
        states={
            ST_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            ST_SET_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_set_chat)],
            ST_ADD_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_add_admin)],
            ST_ADD_STUDENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_add_students)],
            ST_EDIT_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_edit_schedule)],
            ST_JOKE_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_joke_add_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="menu_conv",
        persistent=False,
    )

    # 3) ВАЖНО: ConversationHandler’ы добавляем ПЕРВЫМИ
    app.add_handler(menu_conv)
    app.add_handler(si_conv)

    # 4) Обычные команды (НЕ добавляем отдельно /start и /menu!)
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(CommandHandler("r", cmd_schedule))
    app.add_handler(CommandHandler("d", cmd_duty))

    app.add_handler(CommandHandler("dz", cmd_homework))
    app.add_handler(CommandHandler("dz_list", cmd_homework_list))
    app.add_handler(CommandHandler("dz_edit", cmd_homework_edit))
    app.add_handler(CommandHandler("dz_del", cmd_homework_del))

    app.add_handler(CommandHandler("joke", cmd_joke))
    app.add_handler(CommandHandler("joke_add", cmd_joke_add))

    # админ-рассылка
    app.add_handler(CommandHandler("s", cmd_send_to_chat))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("d_set", cmd_d_set))

    # 5) Джобы
    app.job_queue.run_daily(duty_reminder_job, time=cfg.duty_reminder_time)
    app.job_queue.run_daily(homework_cleanup_job, time=cfg.homework_cleanup_time)

    return app

def main():
    cfg = load_config()
    keep_alive()
    application = build_app(cfg)
    logger.info("Bot started")
    application.run_polling()

if __name__ == "__main__":
    main()

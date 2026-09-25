import os
import sys
import logging
import sqlite3
import asyncio
import re
import json
import base64
import io
import traceback
import tempfile
from typing import Optional
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, ImageEnhance
from datetime import datetime, date, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types.web_app_info import WebAppInfo

# Импорт библиотеки google-genai
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

# Безопасный импорт ReportLab
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logging.critical("❌ ОШИБКА: Не задана переменная окружения BOT_TOKEN!")

RENDER_URL = os.getenv("RENDER_URL", "https://your-app-name.onrender.com")
ADMIN_ID = os.getenv("ADMIN_ID")
ADMIN_KEY = os.getenv("ADMIN_KEY") or os.getenv("ADMIN_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gemini_client = None
if GEMINI_API_KEY and HAS_GENAI:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("✅ Gemini API Client (google-genai) успешно инициализирован.")
    except Exception as e:
        logging.error(f"❌ Ошибка инициализации Gemini Client: {e}")
else:
    logging.warning("⚠️ GEMINI_API_KEY не установлен или google-genai не импортирован.")

ADMIN_CHANNEL_ID_RAW = os.getenv("ADMIN_CHANNEL_ID", "-1004271518848")
try:
    ADMIN_CHANNEL_ID = int(ADMIN_CHANNEL_ID_RAW)
except ValueError:
    ADMIN_CHANNEL_ID = -1004271518848


PAYMENT_DOCS_CHANNEL_ID_RAW = os.getenv("PAYMENT_DOCS_CHANNEL_ID", "-1004442650861")
try:
    PAYMENT_DOCS_CHANNEL_ID = int(PAYMENT_DOCS_CHANNEL_ID_RAW)
except ValueError:
    PAYMENT_DOCS_CHANNEL_ID = -1004442650861

def add_business_days(start_date: date, num_days: int) -> date:
    """Добавляет N рабочих дней, пропуская субботы и воскресенья."""
    cur_date = start_date
    added = 0
    while added < num_days:
        cur_date += timedelta(days=1)
        if cur_date.weekday() < 5:  # 0..4 - Пн-Пт
            added += 1
    return cur_date

def calculate_payment_date(start_date: date, num_days: int = 11) -> date:
    """
    Добавляет 11 обычных календарных дней от даты подачи документов.
    Если дата попадает на выходной (Сб, Вс) или праздник — переносит на следующий рабочий день.
    """
    target = start_date + timedelta(days=num_days)
    
    # Список официальных праздничных дней (месяц, день)
    holidays = {
        (1, 1), (1, 2), (1, 7),   # Новый год, Рождество
        (2, 23),                   # 23 февраля
        (3, 8),                    # 8 марта
        (5, 1), (5, 9),            # Майские праздники
        (7, 3),                    # День Независимости
        (11, 4), (11, 7),          # Ноябрьские праздники
        (12, 25)                   # Католическое Рождество
    }
    
    # Если дата попадает на субботу (5), воскресенье (6) или праздник — сдвигаем на следующий день
    while target.weekday() >= 5 or (target.month, target.day) in holidays:
        target += timedelta(days=1)
        
    return target


BACKUP_CHANNEL_ID_RAW = os.getenv("BACKUP_CHANNEL_ID", str(ADMIN_CHANNEL_ID))
try:
    BACKUP_CHANNEL_ID = int(BACKUP_CHANNEL_ID_RAW)
except ValueError:
    BACKUP_CHANNEL_ID = ADMIN_CHANNEL_ID

CARGO_INPUT_CHANNEL_ID_RAW = os.getenv("CARGO_INPUT_CHANNEL_ID", "")
try:
    CARGO_INPUT_CHANNEL_ID = int(CARGO_INPUT_CHANNEL_ID_RAW) if CARGO_INPUT_CHANNEL_ID_RAW else None
except ValueError:
    CARGO_INPUT_CHANNEL_ID = None

DOCS_CHANNEL_ID_RAW = os.getenv("DOCS_CHANNEL_ID", "-1003928614238")
try:
    DOCS_CHANNEL_ID = int(DOCS_CHANNEL_ID_RAW)
except ValueError:
    DOCS_CHANNEL_ID = -1003928614238

ORDERS_CHANNEL_ID_RAW = os.getenv("ORDERS_CHANNEL_ID", "-1004337686386")
try:
    ORDERS_CHANNEL_ID = int(ORDERS_CHANNEL_ID_RAW)
except ValueError:
    ORDERS_CHANNEL_ID = -1004337686386

from aiogram.client.session.aiohttp import AiohttpSession

# Задаем базовой HTTP-сессии бота лимит ожидания в 5 минут  (300 сек)
session = AiohttpSession(timeout=300.0)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

CHANNELS = {
    "Казахстан 🇰🇿": -1004309918435,
    "Узбекистан 🇺🇿": -1003470705929,
    "Кыргызстан 🇰🇬": -1004470387295,
    "Азербайджан 🇦зербайджан": -1004483200216,
    "Грузия 🇬🇪": -1004340496095,
    "Армения 🇦🇲": -1004335138909
}

CHANNEL_TO_DIRECTION = {v: k for k, v in CHANNELS.items()}
PENDING_COUNTER_OFFERS = {}

ALLOWED_CONVERT_USERS = {"del1nkvent", "daniil_belkaspian"}

def is_convert_allowed(user: types.User) -> bool:
    if not user:
        return False
    if user.username and user.username.lower() in ALLOWED_CONVERT_USERS:
        return True
    if ADMIN_ID and str(user.id) == str(ADMIN_ID):
        return True
    return False

# ==================== ОПРЕДЕЛЕНИЕ ТИПА ФАЙЛА ПО MAGIC BYTES ====================
def detect_mime_type(file_bytes: bytes, file_path: str = "") -> str:
    if file_bytes.startswith(b'%PDF'):
        return "application/pdf"
    elif file_bytes.startswith(b'\x89PNG'):
        return "image/png"
    elif file_bytes.startswith(b'\xff\xd8'):
        return "image/jpeg"
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WEBP':
        return "image/webp"

    if file_path:
        fp = file_path.lower()
        if fp.endswith('.pdf'): return "application/pdf"
        if fp.endswith('.png'): return "image/png"
        if fp.endswith('.webp'): return "image/webp"

    return "image/jpeg"

import tempfile
import shutil
import signal
import html

# ==================== БАЗА ДАННЫХ: БЭКАП И ВОССТАНОВЛЕНИЕ ====================

def make_safe_db_dump_bytes() -> bytes:
    """Создает консистентный срез SQLite базы с принудительной записью WAL."""
    if not os.path.exists("cargo_bot.db"):
        return b""

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_name = tmp.name

    try:
        src = sqlite3.connect("cargo_bot.db")
        try:
            src.execute("PRAGMA wal_checkpoint(FULL);")
        except Exception:
            pass

        dst = sqlite3.connect(tmp_name)
        src.backup(dst)
        dst.close()
        src.close()

        with open(tmp_name, "rb") as f:
            data = f.read()
        return data
    except Exception as e:
        logging.error(f"❌ Ошибка создания дампа базы: {e}")
        return b""
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


_backup_lock = asyncio.Lock()

async def push_db_backup(reason: str = "Автобэкап") -> tuple[bool, str]:
    """Выгружает срез базы в Telegram-канал и закрепляет его (безопасный HTML)."""
    async with _backup_lock:
        try:
            data = make_safe_db_dump_bytes()
            if not data or len(data) < 100:
                msg = "⚠️ База cargo_bot.db пуста или еще не создана."
                logging.warning(msg)
                return False, msg

            now_str = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M:%S")
            size_kb = round(len(data) / 1024, 1)
            safe_reason = html.escape(str(reason))

            caption = (
                f"📦 <b>Резервная копия базы данных</b>\n"
                f"• Причина: <code>{safe_reason}</code>\n"
                f"• Дата (МСК): <code>{now_str}</code>\n"
                f"• Размер: <code>{size_kb} KB</code>\n"
                f"#db_backup"
            )

            doc_file = types.BufferedInputFile(data, filename="cargo_bot.db")

            # 1. Отправка файла в канал
            try:
                sent_msg = await bot.send_document(
                    chat_id=BACKUP_CHANNEL_ID,
                    document=doc_file,
                    caption=caption,
                    parse_mode="HTML"
                )
            except Exception:
                sent_msg = await bot.send_document(
                    chat_id=BACKUP_CHANNEL_ID,
                    document=doc_file,
                    caption=f"📦 Резервная копия базы данных ({size_kb} KB)\nПричина: {reason}\nДата: {now_str}"
                )

            # 2. Закрепление только одного последнего файла
            try:
                try:
                    await bot.unpin_all_chat_messages(chat_id=BACKUP_CHANNEL_ID)
                except Exception as unpin_err:
                    logging.warning(f"Не удалось открепить старые сообщения: {unpin_err}")

                await bot.pin_chat_message(
                    chat_id=BACKUP_CHANNEL_ID,
                    message_id=sent_msg.message_id,
                    disable_notification=True
                )
                logging.info(f"✅ Резервная копия БД успешно выгружена и закреплена ({size_kb} KB).")
                return True, f"Успешно выгружено и закреплено ({size_kb} KB)"
            except Exception as pin_err:
                err_text = f"Файл выгружен, но не закреплен: {pin_err}"
                logging.warning(f"⚠️ {err_text}")
                return True, err_text

        except Exception as e:
            err_text = f"Ошибка отправки в канал {BACKUP_CHANNEL_ID}: {e}"
            logging.error(f"❌ {err_text}")
            return False, err_text


async def restore_db_from_telegram() -> bool:
    """Скачивает последнюю актуальную БД из закрепленного сообщения канала."""
    logging.info(f"🔍 Поиск резервной копии БД в канале {BACKUP_CHANNEL_ID}...")
    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pinned = chat.pinned_message

        if not pinned or not pinned.document:
            logging.warning(
                f"ℹ️ Закрепленная копия БД не найдена в канале {BACKUP_CHANNEL_ID}.\n"
                "Если это первый запуск — создается новая чистая база."
            )
            return False

        doc = pinned.document
        if not doc.file_name or not doc.file_name.endswith(".db"):
            logging.warning(f"⚠️ Закрепленный файл '{doc.file_name}' не является файлом .db. Пропуск.")
            return False

        logging.info(f"⏳ Скачивание резервной копии: {doc.file_name} ({round(doc.file_size / 1024, 1)} KB)...")
        file_info = await bot.get_file(doc.file_id)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        await bot.download_file(file_info.file_path, destination=tmp_path)

        # Проверка целостности файла
        try:
            check_conn = sqlite3.connect(tmp_path)
            check_res = check_conn.execute("PRAGMA integrity_check;").fetchone()
            check_conn.close()
            if not check_res or check_res[0] != "ok":
                logging.error("❌ Файл из закрепа поврежден! Восстановление отменено.")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return False
        except Exception as check_err:
            logging.error(f"❌ Ошибка проверки скачанной базы: {check_err}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False

        # Удаляем временные файлы перед заменой
        for suffix in ["", "-wal", "-shm"]:
            p = f"cargo_bot.db{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        shutil.move(tmp_path, "cargo_bot.db")
        logging.info("✅ База данных успешно восстановлена из закрепа Telegram-канала.")
        return True

    except Exception as e:
        logging.error(f"❌ Не удалось восстановить БД из канала {BACKUP_CHANNEL_ID}: {e}")
        return False


async def auto_backup_db_loop():
    """Фоновый цикл автосохранения по расписанию (время МСК)."""
    last_backup_slot = ""
    while True:
        try:
            # Получаем текущее время по МСК (UTC+3)
            msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
            weekday = msk_now.weekday()  # 0..4 — будни (пн-пт), 5..6 — выходные (сб-вс)
            cur_hour = msk_now.hour
            cur_minute = msk_now.minute

            # Расписание для будних (пн-пт): 08:00, 11:00, 14:00, 17:30, 00:00
            if weekday < 5:
                weekday_slots = [(8, 0), (11, 0), (14, 0), (17, 30), (0, 0)]
                is_target_time = (cur_hour, cur_minute) in weekday_slots
                day_name = "Будни"
            else:
                # Расписание для выходных (сб-вс): 14:00
                weekend_slots = [(14, 0)]
                is_target_time = (cur_hour, cur_minute) in weekend_slots
                day_name = "Выходной"

            current_slot_id = f"{msk_now.strftime('%Y-%m-%d')}_{cur_hour:02d}:{cur_minute:02d}"

            # Если наступило время из расписания и бэкап в эту минуту еще не отправлялся
            if is_target_time and current_slot_id != last_backup_slot:
                last_backup_slot = current_slot_id
                slot_time_str = f"{cur_hour:02d}:{cur_minute:02d}"
                reason = f"По расписанию ({day_name}, {slot_time_str} МСК)"
                await push_db_backup(reason=reason)

        except Exception as e:
            logging.error(f"Ошибка в auto_backup_db_loop: {e}")

        # Проверяем время каждые 25 секунд
        await asyncio.sleep(25)


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect("cargo_bot.db", timeout=15)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            company TEXT,
            name TEXT,
            phone TEXT,
            subscriptions TEXT,
            status TEXT DEFAULT 'ACTIVE',
            verification_status TEXT DEFAULT 'UNVERIFIED',
            pending_company TEXT DEFAULT '',
            pending_name TEXT DEFAULT '',
            pending_phone TEXT DEFAULT ''
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS loads (
            load_id INTEGER PRIMARY KEY AUTOINCREMENT,
            destination_country TEXT,
            date TEXT,
            route TEXT,
            cars_count TEXT,
            price TEXT,
            car_type TEXT,
            cargo_type TEXT,
            weight TEXT,
            text TEXT,
            details TEXT,
            status TEXT DEFAULT 'ACTIVE',
            admin_comment TEXT,
            expires_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cargo_id INTEGER,
            user_id INTEGER,
            message_id INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS confirmed_deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            load_id INTEGER,
            user_id INTEGER,
            date TEXT,
            route TEXT,
            cars INTEGER,
            price TEXT,
            details TEXT,
            docs_submitted INTEGER DEFAULT 0,
            docs_status TEXT DEFAULT 'NONE',
            missing_docs TEXT DEFAULT '',
            last_truck_plate TEXT DEFAULT '',
            last_trailer_plate TEXT DEFAULT '',
            last_driver_name TEXT DEFAULT '',
            driver_phone TEXT DEFAULT '',
            unload_date TEXT DEFAULT '',
            is_unloaded INTEGER DEFAULT 0,
            status TEXT DEFAULT 'CONFIRMED'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bids (
            bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            load_id INTEGER,
            user_id INTEGER,
            cars INTEGER,
            rate TEXT,
            comment TEXT,
            counter_rate TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            text TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_counters (
            admin_chat_id INTEGER PRIMARY KEY,
            bid_id INTEGER,
            action_type TEXT DEFAULT 'COUNTER'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # Таблица создается сразу со всеми нужными полями, а небезопасный дефолтный пароль 123456 удален
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS carrier_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_code TEXT UNIQUE,
            user_id INTEGER DEFAULT 0,
            company TEXT DEFAULT '',
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            status TEXT DEFAULT 'UNUSED',
            max_users INTEGER DEFAULT 5,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_excel_payments (
            order_number TEXT PRIMARY KEY,
            paid_amount TEXT,
            paid_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    migrations = [
        "ALTER TABLE confirmed_deals ADD COLUMN load_id INTEGER",
        "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'ACTIVE'",
        "ALTER TABLE users ADD COLUMN verification_status TEXT DEFAULT 'UNVERIFIED'",
        "ALTER TABLE users ADD COLUMN pending_company TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN pending_name TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN pending_phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN company_key TEXT DEFAULT ''",
        "ALTER TABLE carrier_keys ADD COLUMN max_users INTEGER DEFAULT 5",
        "ALTER TABLE pending_counters ADD COLUMN action_type TEXT DEFAULT 'COUNTER'",
        "ALTER TABLE loads ADD COLUMN cargo_type TEXT",
        "ALTER TABLE loads ADD COLUMN weight TEXT",
        "ALTER TABLE loads ADD COLUMN car_type TEXT",
        "ALTER TABLE loads ADD COLUMN admin_comment TEXT",
        "ALTER TABLE loads ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE bids ADD COLUMN comment TEXT",
        "ALTER TABLE bids ADD COLUMN counter_rate TEXT",
        "ALTER TABLE loads ADD COLUMN expires_at TEXT",
        "ALTER TABLE confirmed_deals ADD COLUMN docs_submitted INTEGER DEFAULT 0",
        "ALTER TABLE confirmed_deals ADD COLUMN docs_status TEXT DEFAULT 'NONE'",
        "ALTER TABLE confirmed_deals ADD COLUMN missing_docs TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN last_truck_plate TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN last_trailer_plate TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN last_driver_name TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN driver_phone TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN unload_date TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN is_unloaded INTEGER DEFAULT 0",
        "ALTER TABLE confirmed_deals ADD COLUMN status TEXT DEFAULT 'CONFIRMED'",
        "ALTER TABLE confirmed_deals ADD COLUMN kaiten_card_id INTEGER",
        "ALTER TABLE confirmed_deals ADD COLUMN full_doc_text TEXT",
        "ALTER TABLE confirmed_deals ADD COLUMN order_number TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN pay_docs_status TEXT DEFAULT 'NONE'",
        "ALTER TABLE confirmed_deals ADD COLUMN pay_docs_error TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN pay_docs_submitted_at TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN planned_payment_date TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN is_paid INTEGER DEFAULT 0",
        "ALTER TABLE confirmed_deals ADD COLUMN paid_amount TEXT DEFAULT ''",
        "ALTER TABLE confirmed_deals ADD COLUMN paid_date TEXT DEFAULT ''"
    ]
    for migration in migrations:
        try:
            cursor.execute(migration)
        except sqlite3.OperationalError:
            pass
    
    conn.commit()
    conn.close()


def add_notification(user_id: int, title: str, text: str):
    try:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO notifications (user_id, title, text) VALUES (?, ?, ?)", (user_id, title, text))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error adding notification: {e}")

# ==================== СОСТОЯНИЯ ====================
class ProfileEditStates(StatesGroup):
    waiting_for_company = State()
    waiting_for_name = State()
    waiting_for_phone = State()

class DealStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_custom_rate = State()

class DocUploadStates(StatesGroup):
    waiting_for_docs = State()

class DocConvertStates(StatesGroup):
    waiting_for_files = State()

class PayDocUploadStates(StatesGroup):
    waiting_for_docs = State()

class AdminEditStates(StatesGroup):
    waiting_for_new_cargo_text = State()

class AdminCounterStates(StatesGroup):
    waiting_for_counter_rate = State()

class AdminPartialStates(StatesGroup):
    waiting_for_cars = State()

class AdminVerifyEditStates(StatesGroup):
    waiting_for_details = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================




def merge_formatted_doc_text(old_text: str, new_text: str) -> str:
    if not old_text or not old_text.strip():
        return new_text.strip()
    if not new_text or not new_text.strip():
        return old_text.strip()

    def parse_lines(txt):
        lines = [l.strip() for l in txt.strip().split('\n') if l.strip()]
        res = {}
        for line in lines:
            if ':' in line:
                k, v = line.split(':', 1)
                res[k.strip()] = v.strip()
        return res

    old_map = parse_lines(old_text)
    new_map = parse_lines(new_text)

    merged = {}
    all_keys = list(old_map.keys())
    for k in new_map.keys():
        if k not in all_keys:
            all_keys.append(k)

    for key in all_keys:
        old_val = old_map.get(key, "")
        new_val = new_map.get(key, "")

        # Если в новых данных "Не распознан/Не указан", а в старых были данные — сохраняем старые
        is_new_invalid = not new_val or any(inv in new_val.lower() for inv in ["не распознан", "не указан", "не распознано", "не распознана"])
        if is_new_invalid and old_val and not any(inv in old_val.lower() for inv in ["не распознан", "не указан", "не распознано", "не распознана"]):
            merged[key] = old_val
        else:
            merged[key] = new_val if new_val else old_val

    return "\n".join([f"{k}: {v}" for k, v in merged.items()])



def build_kaiten_admin_keyboard(deal_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📥 Подать данные в Kaiten", callback_data=f"kaiten_push_{deal_id}"),
        types.InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"kaiten_skip_{deal_id}")
    )
    builder.row(
        types.InlineKeyboardButton(text="📌 В определённую карточку", callback_data=f"kaiten_custom_{deal_id}")
    )
    return builder.as_markup()


def extract_surname_and_name(full_name: str) -> str:
    if not full_name or full_name.lower().strip() in ["не распознан", "не указан"]:
        return "Не распознан"
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return parts[0] if parts else "Не распознан"

def normalize_single_phone(phone_str: str) -> str:
    if not phone_str:
        return ""
    digits = re.sub(r'\D', '', phone_str)
    if not digits:
        return phone_str.strip()

    # Беларусь: 80291234567 -> +375291234567 (11 цифр, начинается с 80)
    if len(digits) == 11 and digits.startswith('80'):
        return f"+375{digits[2:]}"
        
    # РФ / Казахстан: 89551234567 -> +79551234567 (11 цифр, начинается с 8)
    if len(digits) == 11 and digits.startswith('8'):
        return f"+7{digits[1:]}"

    # РФ / Казахстан с цифры 7 без плюса: 79551234567 -> +79551234567
    if len(digits) == 11 and digits.startswith('7'):
        return f"+{digits}"

    # Беларусь с 375 без плюса: 375291234567 -> +375291234567
    if len(digits) == 12 and digits.startswith('375'):
        return f"+{digits}"

    if phone_str.strip().startswith('+'):
        return f"+{digits}"

    return phone_str.strip()

def translate_country(country_str: str) -> str:
    if not country_str or country_str.strip().lower() in ["не распознана", "не указана", "none"]:
        return "Не распознана"
    c = country_str.strip().lower()
    mapping = {
        'uzbekistan': 'Узбекистан', 'uzb': 'Узбекистан', 'узбекистан': 'Узбекистан',
        'kazakhstan': 'Казахстан', 'kzt': 'Казахстан', 'казахстан': 'Казахстан',
        'tajikistan': 'Таджикистан', 'tjk': 'Таджикистан', 'tj': 'Таджикистан', 'таджикистан': 'Таджикистан',
        'russia': 'Россия', 'russian federation': 'Россия', 'россия': 'Россия',
        'belarus': 'Беларусь', 'by': 'Беларусь', 'беларусь': 'Беларусь',
        'kyrgyzstan': 'Кыргызстан', 'kgz': 'Кыргызстан', 'кыргызстан': 'Кыргызстан',
        'georgia': 'Грузия', 'грузия': 'Грузия',
        'azerbaijan': 'Азербайджан', 'азербайджан': 'Азербайджан',
        'armenia': 'Армения', 'армения': 'Армения'
    }
    return mapping.get(c, country_str.strip())

def normalize_phones(phone_str: str) -> str:
    if not phone_str or phone_str.strip().lower() in ["не указан", "не распознан", "—", "-"]:
        return "Не указан"

    parts = re.split(r'[\/\,\;\n]+', phone_str)
    normalized = []
    for p in parts:
        p_clean = p.strip()
        if p_clean:
            norm = normalize_single_phone(p_clean)
            if norm and norm not in normalized:
                normalized.append(norm)

    # Приоритет российским (+7) и белорусским (+375) номерам
    ru_by_phones = [p for p in normalized if p.startswith('+7') or p.startswith('+375')]
    other_phones = [p for p in normalized if not (p.startswith('+7') or p.startswith('+375'))]
    sorted_phones = ru_by_phones + other_phones

    return " / ".join(sorted_phones) if sorted_phones else phone_str.strip()

def get_main_reply_markup(user: Optional[types.User] = None):
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="📱 Вызвать меню"))
    if user and is_convert_allowed(user):
        builder.add(types.KeyboardButton(text="🔄 Преобразовать данные"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

def get_chat_menu_inline_markup(user: Optional[types.User] = None):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🌍 Выбор направлений", callback_data="menu_directions"))
    builder.row(types.InlineKeyboardButton(text="👤 Личный кабинет", callback_data="menu_profile"))
    builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="menu_active"))
    builder.row(types.InlineKeyboardButton(text="🚚 Забранные грузы", callback_data="menu_my_deals"))
    if user and is_convert_allowed(user):
        builder.row(types.InlineKeyboardButton(text="🔄 Преобразовать данные", callback_data="menu_convert_standalone"))
    return builder.as_markup()

def normalize_currency(curr_str: str) -> str:
    c = curr_str.lower().strip()
    if c in ['$', 'долл', 'usd', 'доллар', 'долларов', 'дол', 'д']:
        return "USD"
    elif c in ['руб', 'rub', 'rur', 'р', 'рубль', 'рублей']:
        return "RUB"
    elif c in ['€', 'евро', 'eur', 'е']:
        return "EUR"
    elif c in ['сум', 'сумм', 'узб сум', 'uzs']:
        return "UZS"
    elif c in ['тенге', 'тг', 'kzt']:
        return "KZT"
    return "USD"

def extract_price(text: str) -> str:
    if not text:
        return "Торги"
        
    text_clean = text.strip()
    text_lower = text_clean.lower()
    
    if 'торг' in text_lower:
        return "Торги"
        
    curr_pattern = re.compile(
        r'(\d[\d\s\.,]*)\s*(\$|€|руб|rub|rur|\bр\b|долл|usd|\bдол\b|\bд\b|евро|eur|\bе\b|тенге|kzt|\bтг\b|узб\s*сум|сумм|\bсум\b|uzs)',
        re.IGNORECASE
    )
    match = curr_pattern.search(text_clean)
    if match:
        val = match.group(1).strip(' ,.')
        curr_code = normalize_currency(match.group(2))
        return f"{val} {curr_code}"

    prefix_pattern = re.compile(r'(\$|€)\s*(\d[\d\s\.,]*)', re.IGNORECASE)
    match_prefix = prefix_pattern.search(text_clean)
    if match_prefix:
        val = match_prefix.group(2).strip(' ,.')
        curr_code = "USD" if match_prefix.group(1) == "$" else "EUR"
        return f"{val} {curr_code}"

    no_dates = re.sub(r'\d{1,2}[\./]\d{1,2}(?:[\./]\d{2,4})?', '', text_clean)
    no_cars = re.sub(r'\d+\s*(?:авт[оа]|машин[аы]?[е]?[е]?)', '', no_dates, flags=re.IGNORECASE)
    
    numbers = re.findall(r'\b\d[\d\s\.,]*\d\b|\b\d{3,6}\b', no_cars)
    for num_raw in numbers:
        digits_only = re.sub(r'\D', '', num_raw)
        if not digits_only:
            continue
        num_val = int(digits_only)
        if 1000 <= num_val <= 9999:
            return f"{num_raw.strip()} USD"
        elif num_val >= 10000:
            return f"{num_raw.strip()} RUB"
        elif 100 <= num_val < 1000:
            return f"{num_raw.strip()} USD"

    return "Торги"

def format_custom_rate(rate_text: str) -> str:
    if not rate_text:
        return "Торги"
    formatted = extract_price(rate_text)
    if formatted != "Торги":
        return formatted
    return rate_text.strip()

def extract_time_limit(text: str):
    if not text:
        return None, None

    # Исключаем ложные срабатывания на вес/объем (до 22т, до 15т, до 33 паллет).
    # Ищем строго временные конструкции: "до 15:00", "до 15 МСК", "торги до 17:00"
    
    match_time = re.search(r'\b(?:до|торги\s+до|ставка\s+до)\s*(\d{1,2})[\:\.](\d{2})\b', text, re.IGNORECASE)
    hours = None
    minutes = 0

    if match_time:
        hours = int(match_time.group(1))
        minutes = int(match_time.group(2))
    else:
        match_hour = re.search(r'\b(?:торги\s+до|ставка\s+до|до)\s*(\d{1,2})\s*(?:ч|часов|мск|по\s+мск)\b', text, re.IGNORECASE)
        if match_hour:
            hours = int(match_hour.group(1))
            minutes = 0
        else:
            match_bid = re.search(r'\b(?:торги\s+до|ставки\s+до)\s*(\d{1,2})\b', text, re.IGNORECASE)
            if match_bid:
                hours = int(match_bid.group(1))
                minutes = 0

    if hours is not None and 0 <= hours <= 23 and 0 <= minutes <= 59:
        time_formatted = f"{hours:02d}:{minutes:02d}"
        msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
        expire_dt = msk_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        
        # Если указанный час сегодня уже прошёл, ставим лимит на ближайшие торги завтра
        if expire_dt <= msk_now:
            expire_dt += timedelta(days=1)
            
        expire_str = expire_dt.strftime("%Y-%m-%d %H:%M:%S")
        return time_formatted, expire_str

    return None, None

def parse_cargo_date(date_str: str) -> date | None:
    if not date_str:
        return None
    match = re.search(r'(\d{1,2})[\./](\d{1,2})(?:[\./](\d{2,4}))?', str(date_str))
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year_str = match.group(3)
    
    now = datetime.now()
    if year_str:
        year = int(year_str)
        if year < 100:
            year += 2000
    else:
        year = now.year
        if month < now.month - 6:
            year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return None

def parse_cargo_raw(raw_text: str):
    clean_lines = [line.strip() for line in raw_text.split('\n') if line.strip()]

    date_str = ""
    route_str = ""
    price_str = extract_price(raw_text)
    cars_str = ""
    
    time_limit, expires_at = extract_time_limit(raw_text)
    if time_limit and "по МСК" not in price_str:
        price_str = f"{price_str} (до {time_limit} по МСК)"

    # Регулярное выражение с поддержкой диапазонов (09.08-15.08, 09-15.08, 09.08 - 15.08)
    date_pattern = re.compile(r'(\d{1,2}(?:[\./]\d{1,2})?(?:[\./]\d{2,4})?\s*[-—–]\s*\d{1,2}[\./]\d{1,2}(?:[\./]\d{2,4})?|\d{1,2}[\./]\d{1,2}(?:[\./]\d{2,4})?)')
    cars_pattern = re.compile(r'(\d+)\s*(?:авт[оа]|машин[аы]?[е]?[е]?)', re.IGNORECASE)
    
    for line in clean_lines:
        if not date_str:
            date_match = date_pattern.search(line)
            if date_match:
                date_str = date_match.group(1)
        
        if not cars_str:
            cars_match = cars_pattern.search(line)
            if cars_match:
                cars_str = cars_match.group(1)

        if ('-' in line or '→' in line or '—' in line) and not route_str:
            clean_route = line
            clean_route = date_pattern.sub('', clean_route)
            clean_route = cars_pattern.sub('', clean_route)
            clean_route = re.sub(
                r'[\d\.\,\s]+(?:\$|€|руб|rub|rur|\bр\b|долл|usd|\bдол\b|\bд\b|евро|eur|тенге|kzt|\bтг\b|узб\s*сум|сумм|\bсум\b|uzs|торг|торги)\b.*$',
                '',
                clean_route,
                flags=re.IGNORECASE
            )
            clean_route = re.sub(
                r'[\d\.\,\s]+(?:\$|€|руб|rub|rur|\bр\b|долл|usd|\bдол\b|\bд\b|евро|eur|тенге|kzt|\bтг\b|узб\s*сум|сумм|\bсум\b|uzs)\b',
                '',
                clean_route,
                flags=re.IGNORECASE
            )
            clean_route = re.sub(r'[,;\s]+\d[\d\s\.,]*$', '', clean_route).strip()
            clean_route = re.sub(r'[\s,]+', ' ', clean_route).strip(' ,.-')
            clean_route = re.sub(r'\s*[-—→]+\s*', ' → ', clean_route)
            clean_route = re.sub(r'^\s*,\s*|\s*,\s*$', '', clean_route).strip()
            
            if clean_route:
                route_str = clean_route

    if not date_str:
        date_str = "Дата не указана"
    if not route_str:
        route_str = clean_lines[0] if clean_lines else "Маршрут не указан"
    if not price_str:
        price_str = "Торги"
    if not cars_str:
        cars_str = "1"

    route_str = re.sub(r'[,;\s]+\d+.*$', '', route_str).strip()
    route_str = re.sub(r'^\s*,\s*|\s*,\s*$', '', route_str).strip()

    car_type = "Тент/реф"
    cargo_type = "ТНП"
    weight = "до 22т"
    details_list = []

    vehicle_keywords = ['тент', 'реф', 'мега', 'сцепка', 'тандем', 'изотерм', 'площадка', 'контейнер', 'автовоз', 'цистерна', 'бочка', 'шаланда', 'цельномет', 'штора', 'бортовой']
    
    req_patterns = [
        (r'\b(адр\d?|adr\d?)\b', 'АДР'),
        (r'\b(бок|боковая|бок\s*погрузка)\b', 'Бок погрузка'),
        (r'\b(верх|вверх|верхняя|растентовка)\b', 'Верх погрузка'),
        (r'\b(\d+\s*(?:цмр|cmr|смр))\b', None),
        (r'\b(\d+\s*мест[ао]?\s*(?:загрузки|выгрузки)?|загрузки|выгрузки)\b', None),
        (r'\b(гидроборт)\b', 'Гидроборт'),
        (r'\b(пневмоход|пневмо)\b', 'Пневмоход'),
        (r'\b(коники)\b', 'Коники'),
    ]

    parts = []
    for line in clean_lines:
        if '-' in line or '→' in line or '—' in line or (date_pattern.search(line) and not parts):
            continue
        for p in line.split(','):
            p_clean = p.strip()
            if p_clean:
                parts.append(p_clean)

    found_vehicle = False
    found_cargo = False

    for part in parts:
        part_working = part

        w_match = re.search(r'(\d{1,2}\s*т|до\s*\d{1,2}\s*т|до\s*\d{1,2}\s*тонн)', part_working, re.IGNORECASE)
        if w_match:
            weight = w_match.group(1).strip()
            part_working = re.sub(r'(\d{1,2}\s*т|до\s*\d{1,2}\s*т|до\s*\d{1,2}\s*тонн)', '', part_working, flags=re.IGNORECASE).strip(' ,.-')

        if not part_working:
            continue

        p_work_lower = part_working.lower()

        if not found_vehicle and any(vk in p_work_lower for vk in vehicle_keywords):
            car_type = part_working.capitalize()
            found_vehicle = True
            continue

        is_req = False
        for pat, label in req_patterns:
            m_req = re.search(pat, p_work_lower, re.IGNORECASE)
            if m_req:
                req_val = label if label else m_req.group(1).upper()
                if req_val not in details_list:
                    details_list.append(req_val)
                is_req = True
                break

        if is_req:
            continue

        if not found_cargo and len(part_working) > 1:
            cargo_type = part_working.capitalize()
            found_cargo = True

    details_text = ", ".join(details_list)

    return date_str, route_str, price_str, cars_str, details_text, car_type, cargo_type, weight, expires_at



def parse_cargo_date_range(date_str: str) -> tuple[date | None, date | None]:
    """
    Парсит диапазон дат (например, '09.08-15.08', '09-15.08') или одиночную дату ('09.08').
    Возвращает (start_date, end_date).
    """
    if not date_str:
        return None, None
    s = str(date_str).strip()

    # 1. Полный диапазон: 09.08-15.08 или 09.08.2026-15.08.2026
    match_full = re.search(r'(\d{1,2})[\./](\d{1,2})(?:[\./](\d{2,4}))?\s*[-—–]\s*(\d{1,2})[\./](\d{1,2})(?:[\./](\d{2,4}))?', s)
    if match_full:
        d1, m1, y1 = match_full.group(1), match_full.group(2), match_full.group(3)
        d2, m2, y2 = match_full.group(4), match_full.group(5), match_full.group(6)
        dt1 = parse_cargo_date(f"{d1}.{m1}" + (f".{y1}" if y1 else ""))
        dt2 = parse_cargo_date(f"{d2}.{m2}" + (f".{y2}" if y2 else ""))
        return dt1, dt2

    # 2. Сокращённый диапазон: 09-15.08
    match_short = re.search(r'(\d{1,2})\s*[-—–]\s*(\d{1,2})[\./](\d{1,2})(?:[\./](\d{2,4}))?', s)
    if match_short:
        d1, d2, m2, y2 = match_short.group(1), match_short.group(2), match_short.group(3), match_short.group(4)
        dt1 = parse_cargo_date(f"{d1}.{m2}" + (f".{y2}" if y2 else ""))
        dt2 = parse_cargo_date(f"{d2}.{m2}" + (f".{y2}" if y2 else ""))
        return dt1, dt2

    # 3. Одиночная дата
    dt = parse_cargo_date(s)
    return dt, dt


def parse_multiple_cargos(raw_text: str):
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    date_pattern = re.compile(r'(\d{1,2}[\./]\d{1,2})')
    
    cargo_lines = []
    common_details = []
    
    for line in lines:
        if date_pattern.search(line) and ('-' in line or '→' in line or '—' in line):
            cargo_lines.append(line)
        else:
            common_details.append(line)
            
    if not cargo_lines:
        return [raw_text]
        
    common_details_text = ", ".join(common_details) if common_details else ""
    result_texts = []
    for line in cargo_lines:
        single_raw = line
        if common_details_text:
            single_raw += f"\n{common_details_text}"
        result_texts.append(single_raw)
        
    return result_texts

import html

def format_carrier_info(user_id: int, username: str = "", full_name: str = "") -> str:
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    comp = row[0] if row and row[0] and row[0] != 'Не указана' else "Компания не указана"
    db_name = row[1] if row and row[1] else ""
    phone = row[2] if row and row[2] else "Не указан"

    display_name = db_name or full_name or "Сотрудник"

    if username:
        clean_username = username.lstrip('@')
        user_mention = f"@{clean_username}"
    else:
        user_mention = f"ID: {user_id}"

    return (
        f"🏢 **Компания:** {comp}\n"
        f"👤 **Сотрудник:** {display_name} ({user_mention})\n"
        f"📞 **Телефон сотрудника:** {phone}"
    )

def build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, admin_comment="", is_closed=False):
    if not cars_str.endswith("авто") and not cars_str.endswith("машин"):
        cars_formatted = f"{cars_str} авто"
    else:
        cars_formatted = cars_str

    status_prefix = "🚫 [ГРУЗ ЗАКРЫТ]\n\n" if is_closed else ""
    card = (
        f"{status_prefix}"
        f"📍 {date_str} | {route_str}\n"
        f"💰 {price_str} | 🚚 {cars_formatted}"
    )
    if details_text:
        card += f"\n📦 {details_text}"
    if admin_comment:
        card += f"\n💬 Комментарий логиста: {admin_comment}"
    return card

async def update_cargo_messages_for_all_users(cargo_id: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, route, price, cars_count, details, status, admin_comment FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return
        
    date_str, route_str, price_str, cars_str, details_text, status, admin_comment = row
    is_closed = (status in ['CLOSED', 'EXPIRED'])
    new_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, admin_comment=admin_comment, is_closed=is_closed)
    
    cursor.execute("SELECT user_id, message_id FROM user_messages WHERE cargo_id = ?", (cargo_id,))
    messages_to_edit = cursor.fetchall()

    # Получаем список пользователей, которые уже подтвердили этот груз
    cursor.execute("SELECT DISTINCT user_id FROM confirmed_deals WHERE load_id = ?", (cargo_id,))
    confirmed_users = {c[0] for c in cursor.fetchall()}
    conn.close()

    for u_id, msg_id in messages_to_edit:
        try:
            # Если груз закрыт ИЛИ конкретный пользователь его уже забронировал — убираем кнопки полностью
            if is_closed or (u_id in confirmed_users):
                card_text = f"✅ [ГРУЗ ЗАБРОНИРОВАН ВАМИ]\n\n{new_text}" if (u_id in confirmed_users) else new_text
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=card_text,
                    reply_markup=None
                )
            else:
                builder = InlineKeyboardBuilder()
                web_app_url = f"{RENDER_URL}/webapp?user_id={u_id}"
                builder.row(types.InlineKeyboardButton(text="🚀 Открыть в Web App", web_app=WebAppInfo(url=web_app_url)))
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=builder.as_markup()
                )
        except Exception:
            pass

KAITEN_DOMAIN = os.getenv("KAITEN_DOMAIN", "belkaspian.kaiten.ru")
KAITEN_API_KEY = os.getenv("KAITEN_API_KEY", "")

# Настройки досок
KAITEN_BOARDS = {
    "UZBEKISTAN": {"space_id": 589979, "board_id": 1341041},
    "ASIA_CAUCASUS": {"space_id": 326566, "board_id": 764621}
}

async def kaiten_api_request(method: str, endpoint: str, json_data: dict = None, params: dict = None):
    if not KAITEN_API_KEY:
        logging.warning("⚠️ KAITEN_API_KEY не установлен!")
        return None

    url = f"https://{KAITEN_DOMAIN}/api/v1{endpoint}"
    headers = {
        "Authorization": f"Bearer {KAITEN_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, json=json_data, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in [200, 201]:
                    return await resp.json()
                else:
                    err_text = await resp.text()
                    logging.error(f"❌ Kaiten API error ({resp.status}): {err_text}")
                    return None
    except Exception as e:
        logging.error(f"❌ Kaiten connection error: {e}")
        return None

def extract_cities_from_route(route_str: str) -> list:
    if not route_str:
        return []
    parts = re.split(r'→|-|—|\/|\\', route_str)
    return [p.strip().lower() for p in parts if len(p.strip()) >= 3]

# Точные ID целевых колонок в Kaiten
KAITEN_TARGET_COLUMNS = {
    "UZBEKISTAN": [4660475, 4660653, 4660663, 4660665],
    "ASIA_CAUCASUS": [2729658, 2729682, 2783503, 2729698]
}

ALLOWED_KAITEN_COLUMNS = ["в процессе", "оформлено", "едут у даника", "замена данных"]

async def find_kaiten_card_for_deal(deal_id: int, route_str: str, date_str: str, country_str: str):
    debug_logs = []
    
    if not KAITEN_API_KEY:
        return None, "⚠️ Переменная `KAITEN_API_KEY` не задана на Render!"

    is_uzbekistan = ("узбекистан" in (country_str or "").lower()) or ("узбекистан" in (route_str or "").lower()) or any(c in (route_str or "").lower() for c in ['ташкент', 'самарканд', 'бухара', 'навои', 'джизак', 'фергана'])
    
    direction_key = "UZBEKISTAN" if is_uzbekistan else "ASIA_CAUCASUS"
    board_config = KAITEN_BOARDS[direction_key]
    target_columns = KAITEN_TARGET_COLUMNS[direction_key]
    
    space_id = board_config["space_id"]
    board_id = board_config["board_id"]
    space_name = "Узбекистан" if is_uzbekistan else "Азия и Кавказ"

    debug_logs.append(f"📌 **Маршрут:** `{route_str}`")
    debug_logs.append(f"📅 **Дата:** `{date_str}`")
    debug_logs.append(f"📋 **Целевая доска:** {space_name} (Board ID: `{board_id}`)")
    debug_logs.append(f"📑 **Запрос по ID колонок:** `{target_columns}`")

    # Запрашиваем карточки напрямую из 4 целевых колонок
    cards = []
    for col_id in target_columns:
        col_cards = await kaiten_api_request("GET", f"/cards", params={"column_id": col_id, "archived": "false", "limit": 100})
        if not col_cards or not isinstance(col_cards, list):
            col_cards = await kaiten_api_request("GET", f"/columns/{col_id}/cards", params={"archived": "false", "limit": 100})
        if col_cards and isinstance(col_cards, list):
            cards.extend(col_cards)

    # Запасной запрос по доске
    if not cards:
        cards = await kaiten_api_request("GET", f"/boards/{board_id}/cards", params={"archived": "false", "limit": 100})

    raw_res = cards
    cards = []
    if isinstance(raw_res, list):
        cards = raw_res
    elif isinstance(raw_res, dict):
        cards = raw_res.get("cards") or raw_res.get("data") or raw_res.get("results") or [raw_res]

    if not cards:
        debug_logs.append("❌ **Ошибка API Kaiten:** Не удалось получить список карточек из целевых колонок.")
        return None, "\n".join(debug_logs)

    debug_logs.append(f"📦 Всего активных карточек получено из целевых колонок: `{len(cards)}` шт.")

    route_cities = extract_cities_from_route(route_str)
    day_month_match = re.search(r'(\d{1,2})[\./](\d{1,2})', date_str or "")
    target_day = int(day_month_match.group(1)) if day_month_match else None
    target_month = int(day_month_match.group(2)) if day_month_match else None

    debug_logs.append(f"🏙 Искомые города: `{route_cities}`")
    debug_logs.append(f"🗓 День/Месяц: `{target_day}` / `{target_month}`")

    candidate_cards = []
    cards_starting_with_93 = []

    for card in cards:
        if not isinstance(card, dict):
            continue

        title = (card.get("title") or card.get("name") or "").strip()
        
        # 1. Название карточки должно начинаться на "93"
        if not re.search(r'^\s*93\b', title):
            continue

         # 2. ФИЛЬТР: При первичном поиске берем ТОЛЬКО карточки типа "Данные не внесены"
        card_type_obj = card.get("type")
        type_name = (card_type_obj.get("name") if isinstance(card_type_obj, dict) else str(card_type_obj or "")).lower().strip()
        
        if "данные не внесены" not in type_name:
            continue

        col_data = card.get("column")
        col_title = (col_data.get("title") if isinstance(col_data, dict) else str(col_data or "")).lower().strip()
        due_date_raw = str(card.get("due_date") or card.get("due_datetime") or card.get("due_date_time") or "Не указан")

        cards_starting_with_93.append(f"• `{title}`\n  └ Колонка: `{col_title or 'целевая'}` | Срок: `{due_date_raw}`")

        # 2. Проверка названия колонки (если объект column передан)
        if isinstance(col_data, dict) and col_title:
            if not any(allowed in col_title for allowed in ALLOWED_KAITEN_COLUMNS):
                continue

        title_lower = title.lower()
        score = 0

        # Проверка маршрута (сопоставление городов)
        matched_cities = 0
        for city in route_cities:
            if city in title_lower:
                score += 4
                matched_cities += 1

        if route_cities and matched_cities == 0:
            continue

        # Проверка даты
        if due_date_raw and target_day and target_month:
            try:
                m_date = re.search(r'(\d{4})-(\d{2})-(\d{2})', due_date_raw)
                if m_date:
                    c_month = int(m_date.group(2))
                    c_day = int(m_date.group(3))
                    if c_day == target_day and c_month == target_month:
                        score += 5
            except Exception:
                pass

        if target_day and str(target_day) in title_lower:
            score += 1

        if score > 0 or matched_cities > 0:
            type_name = str(card.get("type", {}).get("name", "")).lower() if isinstance(card.get("type"), dict) else ""
            description = (card.get("description") or "").strip()

            priority_bonus = 0
            if "данные не внесены" in type_name or "данные не внесены" in title_lower:
                priority_bonus += 10
            if not description or len(description) < 20:
                priority_bonus += 5

            candidate_cards.append((score + priority_bonus, card))

    if cards_starting_with_93:
        debug_logs.append(f"\n📋 **Карточки на 93... в целевых колонках ({len(cards_starting_with_93)} шт.):**\n" + "\n".join(cards_starting_with_93[:8]))
    else:
        debug_logs.append("\n⚠️ **Ни одной карточки на '93' не найдено в целевых колонках!**")

    if candidate_cards:
        candidate_cards.sort(key=lambda x: x[0], reverse=True)
        best_item = candidate_cards[0]
        best_card = best_item[1] if isinstance(best_item, (tuple, list)) and len(best_item) > 1 else best_item
        
        if isinstance(best_card, dict):
            debug_logs.append(f"\n✅ **Найдена лучшая карточка:** `{best_card.get('title')}` (ID: `{best_card.get('id')}`)")
            return best_card, "\n".join(debug_logs)

    debug_logs.append("\n⚠️ **Итог:** Ни одна карточка 93... не подошла по фильтру.")
    return None, "\n".join(debug_logs)

KAITEN_TYPE_CACHE = {}

async def get_kaiten_type_id(type_name_query: str):
    key = type_name_query.strip().lower()
    if key in KAITEN_TYPE_CACHE:
        return KAITEN_TYPE_CACHE[key]

    types_list = await kaiten_api_request("GET", "/card-types")
    if not types_list or not isinstance(types_list, list):
        types_list = await kaiten_api_request("GET", "/types")

    if types_list and isinstance(types_list, list):
        for t in types_list:
            name = (t.get("name") or t.get("title") or "").strip().lower()
            t_id = t.get("id")
            if t_id:
                KAITEN_TYPE_CACHE[name] = t_id
                if key in name or name in key:
                    KAITEN_TYPE_CACHE[key] = t_id

    return KAITEN_TYPE_CACHE.get(key)


async def push_data_to_kaiten(deal_id: int, user_id: int, admin_user_name: str = ""):
    if not KAITEN_API_KEY:
        return False, "⚠️ Переменная `KAITEN_API_KEY` не задана в настройках на Render!"

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    if not deal_id or deal_id == 0:
        cursor.execute("SELECT id FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        r_deal = cursor.fetchone()
        if r_deal:
            deal_id = r_deal[0]

    cursor.execute("""
        SELECT cd.kaiten_card_id, cd.route, cd.date, cd.price, cd.last_truck_plate,
               cd.last_trailer_plate, cd.last_driver_name, cd.driver_phone,
               l.destination_country, u.company, u.name, u.phone, cd.full_doc_text,
               cd.docs_status
        FROM confirmed_deals cd
        LEFT JOIN loads l ON cd.load_id = l.load_id
        LEFT JOIN users u ON cd.user_id = u.user_id
        WHERE cd.id = ?
    """, (deal_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False, f"Сделка #{deal_id} не найдена в базе данных"

    saved_card_id, route_str, date_str, agreed_price, truck_plate, trailer_plate, driver_name, driver_phone, country_str, company_str, carrier_name, carrier_phone, full_doc_text, docs_status = row

    card_id = saved_card_id
    card_title = ""

    # Поиск карточки в Кайтен, если ID ещё не сохранён
    if not card_id:
        res = await find_kaiten_card_for_deal(deal_id, route_str, date_str, country_str)
        card = None
        debug_text = "Не удалось выполнить поиск карточки"

        if isinstance(res, (tuple, list)):
            if len(res) > 0 and isinstance(res[0], dict):
                card = res[0]
            if len(res) > 1 and isinstance(res[1], str):
                debug_text = res[1]
        elif isinstance(res, dict):
            card = res

        if not card or not isinstance(card, dict):
            return False, debug_text

        card_id = card.get("id")
        card_title = card.get("title", "")

        if card_id:
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE confirmed_deals SET kaiten_card_id = ? WHERE id = ?", (card_id, deal_id))
            conn.commit()
            conn.close()

    # 1. Формируем описание карточки в формате "Данные на погрузку"
    if full_doc_text and full_doc_text.strip():
        description_text = full_doc_text.strip()
    else:
        description_text = (
            f"ТС (марка, г/н, страна регистрации): гос.ном.: {truck_plate or 'Не указан'}\n"
            f"Прицеп (марка, г/н, страна регистрации): гос.ном.: {trailer_plate or 'Не указан'}\n"
            f"ФИО водителя: {driver_name or 'Не указан'}\n"
            f"Тел (росс): {driver_phone or 'Не указан'}\n"
            f"Водительское удостоверение (№, когда и кем выдано): Не распознано\n"
            f"Паспорт (серия, №, когда и кем выдан): Не распознан\n"
            f"Дата рождения: Не распознана"
        )

    # 2. Определение типа карточки в Кайтен
    target_type_name = "Данные внесены" if docs_status == "FULL" else "Данные внесены частично"
    type_id = await get_kaiten_type_id(target_type_name)

    patch_payload = {"description": description_text}
    if type_id:
        patch_payload["type_id"] = type_id

    # PATCH запрос на обновление описания
    await kaiten_api_request("PATCH", f"/cards/{card_id}", json_data=patch_payload)

    # 3. Формируем строго лаконичный комментарий (Перевозчик + Ставка)
    carrier_title = company_str.strip() if (company_str and company_str.strip() not in ["Не указана", ""]) else (carrier_name or "Перевозчик")
    comment_text = f"{carrier_title}\n{agreed_price or ''}".strip()

    # POST запрос на добавление комментария
    await kaiten_api_request("POST", f"/cards/{card_id}/comments", json_data={"text": comment_text})

    card_url = f"https://{KAITEN_DOMAIN}/card/{card_id}"
    return True, {"card_id": card_id, "url": card_url, "title": card_title}


async def send_cargo_to_user(user_id: int, cargo_id: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    if u_row and u_row[0] == 'BLOCKED':
        conn.close()
        return

    is_verified = bool(u_row and u_row[1] == 'VERIFIED')

    cursor.execute("SELECT date, route, price, cars_count, details, status, admin_comment FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return
        
    date_str, route_str, price_str, cars_str, details_text, status, admin_comment = row
    is_closed = (status in ['CLOSED', 'EXPIRED'])

    # Если пользователь не верифицирован — скрываем ставку и информацию о грузе
    if not is_verified:
        price_str = "🔒 Скрыто"
        details_text = ""
        admin_comment = ""

    formatted_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, admin_comment=admin_comment, is_closed=is_closed)
    
    builder = InlineKeyboardBuilder()
    if not is_closed:
        web_app_url = f"{RENDER_URL}/webapp?user_id={user_id}"
        builder.row(types.InlineKeyboardButton(text="🚀 Открыть в Web App", web_app=WebAppInfo(url=web_app_url)))
    
    try:
        msg = await bot.send_message(
            chat_id=user_id, 
            text=formatted_text, 
            reply_markup=builder.as_markup() if not is_closed else None
        )
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO user_messages (cargo_id, user_id, message_id) VALUES (?, ?, ?)", (cargo_id, user_id, msg.message_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

# ==================== ИИ GEMINI МОДЕЛИ И СХЕМЫ ====================


class ParsedCargoItem(BaseModel):
    destination_country: str = Field(description="Строго одно из направлений: 'Казахстан 🇰🇿', 'Узбекистан 🇺🇿', 'Кыргызстан 🇰🇬', 'Грузия 🇬🇪', 'Азербайджан 🇦🇿', 'Армения 🇦🇲'")
    date: str = Field(description="Дата погрузки или диапазон (например, '15.05', '12-15.05', 'Срочно')")
    route: str = Field(description="Маршрут в виде: Город загрузки → Город выгрузки (например, 'Минск → Ташкент')")
    cars_count: str = Field(default="1", description="Количество авто только цифрой (например, '1', '2')")
    price: str = Field(default="Торги", description="Ставка с валютой (например, '2500 USD', '260 000 RUB') или 'Торги'")
    car_type: str = Field(default="Тент/реф", description="Тип кузова (Тент, Реф, Мега, Сцепка, Изотерм)")
    cargo_type: str = Field(default="ТНП", description="Тип груза (ТНП, Оборудование, Металл и т.д.)")
    weight: str = Field(default="до 22т", description="Вес или объем (например, 'до 22т', '20т')")
    details: str = Field(default="", description="Дополнительные требования через запятую (например: АДР, боковая погрузка, 2 цмр)")
    time_limit: Optional[str] = Field(default="", description="Время окончания торгов по МСК (например, '15:00'), если указано в тексте")

class CargoBatchExtraction(BaseModel):
    cargos: list[ParsedCargoItem] = Field(description="Список всех отдельных грузов из текста")

async def parse_cargos_with_ai(raw_text: str) -> list[dict]:
    """Интеллектуальный разбор заявки или списка заявок через Gemini API."""
    if not GEMINI_API_KEY or not gemini_client or not HAS_GENAI:
        return []

    system_prompt = (
        "Ты — старший диспетчер международной транспортной компании. "
        "Твоя задача — извлечь структурированные данные по каждому грузу из сообщения.\n"
        "Правила:\n"
        "1. Сообщение может содержать как 1 груз, так и список из нескольких грузов — извлеки каждый отдельно.\n"
        "2. Страну назначения destination_country выбери СТРОГО из списка: "
        "['Казахстан 🇰🇿', 'Узбекистан 🇺🇿', 'Кыргызстан 🇰🇬', 'Грузия 🇬🇪', 'Азербайджан 🇦🇿', 'Армения 🇦🇲'].\n"
        "3. Маршрут стандартизируй через стрелку: 'Город → Город'.\n"
        "4. Если цена не указана или написано 'торги' — установи price = 'Торги'.\n"
        "5. Если указано время окончания торгов (например 'до 15:00', 'до 16 МСК') — укажи его в time_limit."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=CargoBatchExtraction,
        temperature=0.0
    )

    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-2.5-flash"]
    for model_name in models_to_try:
        try:
            if hasattr(gemini_client, 'aio'):
                resp = await gemini_client.aio.models.generate_content(model=model_name, contents=[raw_text], config=config)
            else:
                resp = await asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=[raw_text], config=config)

            if resp and resp.text:
                clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip(), flags=re.MULTILINE)
                data = json.loads(clean_json)
                parsed = CargoBatchExtraction(**data)
                return [c.model_dump() for c in parsed.cargos]
        except Exception as e:
            logging.warning(f"⚠️ Ошибка разбора груза моделью {model_name}: {e}")

    return []


class VehicleDetails(BaseModel):
    brand: Optional[str] = Field(default="Не распознан", description="Марка ТС")
    model: Optional[str] = Field(default="", description="Модель ТС")
    plate: Optional[str] = Field(default="Не распознан", description="Гос. номер ТС")
    vin: Optional[str] = Field(default="Не распознан", description="VIN номер")
    country: Optional[str] = Field(default="Не распознана", description="Страна регистрации")

class DocumentDetails(BaseModel):
    full_name: Optional[str] = Field(default="Не распознан", description="ФИО владельца паспорта")
    number: Optional[str] = Field(default="Не распознан", description="Номер документа")
    issue_date: Optional[str] = Field(default="Не распознана", description="Дата выдачи")
    expiry_date: Optional[str] = Field(default="Не указана", description="Срок действия документа")
    authority: Optional[str] = Field(default="Не распознан", description="Орган выдачи")
    country: Optional[str] = Field(default="Не распознана", description="Страна выдачи")

class DriverDetails(BaseModel):
    full_name: Optional[str] = Field(default="Не распознан", description="ФИО водителя из паспорта")
    birth_date: Optional[str] = Field(default="Не распознана", description="Дата рождения")
    phones: Optional[str] = Field(default="Не указан", description="Номера телефонов водителя")
    passport: Optional[DocumentDetails] = None
    license: Optional[DocumentDetails] = None

class ImageClassification(BaseModel):
    image_index: int = Field(description="Порядковый номер")
    category: str = Field(description="Категория документа")

class FullCargoSubmission(BaseModel):
    truck: Optional[VehicleDetails] = None
    trailer: Optional[VehicleDetails] = None
    driver: Optional[DriverDetails] = None
    image_roles: Optional[list[ImageClassification]] = None

class OrderPdfInfo(BaseModel):
    order_number: Optional[str] = Field(default="", description="Номер договора-заявки или номер заявки")
    truck_plate: Optional[str] = Field(default="", description="Гос. номер грузового автомобиля / тягача")
    trailer_plate: Optional[str] = Field(default="", description="Гос. номер полуприцепа / прицепа (если указан)")


class PaymentDocsVerificationResult(BaseModel):
    has_cmr: bool = Field(description="Есть ли в представленных документах ЦМР (CMR)")
    has_invoice: bool = Field(description="Есть ли счет на оплату")
    has_act: bool = Field(description="Есть ли акт выполненных работ")
    has_signed_application: bool = Field(description="Есть ли подписанный договор-заявка")

    app_signed_by_both: bool = Field(description="Подписана ли заявка с двух сторон (2 печати/подписи)")
    app_order_number: Optional[str] = Field(default="", description="Номер заявки из договора-заявки")
    app_truck_plate: Optional[str] = Field(default="", description="Номер авто из договора-заявки")

    cmr_loading_date: Optional[str] = Field(default="", description="Дата загрузки из графы 21 ЦМР (ДД.ММ.ГГГГ)")
    cmr_unloading_date: Optional[str] = Field(default="", description="Дата выгрузки из графы 24 ЦМР (актуальная дата отпуска авто)")
    cmr_truck_plate: Optional[str] = Field(default="", description="Номер авто из графы 22 ЦМР")
    cmr_box16_stamp: bool = Field(description="Есть ли печать/подпись перевозчика в графе 16 ЦМР")
    cmr_box23_stamp: bool = Field(description="Есть ли печать/подпись водителя/перевозчика в графе 23 ЦМР")
    cmr_box24_stamp: bool = Field(description="Есть ли печать и подпись получателя груза в графе 24 ЦМР (или рядом)")

    act_date: Optional[str] = Field(default="", description="Дата акта выполненных работ (ДД.ММ.ГГГГ)")

    is_valid: bool = Field(description="Выполнены ли абсолютно все требования без ошибок")
    errors: list[str] = Field(default=[], description="Список найденных ошибок на русском языке")

async def verify_payment_docs_with_ai(contents_list, expected_order_num="", expected_truck="") -> PaymentDocsVerificationResult:
    """
    Проверяет пакет документов на оплату через Gemini API.
    """
    if not GEMINI_API_KEY or not gemini_client or not HAS_GENAI:
        return PaymentDocsVerificationResult(
            has_cmr=True, has_invoice=True, has_act=True, has_signed_application=True,
            app_signed_by_both=True, cmr_box16_stamp=True, cmr_box23_stamp=True, cmr_box24_stamp=True,
            is_valid=True, errors=[]
        )

    system_prompt = (
        "Ты — эксперт финансовой службы и бухгалтерии транспортной компании. Твоя задача — тщательно проверить пакет документов на оплату:\n"
        "1. Проверь наличие всех 4 типов документов: ЦМР (CMR), Счет, Акт выполненных работ, Подписанная заявка.\n"
        f"2. Заявка: Должна быть подписана и скреплена печатями С 2-Х СТОРОН (заказчик и перевозчик). "
        f"Сверь номер заявки (Ожидаемый: '{expected_order_num}') и номер авто (Ожидаемый: '{expected_truck}').\n"
        "3. ЦМР (CMR):\n"
        "   - Графа 21: Извлеки дату загрузки.\n"
        "   - Графа 24: Извлеки дату выгрузки/отпуска авто (если несколько дат, бери последнюю когда авто отпустили).\n"
        f"   - Графа 22: Сверь номер авто с ожидаемым '{expected_truck}'.\n"
        "   - Проверь наличие печатей/подписей в Графе 16 и Графе 23.\n"
        "   - Графа 24 (или рядом): ОБЯЗАТЕЛЬНО должна быть печать и подпись ПОЛУЧАТЕЛЯ ГРУЗА (подтверждение приемки).\n"
        "4. Акт выполненных работ: Дата акта ДОЛЖНА СТРОГО СОВПАДАТЬ с датой выгрузки из графы 24 ЦМР.\n"
        "5. Если есть хоть одно нарушение — установи is_valid=False и опиши подробную причину в списке errors."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=PaymentDocsVerificationResult,
        temperature=0.1
    )

    models_to_try = [
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash"
    ]

    for model_name in models_to_try:
        try:
            if hasattr(gemini_client, 'aio'):
                resp = await gemini_client.aio.models.generate_content(model=model_name, contents=contents_list, config=config)
            else:
                resp = await asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=contents_list, config=config)

            if resp and resp.text:
                raw_text = resp.text.strip()
                if "```" in raw_text:
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                    raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE)
                raw_json = json.loads(raw_text)
                return PaymentDocsVerificationResult(**raw_json)
        except Exception as e:
            logging.warning(f"⚠️ Ошибка проверки документов на оплату моделью {model_name}: {e}")

    return PaymentDocsVerificationResult(
        has_cmr=False, has_invoice=False, has_act=False, has_signed_application=False,
        app_signed_by_both=False, cmr_box16_stamp=False, cmr_box23_stamp=False, cmr_box24_stamp=False,
        is_valid=False, errors=["Ошибка связи с сервером ИИ. Попробуйте повторить отправку."]
    )



def is_doc_expired_or_expiring_soon(expiry_str: str, threshold_days: int = 20) -> bool:
    if not expiry_str or str(expiry_str).lower().strip() in ["не распознана", "не указана", "бессрочно", "бессрочный", "none", "null"]:
        return False

    clean_str = str(expiry_str).strip()
    
    match = re.search(r'(\d{1,2})[\./-](\d{1,2})[\./-](\d{2,4})', clean_str)
    if match:
        day, month, year_raw = int(match.group(1)), int(match.group(2)), int(match.group(3))
        year = year_raw + 2000 if year_raw < 100 else year_raw
    else:
        match_iso = re.search(r'(\d{4})[\./-](\d{1,2})[\./-](\d{1,2})', clean_str)
        if match_iso:
            year, month, day = int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3))
        else:
            return False

    try:
        exp_date = date(year, month, day)
        today = datetime.now(timezone.utc).date()
        cutoff_date = today + timedelta(days=threshold_days)
        return exp_date <= cutoff_date
    except ValueError:
        return False

try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

def normalize_plate_number(plate_str: str) -> str:
    """
    Очищает гос. номер от случайных пробелов и спецсимволов.
    """
    if not plate_str:
        return ""
    p = plate_str.strip().upper().replace(" ", "")
    p = re.sub(r'[^A-Z0-9А-Я]', '', p)
    return p

def split_and_crop_documents(image_bytes: bytes) -> list[Image.Image]:
    """
    Находит ВСЕ отдельные бланки документов на фотографии,
    отсекает темные сиденья/покрывала и возвращает список вырезанных карточек.
    """
    cropped_images = []
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        pil_img = ImageOps.exif_transpose(pil_img).convert('RGB')

        if not HAS_OPENCV:
            return [pil_img]

        # Создаем рабочие копии
        small_pil = pil_img.copy()
        small_pil.thumbnail((1200, 1200))
        img_cv_small = cv2.cvtColor(np.array(small_pil), cv2.COLOR_RGB2BGR)

        orig_cv = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        orig_h, orig_w = orig_cv.shape[:2]
        small_h, small_w = img_cv_small.shape[:2]

        scale_x = orig_w / float(small_w)
        scale_y = orig_h / float(small_h)

        gray = cv2.cvtColor(img_cv_small, cv2.COLOR_BGR2GRAY)
        
        # Порог по яркости: отделяем светлые документы от темных кожаных сидений/стола
        _, bright_thresh = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 30, 140)
        
        combined = cv2.bitwise_and(edges, bright_thresh)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        dilated = cv2.dilate(combined, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        img_area = small_w * small_h
        found_rects = []

        for cnt in contours:
            area = cv2.contourArea(cnt)
            # Документ занимает от 3% до 90% площади кадра
            if 0.03 * img_area < area < 0.92 * img_area:
                x, y, bw, bh = cv2.boundingRect(cnt)
                
                # Проверка средней яркости прямоугольника (отсекаем черные складки кожи)
                roi_gray = gray[y:y+bh, x:x+bw]
                if roi_gray.size > 0 and cv2.mean(roi_gray)[0] < 65:
                    continue

                aspect_ratio = float(bw) / bh if bh > 0 else 0
                if 0.3 <= aspect_ratio <= 3.2:
                    peri = cv2.arcLength(cnt, True)
                    approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
                    rect_pts = approx if len(approx) == 4 else np.array([[[x, y]], [[x+bw, y]], [[x+bw, y+bh]], [[x, y+bh]]])
                    found_rects.append((area, rect_pts, (x, y, bw, bh)))

        # Сортировка по площади и отсечение вложенных контуров
        found_rects.sort(key=lambda item: item[0], reverse=True)
        final_rects = []
        for area, pts, box in found_rects:
            x1, y1, w1, h1 = box
            is_inside = False
            for f_area, f_pts, f_box in final_rects:
                x2, y2, w2, h2 = f_box
                if x1 >= x2 - 8 and y1 >= y2 - 8 and (x1 + w1) <= (x2 + w2 + 8) and (y1 + h1) <= (y2 + h2 + 8):
                    is_inside = True
                    break
            if not is_inside:
                final_rects.append((area, pts, box))

        # Упорядочивание вырезанных документов сверху-вниз, слева-направо
        final_rects.sort(key=lambda item: (item[2][1] // 100, item[2][0]))

        if final_rects:
            for area, best_rect, box in final_rects:
                pts = best_rect.reshape(4, 2).astype("float32")
                pts[:, 0] *= scale_x
                pts[:, 1] *= scale_y

                rect = np.zeros((4, 2), dtype="float32")
                s = pts.sum(axis=1)
                rect[0] = pts[np.argmin(s)]
                rect[2] = pts[np.argmax(s)]
                diff = np.diff(pts, axis=1)
                rect[1] = pts[np.argmin(diff)]
                rect[3] = pts[np.argmax(diff)]

                (tl, tr, br, bl) = rect
                widthA = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
                widthB = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
                maxWidth = max(int(widthA), int(widthB))

                heightA = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
                heightB = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
                maxHeight = max(int(heightA), int(heightB))

                if maxWidth < 90 or maxHeight < 90:
                    continue

                dst = np.array([
                    [0, 0],
                    [maxWidth - 1, 0],
                    [maxWidth - 1, maxHeight - 1],
                    [0, maxHeight - 1]
                ], dtype="float32")

                M = cv2.getPerspectiveTransform(rect, dst)
                warped = cv2.warpPerspective(orig_cv, M, (maxWidth, maxHeight))
                warped_pil = Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))

                # Авто-поворот: карточки СТС и Прав должны лежать горизонтально
                if warped_pil.height > warped_pil.width * 1.15:
                    warped_pil = warped_pil.rotate(270, expand=True)

                cropped_images.append(warped_pil)

        if not cropped_images:
            return [pil_img]

        return cropped_images
    except Exception as e:
        logging.error(f"Split documents error: {e}")
        try:
            return [Image.open(io.BytesIO(image_bytes)).convert('RGB')]
        except Exception:
            return []

def get_category_priority(cat_str: str) -> int:
    s = str(cat_str).lower().strip()
    
    # 1. Паспорт главный разворот
    if 'passport_front' in s or 'паспорт_лиц' in s or 'passport_main' in s: return 10
    # 2. Паспорт 2-я страница / ID оборот
    if 'passport_back' in s or 'паспорт_оборо' in s or 'id_back' in s: return 12
    if 'passport' in s or 'паспорт' in s: return 10

    # 3. Водительское удостоверение Лицевая
    if 'license_front' in s or 'права_лиц' in s or 'drivers_license_front' in s: return 20
    # 4. Водительское удостоверение Оборотная (сетка категорий)
    if 'license_back' in s or 'права_оборо' in s or 'drivers_license_back' in s: return 22
    if 'license' in s or 'права' in s or 'водительск' in s: return 20

    # 5. СТС Тягача Лицевая (Daimler/Volvo/MAN)
    if ('truck' in s or 'тягач' in s) and ('front' in s or 'лиц' in s or '1' in s): return 30
    # 6. СТС Тягача Оборотная
    if ('truck' in s or 'тягач' in s) and ('back' in s or 'оборо' in s or '2' in s): return 32
    if 'truck' in s or 'тягач' in s or 'стс_тягач' in s: return 30

    # 7. СТС Прицепа Лицевая (Schmitz/Krone)
    if ('trailer' in s or 'прицеп' in s) and ('front' in s or 'лиц' in s or '1' in s): return 40
    # 8. СТС Прицепа Оборотная
    if ('trailer' in s or 'прицеп' in s) and ('back' in s or 'оборо' in s or '2' in s): return 42
    if 'trailer' in s or 'прицеп' in s or 'стс_прицеп' in s: return 40

    # 9+. Прочие документы (лицензии, карточки)
    return 90

async def process_docs_bytes_with_ai(contents, text_notes, is_polyethylene=False, route_str=""):
    fallback_text = (
        "Тягач: Не распознан\n"
        "Прицеп: Не распознан\n"
        "Водитель: Не распознан\n"
        f"Номера телефонов: {text_notes or 'Не указан'}\n"
        "Паспорт: Не распознан\n"
        "Водительское: Не распознано"
    )

    if not GEMINI_API_KEY or not gemini_client or not HAS_GENAI:
        return fallback_text, {}

    system_prompt = """Ты — высокоточный OCR‑аудитор транспортных документов. Извлекай данные строго по визуальному слою изображений (Visual Ground Truth). Никаких догадок, автозамен, исправлений или использования скрытого текста, за исключением явных разрешённых нормализаций, описанных ниже.

==================================================
КРАТКИЕ ПРАВИЛА
==================================================
1. Читай только то, что видно: буква в букву, цифра в цифру.
2. Полный запрет на MRZ (строки с '<'), скрытые PDF‑слои, сторонний OCR и словарные исправления.
3. Если поле нечитаемо или отсутствует — возвращай null; фиксируй причину в review_reasons.
4. Страницы нумеруются по порядку переданных изображений: Page 1, Page 2, ...
5. Не добавляй, не удаляй и не переформатируй поля, не описывай внутреннюю логику промта в ответе.

==================================================
РАЗРЕШЁННЫЕ НОРМАЛИЗАЦИИ (единственные допустимые изменения)
==================================================
- **Телефоны:** удалять пробелы, скобки, тире; оставлять только цифры и ведущий '+'. Возвращать только уникальные номера (без дублей). Сортировку по приоритету (+7, +375) выполняет бэкенд.
- **Номера документов (паспорт, ВУ):** удалять символы "№" и внутренние пробелы между серией и номером.
- **VIN:** удалять пробелы и дефисы перед валидацией длины. Нормализация символов в VIN: 'I'→'1', 'O'→'0', 'Q'→'0' (только в VIN). Если после нормализации длина = 17 — VIN считается валидным.
- **Коды стран:** конвертировать коды (RUS, BY, KZ, PL и т.д.) в полные названия на русском.
- **Даты:** приводить к формату ДД.ММ.ГГГГ, если дата явно распознаваема.
- **Двузначные годы:** для expiry_date/issue_date/date_of_birth дополнять двухзначный год по правилу 00–40 → 20XX; 41–99 → 19XX. Если контекст неоднозначен — поле = null и добавлять review_reasons "Дата указана только месяц/год или двузначный год неоднозначен".
- Любые другие изменения, исправления или транслитерации запрещены.

==================================================
ПРАВИЛА ПОЛЕЙ (ЯСНО И ОДНОЗНАЧНО)
==================================================
driver.full_name
- Источник: СТРОГО И ИСКЛЮЧИТЕЛЬНО страница ПАСПОРТА (Surname / Given names / Patronymic).
- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО брать ФИО с Водительского удостоверения (В/У)! Если паспорта нет — driver.full_name = null.
- MRZ (строка с '<') категорически запрещено использовать; в таком случае full_name = null.
- Сохранять регистр символов как на документе.

passport.issuing_authority
- Считывать ТОЛЬКО краткое наименование или код органа выдачи (например: 'МВД 02001', 'MVD TJ', 'УВД', 'MIA', 'ИИБ').
- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО включать область, регион, район, город или место рождения (например: 'FERGANA REGION', 'SUGHD REGION', 'OBLAST', 'DISTRICT')! Место жительства/рождения — это НЕ орган выдачи.
- Заменять переносы строк и повторяющиеся пробелы на один одиночный пробел.
- Если поле отсутствует — passport.issuing_authority = null.

passport.date_of_birth
- Источник: поле "Date of birth / Дата рождения" на странице паспорта.
- Формат вывода: "ДД.ММ.ГГГГ".
- Если указаны только день и месяц без года — date_of_birth = null и добавлять review_reasons "Дата рождения неполная".
- Если год указан двумя цифрами — дополнять по правилу 00–40 → 20XX; 41–99 → 19XX; при сомнении — null + review_reasons "Двузначный год в дате рождения неоднозначен".
- MRZ категорически не использовать для извлечения даты рождения; если основное поле нечитаемо — date_of_birth = null и needs_human_review = true.

passport.issue_date
- Источник: поле "Issued on / Выдан" или штамп/подпись рядом с меткой органа выдачи.
- Формат вывода: "ДД.ММ.ГГГГ".
- Если указаны только месяц и год — issue_date = null и добавлять review_reasons "Дата выдачи указана только месяц/год".
- Применять правило дополнения двухзначного года 00–40 → 20XX; 41–99 → 19XX; при сомнении — null + review_reasons.
- Заменять переносы строк и повторяющиеся пробелы на один пробел при сохранении.

driver.country
- Источник: поле "Гражданство / Citizenship" в паспорте (если есть) — возвращать полное название страны на русском.
- Если в паспорте поле отсутствует или нечитаемо — country = null.

country (общая)
- Всегда возвращать полное название страны на русском языке.
- Если на бланке указан код (RUS, BY, KZ, PL и т.д.) — конвертировать код в полное название на русском.
- Если страны нет или нечитаема — country = null.

expiry_date
- Формат: "ДД.ММ.ГГГГ".
- Если на документе указан только месяц и год — expiry_date = null и добавлять review_reasons "Дата указана только месяц/год".
- Если бессрочно — "INDEFINITE".
- Если отсутствует/нечитаема — expiry_date = null.

driver.phones
- Возвращать массив cleaned уникальных телефонов (только цифры и ведущий '+').
- Сортировку по приоритету (+7, +375) выполняет бэкенд.
- Если телефонов нет — phones = [].

vehicles (массив объектов)
- Если на изображении присутствуют СТС / регистрационные документы ТС, для КАЖДОГО документа ДОЛЖЕН создаваться объект в массиве "vehicles" (даже если VIN или госномер не распознаны).
- brand: марка по визуальному слою или null.
- model: модель по визуальному слою или null.
- plate_number: гос. номер по визуальному слою (убрать пробелы при записи) или null.
- **Алфавит для plate_number (явно):**
  * Если страна регистрации ТС в {Россия, Беларусь, Казахстан} — для визуально совпадающих графем записывать кириллические символы: **А, В, Е, К, М, Н, О, Р, С, Т, У, Х** (использовать соответствующие Unicode‑символы кириллицы).
  * Если страна регистрации — европейская или явно латинская — записывать латиницей.
  * Если country = null и plate_number состоит только из перечисленных совпадающих графем и цифр — записывать эти графемы кириллицей и добавлять review_reasons "country не определена, применена кириллица по эвристике".
  * **Позиционная логика:** если формат/шаблон номера для страны предполагает цифру в конкретной позиции — трактовать символ как цифру; если позиция предполагает букву — трактовать как букву. Не заменять букву 'O' на цифру '0' (и наоборот) автоматически, если позиционная логика не подтверждает замену.
  * **Неоднозначность:** если в plate_number встречаются неоднозначные графемы (O/0, I/1, S/5 и т.п.) и позиционная логика не однозначна — не менять автоматически; ставить needs_human_review = true и добавлять review_reasons "Неоднозначные графемы в plate_number".
- country: страна регистрации ТС — полное название на русском или null.
- type: одно из TRUCK, TRAILER, UNKNOWN.
  * Примеры: YARIM TIRKAMA / ПРИЦЕП / ПОЛУПРИЦЕП → TRAILER; СЕДЕЛЬНЫЙ ТЯГАЧ / ГРУЗОВОЙ → TRUCK.
  * Если документ явно указывает легковой автомобиль (SEDAN, PASSENGER) и ваша бизнес‑логика требует отдельного типа — интегратор может расширить типы; по умолчанию для не‑грузовых/не‑прицепных ТС возвращать UNKNOWN.
- plate_number и vin принадлежат объекту vehicle (vin внутри каждого элемента vehicles).

VIN
- Перед проверкой удалить пробелы и дефисы.
- После очистки VIN должен состоять ровно из 17 символов.
- Допустимая нормализация: 'I'→'1', 'O'→'0', 'Q'→'0'.
- Если после нормализации длина = 17 — vin считается валидным.
- Если после очистки/нормализации длина ≠ 17 — vin = null.

Рукописный текст
- Если текст в поле написан от руки и есть сомнения хотя бы в одном символе — ставь null и добавляй review_reasons "Рукописный текст/нечитаемо".

==================================================
verification_flags
==================================================
- needs_human_review = true и добавлять строки в review_reasons (без дубликатов), если:
  * На изображении есть Паспорт, но full_name = null → reason: "Отсутствует или нечитаемо ФИО на паспорте".
  * На изображении есть СТС/регистрационный документ, но VIN = null → reason: "VIN не найден или некорректен".
  * Изображение размыто, обрезано или нечитаемо → reason: "Размыто/обрезано".
  * На документе указана только месяц/год в дате или двузначный год неоднозначен → reason: "Дата указана только месяц/год".
  * Текст рукописный и есть сомнения → reason: "Рукописный текст/нечитаемо".
  * Неоднозначные графемы в plate_number (O/0, I/1, S/5 и т.п.) → reason: "Неоднозначные графемы в plate_number".
  * Орган выдачи частично нечитаем → reason: "Орган выдачи частично нечитаем".
  * Есть явные сомнения в символах → reason: краткая причина на русском.
- review_reasons — массив уникальных строк на русском; если причин нет — [].

==================================================
ПОВЕДЕНИЕ ПРИ МНОГОСТРАНИЧНОСТИ
==================================================
- Страницы нумеруются по порядку переданных изображений: Page 1, Page 2, ...
- В image_roles должны быть описаны ВСЕ переданные страницы по порядку.
- Если в пакете несколько паспортов, driver = данные из первого обнаруженного паспорта (по порядку страниц).
- Если документов на ТС нет совсем — возвращать "vehicles": [].
- Для поддержки нескольких водителей интеграция должна передавать отдельные пакеты или оборачивать driver в массив на уровне интеграции.

==================================================
КАТЕГОРИИ И КАЧЕСТВО
==================================================
category: PASSPORT_FRONT | PASSPORT_BACK | DRIVERS_LICENSE_FRONT | DRIVERS_LICENSE_BACK | TRUCK_REGISTRATION | TRAILER_REGISTRATION | OTHER
quality: EXCELLENT | GOOD | POOR | UNREADABLE

==================================================
ФОРМАТ ВЫВОДА (СТРОГО)
==================================================
Выводи ответ только внутри двух тегов: сначала <analysis>, затем <json>. Никакого дополнительного текста, пояснений или markdown‑блоков внутри <json>. НЕ использовать бэктики (```) внутри тега <json>.

<analysis>
Page N: Category=КАТЕГОРИЯ | Quality=КАЧЕСТВО | Fields=краткое перечисление визуально прочитанных данных
</analysis>

<json>
{ 
  "image_roles":[{"page":1,"category":"PASSPORT_FRONT","quality":"EXCELLENT"}],
  "driver":{
    "full_name":null,
    "country":null,
    "passport":{
      "series_number":null,
      "expiry_date":null,
      "date_of_birth":null,
      "issue_date":null,
      "issuing_authority":null
    },
    "license":{"number":null,"expiry_date":null},
    "phones":[]
  },
  "vehicles":[
    {
      "type":"TRUCK",
      "brand":"VOLVO",
      "model":"FH16",
      "plate_number":"А123АА77",
      "vin":"YV2A4X0C1DB123456",
      "country":"Россия"
    }
  ],
  "verification_flags":{"needs_human_review":false,"review_reasons":[]}
}
</json>
"""

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=FullCargoSubmission,
        temperature=0.0
    )

    models_to_try = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-3.1-pro",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    ]

    response = None
    for model_name in models_to_try:
        try:
            if hasattr(gemini_client, 'aio'):
                response = await gemini_client.aio.models.generate_content(model=model_name, contents=contents, config=config)
            else:
                response = await asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=contents, config=config)
            if response and response.text:
                break
        except Exception as e:
            logging.warning(f"⚠️ Модель {model_name} недоступна: {e}")

    if response and response.text:
        try:
            raw_text = response.text.strip()
            if "```" in raw_text:
                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE)

            match_json = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match_json:
                raw_text = match_json.group(0)

            raw_json = json.loads(raw_text)
            
            t = raw_json.get("truck") or {}
            tr = raw_json.get("trailer") or {}
            d = raw_json.get("driver") or {}
            p = d.get("passport") or {}
            l = d.get("license") or {}

            raw_phones = (d.get("phones") or "").strip()
            phones_str = raw_phones if raw_phones and raw_phones.lower() not in ["не указан", "не распознан"] else (text_notes.strip() if text_notes else "Не указан")

            def build_brand_str(v_dict, show_model=True):
                b = (v_dict.get('brand') or '').strip()
                m = (v_dict.get('model') or '').strip()
                if not b and not m: return 'Не распознан'
                if not b: return m
                if show_model and m and m.lower() not in b.lower(): return f"{b} {m}"
                return b

            # ФИО берется СТРОГО с паспорта (без фоллбека на В/У)
            p_full_name = (p.get("full_name") or "").strip()
            if p.get("number") in ["Не распознан", None, ""] or not p_full_name:
                driver_full_name = "Не распознан"
            else:
                driver_full_name = p_full_name

            # Жесткая отсечка названий областей/районов из наименования органа выдачи
            p_auth = (p.get('authority') or 'Не распознан').strip()
            p_auth = re.sub(r'(?i)\b(?:fergana|sughd|khatlon|gorno-badakhshan|region|oblast|obl|district)\b.*$', '', p_auth).strip(' ,.-')
            if not p_auth:
                p_auth = "Не распознан"

            truck_brand_only = (t.get('brand') or 'Не распознан').strip()
            truck_full_brand = build_brand_str(t, show_model=True)
            truck_plate = (t.get('plate') or 'Не распознан').strip()
            truck_vin = (t.get('vin') or 'Не распознан').strip()

            trailer_brand_only = (tr.get('brand') or 'Не распознан').strip()
            trailer_full_brand = build_brand_str(tr, show_model=True)
            trailer_plate = (tr.get('plate') or 'Не распознан').strip()
            trailer_vin = (tr.get('vin') or 'Не распознан').strip()

            p_num = p.get('number') or 'Не распознан'
            p_date = p.get('issue_date') or 'Не распознана'

            l_num = l.get('number') or 'Не распознан'
            l_date = l.get('issue_date') or 'Не распознана'
            l_country = translate_country(l.get('country') or 'Не распознана')

            truck_country = translate_country(t.get('country') or 'Не распознана')
            trailer_country = translate_country(tr.get('country') or 'Не распознана')

            b_date = d.get('birth_date') or 'Не распознана'
            b_date_str = b_date if (b_date.endswith('г.') or b_date.endswith('г')) else f"{b_date}г."

            # Объединенная проверка слова полиэтилен по всем источникам
            route_check_str = f"{route_str or ''} {text_notes or ''}".lower()

            if is_polyethylene or "полиэтилен" in route_check_str or "polyethylene" in route_check_str:
                formatted_output = (
                    f"ТС (марка, г/н, страна регистрации): {truck_brand_only}, гос.ном.: {truck_plate}, {truck_country}\n"
                    f"Прицеп (марка, г/н, страна регистрации): {trailer_brand_only}, гос.ном.: {trailer_plate}, {trailer_country}\n"
                    f"ФИО водителя: {driver_full_name}\n"
                    f"Тел (росс): {phones_str}\n"
                    f"Водительское удостоверение (№, когда и кем выдано): {l_num} от {l_date}г., {l_country}\n"
                    f"Паспорт (серия, №, когда и кем выдан): {p_num} выдан {p_date}г. {p_auth}\n"
                    f"Дата рождения: {b_date_str}"
                )
            elif "боржоми" in route_check_str or "borjomi" in route_check_str:
                formatted_output = (
                    f"Машина: {truck_brand_only}, гос. номер: {truck_plate}/{trailer_plate}\n"
                    f"Водитель: {driver_full_name}\n"
                    f"Паспорт: {p_num} выдан {p_date}г. {p_auth}\n"
                    f"Телефон: {phones_str}\n"
                    f"Права: {l_num} от {l_date}г., {l_country}"
                )
            elif "тольятти" in route_check_str or "tolyatti" in route_check_str:
                formatted_output = (
                    f"Марка и модель: {truck_full_brand}\n"
                    f"Номер ТС: {truck_plate}/{trailer_plate}\n"
                    f"VIN: {truck_vin}\n"
                    f"Водитель: {driver_full_name}\n"
                    f"Паспорт: {p_num} выдан {p_date}г. {p_auth}\n"
                    f"Вод .удостоверение: {l_num} от {l_date}г.\n"
                    f"Телефон: {phones_str}"
                )
            else:
                formatted_output = (
                    f"Авто: {truck_brand_only}/{trailer_brand_only}, гос.ном: {truck_plate}/{trailer_plate}\n"
                    f"Водитель: {driver_full_name}\n"
                    f"Паспорт: {p_num} выдан {p_date}г. {p_auth}\n"
                    f"Водительское: {l_num} от {l_date}г.\n"
                    f"Номер телефона: {phones_str}\n\n\n"
                    f"Тягач: {truck_full_brand}, VIN: {truck_vin}\n"
                    f"Прицеп: {trailer_full_brand}, VIN: {trailer_vin}"
                )

            # Безопасная очистка символов подчёркивания (перенесена в конец)
            formatted_output = formatted_output.replace('_', ' ')
            return formatted_output, raw_json
        except Exception as e:
            logging.error(f"Error parsing Gemini JSON: {e}")

    return fallback_text, {}

async def process_docs_with_ai(photos_file_ids, doc_file_ids, text_notes, is_polyethylene=False, route_str=""):
    all_files = (photos_file_ids or []) + (doc_file_ids or [])
    contents = ["Внимательно изучи документы. ФИО извлекай СТРОГО с ПАСПОРТА. Телефон: " + (text_notes or "Нет")]

    for file_id in all_files:
        try:
            file_info = await bot.get_file(file_id)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            file_bytes = buf.getvalue()
            mime_type = detect_mime_type(file_bytes, file_info.file_path or "")

            raw_imgs = []
            if mime_type == "application/pdf":
                extracted = extract_images_from_pdf_bytes(file_bytes)
                raw_imgs = extracted if extracted else [file_bytes]
            else:
                raw_imgs = [file_bytes]

            # Разрезаем совмещенные фото НАПЕРЕД, чтобы ИИ сразу видел каждую карточку отдельно
            for raw_b in raw_imgs:
                cropped_pil_list = split_and_crop_documents(raw_b)
                for pil_item in cropped_pil_list:
                    out_b = io.BytesIO()
                    pil_item.save(out_b, format="JPEG", quality=90)
                    contents.append(genai_types.Part.from_bytes(data=out_b.getvalue(), mime_type="image/jpeg"))

        except Exception as e:
            logging.error(f"Download file error for AI: {e}")

    formatted_text, raw_json = await process_docs_bytes_with_ai(contents, text_notes, is_polyethylene=is_polyethylene, route_str=route_str)
    return formatted_text, all_files, raw_json
    

async def process_order_pdf_with_ai(file_bytes: bytes) -> tuple[str, str, str]:
    """
    Анализирует 1-ю страницу PDF-заявки через Gemini API.
    Возвращает (order_number, truck_plate, trailer_plate).
    """
    if not GEMINI_API_KEY or not gemini_client or not HAS_GENAI:
        return "", "", ""

    system_prompt = (
        "Ты — эксперт логистической компании. Твоя задача — внимательно изучить первую страницу договора-заявки "
        "и извлечь строго 3 поля:\n"
        "1. order_number: Номер договора-заявки / номер заказа.\n"
        "2. truck_plate: Гос. номер грузового авто / тягача.\n"
        "3. trailer_plate: Гос. номер прицепа / полуприцепа (если есть)."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=OrderPdfInfo,
        temperature=0.1
    )

    contents = [
        "Извлеки номер заявки, гос. номер тягача и номер прицепа с первой страницы документа:",
        genai_types.Part.from_bytes(data=file_bytes, mime_type="application/pdf")
    ]

    models_to_try = [
        # 1. Первичная экономная модель с гигантским лимитом
        "gemini-3.5-flash-lite",
        # 2. Флагманы для максимальной точности
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-2.5-flash",
        # 3. Резервные модели
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-3.1-pro",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    ]

    for model_name in models_to_try:
        try:
            if hasattr(gemini_client, 'aio'):
                response = await gemini_client.aio.models.generate_content(model=model_name, contents=contents, config=config)
            else:
                response = await asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=contents, config=config)
                
            if response and response.text:
                raw_text = response.text.strip()
                if "```" in raw_text:
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                    raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE)
                
                raw_json = json.loads(raw_text)
                order_num = (raw_json.get("order_number") or "").strip()
                truck = (raw_json.get("truck_plate") or "").strip()
                trailer = (raw_json.get("trailer_plate") or "").strip()
                return order_num, truck, trailer
        except Exception as e:
            logging.warning(f"⚠️ Ошибка обработки заявки моделью {model_name}: {e}")

    return "", "", ""


def extract_images_from_pdf_bytes(pdf_bytes: bytes) -> list[bytes]:
    """
    Извлекает растровые изображения со всех страниц PDF документа.
    """
    images_bytes = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            if hasattr(page, 'images') and page.images:
                for img_obj in page.images:
                    if hasattr(img_obj, 'data') and img_obj.data:
                        images_bytes.append(img_obj.data)
                        break
    except Exception as e:
        logging.error(f"Error extracting images from PDF: {e}")
    return images_bytes

async def generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, user_info, ai_formatted_data, raw_json=None) -> bytes:
    """
    Сопоставляет каждую вырезанную карточку с приоритетами категорий ИИ (1..9+)
    и склеивает их в 1 единый многостраничный PDF.
    """
    from pypdf import PdfReader, PdfWriter

    # Распаковываем файлы и нарезаем карточки в том же порядке, что передавался в ИИ
    flat_images = []
    for fname, content in raw_files:
        mime = detect_mime_type(content, fname)
        if mime == "application/pdf":
            extracted = extract_images_from_pdf_bytes(content)
            raw_imgs = extracted if extracted else [content]
        else:
            raw_imgs = [content]

        for raw_b in raw_imgs:
            flat_images.extend(split_and_crop_documents(raw_b))

    page_priorities = {}
    image_roles = raw_json.get("image_roles") if isinstance(raw_json, dict) else []
    for role in (image_roles or []):
        if isinstance(role, dict):
            idx = role.get("image_index")
            cat = role.get("category", "other")
            if idx is not None:
                page_priorities[idx] = get_category_priority(cat)

    processed_pages = []

    for idx, cropped_img in enumerate(flat_images):
        priority = page_priorities.get(idx, 90)
        try:
            if cropped_img:
                cropped_img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                img_buf = io.BytesIO()
                cropped_img.save(img_buf, format="PDF", quality=82)
                processed_pages.append((priority, img_buf.getvalue()))
        except Exception as e:
            logging.error(f"Error processing page {idx}: {e}")

    # Сортировка страниц строго по вашему порядку (1..9+)
    processed_pages.sort(key=lambda x: x[0])

    final_writer = PdfWriter()
    for _, page_bytes in processed_pages:
        try:
            reader = PdfReader(io.BytesIO(page_bytes))
            for page in reader.pages:
                final_writer.add_page(page)
        except Exception as e:
            logging.error(f"Error adding page to final PDF: {e}")

    out_buf = io.BytesIO()
    final_writer.write(out_buf)
    return out_buf.getvalue()

async def sort_pdf_pages(doc_file_id, raw_json) -> io.BytesIO:
    try:
        from pypdf import PdfReader, PdfWriter
        file_info = await bot.get_file(doc_file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)

        reader = PdfReader(buf)
        total_pages = len(reader.pages)

        page_priorities = {}
        image_roles = raw_json.get("image_roles") if isinstance(raw_json, dict) else []
        for role in (image_roles or []):
            if isinstance(role, dict):
                idx = role.get("image_index")
                cat = role.get("category", "other")
                if idx is not None and 0 <= idx < total_pages:
                    page_priorities[idx] = get_category_priority(cat)

        sorted_indices = sorted(range(total_pages), key=lambda i: page_priorities.get(i, 90))

        writer = PdfWriter()
        for idx in sorted_indices:
            writer.add_page(reader.pages[idx])

        out_buf = io.BytesIO()
        writer.write(out_buf)
        out_buf.seek(0)
        return out_buf
    except Exception as e:
        logging.error(f"Error sorting PDF pages: {e}")
        return None

async def create_pdf_report_with_images(route: str, date_str: str, price: str, carrier_info: str, ai_text: str, photo_ids: list) -> io.BytesIO:
    buffer = io.BytesIO()
    images = []

    for pid in photo_ids:
        try:
            file_info = await bot.get_file(pid)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            raw_bytes = buf.getvalue()

            img = Image.open(io.BytesIO(raw_bytes))
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')

            images.append(img)
        except Exception as e:
            logging.error(f"Error processing photo {pid} for PDF report: {e}")

    if images:
        images[0].save(
            buffer, 
            format="PDF", 
            save_all=True, 
            append_images=images[1:]
        )
        buffer.seek(0)

    return buffer



async def auto_promote_payment_docs_status():
    """
    Фоновый процесс: проверяет карточки со статусом 'IN_ACCOUNTING'.
    Если прошло 2 рабочих дня без ошибок -> статус меняется на 'ACCEPTED',
    и рассчитывается 'Плановая дата оплаты' (+11 рабочих дней).
    """
    while True:
        try:
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, pay_docs_submitted_at, user_id, order_number 
                FROM confirmed_deals 
                WHERE pay_docs_status = 'IN_ACCOUNTING' AND pay_docs_submitted_at != ''
            """)
            rows = cursor.fetchall()

            today_dt = datetime.now(timezone.utc).date()

            for deal_id, sub_at_str, u_id, ord_num in rows:
                try:
                    sub_dt = datetime.strptime(sub_at_str[:10], "%Y-%m-%d").date()
                    target_acc_date = add_business_days(sub_dt, 2)

                    if today_dt >= target_acc_date:
                        # Дата рассчитывается строго от даты, когда перевозчик прикрепил документы (sub_dt)
                        planned_pay = calculate_payment_date(sub_dt, 11).strftime("%d.%m.%Y")
                        cursor.execute("""
                            UPDATE confirmed_deals 
                            SET pay_docs_status = 'ACCEPTED', planned_payment_date = ? 
                            WHERE id = ?
                        """, (planned_pay, deal_id))
                        conn.commit()

                        add_notification(
                            u_id, 
                            "Документы на оплату приняты", 
                            f"Ваши документы по заявке {ord_num or deal_id} прошли проверку бухгалтерии. Плановая дата оплаты: {planned_pay}"
                        )
                except Exception as ex:
                    logging.error(f"Error promoting deal {deal_id}: {ex}")

            conn.close()
        except Exception as e:
            logging.error(f"Error in auto_promote_payment_docs_status: {e}")

        await asyncio.sleep(300)  # Проверка каждые 5 минут

    
# ==================== АВТО-ОЧИСТКА ГРУЗОВ ПО ТАЙМЕРУ МСК ====================
async def auto_clean_expired_cargos():
    while True:
        try:
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("SELECT load_id, expires_at FROM loads WHERE status = 'ACTIVE' AND expires_at IS NOT NULL AND expires_at != ''")
            rows = cursor.fetchall()
            
            msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
            expired_ids = []
            
            for load_id, expires_at in rows:
                try:
                    exp_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if msk_now >= exp_dt:
                        expired_ids.append(load_id)
                except Exception:
                    pass

            for eid in expired_ids:
                cursor.execute("UPDATE loads SET status = 'EXPIRED' WHERE load_id = ?", (eid,))
            conn.commit()
            conn.close()
            
            for eid in expired_ids:
                await update_cargo_messages_for_all_users(eid)

        except Exception as e:
            logging.error(f"Error in auto_clean_expired_cargos: {e}")
            
        await asyncio.sleep(30)

# ==================== ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ И МЕНЮ ====================


@dp.message(Command("key"))
async def cmd_activate_key_in_chat(message: types.Message):
    """Привязка ключа доступа прямо из переписки с ботом."""
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("ℹ️ Чтобы привязать ключ доступа, отправьте команду с ключом:\nНапример: `/key 7B9-K2M`", parse_mode="Markdown")
        return

    key_input = parts[1]
    success, result_msg = link_key_to_user_profile(message.from_user.id, key_input)
    if success:
        await message.answer(f"✅ {result_msg}", reply_markup=get_main_reply_markup(message.from_user))
    else:
        await message.answer(f"❌ {result_msg}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    # Проверка перехода по персональной ссылке-приглашению (Deep Link вида /start key_XXX-XXX)
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    if args and args[0].startswith("key_"):
        raw_key = args[0].replace("key_", "").strip()
        success, link_msg = link_key_to_user_profile(user_id, raw_key)
        if success:
            await message.answer(f"✅ {link_msg}")
        else:
            await message.answer(f"⚠️ {link_msg}")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        cursor.execute("""
            INSERT INTO users (user_id, company, name, phone, subscriptions, status, verification_status)
            VALUES (?, 'Не указана', ?, 'Не указан', '', 'ACTIVE', 'UNVERIFIED')
        """, (user_id, message.from_user.full_name))
        conn.commit()
    elif user[0] == 'BLOCKED':
        conn.close()
        await message.answer("Ваш аккаунт заблокирован администратором.")
        return
        
    conn.close()
    await send_welcome_message(message)

async def send_welcome_message(message: types.Message):
    u_id = message.from_user.id
    inline_builder1 = InlineKeyboardBuilder()
    inline_builder1.row(types.InlineKeyboardButton(text="📦 Биржа грузов", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp?tab=catalog&user_id={u_id}")))
    inline_builder1.row(
        types.InlineKeyboardButton(text="🚚 Мои грузы", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp?tab=my&user_id={u_id}")),
        types.InlineKeyboardButton(text="👤 Профиль", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp?tab=profile&user_id={u_id}"))
    )

    await message.answer(
        "Приветствую!\nВыберите нужный раздел в Web-App 👇",
        reply_markup=inline_builder1.as_markup()
    )

    await message.answer(
        "Главное меню:",
        reply_markup=get_chat_menu_inline_markup(message.from_user)
    )

    await message.answer(
        "Используйте кнопку ниже для доступа к меню:",
        reply_markup=get_main_reply_markup(message.from_user)
    )

@dp.message(Command("admin"))
@dp.message(F.text.lower().in_(["/admin", "admin", "админ", "/админ"]))
async def cmd_admin_private(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    builder = InlineKeyboardBuilder()
    admin_web_url = f"{RENDER_URL}/webapp?tab=admin&user_id={user_id}"
    builder.row(types.InlineKeyboardButton(text="🛠 Открыть админ-панель", web_app=WebAppInfo(url=admin_web_url)))
    await message.answer(
        "⚙️ **Панель управления администратора**\n\nНажмите кнопку ниже для перехода:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "📱 Вызвать меню")
@dp.message(F.state == None)
async def cmd_show_menu_button(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📋 **Главное меню:**", reply_markup=get_chat_menu_inline_markup(message.from_user), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_directions")
async def callback_menu_directions(callback: types.CallbackQuery):
    await show_directions_menu(callback)
    await callback.answer()

@dp.callback_query(F.data == "menu_profile")
async def callback_menu_profile(callback: types.CallbackQuery):
    await show_profile_menu(callback)
    await callback.answer()

@dp.callback_query(F.data == "menu_active")
async def callback_menu_active(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    user_subs = [s.strip() for s in (u_row[0].split(",") if u_row and u_row[0] else []) if s.strip()]

    if not user_subs:
        conn.close()
        await callback.message.answer("• У вас нет подписок на направления. Выберите направления в меню.")
        await callback.answer()
        return

    sub_conditions = []
    params = []
    for sub in user_subs:
        clean_sub = sub.split()[0].strip()
        sub_conditions.append("(destination_country LIKE ? OR route LIKE ?)")
        params.extend([f"%{clean_sub}%", f"%{clean_sub}%"])

    sql_filter = " AND (" + " OR ".join(sub_conditions) + ")"

    query = f"""
        SELECT date, route, price, COALESCE(details, ''), cars_count,
               COALESCE(car_type, ''), COALESCE(cargo_type, ''), COALESCE(weight, ''), COALESCE(admin_comment, '')
        FROM loads
        WHERE status = 'ACTIVE' {sql_filter}
        ORDER BY load_id DESC LIMIT 40
    """
    cursor.execute(query, params)
    raw_rows = cursor.fetchall()
    conn.close()

    if not raw_rows:
        await callback.message.answer("• Актуальных грузов по вашим направлениям пока нет.")
        await callback.answer()
        return

    grouped = {}
    for r_date, r_route, r_price, r_details, r_cars, r_cartype, r_cargotype, r_weight, r_comment in raw_rows:
        key = (
            (r_date or '').strip().lower(),
            (r_route or '').strip().lower(),
            (r_price or '').strip().lower(),
            (r_details or '').strip().lower()
        )
        m = re.search(r'\d+', str(r_cars))
        c_num = int(m.group(0)) if m else 1

        if key not in grouped:
            grouped[key] = {
                "date": r_date,
                "route": r_route,
                "price": r_price,
                "details": r_details,
                "cars": c_num,
                "car_type": r_cartype,
                "cargo_type": r_cargotype,
                "weight": r_weight,
                "admin_comment": r_comment
            }
        else:
            grouped[key]["cars"] += c_num

    await callback.message.answer("**СПИСОК АКТУАЛЬНЫХ ГРУЗОВ ПО ВАШИМ НАПРАВЛЕНИЯМ:**", parse_mode="Markdown")

    for item in grouped.values():
        card_text = (
            f"📍 {item['date']} | {item['route']}\n"
            f"💰 {item['price']} | 🚚 {item['cars']} авто"
        )
        cargo_info_parts = [p for p in [item['car_type'], item['cargo_type'], item['weight']] if p]
        if cargo_info_parts:
            card_text += f"\n🚛 {', '.join(cargo_info_parts)}"
        if item['details']:
            card_text += f"\n📦 {item['details']}"
        if item['admin_comment']:
            card_text += f"\n💬 Комментарий: {item['admin_comment']}"

        try:
            await callback.message.answer(card_text)
            await asyncio.sleep(0.05)
        except Exception:
            pass

    builder = InlineKeyboardBuilder()
    u_id = callback.from_user.id
    web_app_url = f"{RENDER_URL}/webapp?user_id={u_id}"
    builder.row(types.InlineKeyboardButton(text="🚀 Открыть в Web App", web_app=WebAppInfo(url=web_app_url)))
    await callback.message.answer("Для бронирования и работы с приложением:", reply_markup=builder.as_markup())

    await callback.answer()

@dp.callback_query(F.data == "menu_my_deals")
async def callback_menu_my_deals(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cd.date, cd.route, cd.price, COALESCE(cd.details, ''), SUM(cd.cars),
               COALESCE(l.car_type, ''), COALESCE(l.cargo_type, ''), COALESCE(l.weight, ''), COALESCE(l.admin_comment, '')
        FROM confirmed_deals cd
        LEFT JOIN loads l ON cd.load_id = l.load_id
        WHERE cd.user_id = ?
        GROUP BY LOWER(TRIM(cd.date)), LOWER(TRIM(cd.route)), LOWER(TRIM(cd.price)), LOWER(TRIM(COALESCE(cd.details, '')))
        ORDER BY MAX(cd.id) DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await callback.answer("У вас пока нет забранных грузов.", show_alert=True)
        return

    await callback.message.answer("**ВАШИ ЗАБРАННЫЕ ГРУЗЫ:**", parse_mode="Markdown")
    for date_str, route_str, price_str, details_text, total_cars, car_type, cargo_type, weight, admin_comment in rows:
        card_text = (
            f"📍 {date_str} | {route_str}\n"
            f"💰 {price_str} | 🚚 {total_cars} авто"
        )
        cargo_info_parts = [p for p in [car_type, cargo_type, weight] if p]
        if cargo_info_parts:
            card_text += f"\n🚛 {', '.join(cargo_info_parts)}"
        if details_text:
            card_text += f"\n📦 {details_text}"
        if admin_comment:
            card_text += f"\n💬 Комментарий: {admin_comment}"

        try:
            await callback.message.answer(card_text)
            await asyncio.sleep(0.05)
        except Exception:
            pass
            
    await callback.answer()

async def show_directions_menu(event):
    user_id = event.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    user_subs = [s.strip() for s in (row[0].split(",") if row and row[0] else []) if s.strip()]
    
    builder = InlineKeyboardBuilder()
    for direction in CHANNELS.keys():
        clean_dir = direction.split()[0].strip().lower()
        is_active = any(clean_dir in s.lower() for s in user_subs)
        mark = "✅ " if is_active else "   "
        builder.row(types.InlineKeyboardButton(
            text=f"{mark}{direction}",
            callback_data=f"toggle_dir_{direction}"
        ))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main_menu"))
    
    text = (
        "🌍 **Выбор направлений**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
        f"Ваши текущие подписки: {', '.join(user_subs) if user_subs else 'ничего не выбрано'}"
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("toggle_dir_"))
async def callback_toggle_direction(callback: types.CallbackQuery):
    direction = callback.data.replace("toggle_dir_", "")
    user_id = callback.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    current_subs = [s.strip() for s in (row[0].split(",") if row and row[0] else []) if s.strip()]
    clean_target = direction.split()[0].strip().lower()

    existing_match = None
    for s in current_subs:
        if clean_target in s.lower():
            existing_match = s
            break

    if existing_match:
        current_subs.remove(existing_match)
    else:
        current_subs.append(direction)
        
    new_subs_str = ",".join(current_subs)
    cursor.execute("UPDATE users SET subscriptions = ? WHERE user_id = ?", (new_subs_str, user_id))
    conn.commit()
    conn.close()
    
    builder = InlineKeyboardBuilder()
    for d in CHANNELS.keys():
        clean_d = d.split()[0].strip().lower()
        is_active = any(clean_d in s.lower() for s in current_subs)
        mark = "✅ " if is_active else "   "
        builder.row(types.InlineKeyboardButton(
            text=f"{mark}{d}",
            callback_data=f"toggle_dir_{d}"
        ))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main_menu"))
        
    text = (
        "🌍 **Выбор направлений**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
        f"Ваши текущие подписки: {', '.join(current_subs) if current_subs else 'ничего не выбрано'}"
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

async def show_profile_menu(event):
    user_id = event.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone, COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    comp = row[0] if row and row[0] else "Не указана"
    name = row[1] if row and row[1] else "Не указано"
    phone = row[2] if row and row[2] else "Не указан"
    v_stat = row[3] if row else "UNVERIFIED"

    stat_map = {
        'VERIFIED': 'ВЕРИФИЦИРОВАН',
        'PENDING': 'НА РАССМОТРЕНИИ',
        'UNVERIFIED': 'НЕ ВЕРИФИЦИРОВАН',
        'REJECTED': 'ОТКЛОНЕН'
    }

    text = (
        f"👤 **Личный кабинет**\n\n"
        f"Статус: **{stat_map.get(v_stat, 'НЕ ВЕРИФИЦИРОВАН')}**\n"
        f"• Компания: {comp}\n"
        f"• Имя: {name}\n"
        f"• Телефон: {phone}"
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✏️ Изменить компанию", callback_data="prof_edit_company"))
    builder.row(types.InlineKeyboardButton(text="✏️ Изменить имя", callback_data="prof_edit_name"))
    builder.row(types.InlineKeyboardButton(text="✏️ Изменить телефон", callback_data="prof_edit_phone"))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main_menu"))

    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_main_menu")
async def callback_back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📋 **Главное меню:**",
        reply_markup=get_chat_menu_inline_markup(callback.from_user),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "prof_edit_company")
async def prof_edit_company_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новое название вашей компании:")
    await state.set_state(ProfileEditStates.waiting_for_company)
    await callback.answer()

@dp.message(ProfileEditStates.waiting_for_company)
async def prof_save_company(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET company = ? WHERE user_id = ?", (message.text.strip(), user_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("• Название компании обновлено!")
    await show_profile_menu(message)

@dp.callback_query(F.data == "prof_edit_name")
async def prof_edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ваше имя:")
    await state.set_state(ProfileEditStates.waiting_for_name)
    await callback.answer()

@dp.message(ProfileEditStates.waiting_for_name)
async def prof_save_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET name = ? WHERE user_id = ?", (message.text.strip(), user_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("• Имя обновлено!")
    await show_profile_menu(message)

@dp.callback_query(F.data == "prof_edit_phone")
async def prof_edit_phone_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ваш номер телефона:")
    await state.set_state(ProfileEditStates.waiting_for_phone)
    await callback.answer()

@dp.message(ProfileEditStates.waiting_for_phone)
async def prof_save_phone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET phone = ? WHERE user_id = ?", (message.text.strip(), user_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("• Телефон обновлен!")
    await show_profile_menu(message)

# ==================== ПРЕОБРАЗОВАТЬ ДАННЫЕ (АВТОНОМНО) ====================

@dp.message(F.text == "🔄 Преобразовать данные")
@dp.callback_query(F.data == "menu_convert_standalone")
async def cmd_convert_data_start(event: types.Message | types.CallbackQuery, state: FSMContext):
    user = event.from_user
    if not is_convert_allowed(user):
        if isinstance(event, types.CallbackQuery):
            await event.answer("Доступ ограничен.", show_alert=True)
        else:
            await event.answer("Функция доступна только уполномоченным пользователям.")
        return

    await state.set_state(DocConvertStates.waiting_for_files)
    await state.update_data(photos=[], documents=[], text_notes="")

    prompt = (
        "🔄 **Преобразование данных (автономно)**\n\n"
        "Присылайте фото документов, PDF-файлы или текстовые заметки.\n"
        "Когда все файлы будут загружены, нажмите **«✅ Завершить и отправить»**.\n"
        "ИИ распознает данные, отсканирует и отсортирует страницы, сгенерирует 1 готовый PDF и отправит в канал логистов."
    )
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="✅ Завершить и отправить"))
    builder.add(types.KeyboardButton(text="❌ Отмена"))
    builder.adjust(1, 1)

    if isinstance(event, types.CallbackQuery):
        await event.message.answer(prompt, reply_markup=builder.as_markup(resize_keyboard=True), parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(prompt, reply_markup=builder.as_markup(resize_keyboard=True), parse_mode="Markdown")

@dp.message(DocConvertStates.waiting_for_files, F.text == "❌ Отмена")
async def handle_convert_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Преобразование отменено.", reply_markup=get_main_reply_markup(message.from_user))

@dp.message(DocConvertStates.waiting_for_files, F.photo)
async def handle_convert_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    
    notes = data.get("text_notes", "")
    if message.caption:
        notes = (notes + "\n" + message.caption).strip()

    await state.update_data(photos=photos, text_notes=notes)

@dp.message(DocConvertStates.waiting_for_files, F.document)
async def handle_convert_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    documents = data.get("documents", [])
    documents.append(message.document.file_id)

    notes = data.get("text_notes", "")
    if message.caption:
        notes = (notes + "\n" + message.caption).strip()

    await state.update_data(documents=documents, text_notes=notes)

@dp.message(DocConvertStates.waiting_for_files, F.text == "✅ Завершить и отправить")
async def handle_convert_finish(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    photos = data.get("photos", [])
    documents = data.get("documents", [])
    notes = data.get("text_notes", "")

    if not photos and not documents and not notes:
        await message.answer("Вы не прислали ни одного фото/файла или заметки.")
        return

    status_msg = await message.answer("⏳ ИИ распознает документы и формирует файлы...")

    try:
        ai_formatted_data, all_file_ids, raw_json = await process_docs_with_ai(photos, documents, notes, is_polyethylene=False, route_str="")

        raw_json = raw_json if isinstance(raw_json, dict) else {}
        d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
        t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
        tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
        p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}

        p_full_name = (p_data.get("full_name") or "").strip()
        if p_data.get("number") in ["Не распознан", None, ""] or not p_full_name:
            driver_name = "ВОДИТЕЛЬ"
        else:
            driver_name = p_full_name.upper()

        truck_plate = (t_data.get("plate") or "Тягач").strip().upper()
        trailer_plate = (tr_data.get("plate") or "Прицеп").strip().upper()

        clean_name = re.sub(r'[^\w\s-]', '', driver_name)
        clean_truck = re.sub(r'[^\w]', '', truck_plate)
        clean_trailer = re.sub(r'[^\w]', '', trailer_plate)

        pdf_filename = f"{clean_name} - {clean_truck}_{clean_trailer}.pdf"
        user_info = format_carrier_info(message.from_user.id, message.from_user.username, message.from_user.full_name)

        admin_msg = (
            f"📄 **ПРЕОБРАЗОВАННЫЕ ДАННЫЕ ДОКУМЕНТОВ**\n\n"
            f"{user_info}\n\n"
            f"{ai_formatted_data}"
        )

        deal_id = None
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cursor.fetchone()
        if row:
            deal_id = row[0]
        conn.close()

        reply_kb = build_kaiten_admin_keyboard(deal_id) if deal_id else None

        try:
            await bot.send_message(
                chat_id=DOCS_CHANNEL_ID, 
                text=admin_msg, 
                reply_markup=reply_kb,
                parse_mode="Markdown"
            )
        except Exception:
            await bot.send_message(
                chat_id=DOCS_CHANNEL_ID, 
                text=admin_msg.replace('*', ''), 
                reply_markup=reply_kb
            )

        raw_files = []
        for fid in (documents or []):
            try:
                f_info = await bot.get_file(fid)
                buf_f = io.BytesIO()
                await bot.download_file(f_info.file_path, destination=buf_f)
                raw_files.append((f_info.file_path or "doc.pdf", buf_f.getvalue()))
            except Exception: pass
        for fid in (photos or []):
            try:
                f_info = await bot.get_file(fid)
                buf_f = io.BytesIO()
                await bot.download_file(f_info.file_path, destination=buf_f)
                raw_files.append((f_info.file_path or "photo.jpg", buf_f.getvalue()))
            except Exception: pass

        if raw_files:
            pdf_bytes = await generate_single_pdf_bytes(raw_files, "Автономное преобразование", "Сегодня", "-", user_info, ai_formatted_data)
            if pdf_bytes:
                clean_name = re.sub(r'[^\w\s-]', '', driver_name).strip() or "ВОДИТЕЛЬ"
                clean_truck = re.sub(r'[^\w]', '', truck_plate).strip()
                clean_trailer = re.sub(r'[^\w]', '', trailer_plate).strip()

                if clean_trailer and clean_trailer.lower() not in ["нераспознан", "неуказан"]:
                    plates_str = f"{clean_truck}_{clean_trailer}"
                else:
                    plates_str = clean_truck

                pdf_filename = f"{clean_name} - {plates_str}.pdf"

                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption="Преобразованный документ", request_timeout=300)

        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.answer("✅ Данные успешно преобразованы и отправлены!", reply_markup=get_main_reply_markup(message.from_user))

    except Exception as e:
        logging.error(f"Error in handle_convert_finish: {e}", exc_info=True)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(f"❌ Произошла ошибка при обработке: {e}", reply_markup=get_main_reply_markup(message.from_user))

    await state.clear()

@dp.message(DocConvertStates.waiting_for_files, F.text)
async def handle_convert_text_notes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    old_notes = data.get("text_notes", "")
    new_notes = (old_notes + "\n" + message.text).strip()
    await state.update_data(text_notes=new_notes)

# ==================== ХЭНДЛЕРЫ ПОДАЧИ / ЗАМЕНЫ ДОКУМЕНТОВ В ЧАТЕ ====================

@dp.message(DocUploadStates.waiting_for_docs, F.photo)
async def handle_doc_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    
    # Считываем номера/текст из подписи к фото
    notes = data.get("text_notes", "")
    if message.caption:
        notes = (notes + "\n" + message.caption).strip()
        
    await state.update_data(photos=photos, text_notes=notes)

@dp.message(DocUploadStates.waiting_for_docs, F.document)
async def handle_doc_document(message: types.Message, state: FSMContext):
    data = await state.get_data()
    documents = data.get("documents", [])
    documents.append(message.document.file_id)

    # Считываем номера/текст из подписи к документу
    notes = data.get("text_notes", "")
    if message.caption:
        notes = (notes + "\n" + message.caption).strip()

    await state.update_data(documents=documents, text_notes=notes)

@dp.message(DocUploadStates.waiting_for_docs, F.text == "❌ Отмена")
async def handle_doc_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Подача данных отменена.", reply_markup=get_main_reply_markup(message.from_user))

@dp.message(DocUploadStates.waiting_for_docs, F.text == "✅ Отправить данные логисту")
async def handle_doc_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    user_obj = message.from_user

    photos = data.get("photos", [])
    documents = data.get("documents", [])
    notes = data.get("text_notes", "")
    route_str = data.get("upload_route", "Маршрут")
    date_str = data.get("upload_date", "Дата")
    price_str = data.get("upload_price", "Ставка")
    deal_id = data.get("upload_deal_id")

    if not photos and not documents and not notes:
        await message.answer("Вы не прислали ни одного фото или файла.")
        return

    is_polyethylene = False
    was_previously_submitted = False
    prev_truck_plate = ""
    prev_trailer_plate = ""
    prev_driver_short_name = ""
    prev_driver_phone = ""
    prev_docs_status = "NONE"
    prev_missing_docs = ""

    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cd.docs_submitted, cd.last_truck_plate, cd.last_driver_name,
                   l.cargo_type, l.details, l.text, cd.last_trailer_plate, cd.driver_phone,
                   cd.docs_status, cd.missing_docs
            FROM confirmed_deals cd 
            LEFT JOIN loads l ON cd.load_id = l.load_id 
            WHERE cd.id = ?
        """, (deal_id,))
        row = cursor.fetchone()
        if row:
            was_previously_submitted = bool(row[0])
            prev_truck_plate = row[1] or ""
            prev_driver_short_name = row[2] or ""
            cargo_info = f"{row[3] or ''} {row[4] or ''} {row[5] or ''}".lower()
            if "полиэтилен" in cargo_info or "polyethylene" in cargo_info:
                is_polyethylene = True
            prev_trailer_plate = row[6] or ""
            prev_driver_phone = row[7] or ""
            prev_docs_status = row[8] or "NONE"
            prev_missing_docs = row[9] or ""
        conn.close()

    ai_formatted_data, sorted_files, raw_json = await process_docs_with_ai(photos, documents, notes, is_polyethylene=is_polyethylene, route_str=route_str)

    raw_json = raw_json if isinstance(raw_json, dict) else {}
    t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
    tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
    d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
    p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}
    l_data = d_data.get("license") if isinstance(d_data.get("license"), dict) else {}

    # Замена / слияние данных: если в новом файле элемент не распознан, сохраняем старый
    new_truck_plate = (t_data.get("plate") or "Не распознан").strip().upper()
    if new_truck_plate in ["НЕ РАСПОЗНАН", "", "—"] and prev_truck_plate:
        new_truck_plate = prev_truck_plate

    new_trailer_plate = (tr_data.get("plate") or "Не распознан").strip().upper()
    if new_trailer_plate in ["НЕ РАСПОЗНАН", "", "—"] and prev_trailer_plate:
        new_trailer_plate = prev_trailer_plate

    p_full_name = (p_data.get("full_name") or "").strip()
    if p_data.get("number") in ["Не распознан", None, ""] or not p_full_name:
        new_driver_short_name = prev_driver_short_name if prev_driver_short_name and prev_driver_short_name != "Не распознан" else "Не распознан"
    else:
        new_driver_short_name = extract_surname_and_name(p_full_name)

    extracted_phone = (d_data.get("phones") or notes or "").strip()
    phone_source = extracted_phone if (extracted_phone and extracted_phone not in ["Не указан", ""]) else prev_driver_phone
    new_driver_phone = normalize_phones(phone_source)

    prev_missing_list = [x.strip() for x in prev_missing_docs.split(',') if x.strip()]
    missing_items = []

    # Проверка паспорта: если в новой загрузке нет файла паспорта, но ранее паспорт был сдан — сохраняем его как правильный
    p_num = p_data.get("number")
    p_exp = p_data.get("expiry_date")
    if p_num and p_num not in ["Не распознан", ""]:
        if is_doc_expired_or_expiring_soon(p_exp, threshold_days=20):
            missing_items.append("Паспорт водителя просрочен или истекает (менее 20 дней) — необходимо прикрепить актуальный документ")
    else:
        is_passport_missing_prev = any("Паспорт водителя" in item for item in prev_missing_list)
        if not was_previously_submitted or is_passport_missing_prev or new_driver_short_name in ["Не распознан", ""]:
            missing_items.append("Паспорт водителя")

    # Проверка прав: аналогично сохраняем ранее сданные права
    l_num = l_data.get("number")
    l_exp = l_data.get("expiry_date")
    if l_num and l_num not in ["Не распознан", ""]:
        if is_doc_expired_or_expiring_soon(l_exp, threshold_days=20):
            missing_items.append("Водительское удостоверение просрочено или истекает (менее 20 дней) — необходимо прикрепить актуальный документ")
    else:
        is_license_missing_prev = any("Водительское удостоверение" in item for item in prev_missing_list)
        if not was_previously_submitted or is_license_missing_prev:
            missing_items.append("Водительское удостоверение")

    if new_truck_plate in ["НЕ РАСПОЗНАН", "", "—"]:
        missing_items.append("Техпаспорт тягача")
    if new_trailer_plate in ["НЕ РАСПОЗНАН", "", "—"]:
        missing_items.append("Техпаспорт прицепа")
    if new_driver_phone in ["Не указан", "", "—"]:
        missing_items.append("Номер телефона")

    docs_status = "FULL" if not missing_items else "PARTIAL"
    missing_docs_str = ", ".join(missing_items)

    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE confirmed_deals 
            SET docs_submitted = 1, docs_status = ?, missing_docs = ?, 
                last_truck_plate = ?, last_trailer_plate = ?, last_driver_name = ?, driver_phone = ?,
                full_doc_text = ?
            WHERE id = ? AND user_id = ?
        """, (docs_status, missing_docs_str, new_truck_plate, new_trailer_plate, new_driver_short_name, new_driver_phone, ai_formatted_data, deal_id, user_id))
        conn.commit()
        conn.close()

    carrier_text = format_carrier_info(user_id, user_obj.username, user_obj.full_name)

    if was_previously_submitted:
        header_title = "🔄 ЗАМЕНА ДАННЫХ ПО ГРУЗУ"
        changes_summary = (
            f"\n🔄 **Детали обновления:**\n"
            f"- **Номер авто:** было `{prev_truck_plate or '—'}` ➔ стало `{new_truck_plate}`\n"
            f"- **Водитель:** было `{prev_driver_short_name or '—'}` ➔ стало `{new_driver_short_name}`\n"
        )
    else:
        header_title = "📄 ПОДАЧА ДАННЫХ ПО ГРУЗУ"
        changes_summary = ""

    admin_msg = (
        f"**{header_title}**\n\n"
        f"📅 {date_str} | 📍 {route_str}\n"
        f"💰 {price_str}\n\n"
        f"{carrier_text}\n"
        f"{changes_summary}\n"
        f"{ai_formatted_data}"
    )

    clean_name = re.sub(r'[^\w\s-]', '', new_driver_short_name)
    clean_truck = re.sub(r'[^\w]', '', new_truck_plate)
    pdf_filename = f"{clean_name} - {clean_truck}.pdf"
    file_caption = f"{date_str} {route_str}"

    # Если deal_id не был передан — находим последнюю активную сделку пользователя
    if not deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cursor.fetchone()
        if row:
            deal_id = row[0]
        conn.close()

    # Формируем клавиатуру с кнопками для Kaiten
    kaiten_builder = InlineKeyboardBuilder()
    if deal_id:
        kaiten_builder.row(
            types.InlineKeyboardButton(text="📥 Подать данные в Kaiten", callback_data=f"kaiten_push_{deal_id}"),
            types.InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"kaiten_skip_{deal_id}")
        )

    try:
        await bot.send_message(
            chat_id=DOCS_CHANNEL_ID, 
            text=admin_msg, 
            reply_markup=kaiten_builder.as_markup() if deal_id else None,
            parse_mode="Markdown"
        )

        # Скачиваем абсолютно все файлы для склейки в 1 общий PDF
        raw_files = []
        for fid in (documents or []):
            try:
                f_info = await bot.get_file(fid)
                buf_f = io.BytesIO()
                await bot.download_file(f_info.file_path, destination=buf_f)
                raw_files.append((f_info.file_path or "doc.pdf", buf_f.getvalue()))
            except Exception as e:
                logging.error(f"Download doc error: {e}")
        for fid in (photos or []):
            try:
                f_info = await bot.get_file(fid)
                buf_f = io.BytesIO()
                await bot.download_file(f_info.file_path, destination=buf_f)
                raw_files.append((f_info.file_path or "photo.jpg", buf_f.getvalue()))
            except Exception as e:
                logging.error(f"Download photo error: {e}")

        if raw_files:
            pdf_bytes = await generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, carrier_text, ai_formatted_data)
            if pdf_bytes:
                clean_name = re.sub(r'[^\w\s-]', '', new_driver_short_name).strip() or "ВОДИТЕЛЬ"
                clean_truck = re.sub(r'[^\w]', '', new_truck_plate).strip()
                clean_trailer = re.sub(r'[^\w]', '', new_trailer_plate).strip()

                if clean_trailer and clean_trailer.lower() not in ["нераспознан", "неуказан"]:
                    plates_str = f"{clean_truck}_{clean_trailer}"
                else:
                    plates_str = clean_truck

                pdf_filename = f"{clean_name} - {plates_str}.pdf"
                file_caption = f"{date_str} {route_str}"

                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption=file_caption)
                
    except Exception as e:
        logging.error(f"Error forwarding docs to admin channel: {e}", exc_info=True)

    await state.clear()
    
    if docs_status == "PARTIAL":
        feedback_msg = f"⚠️ Данные переданы, но не полностью. Не хватает:\n• " + "\n• ".join(missing_items)
    else:
        feedback_msg = "• Все данные успешно переданы логисту!"

    await message.answer(feedback_msg, reply_markup=get_main_reply_markup(message.from_user))

@dp.message(DocUploadStates.waiting_for_docs, F.text)
async def handle_doc_text_notes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    old_notes = data.get("text_notes", "")
    new_notes = (old_notes + "\n" + message.text).strip()
    await state.update_data(text_notes=new_notes)

def detect_country(text: str) -> str:
    t = text.lower()
    if 'казахстан' in t or any(c in t for c in ['алматы', 'астана', 'шымкент', 'караганда', 'актау', 'атырау']):
        return "Казахстан 🇰🇿"
    elif 'узбекистан' in t or any(c in t for c in ['ташкент', 'самарканд', 'бухара', 'навои', 'джизак', 'фергана']):
        return "Узбекистан 🇺🇿"
    elif 'кыргызстан' in t or 'киргизия' in t or 'бишкек' in t or 'ош' in t:
        return "Кыргызстан 🇰🇬"
    elif 'грузия' in t or 'тбилиси' in t or 'поти' in t or 'батуми' in t:
        return "Грузия 🇬🇪"
    elif 'азербайджан' in t or 'баку' in t or 'сумгаит' in t:
        return "Азербайджан 🇦🇿"
    elif 'армения' in t or 'ереван' in t:
        return "Армения 🇦🇲"
    return "Все"

# ==================== ОБРАБОТЧИК ОТВЕТОВ АДМИНА ЧЕРЕЗ PENDING_COUNTERS ====================

async def process_admin_pending_action(chat_id: int, message_text: str) -> bool:
    # 1. Проверяем, не является ли сообщение ручной фиксацией ошибки по номеру заявки (например: "2606060373-000001 Неверная дата акта")
    if message_text and any(char.isdigit() for char in message_text):
        m_ord = re.search(r'(\d{8,12}(?:-\d+)?)\s+(.+)', message_text.strip())
        if m_ord:
            ord_num_query = m_ord.group(1).strip()
            err_text = m_ord.group(2).strip()

            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id FROM confirmed_deals 
                WHERE order_number LIKE ? OR order_number = ?
            """, (f"%{ord_num_query}%", ord_num_query))
            deal_row = cursor.fetchone()

            if deal_row:
                d_id, u_id = deal_row
                cursor.execute("""
                    UPDATE confirmed_deals 
                    SET pay_docs_status = 'AI_ERROR', pay_docs_error = ? 
                    WHERE id = ?
                """, (err_text, d_id))
                conn.commit()
                conn.close()

                add_notification(u_id, "Замечание по документам на оплату", f"По заявке {ord_num_query} поступило замечание: {err_text}")
                await bot.send_message(chat_id=chat_id, text=f"✅ Замечание зафиксировано по заявке {ord_num_query}: `{err_text}`")
                return True
            conn.close()

    # 2. Если это не замечание по ошибке, проверяем действия по кнопкам (COUNTER, PARTIAL, KAITEN_BIND и т.д.)
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT bid_id, COALESCE(action_type, 'COUNTER') FROM pending_counters WHERE admin_chat_id = ?", (chat_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return False

    target_id, action_type = row[0], row[1]
    cursor.execute("DELETE FROM pending_counters WHERE admin_chat_id = ?", (chat_id,))
    conn.commit()

    if action_type == 'COUNTER':
        input_text = message_text.strip()
        cars_count = None
        rate_raw = input_text

        if '|' in input_text:
            parts = input_text.split('|', 1)
            rate_raw = parts[0].strip()
            m_cars = re.search(r'\d+', parts[1])
            if m_cars:
                cars_count = int(m_cars.group(0))
        elif '/' in input_text and not input_text.startswith('/'):
            parts = input_text.split('/', 1)
            rate_raw = parts[0].strip()
            m_cars = re.search(r'\d+', parts[1])
            if m_cars:
                cars_count = int(m_cars.group(0))

        counter_rate = format_custom_rate(rate_raw)

        cursor.execute("SELECT load_id, user_id, rate, cars FROM bids WHERE bid_id = ?", (target_id,))
        bid = cursor.fetchone()
        if bid:
            load_id, user_id, orig_rate, orig_cars = bid
            if cars_count is None or cars_count <= 0:
                cars_count = orig_cars

            cursor.execute("SELECT route FROM loads WHERE load_id = ?", (load_id,))
            l_row = cursor.fetchone()
            route_str = l_row[0] if l_row else "Груз"

            cursor.execute("UPDATE bids SET status = 'COUNTER', counter_rate = ?, cars = ? WHERE bid_id = ?", (counter_rate, cars_count, target_id))
            conn.commit()

            add_notification(user_id, "Встречное предложение", f"Логист предложил вам ставку {counter_rate} на {cars_count} авто по грузу {route_str}.")

            counter_builder = InlineKeyboardBuilder()
            counter_builder.row(
                types.InlineKeyboardButton(text="Принять встречную", callback_data=f"accept_counter_{target_id}"),
                types.InlineKeyboardButton(text="Отклонить", callback_data=f"decline_counter_{target_id}")
            )
            try:
                await bot.send_message(
                    chat_id=user_id, 
                    text=f"Логист предложил встречную ставку **{counter_rate}** ({cars_count} авто) по грузу **{route_str}** (ваша ставка: {orig_rate}).\n\nПринимаете предложение?",
                    reply_markup=counter_builder.as_markup(),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            await bot.send_message(chat_id=chat_id, text=f"• Встречная ставка {counter_rate} ({cars_count} авто) отправлена перевозчику!")

    elif action_type == 'PARTIAL':
        m = re.search(r'\d+', message_text.strip())
        confirm_cars = int(m.group(0)) if m else 1

        cursor.execute("SELECT load_id, user_id, cars, rate FROM bids WHERE bid_id = ?", (target_id,))
        bid = cursor.fetchone()
        if bid:
            load_id, user_id, requested_cars, rate = bid
            if confirm_cars > requested_cars:
                confirm_cars = requested_cars

            cursor.execute("SELECT route, date, details, cars_count FROM loads WHERE load_id = ?", (load_id,))
            load = cursor.fetchone()
            if load:
                route_str, date_str, details_text, cars_count_str = load
                curr_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if re.search(r'\d+', str(cars_count_str)) else 1

                if curr_cars > confirm_cars:
                    cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(curr_cars - confirm_cars), load_id))
                else:
                    cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

                for _ in range(confirm_cars):
                    cursor.execute("""
                        INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                    """, (load_id, user_id, date_str, route_str, rate, details_text))

                cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (target_id,))
                conn.commit()

                add_notification(user_id, "Частичное подтверждение", f"Логист подтвердил {confirm_cars} авто по ставке {rate} ({route_str}).")
                try:
                    await bot.send_message(chat_id=user_id, text=f"• Логист подтвердил **{confirm_cars} авто** по ставке **{rate}** ({route_str}).")
                except Exception:
                    pass

                await update_cargo_messages_for_all_users(load_id)
                await bot.send_message(chat_id=chat_id, text=f"• Подтверждено {confirm_cars} авто по ставке {rate}!")

    elif action_type == 'VERIFY_EDIT':
        raw_del = '|' if '|' in message_text else ('/' if '/' in message_text else '|')
        parts = [p.strip() for p in message_text.split(raw_del)]
        if len(parts) >= 3:
            comp, name, phone = parts[0], parts[1], parts[2]
            cursor.execute("""
                UPDATE users SET 
                    company = ?, name = ?, phone = ?,
                    pending_company = '', pending_name = '', pending_phone = ''
                WHERE user_id = ?
            """, (comp, name, phone, target_id))
            conn.commit()

            add_notification(target_id, "Профиль скорректирован", f"Данные профиля обновлены логистом:\n{comp} | {name} | {phone}")

            carrier_link = format_carrier_info(target_id)
            builder = InlineKeyboardBuilder()
            builder.row(
                types.InlineKeyboardButton(text="Подтвердить", callback_data=f"verify_approve_{target_id}"),
                types.InlineKeyboardButton(text="Отклонить", callback_data=f"verify_reject_{target_id}")
            )
            builder.row(
                types.InlineKeyboardButton(text="Отредактировать", callback_data=f"verify_edit_{target_id}")
            )

            msg_text = (
                f"**ЗАПРОС НА ВЕРИФИКАЦИЮ (ОТРЕДАКТИРОВАНО)**\n\n"
                f"{carrier_link}\n"
                f"• Компания: {comp}\n"
                f"• ФИО: {name}\n"
                f"• Телефон: {phone}"
            )
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=msg_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            await bot.send_message(chat_id=chat_id, text="• Данные профиля успешно обновлены у пользователя!")
        else:
            await bot.send_message(chat_id=chat_id, text="⚠️ Ошибка формата! Используйте разделитель `|` или `/`: `Компания | ФИО | Телефон`")

    elif action_type == 'KAITEN_BIND':
        # Точное извлечение ID карточки из ссылки вида .../card/1341041 или чистого ID
        card_url_match = re.search(r'card/(\d+)', message_text)
        if card_url_match:
            custom_card_id = int(card_url_match.group(1))
        else:
            digits_match = re.search(r'\b\d{5,8}\b', message_text)
            if digits_match:
                custom_card_id = int(digits_match.group(0))
            else:
                any_digits = re.search(r'\d+', message_text)
                custom_card_id = int(any_digits.group(0)) if any_digits else None

        if not custom_card_id:
            conn.close()
            await bot.send_message(chat_id=chat_id, text="⚠️ Ошибка! Не удалось распознать ID карточки Kaiten в сообщении.")
            return True

        cursor.execute("UPDATE confirmed_deals SET kaiten_card_id = ? WHERE id = ?", (custom_card_id, target_id))
        conn.commit()
        conn.close()

        success, result = await push_data_to_kaiten(target_id, 0, admin_user_name="Логист")

        if success and isinstance(result, dict):
            card_url = result.get("url")
            msg_out = f"✅ **Данные успешно внесены в карточку Kaiten:** [{custom_card_id}]({card_url})"
        else:
            msg_out = f"⚠️ Карточка `{custom_card_id}` привязана к сделке #{target_id}, но возникли замечания при заполнении: {result}"

        await bot.send_message(chat_id=chat_id, text=msg_out, parse_mode="Markdown")
        return True

    conn.close()
    return True

# ==================== ПАРСИНГ ИЗ КАНАЛОВ И АДМИН-КАНАЛА ====================

LISTENED_CHATS = []
if ADMIN_CHANNEL_ID:
    LISTENED_CHATS.append(ADMIN_CHANNEL_ID)
if CARGO_INPUT_CHANNEL_ID:
    LISTENED_CHATS.append(CARGO_INPUT_CHANNEL_ID)


@dp.channel_post(F.chat.id == ADMIN_CHANNEL_ID, F.text.func(lambda t: bool(t) and t.strip().lower().startswith(('/test_backup', 'test_backup'))))
@dp.message(F.chat.id == ADMIN_CHANNEL_ID, Command("test_backup"))
async def handle_admin_test_backup(message: types.Message):
    """
    Запуск теста выгрузки базы ИСКЛЮЧИТЕЛЬНО из админ-канала.
    В личных сообщениях бот на эту команду больше не реагирует!
    """
    status_msg = await message.reply(
        f"⏳ Пробую выгрузить базу в канал <code>{BACKUP_CHANNEL_ID}</code>...", 
        parse_mode="HTML"
    )
    
    success, result_text = await push_db_backup(reason="Ручной тест из Админ-канала")
    safe_result = html.escape(str(result_text))

    if success:
        await status_msg.edit_text(
            f"✅ <b>Бэкап успешно отправлен!</b>\n\n"
            f"• Канал бэкапов: <code>{BACKUP_CHANNEL_ID}</code>\n"
            f"• Результат: <code>{safe_result}</code>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Ошибка отправки бэкапа!</b>\n\n"
            f"• Канал бэкапов: <code>{BACKUP_CHANNEL_ID}</code>\n"
            f"• Причина ошибки:\n<code>{safe_result}</code>",
            parse_mode="HTML"
        )



@dp.channel_post(F.chat.id == ADMIN_CHANNEL_ID, F.text.func(lambda t: bool(t) and t.strip().lower().startswith(('/help', 'help', '/помощь', 'помощь', '/команды'))))
async def handle_admin_help_command(message: types.Message):
    """Справка по командам, работающая только в админ-канале."""
    help_text = (
        "📖 **Шпаргалка команд Админ-канала:**\n\n"
        "• **`/menu`** (или **`меню`**) — Кнопка для входа в веб-панель управления (Биржа, Все заказы, Перевозчики, Ключи)\n\n"
        "• **`!Текст сообщения`** — Моментальная рассылка важного сообщения ВСЕМ перевозчикам бота\n"
        "  *(Пример: `!Внимание! Завтра погрузки с 8:00!`)*\n\n"
        "• **`/test_backup`** — Проверка создания бэкапа базы данных с выгрузкой в канал бэкапов\n\n"
        "• **`/help`** (или **`помощь`**) — Показать эту справку\n\n"
        "📝 **Публикация грузов:**\n"
        "Просто отправьте текст заявки в канал — бот через ИИ сам определит маршрут, даты, ставку и опубликует груз на бирже."
    )
    try:
        await message.reply(help_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка отправки help в админ-канал: {e}")

@dp.channel_post(F.text.func(lambda t: bool(t) and t.strip().lower().startswith(('/admin', 'admin', '/меню', '/menu', 'меню'))))
async def handle_admin_menu_command(message: types.Message):
    if message.chat.id != ADMIN_CHANNEL_ID:
        return

    menu_text = (
        "⚙️ **Панель управления Админ-канала:**\n\n"
        "• Нажмите кнопку ниже, чтобы открыть панель прямо внутри Telegram.\n"
        "• Рассылка всем перевозчикам: `!Текст сообщения`"
    )

    # Получаем юзернейм бота для открытия Mini App прямо внутри Telegram
    bot_info = await bot.get_me()
    bot_username = bot_info.username or ""

    builder = InlineKeyboardBuilder()
    if bot_username:
        # Ссылка через t.me/?startapp открывает Web App внутри Telegram без браузера
        tg_internal_url = f"https://t.me/{bot_username}?startapp=admin"
        builder.row(types.InlineKeyboardButton(text="🛠 Открыть админ-панель", url=tg_internal_url))
    else:
        web_app_url = f"{RENDER_URL}/webapp?tab=admin"
        builder.row(types.InlineKeyboardButton(text="🛠 Открыть админ-панель", url=web_app_url))

    try:
        await message.reply(menu_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка отправки меню в админ-канал: {e}")

@dp.channel_post(F.text.startswith("!"))
async def handle_admin_broadcast(message: types.Message):
    if message.chat.id != ADMIN_CHANNEL_ID:
        return
        
    broadcast_text = message.text.strip('!').strip()
    if not broadcast_text:
        return

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE status = 'ACTIVE'")
    users = cursor.fetchall()
    conn.close()

    count = 0
    for (u_id,) in users:
        try:
            await bot.send_message(chat_id=u_id, text=f"📢 **ВАЖНОЕ СООБЩЕНИЕ:**\n\n{broadcast_text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=f"✅ Рассылка завершена! Сообщение доставлено {count} пользователям.")

@dp.channel_post(F.chat.id.in_(LISTENED_CHATS))
async def handle_channel_post(message: types.Message):
    chat_id = message.chat.id
    raw_text = message.text or message.caption or ""

    if chat_id in [ADMIN_CHANNEL_ID, DOCS_CHANNEL_ID] and raw_text:
        is_handled = await process_admin_pending_action(chat_id, raw_text)
        if is_handled:
            return

    if not raw_text or raw_text.startswith("!") or raw_text.startswith("/"):
        return

    is_unified_channel = (CARGO_INPUT_CHANNEL_ID and chat_id == CARGO_INPUT_CHANNEL_ID)

    # 1. Распознавание через ИИ
    ai_cargos = await parse_cargos_with_ai(raw_text)

    # Если ИИ временно недоступен — используем резервный старый метод
    if not ai_cargos:
        splitted_texts = parse_multiple_cargos(raw_text)
        direction_fallback = CHANNEL_TO_DIRECTION.get(chat_id) or detect_country(raw_text)
        ai_cargos = []
        for st in splitted_texts:
            d_date, d_route, d_price, d_cars, d_details, d_cartype, d_cargotype, d_weight, d_expires = parse_cargo_raw(st)
            ai_cargos.append({
                "destination_country": direction_fallback,
                "date": d_date,
                "route": d_route,
                "cars_count": d_cars,
                "price": d_price,
                "car_type": d_cartype,
                "cargo_type": d_cargotype,
                "weight": d_weight,
                "details": d_details,
                "time_limit": ""
            })

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, subscriptions, status FROM users WHERE status != 'BLOCKED'")
    active_users = cursor.fetchall()

    created_info = []

    for item in ai_cargos:
        dest_country = item.get("destination_country") or "Все"
        # Если пост пришел из старого конкретного канала — берем точное направление канала
        if not is_unified_channel and chat_id in CHANNEL_TO_DIRECTION:
            dest_country = CHANNEL_TO_DIRECTION[chat_id]

        c_date = item.get("date") or "Срочно"
        c_route = item.get("route") or "Маршрут не указан"
        c_cars = item.get("cars_count") or "1"
        c_price = item.get("price") or "Торги"
        c_cartype = item.get("car_type") or "Тент/реф"
        c_cargotype = item.get("cargo_type") or "ТНП"
        c_weight = item.get("weight") or "до 22т"
        c_details = item.get("details") or ""
        t_limit = item.get("time_limit") or ""

        expires_at = None
        if t_limit:
            time_formatted, expires_at = extract_time_limit(t_limit)
            if time_formatted and "по МСК" not in c_price:
                c_price = f"{c_price} (до {time_formatted} по МСК)"

        cursor.execute("""
            INSERT INTO loads (destination_country, date, route, cars_count, price, text, details, car_type, cargo_type, weight, expires_at, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """, (dest_country, c_date, c_route, c_cars, c_price, raw_text, c_details, c_cartype, c_cargotype, c_weight, expires_at))
        
        new_cargo_id = cursor.lastrowid
        created_info.append(f"• {c_route} ({dest_country})")

        clean_dir = dest_country.split()[0].strip().lower()

        # 1. АВТОПУБЛИКАЦИЯ В ТЕМАТИЧЕСКИЙ КАНАЛ НАПРАВЛЕНИЯ
        target_channel_id = None
        for chan_name, chan_id in CHANNELS.items():
            if clean_dir in chan_name.lower():
                target_channel_id = chan_id
                break

        # Отправляем в канал направления, только если пост пришел из общего канала
        if target_channel_id and target_channel_id != chat_id:
            try:
                bot_info = await bot.get_me()
                bot_username = bot_info.username or ""

                channel_card = (
                    f"📍 **{c_date} | {c_route}**\n"
                    f"💰 **{c_price}** | 🚚 {c_cars} авто\n"
                    f"🚛 {c_cartype} | {c_cargotype} | {c_weight}"
                )
                if c_details:
                    channel_card += f"\n📦 {c_details}"

                chan_builder = InlineKeyboardBuilder()
                if bot_username:
                    chan_builder.row(types.InlineKeyboardButton(
                        text="📱 Открыть в приложении / Забрать",
                        url=f"https://t.me/{bot_username}?start=load_{new_cargo_id}"
                    ))

                await bot.send_message(
                    chat_id=target_channel_id,
                    text=channel_card,
                    reply_markup=chan_builder.as_markup() if bot_username else None,
                    parse_mode="Markdown"
                )
            except Exception as chan_err:
                logging.error(f"Ошибка публикации в региональный канал ({dest_country}): {chan_err}")

        # 2. Личная рассылка подписанным пользователям в боте
        for u_id, subs, u_status in active_users:
            user_subs_list = [s.strip().lower() for s in (subs or "").split(",") if s.strip()]
            if any(clean_dir in sub for sub in user_subs_list):
                await send_cargo_to_user(u_id, new_cargo_id)
                add_notification(
                    u_id, 
                    "Новый груз", 
                    f"Появился груз на {c_date} по маршруту {c_route}"
                )
                await asyncio.sleep(0.04)

    conn.commit()
    conn.close()

    # Если публикация была в единый канал — бот оставит лаконичный ответ-подтверждение
    if is_unified_channel and created_info:
        try:
            report_text = f"✅ **Грузы распознаны и опубликованы на Бирже ({len(created_info)} шт.):**\n" + "\n".join(created_info)
            await message.reply(report_text, parse_mode="Markdown")
        except Exception:
            pass

# ==================== ЛОГИКА ОБРАБОТКИ ЗАЯВОК И СТАВОК В ЧАТЕ И АДМИНКЕ ====================

@dp.callback_query(F.data.startswith("accept_bid_"))
async def handle_accept_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("accept_bid_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()

    if not bid:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)
        return

    load_id, user_id, requested_cars, rate = bid
    cursor.execute("SELECT route, date, details, cars_count FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()

    if load:
        route_str, date_str, details_text, cars_count_str = load
        curr_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if re.search(r'\d+', str(cars_count_str)) else 1

        if curr_cars > requested_cars:
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(curr_cars - requested_cars), load_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

        for _ in range(requested_cars):
            cursor.execute("""
                INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                VALUES (?, ?, ?, ?, 1, ?, ?)
            """, (load_id, user_id, date_str, route_str, rate, details_text))

        cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()

        add_notification(user_id, "Ставка принята!", f"Ваша ставка {rate} по грузу {route_str} была принята логистом.")
        try:
            await bot.send_message(chat_id=user_id, text=f"• Ваша ставка **{rate}** по грузу **{route_str}** успешно принята логистом!")
        except Exception:
            pass

        await update_cargo_messages_for_all_users(load_id)
        await callback.message.edit_text(callback.message.text + "\n\n• **СТАВКА ПРИНЯТА**")
    else:
        conn.close()
        await callback.answer("Груз не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("decline_bid_"))
async def handle_decline_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("decline_bid_", ""))
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="💸 Слишком дорого", callback_data=f"bid_reason_{bid_id}_expensive"),
        types.InlineKeyboardButton(text="📦 Груз уже отдан", callback_data=f"bid_reason_{bid_id}_taken")
    )
    builder.row(
        types.InlineKeyboardButton(text="🚛 Не подходит ТС", callback_data=f"bid_reason_{bid_id}_wrong_truck"),
        types.InlineKeyboardButton(text="❌ Без причины", callback_data=f"bid_reason_{bid_id}_none")
    )
    await callback.message.edit_text(callback.message.text + "\n\n❓ **Укажите причину отказа перевозчику:**", reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("bid_reason_"))
async def handle_bid_reason_selected(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    bid_id = int(parts[2])
    reason_code = parts[3]

    reasons_map = {
        "expensive": "Ставка слишком высокая",
        "taken": "Груз уже отдан другому перевозчику",
        "wrong_truck": "Не подходит тип транспортного средства",
        "none": "Ставка отклонена логистом"
    }
    reason_text = reasons_map.get(reason_code, "Ставка отклонена")

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()

    if bid:
        load_id, user_id, rate = bid
        cursor.execute("SELECT route FROM loads WHERE load_id = ?", (load_id,))
        l_row = cursor.fetchone()
        route_str = l_row[0] if l_row else "Груз"

        cursor.execute("UPDATE bids SET status = 'DECLINED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()

        add_notification(user_id, "Ставка отклонена", f"Ставка {rate} ({route_str}) отклонена. Причина: {reason_text}")
        try:
            await bot.send_message(chat_id=user_id, text=f"❌ Ваша ставка **{rate}** по грузу **{route_str}** отклонена.\n**Причина:** {reason_text}")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + f"\n\n• **ОТКЛОНЕНО ({reason_text})**")
    else:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("partial_bid_"))
async def handle_partial_bid_start(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("partial_bid_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pending_counters (admin_chat_id, bid_id, action_type) VALUES (?, ?, 'PARTIAL')", (ADMIN_CHANNEL_ID, bid_id))
    conn.commit()
    conn.close()

    await callback.message.reply("Укажите количество подтверждаемых авто (например: `1`):")
    await callback.answer()

@dp.callback_query(F.data.startswith("counter_bid_"))
async def handle_counter_bid_start(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("counter_bid_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pending_counters (admin_chat_id, bid_id, action_type) VALUES (?, ?, 'COUNTER')", (ADMIN_CHANNEL_ID, bid_id))
    conn.commit()
    conn.close()

    await callback.message.reply(
        "Укажите встречную ставку и количество авто через | или / (например: `2600 USD | 2` или `2600 USD / 1`):",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("accept_counter_"))
async def handle_accept_counter(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("accept_counter_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, counter_rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()

    if not bid:
        conn.close()
        await callback.answer("Предложение не найдено.", show_alert=True)
        return

    load_id, user_id, requested_cars, counter_rate = bid
    cursor.execute("SELECT route, date, details, cars_count FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()

    if load:
        route_str, date_str, details_text, cars_count_str = load
        curr_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if re.search(r'\d+', str(cars_count_str)) else 1

        if curr_cars > requested_cars:
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(curr_cars - requested_cars), load_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

        for _ in range(requested_cars):
            cursor.execute("""
                INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                VALUES (?, ?, ?, ?, 1, ?, ?)
            """, (load_id, user_id, date_str, route_str, counter_rate, details_text))

        cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
        conn.commit()

        add_notification(user_id, "Сделка подтверждена", f"Вы приняли встречную ставку {counter_rate} по грузу {route_str}.")
        await update_cargo_messages_for_all_users(load_id)

        admin_notification = (
            f"**Перевозчик ПРИНЯЛ встречную ставку!**\n\n"
            f"• Груз #{load_id} | Маршрут: {route_str}\n"
            f"• Итоговая ставка: {counter_rate} | Авто: {requested_cars}\n\n"
            f"• Перевозчик ID: {user_id}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + "\n\n• **ВЫ ПРИНЯЛИ ВСТРЕЧНУЮ СТАВКУ**")
    else:
        await callback.answer("Груз не найден.", show_alert=True)

    conn.close()

@dp.callback_query(F.data.startswith("decline_counter_"))
async def handle_decline_counter(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("decline_counter_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, counter_rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()

    if bid:
        load_id, user_id, counter_rate = bid
        cursor.execute("SELECT route FROM loads WHERE load_id = ?", (load_id,))
        l_row = cursor.fetchone()
        route_str = l_row[0] if l_row else "Груз"

        cursor.execute("UPDATE bids SET status = 'DECLINED' WHERE bid_id = ?", (bid_id,))
        conn.commit()

        admin_notification = (
            f"**Перевозчик ОТКЛОНИЛ встречную ставку.**\n\n"
            f"• Груз #{load_id} | Маршрут: {route_str}\n"
            f"• Отклоненная ставка: {counter_rate}\n"
            f"• Перевозчик ID: {user_id}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + "\n\n• **ВЫ ОТКЛОНИЛИ ВСТРЕЧНУЮ СТАВКУ**")

    conn.close()

@dp.callback_query(F.data.startswith("verify_approve_"))
async def handle_verify_approve(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("verify_approve_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET verification_status = 'VERIFIED', pending_company = '', pending_name = '', pending_phone = '' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Верификация пройдена!", "Ваша учетная запись успешно верифицирована. Вам открыт полный доступ к ценам и бронированию.")
    try:
        await bot.send_message(chat_id=target_uid, text="Ваша верификация успешно подтверждена! Полный доступ к бирже открыт.")
    except Exception:
        pass

    await callback.message.edit_text(callback.message.text + "\n\n• **ВЕРИФИКАЦИЯ ПОДТВЕРЖДЕНА**")
    await callback.answer()

@dp.callback_query(F.data.startswith("verify_reject_"))
async def handle_verify_reject(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("verify_reject_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET verification_status = 'REJECTED' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Верификация отклонена", "Ваша заявка на верификацию была отклонена администратором.")
    try:
        await bot.send_message(chat_id=target_uid, text="Заявка на верификацию отклонена. Проверьте данные в Личном кабинете и подайте заявку повторно.")
    except Exception:
        pass

    await callback.message.edit_text(callback.message.text + "\n\n• **ВЕРИФИКАЦИЯ ОТКЛОНЕНА**")
    await callback.answer()

@dp.callback_query(F.data.startswith("verify_edit_"))
async def handle_verify_edit_start(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("verify_edit_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pending_counters (admin_chat_id, bid_id, action_type) VALUES (?, ?, 'VERIFY_EDIT')", (ADMIN_CHANNEL_ID, target_uid))
    conn.commit()
    conn.close()

    # Убираем клавиатуру со старого сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.reply(
        "Введите новые данные пользователя в формате:\n"
        "`Компания | ФИО Контакта | Телефон`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("profile_approve_"))
async def handle_profile_approve(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("profile_approve_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users SET 
            company = pending_company,
            name = pending_name,
            phone = pending_phone,
            pending_company = '',
            pending_name = '',
            pending_phone = ''
        WHERE user_id = ?
    """, (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Изменения подтверждены", "Изменения вашего профиля успешно утверждены логистом.")
    await callback.message.edit_text(callback.message.text + "\n\n• **ИЗМЕНЕНИЯ ПРОФИЛЯ УТВЕРЖДЕНЫ**")
    await callback.answer()

@dp.callback_query(F.data.startswith("profile_reject_"))
async def handle_profile_reject(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("profile_reject_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET pending_company = '', pending_name = '', pending_phone = '' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Изменения отклонены", "Изменение данных вашего профиля отклонено администратором.")
    await callback.message.edit_text(callback.message.text + "\n\n• **ИЗМЕНЕНИЯ ПРОФИЛЯ ОТКЛОНЕНЫ**")
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    
    if u_row and u_row[0] == 'BLOCKED':
        conn.close()
        await callback.answer("Ваш аккаунт заблокирован администратором.", show_alert=True)
        return
    if not u_row or u_row[1] != 'VERIFIED':
        conn.close()
        await callback.answer("Для подтверждения грузов пройдите верификацию в Личном кабинете!", show_alert=True)
        return

    cargo_id = int(callback.data.replace("confirm_", ""))
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    await state.update_data(cargo_id=cargo_id, action_type="confirm")
    await callback.message.answer(f"Напишите, сколько грузов вы забираете? (доступно машин: {row[0] if row else '?'})")
    await state.set_state(DealStates.waiting_for_quantity)
    await callback.answer()

@dp.callback_query(F.data.startswith("bid_"))
async def callback_custom_bid(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()

    if u_row and u_row[0] == 'BLOCKED':
        conn.close()
        await callback.answer("Ваш аккаунт заблокирован администратором.", show_alert=True)
        return
    if not u_row or u_row[1] != 'VERIFIED':
        conn.close()
        await callback.answer("Для подачи ставок пройдите верификацию в Личном кабинете!", show_alert=True)
        return

    conn.close()

    cargo_id = int(callback.data.replace("bid_", ""))
    await state.update_data(cargo_id=cargo_id, action_type="bid")
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `2500 USD` или `250.000 RUB`):")
    await state.set_state(DealStates.waiting_for_custom_rate)
    await callback.answer()

@dp.message(DealStates.waiting_for_custom_rate)
async def process_custom_rate(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await message.answer("Ваш аккаунт заблокирован администратором.")
        await state.clear()
        return
    conn.close()

    rate = format_custom_rate(message.text.strip())
    await state.update_data(custom_rate=rate)
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    await message.answer(f"Сколько грузов вы можете поставить по этой ставке ({rate})? (доступно машин: {row[0] if row else '?'})")
    await state.set_state(DealStates.waiting_for_quantity)

@dp.message(DealStates.waiting_for_quantity)
async def process_deal_quantity(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await message.answer("Ваш аккаунт заблокирован администратором.")
        await state.clear()
        return
    conn.close()

    qty_input = message.text.strip()
    data = await state.get_data()
    action_type = data.get("action_type", "confirm")
    cargo_id = data.get("cargo_id")
    rate = data.get("custom_rate")
    user_obj = message.from_user

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count, text, price, date, route, details, status FROM loads WHERE load_id = ?", (cargo_id,))
    load_row = cursor.fetchone()
    
    if not load_row:
        conn.close()
        await message.answer("Груз не найден или уже закрыт.", reply_markup=get_main_reply_markup(message.from_user))
        await state.clear()
        return
        
    current_cars_str, raw_cargo_text, price_str, date_str, route_str, details_text, status = load_row
    
    if status in ['CLOSED', 'EXPIRED']:
        conn.close()
        await message.answer("Этот груз уже закрыт или истек его срок.", reply_markup=get_main_reply_markup(message.from_user))
        await state.clear()
        return

    current_cars = int(re.search(r'\d+', str(current_cars_str)).group(0)) if re.search(r'\d+', str(current_cars_str)) else 1
    requested_cars = int(re.search(r'\d+', qty_input).group(0)) if re.search(r'\d+', qty_input) else 1
    
    warning_text = ""
    if requested_cars > current_cars:
        requested_cars = current_cars
        warning_text = f"Запрошено больше, чем доступно. Берем {current_cars} авто.\n\n"

    carrier_text = format_carrier_info(user_id, user_obj.username, user_obj.full_name)

    if action_type == "bid":
        if requested_cars > current_cars:
            requested_cars = current_cars

        for _ in range(requested_cars):
            cursor.execute("""
                INSERT INTO bids (load_id, user_id, cars, rate, comment)
                VALUES (?, ?, 1, ?, '-')
            """, (cargo_id, user_id, rate))
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(
            types.InlineKeyboardButton(text="Своя ставка", callback_data=f"counter_bid_{bid_id}"),
            types.InlineKeyboardButton(text="Отказать", callback_data=f"decline_bid_{bid_id}")
        )

        admin_notification = (
            f"**НОВАЯ СТАВКА ОТ ПЕРЕВОЗЧИКА ИЗ БОТА**\n\n"
            f"• Рейс #{cargo_id} | Маршрут: {route_str}\n"
            f"• Ставка: {rate} | Авто: {requested_cars}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, reply_markup=None, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Error sending admin confirm notification: {e}")

        await state.clear()
        await message.answer(f"{warning_text}Ваша ставка отправлена администратору на рассмотрение!", reply_markup=get_main_reply_markup(message.from_user))
        return

    if current_cars > requested_cars:
        left_cars = current_cars - requested_cars
        cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), cargo_id))
    else:
        cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))
        
    for _ in range(requested_cars):
        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """, (cargo_id, user_id, date_str, route_str, price_str, details_text))
    
    conn.commit()
    conn.close()

    await update_cargo_messages_for_all_users(cargo_id)

    admin_notification = (
        f"**ГРУЗ ЗАБРАН ПЕРЕВОЗЧИКОМ ИЗ БОТА**\n\n"
        f"• Рейс #{cargo_id} | Маршрут: {route_str}\n"
        f"• Дата: {date_str}\n"
        f"• Ставка: {price_str} | Забрано авто: {requested_cars}\n\n"
        f"{carrier_text}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
    except Exception:
        pass
        
    await state.clear()
    await message.answer(f"{warning_text}Груз закреплен за вами! Просмотреть его можно в разделе «Забранные грузы».", reply_markup=get_main_reply_markup(message.from_user))
    

@dp.callback_query(F.data.startswith("kaiten_push_"))
async def handle_kaiten_push_callback(callback: types.CallbackQuery):
    deal_id = int(callback.data.replace("kaiten_push_", ""))
    admin_name = callback.from_user.full_name or callback.from_user.username or "Логист"

    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardBuilder().row(
            types.InlineKeyboardButton(text="⏳ Внесение в Kaiten...", callback_data="none")
        ).as_markup())
    except Exception:
        pass

    try:
        success, result = await push_data_to_kaiten(deal_id, callback.from_user.id, admin_user_name=admin_name)

        if success and isinstance(result, dict):
            card_id = result.get("card_id")
            card_url = result.get("url")
            status_text = f"\n\n✅ **Внесено в Kaiten:** [{card_id}]({card_url}) (логист: @{callback.from_user.username or admin_name})"
            
            await callback.message.edit_text(
                callback.message.text + status_text,
                reply_markup=None,
                parse_mode="Markdown"
            )
            await callback.answer("Данные успешно внесены в Kaiten!", show_alert=True)
        else:
            builder = InlineKeyboardBuilder()
            builder.row(
                types.InlineKeyboardButton(text="📥 Подать данные в Kaiten", callback_data=f"kaiten_push_{deal_id}"),
                types.InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"kaiten_skip_{deal_id}")
            )
            try:
                await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
            except Exception:
                pass
                
            err_log = result if isinstance(result, str) else "Ошибка работы с Kaiten"
            try:
                await callback.message.reply(
                    f"⚠️ **Результат поиска в Kaiten:**\n\n{err_log}",
                    parse_mode="Markdown"
                )
            except Exception as ex:
                logging.error(f"Error sending debug log: {ex}")

            await callback.answer("Запрос обработан, детали в сообщении.", show_alert=True)

    except Exception as e:
        logging.error(f"❌ Unhandled error in kaiten_push: {e}", exc_info=True)
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="📥 Подать данные в Kaiten", callback_data=f"kaiten_push_{deal_id}"),
            types.InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"kaiten_skip_{deal_id}")
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
            await callback.message.reply(f"❌ **Исключение при работе с Kaiten:** `{str(e)}`", parse_mode="Markdown")
        except Exception:
            pass
        await callback.answer(f"Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("kaiten_custom_"))
async def handle_kaiten_custom_callback(callback: types.CallbackQuery):
    deal_id = int(callback.data.replace("kaiten_custom_", ""))
    current_chat_id = callback.message.chat.id

    # Запоминаем в таблице pending_counters ожидание ID карточки для текущего чата
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO pending_counters (admin_chat_id, bid_id, action_type) VALUES (?, ?, 'KAITEN_BIND')", 
        (current_chat_id, deal_id)
    )
    conn.commit()
    conn.close()

    await callback.message.reply(
        f"📌 **Ручная привязка карточки Kaiten (Сделка #{deal_id})**\n\n"
        "Отправьте сообщением ID карточки или полную ссылку на неё:\n"
        "*(Например: `1341041` или `https://belkaspian.kaiten.ru/space/326566/board/764621/card/1341041`)*",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("kaiten_skip_"))
async def handle_kaiten_skip_callback(callback: types.CallbackQuery):
    admin_name = callback.from_user.username or callback.from_user.full_name or "Логист"
    skip_text = f"\n\n⏭ **Пропущено логистом** (@{admin_name})"

    try:
        if callback.message.caption is not None:
            new_caption = (callback.message.caption or "") + skip_text
            await callback.message.edit_caption(caption=new_caption, reply_markup=None, parse_mode="Markdown")
        elif callback.message.text is not None:
            new_text = (callback.message.text or "") + skip_text
            await callback.message.edit_text(text=new_text, reply_markup=None, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error skipping kaiten: {e}")

    await callback.answer("Заявка пропущена")



@dp.channel_post(F.chat.id == ORDERS_CHANNEL_ID)
async def handle_orders_channel_post(message: types.Message):
    """
    Обработчик файлов заявок из канала заявок.
    Поддерживает отправку одного или нескольких файлов за раз.
    """
    logging.info(f"📩 Получен пост в канале заявок ({ORDERS_CHANNEL_ID}). Наличие документа: {bool(message.document)}")

    if not message.document:
        logging.info("⚠️ Пост в канале заявок не содержит файл-документ.")
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith('.pdf'):
        logging.info(f"⚠️ Файл `{doc.file_name}` пропущен: расширение не .pdf")
        return

    logging.info(f"📄 Начинаем обработку PDF-заявки: {doc.file_name}")

    try:
        file_info = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        file_bytes = buf.getvalue()

        logging.info(f"⏳ Отправляем файл `{doc.file_name}` на распознавание в Gemini AI...")

        # Распознаем данные из PDF с помощью ИИ
        order_number, extracted_truck, extracted_trailer = await process_order_pdf_with_ai(file_bytes)
        logging.info(f"🔍 Результат ИИ: Заявка №'{order_number}', Тягач: '{extracted_truck}', Прицеп: '{extracted_trailer}'")

        if not extracted_truck:
            await bot.send_message(
                chat_id=ORDERS_CHANNEL_ID,
                text=f"⚠️ **Файл `{doc.file_name}`**: Не удалось распознать номер авто на 1-й странице.",
                parse_mode="Markdown"
            )
            return

        # Очищаем номера авто от пробелов и спецсимволов для точного поиска
        clean_extracted_truck = re.sub(r'[\s\W_]', '', extracted_truck).upper()
        clean_extracted_trailer = re.sub(r'[\s\W_]', '', extracted_trailer).upper()

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()

        # Ищем карточки в статусах "в работе" или "едут" (is_unloaded = 0)
        cursor.execute("""
            SELECT cd.id, cd.user_id, cd.date, cd.route, cd.price, 
                   cd.last_truck_plate, cd.last_trailer_plate, cd.last_driver_name
            FROM confirmed_deals cd
            WHERE cd.is_unloaded = 0
            ORDER BY cd.id DESC
        """)
        active_deals = cursor.fetchall()

        matched_deal = None
        for d_id, u_id, d_date, d_route, d_price, db_truck, db_trailer, db_driver in active_deals:
            clean_db_truck = re.sub(r'[\s\W_]', '', db_truck or '').upper()
            clean_db_trailer = re.sub(r'[\s\W_]', '', db_trailer or '').upper()

            # Сопоставление по номеру тягача или прицепа
            if clean_extracted_truck and clean_db_truck and (clean_extracted_truck in clean_db_truck or clean_db_truck in clean_extracted_truck):
                matched_deal = (d_id, u_id, d_date, d_route, d_price, db_truck, db_trailer, db_driver)
                break
            elif clean_extracted_trailer and clean_db_trailer and (clean_extracted_trailer in clean_db_trailer or clean_db_trailer in clean_extracted_trailer):
                matched_deal = (d_id, u_id, d_date, d_route, d_price, db_truck, db_trailer, db_driver)
                break

        if not matched_deal:
            conn.close()
            await bot.send_message(
                chat_id=ORDERS_CHANNEL_ID,
                text=f"❌ **Заявка №{order_number or 'б/н'}** (`{doc.file_name}`):\n"
                     f"Не найдена активная карточка по номеру авто: `{extracted_truck}`.",
                parse_mode="Markdown"
            )
            return

        deal_id, carrier_uid, date_str, route_str, price_str, truck_plate, trailer_plate, driver_name = matched_deal

        # Обновляем "Номер заявки" в БД
        cursor.execute("UPDATE confirmed_deals SET order_number = ? WHERE id = ?", (order_number, deal_id))
        conn.commit()
        conn.close()

        # Формируем подпись для сообщения перевозчику
        final_truck = truck_plate or extracted_truck or "Авто"
        final_trailer = trailer_plate or extracted_trailer or ""
        plates_str = f"{final_truck}/{final_trailer}" if final_trailer else final_truck
        final_driver = driver_name if driver_name and driver_name.lower() != "не распознан" else "Водитель"

        carrier_caption = (
            f"{date_str} {route_str}, {price_str}\n"
            f"{plates_str} - {final_driver}"
        )

        # Отправляем PDF с заявкой перевозчику
        try:
            pdf_file = types.BufferedInputFile(file_bytes, filename=doc.file_name)
            await bot.send_document(
                chat_id=carrier_uid,
                document=pdf_file,
                caption=carrier_caption
            )
            
            # Отчет логистам в канал заявок
            await bot.send_message(
                chat_id=ORDERS_CHANNEL_ID,
                text=f"✅ **Заявка №{order_number or 'б/н'} успешно отправлена!**\n\n"
                     f"📍 {carrier_caption}\n"
                     f"👤 Перевозчик ID: `{carrier_uid}`",
                parse_mode="Markdown"
            )
        except Exception as send_err:
            logging.error(f"Error sending order PDF to carrier {carrier_uid}: {send_err}")
            await bot.send_message(
                chat_id=ORDERS_CHANNEL_ID,
                text=f"⚠️ **Заявка №{order_number} привязана в системе**, но не удалось отправить файл в чат перевозчику (ID: {carrier_uid})."
            )

    except Exception as e:
        logging.error(f"Error processing order PDF post: {e}", exc_info=True)


@dp.channel_post(F.chat.id == PAYMENT_DOCS_CHANNEL_ID)
async def handle_payment_excel_post(message: types.Message):
    """
    Обработчик Excel файлов выплат (.xlsx / .xls) в канале документов на оплату.
    """
    if not message.document:
        return

    doc = message.document
    fn = (doc.file_name or "").lower()
    if not (fn.endswith('.xlsx') or fn.endswith('.xls')):
        return

    logging.info(f"📊 Получен Excel файл выплат: {doc.file_name}")

    try:
        file_info = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        file_bytes = buf.getvalue()

        import pandas as pd
        
        # Читаем книгу Excel
        excel_file = pd.ExcelFile(io.BytesIO(file_bytes))
        current_year_str = str(datetime.now().year)

        # Выбираем лист с актуальным годом (например "2026") или первый активный
        target_sheet = None
        for sheet in excel_file.sheet_names:
            if current_year_str in sheet:
                target_sheet = sheet
                break
        if not target_sheet:
            target_sheet = excel_file.sheet_names[0]

        df = pd.read_excel(excel_file, sheet_name=target_sheet, header=None)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()

        # Получаем список ранее обработанных заказов
        cursor.execute("SELECT order_number FROM processed_excel_payments")
        already_processed = {r[0] for r in cursor.fetchall()}

        paid_notifications = {}  # {user_id: [список строк оплат]}

        for idx, row in df.iterrows():
            if len(row) < 3:
                continue

            raw_order = str(row[0]).strip() if pd.notna(row[0]) else ""
            raw_amount = str(row[2]).strip() if pd.notna(row[2]) else ""
            raw_curr = str(row[3]).strip() if len(row) > 3 and pd.notna(row[3]) else "руб"

            if not raw_order or raw_order.lower() in ["номер", "заказ", "номер заказа", "nan"]:
                continue

            if raw_order in already_processed:
                continue

            # Ищем сделки по номеру заказа
            cursor.execute("""
                SELECT id, user_id, order_number 
                FROM confirmed_deals 
                WHERE (order_number LIKE ? OR order_number = ?) AND is_unloaded = 1
            """, (f"%{raw_order}%", raw_order))
            deal_rows = cursor.fetchall()

            if deal_rows:
                today_str = datetime.now().strftime("%d.%m.%Y")
                amount_str = f"{raw_amount} {raw_curr}".strip()

                for d_id, u_id, ord_num in deal_rows:
                    cursor.execute("""
                        UPDATE confirmed_deals 
                        SET pay_docs_status = 'PAID', is_paid = 1, paid_amount = ?, paid_date = ?
                        WHERE id = ?
                    """, (amount_str, today_str, d_id))

                    cursor.execute("""
                        INSERT OR IGNORE INTO processed_excel_payments (order_number, paid_amount, paid_date)
                        VALUES (?, ?, ?)
                    """, (raw_order, amount_str, today_str))

                    if u_id not in paid_notifications:
                        paid_notifications[u_id] = []
                    paid_notifications[u_id].append(f"{ord_num or raw_order} / {amount_str}")

        conn.commit()
        conn.close()

        # Рассылка уведомлений перевозчикам
        for u_id, items in paid_notifications.items():
            msg_text = "Сегодня вам оплатили:\n\n" + "\n".join(items)
            add_notification(u_id, "Выплата по грузам", msg_text)
            try:
                await bot.send_message(chat_id=u_id, text=msg_text)
            except Exception:
                pass

        await bot.send_message(
            chat_id=PAYMENT_DOCS_CHANNEL_ID,
            text=f"✅ **Excel реестр обработан!** Найдено совпадений и уведомлено перевозчиков: {len(paid_notifications)}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error processing payment Excel: {e}", exc_info=True)
        await bot.send_message(chat_id=PAYMENT_DOCS_CHANNEL_ID, text=f"⚠️ Ошибка чтения файла выплат: {e}")


# ==================== WEB APP БЭКЕНД И REST API ====================

async def get_loads_api(request):
    country = request.query.get('country')
    raw_uid = request.query.get('user_id')
    user_id = int(raw_uid) if raw_uid and raw_uid.isdigit() else 0

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT subscriptions, COALESCE(verification_status, 'UNVERIFIED'), status FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    
    # Если перевозчик заблокирован — возвращаем пустой список и статус блокировки
    if u_row and u_row[2] == 'BLOCKED':
        conn.close()
        return web.json_response({"loads": [], "is_verified": False, "is_blocked": True})

    user_subs = []
    is_verified = False
    if u_row:
        if u_row[0]:
            user_subs = [s.strip() for s in u_row[0].split(',') if s.strip()]
        is_verified = (u_row[1] == 'VERIFIED')

    if user_id and not user_subs and not (country and country != 'ALL'):
        conn.close()
        return web.json_response({"loads": [], "is_verified": is_verified, "is_blocked": False})

    query = """
        SELECT load_id, route, date, cars_count, price, text, 
               COALESCE(cargo_type, 'ТНП'), 
               COALESCE(weight, 'до 22т'), 
               COALESCE(car_type, 'Тент/реф'),
               COALESCE(admin_comment, ''),
               COALESCE(destination_country, 'Все')
        FROM loads 
        WHERE status = 'ACTIVE'
    """
    params = []

    if country and country != 'ALL':
        query += " AND (destination_country LIKE ? OR route LIKE ?)"
        params.extend([f"%{country}%", f"%{country}%"])
    else:
        if user_subs:
            sub_conditions = []
            for sub in user_subs:
                clean_sub = sub.split()[0].strip()
                sub_conditions.append("(destination_country LIKE ? OR route LIKE ?)")
                params.extend([f"%{clean_sub}%", f"%{clean_sub}%"])
            query += " AND (" + " OR ".join(sub_conditions) + ")"
        
    query += " ORDER BY load_id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    msk_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()

    loads = []
    for r in rows:
        dt_start, dt_end = parse_cargo_date_range(r[2])
        is_today = bool(dt_start and dt_end and dt_start <= msk_today <= dt_end)
        loads.append({
            "id": r[0],
            "route": r[1] if r[1] else "Не указан",
            "date": r[2] if r[2] else "Срочно",
            "cars": r[3] if r[3] else "1",
            "price": r[4] if is_verified else "🔒 Скрыто",
            "raw_text": r[5] if is_verified else "",
            "cargo_type": r[6] if is_verified else "🔒 Скрыто",
            "weight": r[7] if is_verified else "🔒 Скрыто",
            "car_type": r[8] if is_verified else "🔒 Скрыто",
            "admin_comment": r[9] if is_verified else "",
            "country": r[10],
            "is_today": is_today
        })
    return web.json_response({"loads": loads, "is_verified": is_verified})

async def my_loads_api(request):
    raw_uid = request.query.get('user_id')
    if not raw_uid:
        return web.json_response({"deals": []})
        
    try:
        user_id = int(raw_uid)
    except (ValueError, TypeError):
        return web.json_response({"deals": []})
        
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    # Проверка блокировки
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    if u_row and u_row[0] == 'BLOCKED':
        conn.close()
        return web.json_response({"deals": [], "is_blocked": True})
    
    try:
        # Узнаем, привязан ли сотрудник к общему ключу компании
        cursor.execute("SELECT company_key FROM users WHERE user_id = ?", (user_id,))
        k_row = cursor.fetchone()
        comp_key = k_row[0] if k_row and k_row[0] else ""

        # Если сотрудник привязан к компании по ключу — показываем все грузы этой компании,
        # если нет — только его личные
        if comp_key:
            user_filter_sql = "cd.user_id IN (SELECT user_id FROM users WHERE company_key = ?)"
            user_filter_params = (comp_key,)
            bid_filter_sql = "b.user_id IN (SELECT user_id FROM users WHERE company_key = ?) AND b.status IN ('PENDING', 'COUNTER')"
            bid_filter_params = (comp_key,)
        else:
            user_filter_sql = "cd.user_id = ?"
            user_filter_params = (user_id,)
            bid_filter_sql = "b.user_id = ? AND b.status IN ('PENDING', 'COUNTER')"
            bid_filter_params = (user_id,)

        # Добавлено получение полей оплаты и скрытие грузов, оплаченных более 5 дней назад
        cursor.execute(f"""
            SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, 
                   COALESCE(cd.details, ''), COALESCE(cd.status, 'CONFIRMED'),
                   COALESCE(l.car_type, 'Тент/реф'),
                   COALESCE(l.cargo_type, 'ТНП'),
                   COALESCE(l.weight, 'до 22т'),
                   COALESCE(cd.docs_submitted, 0),
                   COALESCE(cd.docs_status, 'NONE'),
                   COALESCE(cd.missing_docs, ''),
                   COALESCE(cd.last_truck_plate, ''),
                   COALESCE(cd.last_trailer_plate, ''),
                   COALESCE(cd.last_driver_name, ''),
                   COALESCE(cd.driver_phone, ''),
                   COALESCE(cd.unload_date, ''),
                   COALESCE(cd.is_unloaded, 0),
                   COALESCE(cd.order_number, ''),
                   COALESCE(u_book.name, 'Сотрудник'),
                   COALESCE(u_book.phone, ''),
                   COALESCE(cd.is_paid, 0),
                   COALESCE(cd.paid_date, ''),
                   COALESCE(cd.planned_payment_date, '')
            FROM confirmed_deals cd
            LEFT JOIN loads l ON cd.load_id = l.load_id
            LEFT JOIN users u_book ON cd.user_id = u_book.user_id
            WHERE {user_filter_sql}
            ORDER BY cd.id DESC
        """, user_filter_params)
        confirmed_rows = cursor.fetchall()
        
        cursor.execute(f"""
            SELECT b.bid_id, b.load_id, l.date, l.route, b.cars, 
                   COALESCE(b.counter_rate, b.rate) as price,
                   COALESCE(l.details, ''), b.status,
                   COALESCE(l.car_type, 'Тент/реф'),
                   COALESCE(l.cargo_type, 'ТНП'),
                   COALESCE(l.weight, 'до 22т'),
                   0 as docs_submitted
            FROM bids b
            JOIN loads l ON b.load_id = l.load_id
            WHERE {bid_filter_sql}
            ORDER BY b.bid_id DESC
        """, bid_filter_params)
        pending_rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error querying my_loads: {e}")
        conn.close()
        return web.json_response({"deals": []})
    
    conn.close()
    
    msk_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    deals = []

    for r in pending_rows:
        bid_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub = r
        dt_start, dt_end = parse_cargo_date_range(date_str)
        is_today = bool(dt_start and dt_end and dt_start <= msk_today <= dt_end)
        is_transit = False

        deals.append({
            "id": f"bid_{bid_id}",
            "bid_id": bid_id,
            "load_id": load_id,
            "date": date_str or "Срочно",
            "route": route_str or "Маршрут",
            "price": price_str or "Торги",
            "status": status_str,
            "details": details_str or "",
            "car_type": car_type or "Тент/реф",
            "cargo_type": cargo_type or "ТНП",
            "weight": weight or "до 22т",
            "is_today": is_today,
            "is_transit": is_transit,
            "is_unloaded": False,
            "docs_submitted": False,
            "docs_status": "NONE",
            "missing_docs": "",
            "truck_plate": "",
            "trailer_plate": "",
            "driver_name": "",
            "driver_phone": "",
            "unload_date": ""
        })

    for r in confirmed_rows:
        for r in confirmed_rows:
        deal_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub, docs_stat, miss_docs, tr_plate, trl_plate, drv_name, drv_phone, unl_date, is_unl, ord_num, b_name, b_phone, is_paid, paid_date, planned_pay = r

        # Если груз оплачен и прошло больше 5 дней — скрываем его из текущего экрана
        if is_paid and paid_date:
            try:
                p_dt = datetime.strptime(paid_date[:10], "%d.%m.%Y").date()
                if (msk_today - p_dt).days > 5:
                    continue
            except Exception:
                pass

        dt_start, dt_end = parse_cargo_date_range(date_str)
        is_today = bool(dt_start and dt_end and dt_start <= msk_today <= dt_end)
        has_submitted_docs = bool(docs_sub) or (docs_stat and docs_stat != 'NONE')
        is_transit = bool(dt_end and msk_today > dt_end and not is_unl and has_submitted_docs)

        deals.append({
            "id": f"deal_{deal_id}",
            "deal_id": deal_id,
            "load_id": load_id,
            "date": date_str or "Срочно",
            "route": route_str or "Маршрут",
            "price": price_str or "Торги",
            "status": status_str or "CONFIRMED",
            "details": details_str or "",
            "car_type": car_type or "Тент/реф",
            "cargo_type": cargo_type or "ТНП",
            "weight": weight or "до 22т",
            "is_today": is_today,
            "is_transit": is_transit,
            "is_unloaded": bool(is_unl),
            "docs_submitted": bool(docs_sub),
            "docs_status": docs_stat or "NONE",
            "missing_docs": miss_docs or "",
            "truck_plate": tr_plate or "",
            "trailer_plate": trl_plate or "",
            "driver_name": drv_name or "",
            "driver_phone": drv_phone or "",
            "unload_date": unl_date or "",
            "order_number": ord_num or "",
            "booked_by_name": b_name or "Сотрудник",
            "booked_by_phone": b_phone or ""
        })
            
    return web.json_response({"deals": deals})

async def direct_upload_docs_api(request):
    deal_id = None
    user_id = 0
    try:
        reader = await request.multipart()
        phone_input = ""
        raw_files = []

        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == 'deal_id':
                deal_id = int(await field.text())
            elif field.name == 'user_id':
                user_id = int(await field.text())
            elif field.name == 'phone':
                phone_input = (await field.text()).strip()
            elif field.name == 'files':
                filename = field.filename or "file"
                content = await field.read()
                if content:
                    raw_files.append((filename, content))

        if not deal_id or not user_id:
            return web.json_response({"error": "Ошибка валидации параметров"}, status=400)

        if not raw_files and not phone_input:
            return web.json_response({"error": "Укажите номер телефона или прикрепите файлы"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()

        # Ставим промежуточный статус считывания
        cursor.execute("UPDATE confirmed_deals SET docs_status = 'PROCESSING' WHERE id = ? AND user_id = ?", (deal_id, user_id))
        conn.commit()

        cursor.execute("""
            SELECT cd.docs_submitted, cd.last_truck_plate, cd.last_driver_name,
                   l.cargo_type, l.details, l.text, cd.last_trailer_plate, cd.driver_phone, 
                   cd.route, cd.date, cd.price, cd.docs_status, cd.missing_docs
            FROM confirmed_deals cd 
            LEFT JOIN loads l ON cd.load_id = l.load_id 
            WHERE cd.id = ? AND cd.user_id = ?
        """, (deal_id, user_id))
        deal_row = cursor.fetchone()

        was_previously_submitted = bool(deal_row[0]) if deal_row else False
        prev_truck_plate = deal_row[1] if deal_row else ""
        prev_driver_short_name = deal_row[2] if deal_row else ""
        cargo_info = f"{deal_row[3] or ''} {deal_row[4] or ''} {deal_row[5] or ''}".lower() if deal_row else ""
        is_polyethylene = bool("полиэтилен" in cargo_info or "polyethylene" in cargo_info)
        prev_trailer_plate = deal_row[6] if deal_row else ""
        prev_driver_phone = deal_row[7] if deal_row else ""
        route_str = deal_row[8] if deal_row else "Маршрут"
        date_str = deal_row[9] if deal_row else "Дата"
        price_str = deal_row[10] if deal_row else "Ставка"
        prev_missing_docs = deal_row[12] if deal_row else ""

        user_info = format_carrier_info(user_id)

        if raw_files:
            contents = ["Изучи документы. ФИО извлекай СТРОГО из ПАСПОРТА. Номер телефона: " + (phone_input or "Нет")]
            for fname, content in raw_files:
                mime = detect_mime_type(content, fname)
                contents.append(genai_types.Part.from_bytes(data=content, mime_type=mime))

            ai_formatted_data, raw_json = await process_docs_bytes_with_ai(contents, phone_input, is_polyethylene=is_polyethylene, route_str=route_str)
            
            raw_json = raw_json if isinstance(raw_json, dict) else {}
            t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
            tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
            d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
            p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}
            l_data = d_data.get("license") if isinstance(d_data.get("license"), dict) else {}

            new_truck_plate = (t_data.get("plate") or "Не распознан").strip().upper()
            if new_truck_plate in ["НЕ РАСПОЗНАН", "", "—"] and prev_truck_plate:
                new_truck_plate = prev_truck_plate

            new_trailer_plate = (tr_data.get("plate") or "Не распознан").strip().upper()
            if new_trailer_plate in ["НЕ РАСПОЗНАН", "", "—"] and prev_trailer_plate:
                new_trailer_plate = prev_trailer_plate

            p_full_name = (p_data.get("full_name") or "").strip()
            if p_data.get("number") in ["Не распознан", None, ""] or not p_full_name:
                new_driver_short_name = prev_driver_short_name if prev_driver_short_name and prev_driver_short_name != "Не распознан" else "Не распознан"
            else:
                new_driver_short_name = extract_surname_and_name(p_full_name)

            extracted_phone = phone_input or (d_data.get("phones") or "").strip()
            if extracted_phone and extracted_phone not in ["Не указан", ""]:
                new_driver_phone = normalize_phones(extracted_phone)
            elif prev_driver_phone:
                new_driver_phone = normalize_phones(prev_driver_phone)
            else:
                new_driver_phone = "Не указан"
        else:
            raw_json = {}
            new_truck_plate = prev_truck_plate or "НЕ РАСПОЗНАН"
            new_trailer_plate = prev_trailer_plate or "НЕ РАСПОЗНАН"
            new_driver_short_name = prev_driver_short_name or "Не распознан"
            new_driver_phone = normalize_phones(phone_input) if phone_input else (normalize_phones(prev_driver_phone) if prev_driver_phone else "Не указан")

            ai_formatted_data = (
                f"Тягач: {new_truck_plate}\n"
                f"Прицеп: {new_trailer_plate}\n"
                f"Водитель: {new_driver_short_name}\n"
                f"Номера телефонов: {new_driver_phone}\n"
                f"Паспорт: Сохранен ранее\n"
                f"Водительское: Сохранено ранее"
            )

        prev_missing_list = [x.strip() for x in prev_missing_docs.split(',') if x.strip()]
        missing_items = []

        p_num = p_data.get("number") if raw_files else None
        p_exp = p_data.get("expiry_date") if raw_files else None
        if p_num and p_num not in ["Не распознан", ""]:
            if is_doc_expired_or_expiring_soon(p_exp, threshold_days=20):
                missing_items.append("Паспорт водителя просрочен или истекает (менее 20 дней) — необходимо прикрепить актуальный документ")
        else:
            is_passport_missing_prev = any("Паспорт водителя" in item for item in prev_missing_list)
            if not was_previously_submitted or is_passport_missing_prev or new_driver_short_name in ["Не распознан", ""]:
                missing_items.append("Паспорт водителя")

        l_num = l_data.get("number") if raw_files else None
        l_exp = l_data.get("expiry_date") if raw_files else None
        if l_num and l_num not in ["Не распознан", ""]:
            if is_doc_expired_or_expiring_soon(l_exp, threshold_days=20):
                missing_items.append("Водительское удостоверение просрочено или истекает (менее 20 дней) — необходимо прикрепить актуальный документ")
        else:
            is_license_missing_prev = any("Водительское удостоверение" in item for item in prev_missing_list)
            if not was_previously_submitted or is_license_missing_prev:
                missing_items.append("Водительское удостоверение")

        if new_truck_plate in ["НЕ РАСПОЗНАН", "", "—"]: missing_items.append("Техпаспорт тягача")
        if new_trailer_plate in ["НЕ РАСПОЗНАН", "", "—"]: missing_items.append("Техпаспорт прицепа")
        if new_driver_phone in ["Не указан", "", "—"]: missing_items.append("Номер телефона")

        docs_status = "FULL" if not missing_items else "PARTIAL"
        missing_docs_str = ", ".join(missing_items) # <--- ИСПРАВЛЕНО (было missing_str)

        cursor.execute("""
            UPDATE confirmed_deals 
            SET docs_submitted = 1, docs_status = ?, missing_docs = ?, 
                last_truck_plate = ?, last_trailer_plate = ?, last_driver_name = ?, driver_phone = ?,
                full_doc_text = ?
            WHERE id = ? AND user_id = ?
        """, (docs_status, missing_docs_str, new_truck_plate, new_trailer_plate, new_driver_short_name, new_driver_phone, ai_formatted_data, deal_id, user_id))
        conn.commit()
        conn.close()

        header_title = "🔄 ЗАМЕНА ДАННЫХ ПО ГРУЗУ" if was_previously_submitted else "📄 ПОДАЧА ДАННЫХ ПО ГРУЗУ"
        admin_msg = (
            f"**{header_title}**\n\n"
            f"📅 {date_str} | 📍 {route_str}\n"
            f"💰 {price_str}\n\n"
            f"{user_info}\n\n"
            f"{ai_formatted_data}"
        )

        active_deal_id = deal_id or 0
        kaiten_builder = InlineKeyboardBuilder()
        kaiten_builder.row(
            types.InlineKeyboardButton(text="📥 Подать данные в Kaiten", callback_data=f"kaiten_push_{active_deal_id}"),
            types.InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"kaiten_skip_{active_deal_id}")
        )

        try:
            await bot.send_message(
                chat_id=DOCS_CHANNEL_ID, 
                text=admin_msg, 
                reply_markup=kaiten_builder.as_markup(),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Markdown send failed in direct upload: {e}")

        if raw_files:
            clean_name = re.sub(r'[^\w\s-]', '', new_driver_short_name).strip() or "ВОДИТЕЛЬ"
            clean_truck = re.sub(r'[^\w]', '', new_truck_plate).strip()
            clean_trailer = re.sub(r'[^\w]', '', new_trailer_plate).strip()

            if clean_trailer and clean_trailer.lower() not in ["нераспознан", "неуказан"]:
                plates_str = f"{clean_truck}_{clean_trailer}"
            else:
                plates_str = clean_truck

            pdf_filename = f"{clean_name} - {plates_str}.pdf"
            file_caption = f"{date_str} {route_str}"

            pdf_bytes = await generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, user_info, ai_formatted_data, raw_json)
            if pdf_bytes:
                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption=file_caption)

        return web.json_response({"status": "success", "docs_status": docs_status})
    except Exception as e:
        logging.error(f"Error in direct_upload_docs_api: {e}", exc_info=True)
        # Откат застрявшего статуса в случае ошибки
        if deal_id:
            try:
                conn = sqlite3.connect("cargo_bot.db")
                cursor = conn.cursor()
                cursor.execute("UPDATE confirmed_deals SET docs_status = 'PARTIAL' WHERE id = ? AND docs_status = 'PROCESSING'", (deal_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass
        return web.json_response({"error": str(e)}, status=400)

async def set_unload_date_api(request):
    try:
        data = await request.json()
        deal_id_raw = data.get('deal_id')
        unload_date = data.get('unload_date', '')
        user_id = int(data.get('user_id', 0))

        digits = re.findall(r'\d+', str(deal_id_raw))
        clean_deal_id = int(digits[0]) if digits else 0

        if not clean_deal_id or not unload_date or not user_id:
            return web.json_response({"error": "Неверные данные"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE confirmed_deals 
            SET unload_date = ?, is_unloaded = 1, status = 'UNLOADED'
            WHERE id = ? AND user_id = ?
        """, (unload_date, clean_deal_id, user_id))
        conn.commit()
        conn.close()

        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def notifications_get_api(request):
    raw_uid = request.query.get('user_id')
    if not raw_uid:
        return web.json_response({"notifications": [], "unread_count": 0})
    try:
        user_id = int(raw_uid)
    except (ValueError, TypeError):
        return web.json_response({"notifications": [], "unread_count": 0})

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, text, is_read, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 30", (user_id,))
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,))
    unread_count = cursor.fetchone()[0]
    conn.close()

    notifs = [{"id": r[0], "title": r[1], "text": r[2], "is_read": r[3], "created_at": r[4]} for r in rows]
    return web.json_response({"notifications": notifs, "unread_count": unread_count})

async def notifications_read_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        if user_id:
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
    except Exception:
        pass
    return web.json_response({"status": "success"})

async def profile_get_api(request):
    raw_uid = request.query.get('user_id')
    if not raw_uid:
        return web.json_response({"profile": None})
    try:
        user_id = int(raw_uid)
    except (ValueError, TypeError):
        return web.json_response({"profile": None})

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT company, name, phone, subscriptions, 
               COALESCE(verification_status, 'UNVERIFIED'),
               COALESCE(pending_company, ''), COALESCE(pending_name, ''), COALESCE(pending_phone, '')
        FROM users WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return web.json_response({
            "profile": {
                "company": row[0] or "",
                "name": row[1] or "",
                "phone": row[2] or "",
                "subscriptions": row[3] or "",
                "verification_status": row[4] or "UNVERIFIED",
                "pending_company": row[5] or "",
                "pending_name": row[6] or "",
                "pending_phone": row[7] or ""
            }
        })
    return web.json_response({"profile": None})

async def profile_post_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)

        company = data.get('company', '').strip()
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip()
        subscriptions = data.get('subscriptions', '')

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT verification_status FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        v_status = row[0] if row else 'UNVERIFIED'

        if v_status == 'VERIFIED':
            cursor.execute("""
                UPDATE users SET subscriptions = ? WHERE user_id = ?
            """, (subscriptions, user_id))
        else:
            cursor.execute("""
                INSERT INTO users (user_id, company, name, phone, subscriptions, verification_status, status)
                VALUES (?, ?, ?, ?, ?, 'UNVERIFIED', 'ACTIVE')
                ON CONFLICT(user_id) DO UPDATE SET
                    company = excluded.company,
                    name = excluded.name,
                    phone = excluded.phone,
                    subscriptions = excluded.subscriptions
            """, (user_id, company, name, phone, subscriptions))

        conn.commit()
        conn.close()
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def request_verification_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT company, name, phone, verification_status FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if not row or not row[0] or not row[1] or not row[2]:
            conn.close()
            return web.json_response({"error": "Заполните все данные профиля (Компания, ФИО, Телефон)"}, status=400)

        cursor.execute("UPDATE users SET verification_status = 'PENDING' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

        carrier_link = format_carrier_info(user_id)

        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="Подтвердить", callback_data=f"verify_approve_{user_id}"),
            types.InlineKeyboardButton(text="Отклонить", callback_data=f"verify_reject_{user_id}")
        )
        builder.row(
            types.InlineKeyboardButton(text="Отредактировать", callback_data=f"verify_edit_{user_id}")
        )

        msg_text = (
            f"**ЗАПРОС НА ВЕРИФИКАЦИЮ**\n\n"
            f"{carrier_link}\n"
            f"• Компания: {row[0]}\n"
            f"• ФИО: {row[1]}\n"
            f"• Телефон: {row[2]}"
        )

        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=msg_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def submit_profile_changes_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        new_company = data.get('company', '').strip()
        new_name = data.get('name', '').strip()
        new_phone = data.get('phone', '').strip()

        if not user_id or not new_company or not new_name or not new_phone:
            return web.json_response({"error": "Заполните все поля профиля"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return web.json_response({"error": "Пользователь не найден"}, status=400)

        old_company, old_name, old_phone = row[0] or "", row[1] or "", row[2] or ""

        if new_company == old_company and new_name == old_name and new_phone == old_phone:
            conn.close()
            return web.json_response({"error": "Данные не изменились"}, status=400)

        cursor.execute("""
            UPDATE users SET 
                pending_company = ?,
                pending_name = ?,
                pending_phone = ?
            WHERE user_id = ?
        """, (new_company, new_name, new_phone, user_id))
        conn.commit()
        conn.close()

        carrier_link = format_carrier_info(user_id)

        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="Подтвердить", callback_data=f"profile_approve_{user_id}"),
            types.InlineKeyboardButton(text="Отклонить", callback_data=f"profile_reject_{user_id}")
        )

        msg_text = (
            f"**ЗАПРОС НА ИЗМЕНЕНИЕ ДАННЫХ ПРОФИЛЯ**\n\n"
            f"{carrier_link}\n\n"
            f"• Было:\n  Компания: {old_company}\n  ФИО: {old_name}\n  Тел: {old_phone}\n\n"
            f"• Стало:\n  Компания: {new_company}\n  ФИО: {new_name}\n  Тел: {new_phone}"
        )

        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=msg_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def submit_docs_prompt_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        deal_id_raw = data.get('deal_id')
        digits = re.findall(r'\d+', str(deal_id_raw))
        clean_deal_id = int(digits[0]) if digits else 0

        if not user_id or not clean_deal_id:
            return web.json_response({"error": "Ошибка данных"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT date, route, price, docs_submitted FROM confirmed_deals WHERE id = ? AND user_id = ?", (clean_deal_id, user_id))
        deal = cursor.fetchone()
        conn.close()

        date_str = deal[0] if deal else "Ближайшая"
        route_str = deal[1] if deal else "Маршрут"
        price_str = deal[2] if deal else "Ставка"
        is_replacement = bool(deal[3]) if deal else False

        state_ctx = dp.fsm.get_context(bot, user_id, user_id)
        await state_ctx.set_state(DocUploadStates.waiting_for_docs)
        await state_ctx.update_data(
            upload_deal_id=clean_deal_id,
            upload_route=route_str,
            upload_date=date_str,
            upload_price=price_str,
            photos=[],
            documents=[],
            text_notes=""
        )

        title_header = "🔄 **Замена данных по грузу**" if is_replacement else "📄 **Подача данных по грузу**"
        prompt_text = (
            f"{title_header}\n\n"
            f"📍 {route_str} ({date_str})\n"
            f"💰 Ставка: {price_str}\n\n"
            f"Пожалуйста, отправьте в этот чат фото или PDF-файлы документов.\n"
            f"Когда закончите, нажмите кнопку **«✅ Отправить данные логисту»**."
        )

        builder = ReplyKeyboardBuilder()
        builder.add(types.KeyboardButton(text="✅ Отправить данные логисту"))
        builder.add(types.KeyboardButton(text="❌ Отмена"))
        builder.adjust(1, 1)

        await bot.send_message(chat_id=user_id, text=prompt_text, reply_markup=builder.as_markup(resize_keyboard=True), parse_mode="Markdown")
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def book_load_api(request):
    raw_id = request.match_info.get('id', '')
    digits = re.findall(r'\d+', str(raw_id))
    if not digits:
        return web.json_response({"error": "Неверный ID груза"}, status=400)
    load_id = int(digits[0])

    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
    except Exception:
        return web.json_response({"error": "Неверный формат данных"}, status=400)

    if not user_id:
        return web.json_response({"error": "Пользователь не определён."}, status=400)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    cursor.execute("SELECT COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    v_row = cursor.fetchone()
    if not v_row or v_row[0] != 'VERIFIED':
        conn.close()
        return web.json_response({"error": "Для подтверждения грузов и подачи ставок пройдите верификацию в Личном кабинете!"}, status=403)

    first_name = data.get('first_name', '')
    username = data.get('username', '')
    action = data.get('action') 
    proposed_price = format_custom_rate(data.get('proposed_price', ''))
    carrier_comment = data.get('comment', '')
    requested_cars = int(data.get('cars', 1))

    cursor.execute("SELECT status, route, date, price, cars_count, details, admin_comment FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()
    
    if not load or load[0] != 'ACTIVE':
        conn.close()
        return web.json_response({"error": "Груз недоступен"}, status=400)
        
    status, route_str, date_str, price_str, cars_count_str, details_text, admin_comment = load
    current_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if cars_count_str and re.search(r'\d+', str(cars_count_str)) else 1

    if requested_cars > current_cars:
        requested_cars = current_cars

    carrier_text = format_carrier_info(user_id, username, first_name)

    if action == 'confirm':
        if current_cars > requested_cars:
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(current_cars - requested_cars), load_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

        for _ in range(requested_cars):
            cursor.execute("""
                INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                VALUES (?, ?, ?, ?, 1, ?, ?)
            """, (load_id, user_id, date_str, route_str, price_str, details_text))

        conn.commit()
        conn.close()

        add_notification(user_id, "Груз забронирован", f"Вы забронировали груз {route_str} ({requested_cars} авто, {price_str}).")
        await update_cargo_messages_for_all_users(load_id)

        admin_notification = (
            f"**ГРУЗ ЗАБРАН ИЗ WEB APP**\n\n"
            f"• Рейс #{load_id} | Маршрут: {route_str}\n"
            f"• Дата: {date_str}\n"
            f"• Ставка: {price_str} | Забрано авто: {requested_cars}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})

    elif action == 'bid':
        # Ограничиваем количество запрашиваемых машин общим доступным остатком
        if requested_cars > current_cars:
            requested_cars = current_cars

        # Добавляем ровно requested_cars записей на согласование (но не более доступных)
        for _ in range(requested_cars):
            cursor.execute("""
                INSERT INTO bids (load_id, user_id, cars, rate, comment)
                VALUES (?, ?, 1, ?, ?)
            """, (load_id, user_id, proposed_price, carrier_comment or 'Своя ставка'))
            
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        add_notification(user_id, "Ставка отправлена", f"Ваша ставка {proposed_price} по грузу {route_str} отправлена логисту.")

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(
            types.InlineKeyboardButton(text="Своя ставка", callback_data=f"counter_bid_{bid_id}"),
            types.InlineKeyboardButton(text="Отказать", callback_data=f"decline_bid_{bid_id}")
        )

        admin_notification = (
            f"**НОВАЯ СТАВКА ИЗ WEB APP**\n\n"
            f"• Рейс #{load_id} | Маршрут: {route_str}\n"
            f"• Ставка: {proposed_price} | Авто: {requested_cars}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, reply_markup=admin_builder.as_markup(), parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})

    conn.close()
    return web.json_response({"error": "Неизвестное действие"}, status=400)

async def accept_counter_api(request):
    try:
        data = await request.json()
        bid_id = int(data.get('bid_id', 0))
        user_id = int(data.get('user_id', 0))
        if not bid_id or not user_id:
            return web.json_response({"error": "Неверные данные"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT load_id, user_id, cars, counter_rate FROM bids WHERE bid_id = ? AND user_id = ?", (bid_id, user_id))
        bid = cursor.fetchone()

        if not bid:
            conn.close()
            return web.json_response({"error": "Заявка не найдена"}, status=404)

        load_id, user_id, requested_cars, counter_rate = bid
        cursor.execute("SELECT route, date, details, cars_count FROM loads WHERE load_id = ?", (load_id,))
        load = cursor.fetchone()

        if load:
            route_str, date_str, details_text, cars_count_str = load
            curr_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if re.search(r'\d+', str(cars_count_str)) else 1

            if curr_cars > requested_cars:
                cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(curr_cars - requested_cars), load_id))
            else:
                cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

            for _ in range(requested_cars):
                cursor.execute("""
                    INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                """, (load_id, user_id, date_str, route_str, counter_rate, details_text))

            cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
            conn.commit()
            conn.close()

            add_notification(user_id, "Сделка подтверждена", f"Вы приняли встречную ставку {counter_rate} по грузу {route_str}.")
            await update_cargo_messages_for_all_users(load_id)

            admin_notification = (
                f"**Перевозчик ПРИНЯЛ встречную ставку из Web App!**\n\n"
                f"• Груз #{load_id} | Маршрут: {route_str}\n"
                f"• Итоговая ставка: {counter_rate} | Авто: {requested_cars}\n\n"
                f"• Перевозчик ID: {user_id}"
            )
            try:
                await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
            except Exception:
                pass

            return web.json_response({"status": "success"})
        else:
            conn.close()
            return web.json_response({"error": "Груз не найден"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def decline_counter_api(request):
    try:
        data = await request.json()
        raw_bid_id = data.get('bid_id', 0)
        digits = re.findall(r'\d+', str(raw_bid_id))
        bid_id = int(digits[0]) if digits else 0
        user_id = int(data.get('user_id', 0))

        if not bid_id or not user_id:
            return web.json_response({"error": "Неверные данные"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT load_id, user_id, counter_rate FROM bids WHERE bid_id = ? AND user_id = ?", (bid_id, user_id))
        bid = cursor.fetchone()

        if not bid:
            conn.close()
            return web.json_response({"error": "Заявка не найдена"}, status=404)

        load_id, user_id, counter_rate = bid
        cursor.execute("SELECT route FROM loads WHERE load_id = ?", (load_id,))
        l_row = cursor.fetchone()
        route_str = l_row[0] if l_row else "Груз"

        cursor.execute("UPDATE bids SET status = 'DECLINED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()

        carrier_link = format_carrier_info(user_id)
        admin_notification = (
            f"**Перевозчик ОТКЛОНИЛ встречную ставку из Web App!**\n\n"
            f"• Груз #{load_id} | Маршрут: {route_str}\n"
            f"• Отклоненная ставка: {counter_rate}\n"
            f"{carrier_link}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
        
def is_admin_authorized(request, data: dict = None) -> bool:
    """Безопасная проверка: либо Telegram ID владельца (ADMIN_ID), либо секретный ADMIN_KEY из Environment."""
    token = request.headers.get("X-Admin-Key")
    if not token and data:
        token = data.get("admin_key")

    uid = request.query.get("user_id")
    if not uid and data:
        uid = data.get("user_id")

    # 1. Если вход из вашего личного Telegram (ADMIN_ID) — вход автоматический
    if ADMIN_ID and uid and str(uid).strip() == str(ADMIN_ID).strip():
        return True

    # 2. Если вход с другого ПК/браузера — проверяем ключ из переменной окружения
    env_key = (ADMIN_KEY or "").strip()
    if env_key and token and str(token).strip() == env_key:
        return True

    return False

async def admin_verify_pass_api(request):
    try:
        data = await request.json()
        entered_key = data.get('password', '').strip()
        user_id = data.get('user_id', 0)

        env_key = (ADMIN_KEY or "").strip()
        is_owner_tg = bool(ADMIN_ID and str(user_id).strip() == str(ADMIN_ID).strip())
        is_key_match = bool(env_key and entered_key == env_key)

        if is_owner_tg or is_key_match:
            return web.json_response({"status": "success", "admin_key": env_key or entered_key})

        return web.json_response({"error": "Неверный ключ администратора"}, status=403)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_get_carriers_api(request):
    if not is_admin_authorized(request):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, company, name, phone, status, subscriptions FROM users ORDER BY user_id DESC")
    rows = cursor.fetchall()
    conn.close()
    carriers = [{"user_id": r[0], "company": r[1] or "Не указана", "name": r[2] or "Не указано", "phone": r[3] or "Не указан", "status": r[4] or "ACTIVE", "subscriptions": r[5] or ""} for r in rows]
    return web.json_response({"carriers": carriers})

async def admin_toggle_carrier_status_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        u_id = int(data.get('user_id'))
        reason = data.get('reason', '').strip()
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM users WHERE user_id = ?", (u_id,))
        row = cursor.fetchone()
        new_status = 'BLOCKED' if row and row[0] == 'ACTIVE' else 'ACTIVE'
        cursor.execute("UPDATE users SET status = ? WHERE user_id = ?", (new_status, u_id))
        conn.commit()
        conn.close()

        if new_status == 'BLOCKED':
            msg_text = f"⛔️ Ваш аккаунт заблокирован администратором.\nПричина: {reason if reason else 'Нарушение правил'}"
            add_notification(u_id, "Аккаунт заблокирован", msg_text)
            try:
                await bot.send_message(chat_id=u_id, text=msg_text)
            except Exception: pass
        else:
            msg_text = "✅ Ваш аккаунт разблокирован администратором. Все функции бота и WebApp снова доступны."
            add_notification(u_id, "Аккаунт разблокирован", msg_text)
            try:
                await bot.send_message(chat_id=u_id, text=msg_text)
            except Exception: pass

        return web.json_response({"status": "success", "new_status": new_status})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_get_loads_api(request):
    if not is_admin_authorized(request):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    query = """
        SELECT l.load_id, l.route, l.date, l.cars_count, l.price, l.status,
               COALESCE(l.car_type, 'Тент/реф'), COALESCE(l.cargo_type, 'ТНП'),
               COALESCE(l.weight, 'до 22т'), COALESCE(l.admin_comment, ''),
               GROUP_CONCAT(u.company || ' (' || u.name || ')', '; ') as carriers,
               COALESCE(l.destination_country, 'Все')
        FROM loads l
        LEFT JOIN confirmed_deals cd ON l.load_id = cd.load_id
        LEFT JOIN users u ON cd.user_id = u.user_id
        GROUP BY l.load_id
        ORDER BY l.load_id DESC
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()
    loads = [{
        "id": r[0], "route": r[1], "date": r[2], "cars": r[3], "price": r[4],
        "status": r[5], "car_type": r[6], "cargo_type": r[7], "weight": r[8],
        "admin_comment": r[9], "carrier_info": r[10] or "Не забран",
        "destination_country": r[11]
    } for r in rows]
    return web.json_response({"loads": loads})

async def admin_edit_load_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        load_id = int(data.get('id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE loads 
            SET route = ?, date = ?, price = ?, cars_count = ?, car_type = ?, cargo_type = ?, weight = ?, admin_comment = ?
            WHERE load_id = ?
        """, (data.get('route'), data.get('date'), data.get('price'), data.get('cars'), data.get('car_type'), data.get('cargo_type'), data.get('weight'), data.get('admin_comment'), load_id))
        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(load_id)
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_toggle_load_active_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        load_id = int(data.get('id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM loads WHERE load_id = ?", (load_id,))
        row = cursor.fetchone()
        new_status = 'CLOSED' if row and row[0] == 'ACTIVE' else 'ACTIVE'
        cursor.execute("UPDATE loads SET status = ? WHERE load_id = ?", (new_status, load_id))
        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(load_id)
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_hard_delete_load_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        load_id = int(data.get('id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM loads WHERE load_id = ?", (load_id,))
        cursor.execute("DELETE FROM confirmed_deals WHERE load_id = ?", (load_id,))
        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(load_id)
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_export_excel_api(request):
    if not is_admin_authorized(request):
        return web.Response(text="Доступ запрещен", status=403)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cd.id, cd.order_number, cd.date, cd.route, cd.price, 
               COALESCE(u.company, 'Не указана'), COALESCE(u.name, ''), COALESCE(u.phone, ''),
               COALESCE(cd.last_truck_plate, ''), COALESCE(cd.last_driver_name, ''),
               COALESCE(cd.driver_phone, ''), COALESCE(cd.planned_payment_date, ''),
               CASE WHEN cd.is_paid = 1 THEN 'Оплачен' ELSE 'Ожидает' END,
               COALESCE(cd.paid_amount, ''), COALESCE(cd.paid_date, '')
        FROM confirmed_deals cd
        LEFT JOIN users u ON cd.user_id = u.user_id
        ORDER BY cd.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    import pandas as pd
    columns = [
        "ID сделки", "Номер заявки", "Дата", "Маршрут", "Ставка",
        "Компания", "Контакт логиста", "Тел. логиста", "Госномер ТС",
        "Водитель", "Тел. водителя", "План. дата оплаты", "Статус оплаты", "Сумма оплаты", "Дата оплаты"
    ]
    df = pd.DataFrame(rows, columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl' if 'openpyxl' in sys.modules else None) as writer:
        df.to_excel(writer, index=False, sheet_name="Рейсы")
    output.seek(0)

    filename = f"deals_report_{datetime.now().strftime('%d_%m_%Y')}.xlsx"
    return web.Response(
        body=output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

async def admin_get_confirmed_deals_api(request):
    if not is_admin_authorized(request):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, cd.details, cd.user_id,
               COALESCE(u.company, 'Не указана'), COALESCE(u.name, 'Пользователь'), COALESCE(u.phone, 'Не указан'),
               COALESCE(u.status, 'ACTIVE'),
               COALESCE(l.destination_country, '')
        FROM confirmed_deals cd
        LEFT JOIN users u ON cd.user_id = u.user_id
        LEFT JOIN loads l ON cd.load_id = l.load_id
        ORDER BY cd.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    deals = [{
        "deal_id": r[0], "load_id": r[1], "date": r[2], "route": r[3], "cars": r[4], 
        "price": r[5], "details": r[6], "user_id": r[7], "company": r[8], 
        "name": r[9], "phone": r[10], "carrier_status": r[11], "destination_country": r[12]
    } for r in rows]
    return web.json_response({"deals": deals})

async def admin_edit_deal_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        deal_id = int(data.get('deal_id'))
        new_cars = int(data.get('cars', 1))
        new_route = data.get('route')
        new_date = data.get('date')
        new_price = format_custom_rate(data.get('price'))
        new_details = data.get('details', '')

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT load_id, user_id, cars FROM confirmed_deals WHERE id = ?", (deal_id,))
        row = cursor.fetchone()

        if row:
            load_id, user_id, old_cars = row[0], row[1], row[2]

            cursor.execute("""
                UPDATE confirmed_deals 
                SET route = ?, date = ?, price = ?, cars = 1, details = ?
                WHERE id = ?
            """, (new_route, new_date, new_price, new_details, deal_id))

            diff = new_cars - old_cars
            if diff > 0:
                for _ in range(diff):
                    cursor.execute("""
                        INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                    """, (load_id, user_id, new_date, new_route, new_price, new_details))

                add_notification(user_id, "Добавлен груз", f"Количество авто по заказу ({new_route}) изменено на {new_cars}. Вам добавлен второй такой же груз.")
                try:
                    await bot.send_message(chat_id=user_id, text=f"• В ваш раздел «Мои грузы» добавлен еще {diff} аналогичный груз ({new_route}).")
                except Exception: pass

            conn.commit()
            conn.close()
            return web.json_response({"status": "success"})
        else:
            conn.close()
            return web.json_response({"error": "Сделка не найдена"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_cancel_deal_api(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен. Требуется админ-ключ."}, status=403)

    try:
        deal_id = int(data.get('deal_id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, route, load_id, cars FROM confirmed_deals WHERE id = ?", (deal_id,))
        row = cursor.fetchone()

        if row:
            carrier_id, route, load_id, deal_cars = row[0], row[1], row[2], row[3]
            cursor.execute("DELETE FROM confirmed_deals WHERE id = ?", (deal_id,))
            
            cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (load_id,))
            l_row = cursor.fetchone()
            if l_row:
                curr_cars = int(re.search(r'\d+', str(l_row[0])).group(0)) if re.search(r'\d+', str(l_row[0])) else 0
                new_cars = curr_cars + deal_cars
                cursor.execute("UPDATE loads SET cars_count = ?, status = 'ACTIVE' WHERE load_id = ?", (str(new_cars), load_id))

            conn.commit()
            conn.close()

            add_notification(carrier_id, "Сделка отменена", f"Ваша подтверждённая сделка по маршруту {route} отменена логистом.")
            try:
                await bot.send_message(chat_id=carrier_id, text=f"⚠️ Ваш груз по маршруту {route} отменен администратором.")
            except Exception:
                pass

            if load_id:
                await update_cargo_messages_for_all_users(load_id)

            return web.json_response({"status": "success"})
        else:
            conn.close()
            return web.json_response({"error": "Сделка не найдена"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

import secrets

def generate_carrier_code() -> str:
    """Генерирует ключ формата XXX-XXX без путающихся символов (0/O, 1/I)."""
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(alphabet) for _ in range(3))
    part2 = "".join(secrets.choice(alphabet) for _ in range(3))
    return f"{part1}-{part2}"

async def activate_carrier_key_api(request):
    """Активация ключа сотрудником через веб-браузер с проверкой лимита."""
    try:
        data = await request.json()
        raw_key = data.get('key_code', '').strip().upper()
        clean_key = re.sub(r'[^A-Z0-9]', '', raw_key)
        if len(clean_key) == 6:
            formatted_key = f"{clean_key[:3]}-{clean_key[3:]}"
        else:
            formatted_key = raw_key

        company_input = data.get('company', '').strip()
        employee_name = data.get('name', '').strip()
        employee_phone = data.get('phone', '').strip()

        if not employee_name or not employee_phone:
            return web.json_response({"error": "Укажите имя сотрудника и контактный телефон."}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, status, company, COALESCE(max_users, 5) FROM carrier_keys WHERE key_code = ?", (formatted_key,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return web.json_response({"error": "Ключ не найден. Проверьте правильность ввода."}, status=404)

        key_id, k_status, saved_comp, max_users = row

        if k_status == 'BLOCKED':
            conn.close()
            return web.json_response({"error": "Ключ этой компании заблокирован администратором."}, status=403)

        # Проверка лимита сотрудников компании
        cursor.execute("SELECT COUNT(*) FROM users WHERE company_key = ? AND status != 'BLOCKED'", (formatted_key,))
        active_count = cursor.fetchone()[0]
        if active_count >= max_users:
            conn.close()
            return web.json_response({"error": f"Достигнут лимит сотрудников компании (макс. {max_users} чел.)."}, status=400)

        final_company = saved_comp if saved_comp and saved_comp != '—' else (company_input or "Компания")

        import random
        new_employee_id = 80000000 + random.randint(10000, 999999)

        cursor.execute("""
            INSERT INTO users (user_id, company, name, phone, subscriptions, status, verification_status, company_key)
            VALUES (?, ?, ?, ?, 'Казахстан,Узбекистан,Кыргызстан,Грузия,Азербайджан,Армения', 'ACTIVE', 'VERIFIED', ?)
        """, (new_employee_id, final_company, employee_name, employee_phone, formatted_key))

        cursor.execute("""
            UPDATE carrier_keys 
            SET status = 'ACTIVE', company = ?
            WHERE id = ?
        """, (final_company, key_id))

        conn.commit()
        conn.close()

        try:
            msg_text = (
                f"🔑 **ПОДКЛЮЧЕН СОТРУДНИК К КЛЮЧУ**\n\n"
                f"• Ключ компании: `{formatted_key}`\n"
                f"🏢 **Компания:** {final_company}\n"
                f"👤 **Сотрудник:** {employee_name}\n"
                f"📞 **Телефон:** {employee_phone}\n"
                f"💻 **Формат:** Веб-браузер (Web-ID: `{new_employee_id}`)"
            )
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=msg_text, parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({
            "status": "success",
            "user_id": new_employee_id,
            "company": final_company,
            "name": employee_name,
            "phone": employee_phone,
            "key_code": formatted_key
        })

    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

def link_key_to_user_profile(target_user_id: int, key_code: str) -> tuple[bool, str]:
    """Привязывает Telegram-аккаунт сотрудника к ключу компании с проверкой лимита и статуса блокировки."""
    raw_key = key_code.strip().upper()
    clean = re.sub(r'[^A-Z0-9]', '', raw_key)
    formatted = f"{clean[:3]}-{clean[3:]}" if len(clean) == 6 else raw_key

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    # Проверяем, не заблокирован ли сам пользователь (например, уволен ранее)
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (target_user_id,))
    u_row = cursor.fetchone()
    if u_row and u_row[0] == 'BLOCKED':
        conn.close()
        return False, "Ваш профиль заблокирован. Доступ к корпоративным ключам закрыт."

    cursor.execute("SELECT id, company, status, COALESCE(max_users, 5) FROM carrier_keys WHERE key_code = ?", (formatted,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return False, "Ключ не найден. Проверьте правильность ввода."

    k_id, comp, k_status, max_users = row

    if k_status == 'BLOCKED':
        conn.close()
        return False, "Данный ключ доступа заблокирован администратором."

    # Проверка лимита сотрудников (если пользователь еще не был привязан к этому ключу)
    cursor.execute("SELECT COUNT(*) FROM users WHERE company_key = ? AND status != 'BLOCKED' AND user_id != ?", (formatted, target_user_id))
    active_count = cursor.fetchone()[0]
    if active_count >= max_users:
        conn.close()
        return False, f"Достигнут лимит сотрудников компании (максимум {max_users} чел.)."

    final_comp = comp if comp and comp != '—' else "Компания"

    cursor.execute("""
        INSERT INTO users (user_id, company, name, phone, subscriptions, status, verification_status, company_key)
        VALUES (?, ?, 'Сотрудник', '', 'Казахстан,Узбекистан,Кыргызстан,Грузия,Азербайджан,Армения', 'ACTIVE', 'VERIFIED', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            company = excluded.company,
            company_key = excluded.company_key,
            status = 'ACTIVE',
            verification_status = 'VERIFIED'
    """, (target_user_id, final_comp, formatted))

    cursor.execute("UPDATE carrier_keys SET status = 'ACTIVE' WHERE id = ?", (k_id,))
    conn.commit()
    conn.close()

    return True, f"Вы успешно подключены к компании «{final_comp}»! Все грузы компании теперь доступны."

async def link_carrier_key_api(request):
    """API для привязки ключа прямо из WebApp в Личном кабинете."""
    try:
        data = await request.json()
        uid = int(data.get('user_id', 0))
        key = data.get('key_code', '').strip()
        if not uid or not key:
            return web.json_response({"error": "Укажите ID пользователя и ключ"}, status=400)

        success, msg = link_key_to_user_profile(uid, key)
        if success:
            return web.json_response({"status": "success", "message": msg})
        return web.json_response({"error": msg}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_get_keys_api(request):
    """Список всех ключей со списком привязанных сотрудников для админки."""
    if not is_admin_authorized(request):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, key_code, company, status, COALESCE(max_users, 5), created_at
        FROM carrier_keys
        ORDER BY id DESC
    """)
    rows = cursor.fetchall()

    keys = []
    for r in rows:
        k_id, k_code, comp, status, max_u, created_at = r
        cursor.execute("""
            SELECT user_id, name, phone, status
            FROM users
            WHERE company_key = ?
            ORDER BY user_id DESC
        """, (k_code,))
        emp_rows = cursor.fetchall()
        employees = [{
            "user_id": er[0],
            "name": er[1] or "Сотрудник",
            "phone": er[2] or "Не указан",
            "status": er[3] or "ACTIVE"
        } for er in emp_rows]

        keys.append({
            "id": k_id,
            "key_code": k_code,
            "company": comp or "—",
            "status": status or "UNUSED",
            "max_users": max_u,
            "employees_count": len(employees),
            "employees": employees,
            "created_at": created_at or ""
        })

    conn.close()

    bot_username = ""
    try:
        b_info = await bot.get_me()
        bot_username = b_info.username or ""
    except Exception:
        pass

    return web.json_response({
        "keys": keys,
        "bot_username": bot_username,
        "render_url": RENDER_URL.rstrip('/')
    })

async def admin_generate_key_api(request):
    """Генерация нового ключа с возможностью сразу задать компанию и лимит."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    company = data.get('company', '').strip()
    try:
        max_users = int(data.get('max_users', 5))
    except (ValueError, TypeError):
        max_users = 5

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    new_code = ""
    for _ in range(10):
        code_candidate = generate_carrier_code()
        cursor.execute("SELECT id FROM carrier_keys WHERE key_code = ?", (code_candidate,))
        if not cursor.fetchone():
            new_code = code_candidate
            break

    if not new_code:
        conn.close()
        return web.json_response({"error": "Не удалось сгенерировать код"}, status=500)

    cursor.execute("""
        INSERT INTO carrier_keys (key_code, company, status, max_users) 
        VALUES (?, ?, 'UNUSED', ?)
    """, (new_code, company, max_users))
    conn.commit()
    conn.close()

    return web.json_response({"status": "success", "key_code": new_code})

async def admin_unlink_employee_api(request):
    """Отвязка конкретного сотрудника с блокировкой его аккаунта от повторного входа."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    u_id = int(data.get('user_id', 0))
    if not u_id:
        return web.json_response({"error": "ID пользователя не указан"}, status=400)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    # Блокируем профиль уволенного сотрудника, чтобы он не смог использовать ключ повторно
    cursor.execute("UPDATE users SET company_key = '', verification_status = 'UNVERIFIED', status = 'BLOCKED' WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()

    add_notification(u_id, "Доступ отключен", "Вы были отвязаны от профиля компании администратором.")
    try:
        await bot.send_message(chat_id=u_id, text="⚠️ Вы были отвязаны от профиля компании администратором. Доступ закрыт.")
    except Exception:
        pass

    return web.json_response({"status": "success"})

async def admin_rotate_company_key_api(request):
    """Смена ключа для компании: старый ключ сгорает, текущие сотрудники остаются в системе."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    key_id = int(data.get('id', 0))
    if not key_id:
        return web.json_response({"error": "ID ключа не указан"}, status=400)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT key_code, company FROM carrier_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return web.json_response({"error": "Ключ не найден"}, status=404)

    old_code, company_name = row

    # Генерируем новый уникальный ключ
    new_code = ""
    for _ in range(10):
        cand = generate_carrier_code()
        cursor.execute("SELECT id FROM carrier_keys WHERE key_code = ?", (cand,))
        if not cursor.fetchone():
            new_code = cand
            break

    if not new_code:
        conn.close()
        return web.json_response({"error": "Ошибка генерации кода"}, status=500)

    # 1. Заменяем ключ у компании
    cursor.execute("UPDATE carrier_keys SET key_code = ? WHERE id = ?", (new_code, key_id))
    # 2. Обновляем привязку у всех текущих работающих сотрудников этой компании
    cursor.execute("UPDATE users SET company_key = ? WHERE company_key = ?", (new_code, old_code))

    conn.commit()
    conn.close()

    return web.json_response({"status": "success", "new_key": new_code, "company": company_name})

async def admin_edit_key_api(request):
    """Изменение названия компании и лимита сотрудников у ключа."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    key_id = int(data.get('id', 0))
    new_company = data.get('company', '').strip()
    try:
        new_max_users = int(data.get('max_users', 5))
    except (ValueError, TypeError):
        new_max_users = 5

    if not key_id:
        return web.json_response({"error": "ID ключа не указан"}, status=400)

    conn = sqlite3.connect("cargo_bot.db", timeout=15)
    cursor = conn.cursor()
    cursor.execute("SELECT key_code FROM carrier_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return web.json_response({"error": "Ключ не найден"}, status=404)

    key_code = row[0]

    # Обновляем сам ключ
    cursor.execute("""
        UPDATE carrier_keys 
        SET company = ?, max_users = ? 
        WHERE id = ?
    """, (new_company, new_max_users, key_id))

    # Обновляем название компании у всех привязанных к этому ключу сотрудников
    if new_company:
        cursor.execute("UPDATE users SET company = ? WHERE company_key = ?", (new_company, key_code))

    conn.commit()
    conn.close()

    return web.json_response({"status": "success"})

async def admin_delete_key_api(request):
    """Безвозвратное удаление ключа и отвязка сотрудников."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    key_id = int(data.get('id', 0))
    if not key_id:
        return web.json_response({"error": "ID ключа не указан"}, status=400)

    conn = sqlite3.connect("cargo_bot.db", timeout=15)
    cursor = conn.cursor()
    cursor.execute("SELECT key_code FROM carrier_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return web.json_response({"error": "Ключ не найден"}, status=404)

    key_code = row[0]
    cursor.execute("DELETE FROM carrier_keys WHERE id = ?", (key_id,))
    # Отвязываем сотрудников от удаленного ключа
    cursor.execute("UPDATE users SET company_key = '' WHERE company_key = ?", (key_code,))
    conn.commit()
    conn.close()

    return web.json_response({"status": "success"})

async def admin_toggle_key_status_api(request):
    """Блокировка или разблокировка ключа и всех его сотрудников."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    if not is_admin_authorized(request, data):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    key_id = int(data.get('id', 0))
    if not key_id:
        return web.json_response({"error": "ID ключа не указан"}, status=400)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, key_code FROM carrier_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return web.json_response({"error": "Ключ не найден"}, status=404)

    current_status, key_code = row
    new_status = 'BLOCKED' if current_status != 'BLOCKED' else 'ACTIVE'

    cursor.execute("UPDATE carrier_keys SET status = ? WHERE id = ?", (new_status, key_id))

    user_status = 'BLOCKED' if new_status == 'BLOCKED' else 'ACTIVE'
    cursor.execute("UPDATE users SET status = ? WHERE company_key = ?", (user_status, key_code))

    conn.commit()
    conn.close()

    return web.json_response({"status": "success", "new_status": new_status})

async def serve_index(request):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        index_path = os.path.join(base_dir, "static", "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return web.Response(text=html_content, content_type='text/html')
    except Exception as e:
        return web.Response(text=f"Error loading index.html: {e}", status=500)


async def submit_pay_docs_prompt_api(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id', 0))
        deal_id_raw = data.get('deal_id')
        digits = re.findall(r'\d+', str(deal_id_raw))
        clean_deal_id = int(digits[0]) if digits else 0

        if not user_id or not clean_deal_id:
            return web.json_response({"error": "Ошибка данных"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT date, route, price, order_number, last_truck_plate FROM confirmed_deals WHERE id = ? AND user_id = ?", (clean_deal_id, user_id))
        deal = cursor.fetchone()
        conn.close()

        if not deal:
            return web.json_response({"error": "Сделка не найдена"}, status=404)

        date_str, route_str, price_str, ord_num, truck_plate = deal

        state_ctx = dp.fsm.get_context(bot, user_id, user_id)
        await state_ctx.set_state(PayDocUploadStates.waiting_for_docs)
        await state_ctx.update_data(
            pay_deal_id=clean_deal_id,
            pay_route=route_str,
            pay_order_num=ord_num,
            pay_truck=truck_plate,
            photos=[],
            documents=[]
        )

        prompt_text = (
            f"📄 **Подача документов на оплату**\n\n"
            f"📍 {route_str} ({date_str})\n"
            f"📋 Заявка №: `{ord_num or 'б/н'}`\n\n"
            f"Отправьте в этот чат файлы пакета документов:\n"
            f"1. ЦМР (CMR)\n2. Счет на оплату\n3. Акт выполненных работ\n4. Подписанная заявка\n\n"
            f"Когда отправка завершена, нажмите кнопку **«✅ Проверить документы на оплату»**."
        )

        builder = ReplyKeyboardBuilder()
        builder.add(types.KeyboardButton(text="✅ Проверить документы на оплату"))
        builder.add(types.KeyboardButton(text="❌ Отмена"))
        builder.adjust(1, 1)

        await bot.send_message(chat_id=user_id, text=prompt_text, reply_markup=builder.as_markup(resize_keyboard=True), parse_mode="Markdown")
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

@dp.message(PayDocUploadStates.waiting_for_docs, F.text == "❌ Отмена")
async def handle_pay_docs_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Подача документов на оплату отменена.", reply_markup=get_main_reply_markup(message.from_user))

@dp.message(PayDocUploadStates.waiting_for_docs, F.photo)
async def handle_pay_doc_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)

@dp.message(PayDocUploadStates.waiting_for_docs, F.document)
async def handle_pay_doc_document(message: types.Message, state: FSMContext):
    data = await state.get_data()
    documents = data.get("documents", [])
    documents.append(message.document.file_id)
    await state.update_data(documents=documents)

@dp.message(PayDocUploadStates.waiting_for_docs, F.text == "✅ Проверить документы на оплату")
async def handle_pay_docs_finish(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    photos = data.get("photos", [])
    documents = data.get("documents", [])
    deal_id = data.get("pay_deal_id")
    ord_num = data.get("pay_order_num", "")
    truck_plate = data.get("pay_truck", "")

    if not photos and not documents:
        await message.answer("Вы не прислали ни одного документа.")
        return

    status_msg = await message.answer("⏳ ИИ проверяет пакет документов на оплату...")

    all_files = photos + documents
    contents_list = ["Изучи пакет документов на оплату:"]

    for fid in all_files:
        try:
            file_info = await bot.get_file(fid)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            raw_bytes = buf.getvalue()
            mime = detect_mime_type(raw_bytes, file_info.file_path or "")
            contents_list.append(genai_types.Part.from_bytes(data=raw_bytes, mime_type=mime))
        except Exception as e:
            logging.error(f"Error downloading file for pay docs AI: {e}")

    result = await verify_payment_docs_with_ai(contents_list, expected_order_num=ord_num, expected_truck=truck_plate)

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    if not result.is_valid:
        err_msg = "Ошибки в документах на оплату:\n• " + "\n• ".join(result.errors)
        cursor.execute("UPDATE confirmed_deals SET pay_docs_status = 'AI_ERROR', pay_docs_error = ? WHERE id = ?", (err_msg, deal_id))
        conn.commit()
        conn.close()

        await state.clear()
        try: await status_msg.delete()
        except Exception: pass

        await message.answer(f"❌ {err_msg}\n\nПожалуйста, исправьте указанные ошибки и отправьте документы повторно.", reply_markup=get_main_reply_markup(message.from_user))
        return

    today_dt = datetime.now(timezone.utc).date()
    planned_pay_dt = add_business_days(today_dt, 11)
    planned_pay_str = planned_pay_dt.strftime("%d.%m.%Y")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
        UPDATE confirmed_deals 
        SET pay_docs_status = 'IN_ACCOUNTING', pay_docs_error = '', pay_docs_submitted_at = ?, planned_payment_date = ? 
        WHERE id = ?
    """, (today_iso, planned_pay_str, deal_id))
    conn.commit()

    cursor.execute("SELECT route, date, price FROM confirmed_deals WHERE id = ?", (deal_id,))
    deal_info = cursor.fetchone()
    conn.close()

    route_str = deal_info[0] if deal_info else ""
    date_str = deal_info[1] if deal_info else ""
    price_str = deal_info[2] if deal_info else ""

    caption_text = (
        f"Заказ: {ord_num or 'б/н'}\n"
        f"Дата загрузки: {result.cmr_loading_date or 'Не указана'}\n"
        f"Дата выгрузки: {result.cmr_unloading_date or 'Не указана'}"
    )

    try:
        raw_file_tuples = []
        for fid in all_files:
            try:
                f_info = await bot.get_file(fid)
                buf_f = io.BytesIO()
                await bot.download_file(f_info.file_path, destination=buf_f)
                raw_file_tuples.append((f_info.file_path or "doc", buf_f.getvalue()))
            except Exception: pass

        pdf_bytes = await generate_single_pdf_bytes(raw_file_tuples, route_str, date_str, price_str, "", "")
        pdf_filename = f"{ord_num or 'Заявка'}.pdf"

        if pdf_bytes:
            pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
            await bot.send_document(chat_id=PAYMENT_DOCS_CHANNEL_ID, document=pdf_file, caption=caption_text)
    except Exception as e:
        logging.error(f"Error sending compiled PDF to payment channel: {e}")

    await state.clear()
    try: await status_msg.delete()
    except Exception: pass

    await message.answer(
        f"✅ Документы на оплату успешно приняты ИИ и переданы в бухгалтерию!\n\n"
        f"🗓 Плановая дата оплаты: **{planned_pay_str}**",
        reply_markup=get_main_reply_markup(message.from_user),
        parse_mode="Markdown"
    )



async def submit_pay_docs_direct_api(request):
    try:
        reader = await request.multipart()
        deal_id = 0
        user_id = 0
        raw_files = []

        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == 'deal_id':
                deal_id = int(await field.text())
            elif field.name == 'user_id':
                user_id = int(await field.text())
            elif field.name == 'files':
                fn = field.filename or "doc"
                content = await field.read()
                if content:
                    raw_files.append((fn, content))

        if not deal_id or not user_id or not raw_files:
            return web.json_response({"error": "Загрузите хотя бы 1 документ"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT order_number, last_truck_plate, route, date, price FROM confirmed_deals WHERE id = ? AND user_id = ?", (deal_id, user_id))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return web.json_response({"error": "Сделка не найдена"}, status=404)

        ord_num, truck_plate, route_str, date_str, price_str = row

        contents_list = ["Изучи пакет документов на оплату:"]
        for fn, file_bytes in raw_files:
            mime = detect_mime_type(file_bytes, fn)
            contents_list.append(genai_types.Part.from_bytes(data=file_bytes, mime_type=mime))

        # Вызываем ИИ-агента для проверки
        result = await verify_payment_docs_with_ai(contents_list, expected_order_num=ord_num, expected_truck=truck_plate)

        if not result.is_valid:
            err_msg = " Ошибки в документах:\n• " + "\n• ".join(result.errors)
            cursor.execute("UPDATE confirmed_deals SET pay_docs_status = 'AI_ERROR', pay_docs_error = ? WHERE id = ?", (err_msg, deal_id))
            conn.commit()
            conn.close()

            add_notification(user_id, "Ошибки в документах на оплату", err_msg)
            return web.json_response({"status": "error", "error": err_msg}, status=400)

        # Если все хорошо: рассчитываем плановую дату оплаты (+11 рабочих дней от сегодня)
        today_dt = datetime.now(timezone.utc).date()
        planned_pay_dt = add_business_days(today_dt, 11)
        planned_pay_str = planned_pay_dt.strftime("%d.%m.%Y")
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("""
            UPDATE confirmed_deals 
            SET pay_docs_status = 'IN_ACCOUNTING', pay_docs_error = '', pay_docs_submitted_at = ?, planned_payment_date = ? 
            WHERE id = ?
        """, (today_iso, planned_pay_str, deal_id))
        conn.commit()
        conn.close()

        # Генерируем 1 объединенный PDF файл
        pdf_bytes = await generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, "", "")
        pdf_filename = f"{ord_num or 'Заявка'}.pdf"

        # Отправляем в канал документов на оплату
        caption_text = (
            f"Заказ: {ord_num or 'б/н'}\n"
            f"Дата загрузки: {result.cmr_loading_date or 'Не указана'}\n"
            f"Дата выгрузки: {result.cmr_unloading_date or 'Не указана'}"
        )

        if pdf_bytes:
            pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
            await bot.send_document(chat_id=PAYMENT_DOCS_CHANNEL_ID, document=pdf_file, caption=caption_text)

        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Error in submit_pay_docs_direct_api: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=400)


# ==================== СЕРВЕР И ЗАПУСК ====================

async def handle_ping(request):
    return web.Response(text="Bot is running!", status=200)

async def self_ping():
    await asyncio.sleep(10)
    import aiohttp
    url = RENDER_URL.rstrip('/') if RENDER_URL else None
    if not url or "your-app-name" in url: return

    ping_url = f"{url}/ping"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    logging.info(f"Self-ping ({ping_url}) status: {response.status}")
        except Exception:
            pass
        await asyncio.sleep(180)

async def webserver_on_startup(app):
    asyncio.create_task(self_ping())
    asyncio.create_task(auto_clean_expired_cargos())
    asyncio.create_task(auto_promote_payment_docs_status())
    asyncio.create_task(auto_backup_db_loop())

async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def web_server():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/webapp", serve_index)
    app.router.add_get("/admin", serve_index)
    app.router.add_get("/webapp/{page}", serve_index)
    app.router.add_get("/api/loads", get_loads_api)
    app.router.add_get("/api/my_loads", my_loads_api)
    app.router.add_post("/api/set_unload_date", set_unload_date_api)
    app.router.add_get("/api/notifications", notifications_get_api)
    app.router.add_post("/api/notifications/read", notifications_read_api)
    app.router.add_get("/api/profile", profile_get_api)
    app.router.add_post("/api/profile", profile_post_api)
    app.router.add_post("/api/request_verification", request_verification_api)
    app.router.add_post("/api/submit_profile_changes", submit_profile_changes_api)
    app.router.add_post("/api/book/{id}", book_load_api)
    app.router.add_post("/api/accept_counter", accept_counter_api)
    app.router.add_post("/api/decline_counter", decline_counter_api)
    app.router.add_post("/api/submit_docs_prompt", submit_docs_prompt_api)
    app.router.add_post("/api/my_loads/direct_upload", direct_upload_docs_api)
    app.router.add_post("/api/my_loads/submit_pay_docs_direct", submit_pay_docs_direct_api)
    app.router.add_post("/api/submit_pay_docs_prompt", submit_pay_docs_prompt_api)

    app.router.add_post("/api/admin/verify_pass", admin_verify_pass_api)
    app.router.add_get("/api/admin/carriers", admin_get_carriers_api)
    app.router.add_post("/api/admin/carrier_status", admin_toggle_carrier_status_api)
    app.router.add_get("/api/admin/loads", admin_get_loads_api)
    app.router.add_post("/api/admin/edit_load", admin_edit_load_api)
    app.router.add_post("/api/admin/toggle_load_active", admin_toggle_load_active_api)
    app.router.add_post("/api/admin/hard_delete_load", admin_hard_delete_load_api)
    app.router.add_get("/api/admin/confirmed_deals", admin_get_confirmed_deals_api)
    app.router.add_get("/api/admin/export_excel", admin_export_excel_api)
    app.router.add_post("/api/admin/edit_deal", admin_edit_deal_api)
    app.router.add_post("/api/admin/cancel_deal", admin_cancel_deal_api)
    app.router.add_post("/api/carrier/activate_key", activate_carrier_key_api)
    app.router.add_post("/api/carrier/link_key", link_carrier_key_api)
    app.router.add_get("/api/admin/keys", admin_get_keys_api)
    app.router.add_post("/api/admin/generate_key", admin_generate_key_api)
    app.router.add_post("/api/admin/toggle_key_status", admin_toggle_key_status_api)
    app.router.add_post("/api/admin/unlink_employee", admin_unlink_employee_api)
    app.router.add_post("/api/admin/rotate_company_key", admin_rotate_company_key_api)
    app.router.add_post("/api/admin/edit_key", admin_edit_key_api)
    app.router.add_post("/api/admin/delete_key", admin_delete_key_api)
    
    app.on_startup.append(webserver_on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"✅ Web server started on port {port}")
    await asyncio.Event().wait()

async def main():
    # 1. ТОЛЬКО СКАЧИВАЕМ актуальную базу из закрепа канала бэкапов
    await restore_db_from_telegram()
    
    # 2. Проверяем таблицы и накатываем миграции
    init_db()

    # 3. Настраиваем перехват сигнала выключения контейнера (SIGTERM)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def on_shutdown_signal(sig_name):
        logging.info(f"🛑 Получен сигнал {sig_name}! Сохраняем базу перед деплоем...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: on_shutdown_signal(s.name))
        except (NotImplementedError, RuntimeError):
            pass

    # 4. Запускаем бота и веб-сервер
    bot_task = asyncio.create_task(run_bot())
    web_task = asyncio.create_task(web_server())

    logging.info("🚀 Бот и веб-сервер успешно запущены.")

    # 5. Ожидаем сигнала на перезапуск контейнера (при новом деплое)
    await stop_event.wait()

    # 6. ВЫГРУЗКА: сохраняем базу в закреп канала ПЕРЕД тем, как старый контейнер удалится
    logging.info("🛑 Сохранение финального бэкапа в канал перед перезапуском...")
    try:
        await push_db_backup(reason="Деплой нового релиза (SIGTERM)")
    except Exception as e:
        logging.error(f"Ошибка сохранения БД при выключении: {e}")

    # Закрываем соединения
    bot_task.cancel()
    web_task.cancel()
    await asyncio.gather(bot_task, web_task, return_exceptions=True)
    await bot.session.close()
    logging.info("✅ Контейнер завершил работу, база надежно сохранена в канале.")

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    print("🚀 Запуск сервера и бота...", flush=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)

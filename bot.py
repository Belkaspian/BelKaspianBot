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

DOCS_CHANNEL_ID_RAW = os.getenv("DOCS_CHANNEL_ID", "-1003928614238")
try:
    DOCS_CHANNEL_ID = int(DOCS_CHANNEL_ID_RAW)
except ValueError:
    DOCS_CHANNEL_ID = -1003928614238

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

CHANNELS = {
    "Казахстан 🇰🇿": -1004309918435,
    "Узбекистан 🇺🇿": -1003470705929,
    "Кыргызстан 🇰🇬": -1004470387295,
    "Азербайджан 🇦🇿": -1004483200216,
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

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA journal_mode=WAL;")
    
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
            bid_id INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    migrations = [
        "ALTER TABLE confirmed_deals ADD COLUMN load_id INTEGER",
        "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'ACTIVE'",
        "ALTER TABLE users ADD COLUMN verification_status TEXT DEFAULT 'UNVERIFIED'",
        "ALTER TABLE users ADD COLUMN pending_company TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN pending_name TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN pending_phone TEXT DEFAULT ''",
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
        "ALTER TABLE confirmed_deals ADD COLUMN status TEXT DEFAULT 'CONFIRMED'"
    ]
    for migration in migrations:
        try:
            cursor.execute(migration)
        except sqlite3.OperationalError:
            pass 

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', '123456')")
    
    conn.commit()
    conn.close()

init_db()

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

class AdminEditStates(StatesGroup):
    waiting_for_new_cargo_text = State()

class AdminCounterStates(StatesGroup):
    waiting_for_counter_rate = State()

class AdminPartialStates(StatesGroup):
    waiting_for_cars = State()

class AdminVerifyEditStates(StatesGroup):
    waiting_for_details = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def extract_surname_and_name(full_name: str) -> str:
    if not full_name or full_name.lower().strip() in ["не распознан", "не указан"]:
        return "Не распознан"
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return parts[0] if parts else "Не распознан"

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
    match = re.search(r'до\s*(\d{1,2}[\:\.]\d{2}|\d{1,2})\b', text, re.IGNORECASE)
    if match:
        raw_time = match.group(1).replace('.', ':')
        try:
            if ':' not in raw_time:
                hours = int(raw_time)
                minutes = 0
            else:
                parts = raw_time.split(':')
                hours, minutes = int(parts[0]), int(parts[1])
                
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                time_formatted = f"{hours:02d}:{minutes:02d}"
                msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
                expire_dt = msk_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
                expire_str = expire_dt.strftime("%Y-%m-%d %H:%M:%S")
                return time_formatted, expire_str
        except (ValueError, TypeError):
            pass
        
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

    date_pattern = re.compile(r'(\d{1,2}[\./]\d{1,2})')
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

def format_carrier_info(user_id: int, username: str = "", full_name: str = "") -> str:
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    comp = row[0] if row and row[0] and row[0] != 'Не указана' else "Не указана"
    db_name = row[1] if row and row[1] else ""
    phone = row[2] if row and row[2] else "Не указан"

    display_name = db_name or full_name or "Перевозчик"

    if username:
        clean_username = username.lstrip('@')
        user_link = f"[@{clean_username}](tg://user?id={user_id})"
    else:
        user_link = f"[{display_name}](tg://user?id={user_id})"

    return f"👤 Перевозчик: {user_link}\n🏢 {comp}, {display_name} {phone}"

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
    conn.close()

    for u_id, msg_id in messages_to_edit:
        try:
            if is_closed:
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=None
                )
            else:
                builder = InlineKeyboardBuilder()
                web_app_url = f"{RENDER_URL}/webapp"
                builder.row(types.InlineKeyboardButton(text="🚀 Открыть в Web App", web_app=WebAppInfo(url=web_app_url)))
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=builder.as_markup()
                )
        except Exception:
            pass

async def send_cargo_to_user(user_id: int, cargo_id: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        return

    cursor.execute("SELECT date, route, price, cars_count, details, status, admin_comment FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return
        
    date_str, route_str, price_str, cars_str, details_text, status, admin_comment = row
    is_closed = (status in ['CLOSED', 'EXPIRED'])
    formatted_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, admin_comment=admin_comment, is_closed=is_closed)
    
    builder = InlineKeyboardBuilder()
    if not is_closed:
        web_app_url = f"{RENDER_URL}/webapp"
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

def is_doc_expired_or_expiring_soon(expiry_str: str, threshold_days: int = 15) -> bool:
    if not expiry_str or str(expiry_str).lower().strip() in ["не распознана", "не указана", "бессрочно", "бессрочный"]:
        return False
    
    match = re.search(r'(\d{1,2})[\./-](\d{1,2})[\./-](\d{2,4})', str(expiry_str))
    if not match:
        match_iso = re.search(r'(\d{4})[\./-](\d{1,2})[\./-](\d{1,2})', str(expiry_str))
        if match_iso:
            year, month, day = int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3))
        else:
            return False
    else:
        day, month, year_raw = int(match.group(1)), int(match.group(2)), int(match.group(3))
        year = year_raw + 2000 if year_raw < 100 else year_raw

    try:
        exp_date = date(year, month, day)
        today = datetime.now(timezone.utc).date()
        cutoff_date = today + timedelta(days=threshold_days)
        return exp_date <= cutoff_date
    except ValueError:
        return False

def get_category_priority(cat_str: str) -> int:
    s = str(cat_str).lower().strip()
    if 'passport_front' in s or 'паспорт_лиц' in s: return 10
    if 'passport' in s or 'паспорт' in s: return 15
    if 'license_front' in s or 'права_лиц' in s: return 20
    if 'license' in s or 'права' in s or 'водительск' in s: return 25
    if 'truck' in s or 'тягач' in s or 'стс_тягач' in s: return 30
    if 'trailer' in s or 'прицеп' in s or 'стс_прицеп' in s: return 40
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

    system_prompt = (
        "Ты — эксперт логистической компании по распознаванию международных документов водителей и ТС.\n"
        "1. Распознавай данные с 100% точностью!\n"
        "2. ВАЖНО: ФИО водителя извлекай СТРОГО И ТОЛЬКО из ПАСПОРТА! Не бери ФИО с водительского удостоверения. "
        "Если паспорт не загружен или ФИО в паспорте не видно — в поле 'full_name' водителя указывай 'Не распознан'.\n"
        "3. Внимательно извлекай данные из всех страниц присланных PDF-файлов и фотографий.\n"
        "4. Марки ТС: В свидетельствах о регистрации ТС проверяй марку (brand) и модель (model).\n"
        "5. Номера телефонов: Если указан телефон — внеси его в поле 'phones'.\n"
        "6. Даты окончания документов: Обязательно извлекай expiry_date для паспорта и прав.\n"
        "7. Для каждой страницы укажи 'category' в 'image_roles'."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=FullCargoSubmission,
        temperature=0.1
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

            p_full_name = (p.get("full_name") or d.get("full_name") or "").strip()
            if p.get("number") in ["Не распознан", None, ""] or not p_full_name:
                driver_full_name = "Не распознан"
            else:
                driver_full_name = p_full_name

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
            p_auth = p.get('authority') or 'Не распознан'

            l_num = l.get('number') or 'Не распознан'
            l_date = l.get('issue_date') or 'Не распознана'
            l_country = l.get('country') or 'Не распознана'

            b_date = d.get('birth_date') or 'Не распознана'
            b_date_str = b_date if (b_date.endswith('г.') or b_date.endswith('г')) else f"{b_date}г."

            route_check_str = (route_str or text_notes or "").lower()

            if is_polyethylene or "полиэтилен" in route_check_str or "polyethylene" in route_check_str:
                formatted_output = (
                    f"ТС (марка, г/н, страна регистрации): {truck_brand_only}, гос.ном.: {truck_plate}, {t.get('country') or 'Не распознана'}\n"
                    f"Прицеп (марка, г/н, страна регистрации): {trailer_brand_only}, гос.ном.: {trailer_plate}, {tr.get('country') or 'Не распознана'}\n"
                    f"ФИО водителя: {driver_full_name}\n"
                    f"Тел (росс): {phones_str}\n"
                    f"Водительское удостоверение (№, когда и кем выдано): № {l_num} от {l_date}г., {l_country}\n"
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
            contents.append(genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        except Exception as e:
            logging.error(f"Download file error for AI: {e}")

    formatted_text, raw_json = await process_docs_bytes_with_ai(contents, text_notes, is_polyethylene=is_polyethylene, route_str=route_str)
    return formatted_text, all_files, raw_json
    async def generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, user_info, ai_formatted_data) -> bytes:
    buffer = io.BytesIO()
    images = []

    for fname, content in raw_files:
        mime = detect_mime_type(content, fname)
        if mime == "application/pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(content))
                out_buf = io.BytesIO()
                buffer.write(content)
                return buffer.getvalue()
            except Exception:
                pass
        else:
            try:
                img = Image.open(io.BytesIO(content))
                img = ImageOps.exif_transpose(img)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                images.append(img)
            except Exception as e:
                logging.error(f"Error converting image to PDF: {e}")

    if images:
        images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
        return buffer.getvalue()

    return b""

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

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
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
    web_app_url = f"{RENDER_URL}/webapp"
    inline_builder1 = InlineKeyboardBuilder()
    inline_builder1.row(types.InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=web_app_url)))

    await message.answer(
        "Приветствую!\nДля удобства использования нашего бота есть Web-App 👇",
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
        await callback.message.answer("📦 У вас нет подписок на направления. Выберите направления в меню.")
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
        await callback.message.answer("📦 Актуальных грузов по вашим направлениям пока нет.")
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

    await callback.message.answer("📦 **Список актуальных грузов по вашим направлениям:**", parse_mode="Markdown")

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
    web_app_url = f"{RENDER_URL}/webapp"
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

    await callback.message.answer("📦 **Ваши забранные грузы:**", parse_mode="Markdown")
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
        'VERIFIED': '✅ ВЕРИФИЦИРОВАН',
        'PENDING': '⏳ НА РАССМОТРЕНИИ',
        'UNVERIFIED': '❌ НЕ ВЕРИФИЦИРОВАН',
        'REJECTED': '❌ ОТКЛОНЕН'
    }

    text = (
        f"👤 **Личный кабинет**\n\n"
        f"Статус: **{stat_map.get(v_stat, 'НЕ ВЕРИФИЦИРОВАН')}**\n"
        f"🏢 Компания: {comp}\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}"
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
    await message.answer("✅ Название компании обновлено!")
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
    await message.answer("✅ Имя обновлено!")
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
    await message.answer("✅ Телефон обновлен!")
    await show_profile_menu(message)

# ==================== ПРЕОБРАЗОВАТЬ ДАННЫЕ (АВТОНОМНО) ====================

@dp.message(F.text == "🔄 Преобразовать данные")
@dp.callback_query(F.data == "menu_convert_standalone")
async def cmd_convert_data_start(event: types.Message | types.CallbackQuery, state: FSMContext):
    user = event.from_user
    if not is_convert_allowed(user):
        if isinstance(event, types.CallbackQuery):
            await event.answer("⚠️ Доступ ограничен.", show_alert=True)
        else:
            await event.answer("⚠️ Функция доступна только уполномоченным пользователям.")
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
    await message.answer("❌ Преобразование отменено.", reply_markup=get_main_reply_markup(message.from_user))

@dp.message(DocConvertStates.waiting_for_files, F.photo)
async def handle_convert_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)

@dp.message(DocConvertStates.waiting_for_files, F.document)
async def handle_convert_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    documents = data.get("documents", [])
    documents.append(message.document.file_id)
    await state.update_data(documents=documents)

@dp.message(DocConvertStates.waiting_for_files, F.text == "✅ Завершить и отправить")
async def handle_convert_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    documents = data.get("documents", [])
    notes = data.get("text_notes", "")

    if not photos and not documents and not notes:
        await message.answer("⚠️ Вы не прислали ни одного фото/файла или заметки.")
        return

    status_msg = await message.answer("🔄 ИИ распознает документы и сортирует страницы в PDF...")

    ai_formatted_data, sorted_files, raw_json = await process_docs_with_ai(photos, documents, notes, is_polyethylene=False, route_str="")

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

    try:
        await bot.send_message(chat_id=DOCS_CHANNEL_ID, text=admin_msg, parse_mode="Markdown")

        if documents:
            sorted_pdf_buf = await sort_pdf_pages(documents[0], raw_json) if len(documents) == 1 else None
            if sorted_pdf_buf:
                pdf_file = types.BufferedInputFile(sorted_pdf_buf.getvalue(), filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption="Преобразованный документ")
            else:
                for doc_id in documents:
                    await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=doc_id, caption="Документ")
        elif photos:
            pdf_buf = await create_pdf_report_with_images("Автономная проверка", "Сегодня", "-", user_info, ai_formatted_data, sorted_files)
            pdf_bytes = pdf_buf.getvalue()
            if pdf_bytes:
                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption="Преобразованный документ")

        await status_msg.delete()
        await message.answer("✅ Данные преобразованы и отправлены в канал!", reply_markup=get_main_reply_markup(message.from_user))
    except Exception as e:
        logging.error(f"Error sending converted docs: {e}")
        await message.answer("❌ Ошибка при отправке результатов.")

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
    await state.update_data(photos=photos)

@dp.message(DocUploadStates.waiting_for_docs, F.document)
async def handle_doc_document(message: types.Message, state: FSMContext):
    data = await state.get_data()
    documents = data.get("documents", [])
    documents.append(message.document.file_id)
    await state.update_data(documents=documents)

@dp.message(DocUploadStates.waiting_for_docs, F.text == "❌ Отмена")
async def handle_doc_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Подача данных отменена.", reply_markup=get_main_reply_markup(message.from_user))

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
        await message.answer("⚠️ Вы не прислали ни одного фото или файла.")
        return

    is_polyethylene = False
    was_previously_submitted = False
    prev_truck_plate = ""
    prev_trailer_plate = ""
    prev_driver_short_name = ""
    prev_driver_phone = ""

    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cd.docs_submitted, cd.last_truck_plate, cd.last_driver_name,
                   l.cargo_type, l.details, l.text, cd.last_trailer_plate, cd.driver_phone
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
        conn.close()

    ai_formatted_data, sorted_files, raw_json = await process_docs_with_ai(photos, documents, notes, is_polyethylene=is_polyethylene, route_str=route_str)

    t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
    tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
    d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
    p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}
    l_data = d_data.get("license") if isinstance(d_data.get("license"), dict) else {}

    new_truck_plate = (t_data.get("plate") or "Не распознан").strip().upper()
    if new_truck_plate in ["НЕ РАСПОЗНАН", ""] and prev_truck_plate:
        new_truck_plate = prev_truck_plate

    new_trailer_plate = (tr_data.get("plate") or "Не распознан").strip().upper()
    if new_trailer_plate in ["НЕ РАСПОЗНАН", ""] and prev_trailer_plate:
        new_trailer_plate = prev_trailer_plate

    p_full_name = (p_data.get("full_name") or "").strip()
    if p_data.get("number") in ["Не распознан", None, ""] or not p_full_name:
        new_driver_short_name = "Не распознан"
    else:
        new_driver_short_name = extract_surname_and_name(p_full_name)

    extracted_phone = (d_data.get("phones") or notes or "").strip()
    if extracted_phone and extracted_phone not in ["Не указан", ""]:
        new_driver_phone = extracted_phone
    elif prev_driver_phone:
        new_driver_phone = prev_driver_phone
    else:
        new_driver_phone = "Не указан"

    missing_items = []

    p_num = p_data.get("number")
    p_exp = p_data.get("expiry_date")
    if not p_num or p_num == "Не распознан" or new_driver_short_name == "Не распознан":
        missing_items.append("Паспорт водителя")
    elif is_doc_expired_or_expiring_soon(p_exp, 15):
        missing_items.append("Паспорт водителя (просрочен/истекает)")

    l_num = l_data.get("number")
    l_exp = l_data.get("expiry_date")
    if not l_num or l_num == "Не распознан":
        missing_items.append("Водительское удостоверение")
    elif is_doc_expired_or_expiring_soon(l_exp, 15):
        missing_items.append("Водительское удостоверение (просрочено/истекает)")

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
                last_truck_plate = ?, last_trailer_plate = ?, last_driver_name = ?, driver_phone = ?
            WHERE id = ? AND user_id = ?
        """, (docs_status, missing_docs_str, new_truck_plate, new_trailer_plate, new_driver_short_name, new_driver_phone, deal_id, user_id))
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

    try:
        await bot.send_message(chat_id=DOCS_CHANNEL_ID, text=admin_msg, parse_mode="Markdown")

        if documents:
            sorted_pdf_buf = await sort_pdf_pages(documents[0], raw_json) if len(documents) == 1 else None
            if sorted_pdf_buf:
                pdf_file = types.BufferedInputFile(sorted_pdf_buf.getvalue(), filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption=file_caption)
            else:
                for doc_id in documents:
                    await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=doc_id, caption=file_caption)
        elif photos:
            pdf_buf = await create_pdf_report_with_images(route_str, date_str, price_str, carrier_text, ai_formatted_data, sorted_files)
            pdf_bytes = pdf_buf.getvalue()
            if pdf_bytes:
                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption=file_caption)
    except Exception as e:
        logging.error(f"Error forwarding docs to admin channel: {e}", exc_info=True)

    await state.clear()
    
    if docs_status == "PARTIAL":
        feedback_msg = f"⚠️ Данные переданы, но не полностью. Не хватает:\n• " + "\n• ".join(missing_items)
    else:
        feedback_msg = "✅ Все данные успешно переданы логисту!"

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

# ==================== ПАРСИНГ ИЗ КАНАЛОВ И АДМИН-КАНАЛА ====================

LISTENED_CHATS = list(CHANNEL_TO_DIRECTION.keys())
if ADMIN_CHANNEL_ID not in LISTENED_CHATS:
    LISTENED_CHATS.append(ADMIN_CHANNEL_ID)

@dp.channel_post(F.text.startswith("/меню"))
async def handle_admin_menu_command(message: types.Message):
    if message.chat.id != ADMIN_CHANNEL_ID:
        return
        
    menu_text = (
        "⚙️ **Панель управления Админ-канала:**\n\n"
        "• Для моментальной рассылки ВСЕМ пользователям отправьте сообщение с восклицательными знаками, например: `!Внимание! Завтра погрузки с 8:00!`\n"
        "• Нажмите кнопку ниже, чтобы открыть веб-панель управления."
    )
    builder = InlineKeyboardBuilder()
    web_app_url = f"{RENDER_URL}/webapp"
    builder.row(types.InlineKeyboardButton(text="🛠 Открыть админ-панель", web_app=WebAppInfo(url=web_app_url)))
    
    await message.answer(menu_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

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
    raw_text = message.text or message.caption
    if not raw_text or raw_text.startswith("!") or raw_text.startswith("/"):
        return
        
    direction = CHANNEL_TO_DIRECTION.get(chat_id)
    if not direction:
        date_str, route_str, price_str, cars_str, details_text, car_type, cargo_type, weight, expires_at = parse_cargo_raw(raw_text)
        direction = detect_country(route_str or raw_text)

    splitted_texts = parse_multiple_cargos(raw_text)
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    created_cargo_ids = []
    
    for single_text in splitted_texts:
        date_str, route_str, price_str, cars_str, details_text, car_type, cargo_type, weight, expires_at = parse_cargo_raw(single_text)
        cursor.execute("""
            INSERT INTO loads (destination_country, date, route, cars_count, price, text, details, car_type, cargo_type, weight, expires_at, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """, (direction, date_str, route_str, cars_str, price_str, single_text, details_text, car_type, cargo_type, weight, expires_at))
        created_cargo_ids.append(cursor.lastrowid)
        
    conn.commit()
    cursor.execute("SELECT user_id, subscriptions, status FROM users")
    all_users = cursor.fetchall()
    
    clean_dir = direction.split()[0].strip()
    for cargo_id in created_cargo_ids:
        cursor.execute("SELECT date, route FROM loads WHERE load_id = ?", (cargo_id,))
        c_row = cursor.fetchone()
        c_date = c_row[0] if c_row else "ближайшую дату"
        
        for u_id, subs, u_status in all_users:
            if u_status == 'BLOCKED':
                continue
            user_subs_list = [s.strip() for s in (subs or "").split(",") if s.strip()]
            if any(clean_dir.lower() in sub.lower() for sub in user_subs_list):
                await send_cargo_to_user(u_id, cargo_id)
                add_notification(
                    u_id, 
                    "Новый груз", 
                    f"Появился груз на {c_date} по направлению {direction}"
                )
                await asyncio.sleep(0.05)
                
    conn.close()

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
            await bot.send_message(chat_id=user_id, text=f"✅ Ваша ставка **{rate}** по грузу **{route_str}** успешно принята логистом!")
        except Exception:
            pass

        await update_cargo_messages_for_all_users(load_id)
        await callback.message.edit_text(callback.message.text + "\n\n✅ **СТАВКА ПРИНЯТА**")
    else:
        conn.close()
        await callback.answer("Груз не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("decline_bid_"))
async def handle_decline_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("decline_bid_", ""))
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

        add_notification(user_id, "Ставка отклонена", f"Ваша ставка {rate} по грузу {route_str} была отклонена.")
        try:
            await bot.send_message(chat_id=user_id, text=f"❌ К сожалению, ваша ставка **{rate}** по грузу **{route_str}** была отклонена логистом.")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + "\n\n❌ **СТАВКА ОТКЛОНЕНА**")
    else:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("partial_bid_"))
async def handle_partial_bid_start(callback: types.CallbackQuery, state: FSMContext):
    bid_id = int(callback.data.replace("partial_bid_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    conn.close()

    if not bid:
        await callback.answer("Ставка не найдена.", show_alert=True)
        return

    requested_cars, rate = bid
    await state.set_state(AdminPartialStates.waiting_for_cars)
    await state.update_data(partial_bid_id=bid_id)
    await callback.message.reply(f"🔀 Введите количество подтверждаемых авто (запрошено: {requested_cars} авто по ставке {rate}):")
    await callback.answer()

@dp.message(AdminPartialStates.waiting_for_cars)
async def handle_partial_bid_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bid_id = data.get("partial_bid_id")
    
    m = re.search(r'\d+', message.text.strip())
    confirm_cars = int(m.group(0)) if m else 1

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, rate FROM bids WHERE bid_id = ?", (bid_id,))
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

            cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
            conn.commit()

            add_notification(user_id, "Частичное подтверждение", f"Логист подтвердил {confirm_cars} авто по ставке {rate} ({route_str}).")
            try:
                await bot.send_message(chat_id=user_id, text=f"🔀 Логист частично подтвердил вашу заявку по грузу **{route_str}**: **{confirm_cars} авто** по ставке **{rate}**!")
            except Exception:
                pass

            await update_cargo_messages_for_all_users(load_id)
            await message.answer(f"✅ Подтверждено {confirm_cars} авто по ставке {rate}!")
        else:
            await message.answer("Груз не найден.")
    else:
        await message.answer("Ставка не найдена.")

    conn.close()
    await state.clear()

@dp.callback_query(F.data.startswith("counter_bid_"))
async def handle_counter_bid_start(callback: types.CallbackQuery, state: FSMContext):
    bid_id = int(callback.data.replace("counter_bid_", ""))
    await state.set_state(AdminCounterStates.waiting_for_counter_rate)
    await state.update_data(counter_bid_id=bid_id)
    await callback.message.reply("💡 Введите вашу встречную цену/ставку для перевозчика (например: `2600 USD`):")
    await callback.answer()

@dp.message(AdminCounterStates.waiting_for_counter_rate)
async def handle_counter_bid_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bid_id = data.get("counter_bid_id")
    counter_rate = format_custom_rate(message.text.strip())

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()

    if bid:
        load_id, user_id, orig_rate = bid
        cursor.execute("SELECT route FROM loads WHERE load_id = ?", (load_id,))
        l_row = cursor.fetchone()
        route_str = l_row[0] if l_row else "Груз"

        cursor.execute("UPDATE bids SET status = 'COUNTER', counter_rate = ? WHERE bid_id = ?", (counter_rate, bid_id))
        conn.commit()

        add_notification(user_id, "Встречное предложение", f"Логист предложил вам ставку {counter_rate} по грузу {route_str}.")
        
        counter_builder = InlineKeyboardBuilder()
        counter_builder.row(
            types.InlineKeyboardButton(text="✅ Принять встречную", callback_data=f"accept_counter_{bid_id}"),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_counter_{bid_id}")
        )
        try:
            await bot.send_message(
                chat_id=user_id, 
                text=f"💡 Логист предложил встречную ставку **{counter_rate}** по грузу **{route_str}** (ваше предложение было: {orig_rate}).\n\nПринимаете предложение?",
                reply_markup=counter_builder.as_markup(),
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await message.answer(f"✅ Встречная ставка {counter_rate} отправлена перевозчику!")
    else:
        await message.answer("Ставка не найдена.")

    conn.close()
    await state.clear()

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
            f"✅ **Перевозчик ПРИНЯЛ встречную ставку!**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"💰 Итоговая ставка: {counter_rate} | 🚛 Авто: {requested_cars}\n\n"
            f"👤 Перевозчик ID: {user_id}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + "\n\n✅ **ВЫ ПРИНЯЛИ ВСТРЕЧНУЮ СТАВКУ**")
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
            f"❌ **Перевозчик ОТКЛОНИЛ встречную ставку.**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"💵 Отклоненная ставка: {counter_rate}\n"
            f"👤 Перевозчик ID: {user_id}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        await callback.message.edit_text(callback.message.text + "\n\n❌ **ВЫ ОТКЛОНИЛИ ВСТРЕЧНУЮ СТАВКУ**")

    conn.close()

@dp.callback_query(F.data.startswith("verify_approve_"))
async def handle_verify_approve(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("verify_approve_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET verification_status = 'VERIFIED' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Верификация пройдена!", "🎉 Ваша учетная запись успешно верифицирована. Вам открыт полный доступ к ценам и бронированию.")
    try:
        await bot.send_message(chat_id=target_uid, text="🎉 **Поздравляем!** Ваша верификация успешно подтверждена. Полный доступ к бирже открыт!")
    except Exception:
        pass

    await callback.message.edit_text(callback.message.text + "\n\n✅ **ВЕРИФИКАЦИЯ ПОДТВЕРЖДЕНА**")
    await callback.answer()

@dp.callback_query(F.data.startswith("verify_reject_"))
async def handle_verify_reject(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("verify_reject_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET verification_status = 'REJECTED' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Верификация отклонена", "❌ Ваша заявка на верификацию была отклонена администратором.")
    try:
        await bot.send_message(chat_id=target_uid, text="❌ К сожалению, ваша заявка на верификацию отклонена. Проверьте данные в Личном кабинете и подайте заявку повторно.")
    except Exception:
        pass

    await callback.message.edit_text(callback.message.text + "\n\n❌ **ВЕРИФИКАЦИЯ ОТКЛОНЕНА**")
    await callback.answer()

@dp.callback_query(F.data.startswith("verify_edit_"))
async def handle_verify_edit_start(callback: types.CallbackQuery, state: FSMContext):
    target_uid = int(callback.data.replace("verify_edit_", ""))
    await state.set_state(AdminVerifyEditStates.waiting_for_details)
    await state.update_data(verify_target_uid=target_uid)
    
    await callback.message.reply(
        "✏️ **Редактирование данных пользователя**\n\n"
        "Отправьте новые данные в формате:\n"
        "`Название компании | ФИО Контакта | Телефон`\n\n"
        "Пример:\n`ООО ТрансЛогистик | Иванов Иван | +375291234567`"
    )
    await callback.answer()

@dp.message(AdminVerifyEditStates.waiting_for_details)
async def handle_verify_edit_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("verify_target_uid")
    
    parts = [p.strip() for p in message.text.split('|')]
    if len(parts) < 3:
        await message.answer("⚠️ Неверный формат! Введите строго через разделитель `|`: `Компания | ФИО | Телефон`")
        return

    comp, name, phone = parts[0], parts[1], parts[2]

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET company = ?, name = ?, phone = ? WHERE user_id = ?", (comp, name, phone, target_uid))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Профиль отредактирован", f"Администратор скорректировал данные вашего профиля:\n{comp} | {name} | {phone}")

    carrier_link = format_carrier_info(target_uid)
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"verify_approve_{target_uid}"),
        types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"verify_reject_{target_uid}")
    )
    builder.row(
        types.InlineKeyboardButton(text="✏️ Отредактировать", callback_data=f"verify_edit_{target_uid}")
    )

    msg_text = (
        f"🛡️ **ЗАПРОС НА ВЕРИФИКАЦИЮ (ОТРЕДАКТИРОВАНО)**\n\n"
        f"{carrier_link}\n"
        f"🏢 Компания: {comp}\n"
        f"👤 ФИО: {name}\n"
        f"📞 Телефон: {phone}"
    )

    await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=msg_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await message.answer("✅ Данные обновлены! Новое меню отправлено в админ-канал.")
    await state.clear()

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

    add_notification(target_uid, "Изменения подтверждены", "✅ Изменения вашего профиля успешно утверждены логистом.")
    await callback.message.edit_text(callback.message.text + "\n\n✅ **ИЗМЕНЕНИЯ ПРОФИЛЯ УТВЕРЖДЕНЫ**")
    await callback.answer()

@dp.callback_query(F.data.startswith("profile_reject_"))
async def handle_profile_reject(callback: types.CallbackQuery):
    target_uid = int(callback.data.replace("profile_reject_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET pending_company = '', pending_name = '', pending_phone = '' WHERE user_id = ?", (target_uid,))
    conn.commit()
    conn.close()

    add_notification(target_uid, "Изменения отклонены", "❌ Изменение данных вашего профиля отклонено администратором.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ **ИЗМЕНЕНИЯ ПРОФИЛЯ ОТКЛОНЕНЫ**")
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
        warning_text = f"⚠️ Запрошено больше, чем доступно. Берем {current_cars} авто.\n\n"

    carrier_text = format_carrier_info(user_id, user_obj.username, user_obj.full_name)

    if action_type == "bid":
        cursor.execute("""
            INSERT INTO bids (load_id, user_id, cars, rate, comment)
            VALUES (?, ?, ?, ?, '-')
        """, (cargo_id, user_id, requested_cars, rate))
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="🔀 Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(
            types.InlineKeyboardButton(text="💡 Своя ставка", callback_data=f"counter_bid_{bid_id}"),
            types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{bid_id}")
        )

        admin_notification = (
            f"💰 **Новая ставка от перевозчика через бота!**\n\n"
            f"🆔 Груз #{cargo_id} | Маршрут: {route_str}\n"
            f"💵 Ставка: {rate} | 🚛 Авто: {requested_cars}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, reply_markup=admin_builder.as_markup(), parse_mode="Markdown")
        except Exception:
            pass

        await state.clear()
        await message.answer(f"{warning_text}✅ Ваша ставка отправлена администратору на рассмотрение!", reply_markup=get_main_reply_markup(message.from_user))
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
        f"🎯 **Груз забран перевозчиком через бота!**\n\n"
        f"🆔 Груз #{cargo_id} | Маршрут: {route_str}\n"
        f"📅 Дата: {date_str}\n"
        f"💰 Ставка: {price_str} | 🚛 Забрано авто: {requested_cars}\n\n"
        f"{carrier_text}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
    except Exception:
        pass
        
    await state.clear()
    await message.answer(f"{warning_text}✅ Груз закреплен за вами! Просмотреть его можно в разделе «Забранные грузы».", reply_markup=get_main_reply_markup(message.from_user))
    # ==================== WEB APP БЭКЕНД И REST API ====================

async def get_loads_api(request):
    country = request.query.get('country')
    raw_uid = request.query.get('user_id')
    user_id = int(raw_uid) if raw_uid and raw_uid.isdigit() else 0

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT subscriptions, COALESCE(verification_status, 'UNVERIFIED') FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    
    user_subs = []
    is_verified = False
    if u_row:
        if u_row[0]:
            user_subs = [s.strip() for s in u_row[0].split(',') if s.strip()]
        is_verified = (u_row[1] == 'VERIFIED')

    if user_id and not user_subs and not (country and country != 'ALL'):
        conn.close()
        return web.json_response({"loads": [], "is_verified": is_verified})

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
        c_date = parse_cargo_date(r[2])
        is_today = bool(c_date and c_date == msk_today)
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
    
    try:
        cursor.execute("""
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
                   COALESCE(cd.is_unloaded, 0)
            FROM confirmed_deals cd
            LEFT JOIN loads l ON cd.load_id = l.load_id
            WHERE cd.user_id = ?
            ORDER BY cd.id DESC
        """, (user_id,))
        confirmed_rows = cursor.fetchall()
        
        cursor.execute("""
            SELECT b.bid_id, b.load_id, l.date, l.route, b.cars, 
                   COALESCE(b.counter_rate, b.rate) as price,
                   COALESCE(l.details, ''), b.status,
                   COALESCE(l.car_type, 'Тент/реф'),
                   COALESCE(l.cargo_type, 'ТНП'),
                   COALESCE(l.weight, 'до 22т'),
                   0 as docs_submitted
            FROM bids b
            JOIN loads l ON b.load_id = l.load_id
            WHERE b.user_id = ? AND b.status IN ('PENDING', 'COUNTER')
            ORDER BY b.bid_id DESC
        """, (user_id,))
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
        c_date = parse_cargo_date(date_str)
        is_today = bool(c_date and c_date == msk_today)
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
        deal_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub, docs_stat, miss_docs, tr_plate, trl_plate, drv_name, drv_phone, unl_date, is_unl = r

        c_date = parse_cargo_date(date_str)
        is_today = bool(c_date and c_date == msk_today)
        has_submitted_docs = bool(docs_sub) or (docs_stat and docs_stat != 'NONE')
        is_transit = bool(c_date and msk_today > c_date and not is_unl and has_submitted_docs)

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
            "unload_date": unl_date or ""
        })
            
    return web.json_response({"deals": deals})

async def direct_upload_docs_api(request):
    try:
        reader = await request.multipart()
        deal_id = None
        user_id = 0
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

        cursor.execute("UPDATE confirmed_deals SET docs_status = 'PROCESSING' WHERE id = ? AND user_id = ?", (deal_id, user_id))
        conn.commit()

        cursor.execute("""
            SELECT cd.docs_submitted, cd.last_truck_plate, cd.last_driver_name,
                   l.cargo_type, l.details, l.text, cd.last_trailer_plate, cd.driver_phone, 
                   cd.route, cd.date, cd.price
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

        user_info = format_carrier_info(user_id)

        if raw_files:
            contents = ["Изучи документы. ФИО извлекай СТРОГО из ПАСПОРТА. Номер телефона: " + (phone_input or "Нет")]
            for fname, content in raw_files:
                mime = detect_mime_type(content, fname)
                contents.append(genai_types.Part.from_bytes(data=content, mime_type=mime))

            ai_formatted_data, raw_json = await process_docs_bytes_with_ai(contents, phone_input, is_polyethylene=is_polyethylene, route_str=route_str)
            
            t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
            tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
            d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
            p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}
            l_data = d_data.get("license") if isinstance(d_data.get("license"), dict) else {}

            new_truck_plate = (t_data.get("plate") or "Не распознан").strip().upper()
            if new_truck_plate in ["НЕ РАСПОЗНАН", ""] and prev_truck_plate:
                new_truck_plate = prev_truck_plate

            new_trailer_plate = (tr_data.get("plate") or "Не распознан").strip().upper()
            if new_trailer_plate in ["НЕ РАСПОЗНАН", ""] and prev_trailer_plate:
                new_trailer_plate = prev_trailer_plate

            p_full_name = (p_data.get("full_name") or "").strip()
            if p_num := p_data.get("number"):
                if p_num != "Не распознан" and p_full_name:
                    new_driver_short_name = extract_surname_and_name(p_full_name)
                else:
                    new_driver_short_name = prev_driver_short_name or "Не распознан"
            else:
                new_driver_short_name = prev_driver_short_name or "Не распознан"

            extracted_phone = phone_input or (d_data.get("phones") or "").strip()
            new_driver_phone = extracted_phone if (extracted_phone and extracted_phone != "Не указан") else (prev_driver_phone or "Не указан")
        else:
            raw_json = {}
            new_truck_plate = prev_truck_plate or "НЕ РАСПОЗНАН"
            new_trailer_plate = prev_trailer_plate or "НЕ РАСПОЗНАН"
            new_driver_short_name = prev_driver_short_name or "Не распознан"
            new_driver_phone = phone_input or prev_driver_phone or "Не указан"

            ai_formatted_data = (
                f"Тягач: {new_truck_plate}\n"
                f"Прицеп: {new_trailer_plate}\n"
                f"Водитель: {new_driver_short_name}\n"
                f"Номера телефонов: {new_driver_phone}\n"
                f"Паспорт: Документы не загружены\n"
                f"Водительское: Документы не загружены"
            )

        missing_items = []

        p_num = raw_json.get("driver", {}).get("passport", {}).get("number")
        if not p_num or p_num == "Не распознан" or new_driver_short_name == "Не распознан":
            missing_items.append("Паспорт водителя")

        l_num = raw_json.get("driver", {}).get("license", {}).get("number")
        if not l_num or l_num == "Не распознан":
            missing_items.append("Водительское удостоверение")

        if new_truck_plate in ["НЕ РАСПОЗНАН", "", "—"]: missing_items.append("Техпаспорт тягача")
        if new_trailer_plate in ["НЕ РАСПОЗНАН", "", "—"]: missing_items.append("Техпаспорт прицепа")
        if new_driver_phone in ["Не указан", "", "—"]: missing_items.append("Номер телефона")

        docs_status = "FULL" if not missing_items else "PARTIAL"
        missing_str = ", ".join(missing_items)

        cursor.execute("""
            UPDATE confirmed_deals 
            SET docs_submitted = 1, docs_status = ?, missing_docs = ?, 
                last_truck_plate = ?, last_trailer_plate = ?, last_driver_name = ?, driver_phone = ?
            WHERE id = ? AND user_id = ?
        """, (docs_status, missing_str, new_truck_plate, new_trailer_plate, new_driver_short_name, new_driver_phone, deal_id, user_id))
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
        await bot.send_message(chat_id=DOCS_CHANNEL_ID, text=admin_msg, parse_mode="Markdown")

        if raw_files:
            clean_name = re.sub(r'[^\w\s-]', '', new_driver_short_name)
            clean_truck = re.sub(r'[^\w]', '', new_truck_plate)
            pdf_filename = f"{clean_name} - {clean_truck}.pdf"
            file_caption = f"{date_str} {route_str}"

            pdf_bytes = await generate_single_pdf_bytes(raw_files, route_str, date_str, price_str, user_info, ai_formatted_data)
            if pdf_bytes:
                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(chat_id=DOCS_CHANNEL_ID, document=pdf_file, caption=file_caption)

        return web.json_response({"status": "success", "docs_status": docs_status})
    except Exception as e:
        logging.error(f"Error in direct_upload_docs_api: {e}", exc_info=True)
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
        cursor.execute("SELECT verification_status, company, name, phone FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        v_status = row[0] if row else 'UNVERIFIED'

        if v_status == 'VERIFIED':
            cursor.execute("""
                UPDATE users SET 
                    subscriptions = ?,
                    pending_company = ?,
                    pending_name = ?,
                    pending_phone = ?
                WHERE user_id = ?
            """, (subscriptions, company, name, phone, user_id))
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
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"verify_approve_{user_id}"),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"verify_reject_{user_id}")
        )
        builder.row(
            types.InlineKeyboardButton(text="✏️ Отредактировать", callback_data=f"verify_edit_{user_id}")
        )

        msg_text = (
            f"🛡️ **ЗАПРОС НА ВЕРИФИКАЦИЮ**\n\n"
            f"{carrier_link}\n"
            f"🏢 Компания: {row[0]}\n"
            f"👤 ФИО: {row[1]}\n"
            f"📞 Телефон: {row[2]}"
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
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT company, name, phone, pending_company, pending_name, pending_phone FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()

        if not row or not row[3]:
            return web.json_response({"error": "Нет сохраненных изменений"}, status=400)

        carrier_link = format_carrier_info(user_id)

        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"profile_approve_{user_id}"),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"profile_reject_{user_id}")
        )

        msg_text = (
            f"📝 **ЗАПРОС НА ИЗМЕНЕНИЕ ДАННЫХ ПРОФИЛЯ**\n\n"
            f"{carrier_link}\n\n"
            f"🔴 **Было:**\n"
            f"• Компания: {row[0]}\n• ФИО: {row[1]}\n• Тел: {row[2]}\n\n"
            f"🟢 **Стало:**\n"
            f"• Компания: {row[3]}\n• ФИО: {row[4]}\n• Тел: {row[5]}"
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
            f"🎯 **Груз забран из Web App!**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"📅 Дата: {date_str} | 💰 Ставка: {price_str} | 🚛 Забрано: {requested_cars} авто\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})

    elif action == 'bid':
        cursor.execute("""
            INSERT INTO bids (load_id, user_id, cars, rate, comment)
            VALUES (?, ?, ?, ?, ?)
        """, (load_id, user_id, requested_cars, proposed_price, carrier_comment or 'Своя ставка'))
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        add_notification(user_id, "Ставка отправлена", f"Ваша ставка {proposed_price} по грузу {route_str} отправлена логисту.")

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="🔀 Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(
            types.InlineKeyboardButton(text="💡 Своя ставка", callback_data=f"counter_bid_{bid_id}"),
            types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{bid_id}")
        )

        admin_notification = (
            f"💰 **Новая ставка из Web App!**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"💵 Ставка: {proposed_price} | 🚛 Авто: {requested_cars}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, reply_markup=admin_builder.as_markup(), parse_mode="Markdown")
        except Exception:
            pass

        return web.json_response({"status": "success"})

    conn.close()
    return web.json_response({"error": "Неизвестное действие"}, status=400)

async def admin_verify_pass_api(request):
    try:
        data = await request.json()
        password = data.get('password', '')
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'admin_password'")
        row = cursor.fetchone()
        conn.close()
        db_pass = row[0] if row else '123456'
        if password == db_pass:
            return web.json_response({"status": "success"})
        return web.json_response({"error": "Неверный пароль"}, status=403)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_get_carriers_api(request):
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
        u_id = int(data.get('user_id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM users WHERE user_id = ?", (u_id,))
        row = cursor.fetchone()
        new_status = 'BLOCKED' if row and row[0] == 'ACTIVE' else 'ACTIVE'
        cursor.execute("UPDATE users SET status = ? WHERE user_id = ?", (new_status, u_id))
        conn.commit()
        conn.close()
        return web.json_response({"status": "success", "new_status": new_status})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_get_loads_api(request):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    query = """
        SELECT l.load_id, l.route, l.date, l.cars_count, l.price, l.status,
               COALESCE(l.car_type, 'Тент/реф'), COALESCE(l.cargo_type, 'ТНП'),
               COALESCE(l.weight, 'до 22т'), COALESCE(l.admin_comment, ''),
               GROUP_CONCAT(u.company || ' (' || u.name || ')', '; ') as carriers
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
        "admin_comment": r[9], "carrier_info": r[10] or "Не забран"
    } for r in rows]
    return web.json_response({"loads": loads})

async def admin_edit_load_api(request):
    try:
        data = await request.json()
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

async def admin_get_confirmed_deals_api(request):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, cd.details, cd.user_id,
               COALESCE(u.company, 'Не указана'), COALESCE(u.name, 'Пользователь'), COALESCE(u.phone, 'Не указан')
        FROM confirmed_deals cd
        LEFT JOIN users u ON cd.user_id = u.user_id
        ORDER BY cd.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    deals = [{"deal_id": r[0], "load_id": r[1], "date": r[2], "route": r[3], "cars": r[4], "price": r[5], "details": r[6], "user_id": r[7], "company": r[8], "name": r[9], "phone": r[10]} for r in rows]
    return web.json_response({"deals": deals})

async def admin_edit_deal_api(request):
    try:
        data = await request.json()
        deal_id = int(data.get('deal_id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE confirmed_deals 
            SET route = ?, date = ?, price = ?, cars = ?
            WHERE id = ?
        """, (data.get('route'), data.get('date'), format_custom_rate(data.get('price')), int(data.get('cars', 1)), deal_id))
        conn.commit()
        conn.close()
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_cancel_deal_api(request):
    try:
        data = await request.json()
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

async def serve_index(request):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        index_path = os.path.join(base_dir, "static", "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return web.Response(text=html_content, content_type='text/html')
    except Exception as e:
        return web.Response(text=f"Error loading index.html: {e}", status=500)

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

async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/webapp", serve_index)
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
    app.router.add_post("/api/submit_docs_prompt", submit_docs_prompt_api)
    app.router.add_post("/api/my_loads/direct_upload", direct_upload_docs_api)

    app.router.add_post("/api/admin/verify_pass", admin_verify_pass_api)
    app.router.add_get("/api/admin/carriers", admin_get_carriers_api)
    app.router.add_post("/api/admin/carrier_status", admin_toggle_carrier_status_api)
    app.router.add_get("/api/admin/loads", admin_get_loads_api)
    app.router.add_post("/api/admin/edit_load", admin_edit_load_api)
    app.router.add_post("/api/admin/toggle_load_active", admin_toggle_load_active_api)
    app.router.add_post("/api/admin/hard_delete_load", admin_hard_delete_load_api)
    app.router.add_get("/api/admin/confirmed_deals", admin_get_confirmed_deals_api)
    app.router.add_post("/api/admin/edit_deal", admin_edit_deal_api)
    app.router.add_post("/api/admin/cancel_deal", admin_cancel_deal_api)
    
    app.on_startup.append(webserver_on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"✅ Web server started on port {port}")
    await asyncio.Event().wait()

async def main():
    await asyncio.gather(run_bot(), web_server())

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

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
from pydantic import BaseModel, Field
from PIL import Image, ImageOps
from datetime import datetime, date, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types.web_app_info import WebAppInfo

# Импорт официального библиотеки google-genai
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

# Безопасный импорт ReportLab для генерации PDF
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
    logging.critical("❌ ОШИБКА: Не задана переменная окружения BOT_TOKEN на Render!")

RENDER_URL = os.getenv("RENDER_URL", "https://your-app-name.onrender.com")
ADMIN_ID = os.getenv("ADMIN_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Инициализация Gemini Client с использованием SDK google-genai
gemini_client = None
if GEMINI_API_KEY and HAS_GENAI:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("✅ Gemini API Client (google-genai) успешно инициализирован.")
    except Exception as e:
        logging.error(f"❌ Ошибка инициализации Gemini Client: {e}")
elif not HAS_GENAI:
    logging.warning("⚠️ Пакет google-genai не установлен. Установите с помощью: pip install google-genai")

ADMIN_CHANNEL_ID_RAW = os.getenv("ADMIN_CHANNEL_ID", "-1004271518848")
try:
    ADMIN_CHANNEL_ID = int(ADMIN_CHANNEL_ID_RAW)
except ValueError:
    ADMIN_CHANNEL_ID = -1004271518848

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


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            company TEXT,
            name TEXT,
            phone TEXT,
            subscriptions TEXT,
            status TEXT DEFAULT 'ACTIVE'
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
            docs_submitted INTEGER DEFAULT 0
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
        "ALTER TABLE confirmed_deals ADD COLUMN missing_docs TEXT DEFAULT ''"
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

class AdminEditStates(StatesGroup):
    waiting_for_new_cargo_text = State()

class AdminCounterStates(StatesGroup):
    waiting_for_counter_rate = State()

class AdminPassState(StatesGroup):
    waiting_for_new_pass = State()


# ==================== ВАЛЮТЫ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="📱 Вызвать меню"))
    return builder.as_markup(resize_keyboard=True)

def get_chat_menu_inline_markup():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🌍 Выбор направлений", callback_data="menu_directions"))
    builder.row(types.InlineKeyboardButton(text="👤 Личный кабинет", callback_data="menu_profile"))
    builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="menu_active"))
    builder.row(types.InlineKeyboardButton(text="🚚 Забранные грузы", callback_data="menu_my_deals"))
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

def is_auction_price(price_str: str) -> bool:
    if not price_str:
        return True
    return 'торг' in str(price_str).lower()

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
                
            # Проверяем валидность часов и минут, чтобы избежать ValueError при "до 25 тонн"
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

        if '-' in line or '→' in line:
            clean_route = date_pattern.sub('', line)
            clean_route = cars_pattern.sub('', clean_route)
            clean_route = re.sub(
                r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|UZS|сум|сумм|руб|долл|доллар|\$|€|тг)', 
                '', 
                clean_route, 
                flags=re.IGNORECASE
            )
            clean_route = clean_route.strip(' ,.-')
            if clean_route:
                route_str = clean_route.replace(' - ', ' → ').replace('-', '→')

    if not date_str:
        date_str = "Дата не указана"
    if not route_str:
        route_str = clean_lines[0] if clean_lines else "Маршрут не указан"
    if not price_str:
        price_str = "Торги"
    if not cars_str:
        cars_str = "1"

    route_str = re.sub(r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|UZS|сум|руб|долл|\$|€|тг).*$', '', route_str, flags=re.IGNORECASE)
    route_str = re.sub(r'\s*,\s*,\s*', ', ', route_str)
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
        if '-' in line or '→' in line or (date_pattern.search(line) and not parts):
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
        if date_pattern.search(line) and ('-' in line or '→' in line):
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

def format_carrier_info(user_id: int, username: str, full_name: str) -> str:
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    user_link = f"@{username}" if username else f"tg://user?id={user_id}"
    comp = row[0] if row and row[0] and row[0] != 'Не указана' else "Не указана"
    name = row[1] if row and row[1] else full_name
    phone = row[2] if row and row[2] else "Не указан"

    return f"👤 Перевозчик: {user_link}\n🏢 {comp}, {name} {phone}"

def build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, is_closed=False):
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
    return card

async def update_cargo_messages_for_all_users(cargo_id: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, route, price, cars_count, details, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return
        
    date_str, route_str, price_str, cars_str, details_text, status = row
    is_closed = (status in ['CLOSED', 'EXPIRED'])
    new_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, is_closed=is_closed)
    
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

    cursor.execute("SELECT date, route, price, cars_count, details, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return
        
    date_str, route_str, price_str, cars_str, details_text, status = row
    is_closed = (status in ['CLOSED', 'EXPIRED'])
    formatted_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, is_closed=is_closed)
    
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


# ==================== ИИ GEMINI С ИСПОЛЬЗОВАНИЕМ GOOGLE-GENAI SDK И ГЕНЕРАЦИЯ PDF ====================

from typing import Optional

# ==================== СХЕМЫ PYDANTIC И ИИ-АГЕНТ ====================

class VehicleDetails(BaseModel):
    brand: Optional[str] = Field(default="Не распознан", description="ТОЛЬКО марка ТС БЕЗ модели! Внимательно проверяй орфографию (например: WIELTON, SCHMITZ, KRONE, KÖGEL, KÄSSBOHRER, DAF, VOLVO, SCANIA, MAN, MERCEDES-BENZ, SITRAK, MAZ, KAMAZ)")
    model: Optional[str] = Field(default="", description="ТОЛЬКО модель ТС без марки (например: XF 105, FH13, NS34, S.KO)")
    plate: Optional[str] = Field(default="Не распознан", description="Гос. номер ТС")
    vin: Optional[str] = Field(default="Не распознан", description="VIN номер (17 символов)")
    country: Optional[str] = Field(default="Не распознана", description="Страна регистрации СТРОГО НА РУССКОМ ЯЗЫКЕ (например: Узбекистан, Казахстан, Беларусь, Россия)")

class DocumentDetails(BaseModel):
    number: Optional[str] = Field(default="Не распознан", description="Номер документа")
    issue_date: Optional[str] = Field(default="Не распознана", description="Дата выдачи")
    expiry_date: Optional[str] = Field(default="Не указана", description="Дата окончания / срок действия документа (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)")
    authority: Optional[str] = Field(default="Не распознан", description="Орган выдачи документа в оригинальном написании с документа")
    country: Optional[str] = Field(default="Не распознана", description="Страна выдачи документа СТРОГО НА РУССКОМ ЯЗЫКЕ (например: Беларусь, Узбекистан, Казахстан, Россия)")

class DriverDetails(BaseModel):
    full_name: Optional[str] = Field(default="Не распознан", description="ФИО водителя в оригинальном написании (не переводить)")
    birth_date: Optional[str] = Field(default="Не распознана", description="Дата рождения водителя")
    phones: Optional[str] = Field(default="Не указан", description="Номера телефонов (+7... первым, остальные через '/')")
    passport: Optional[DocumentDetails] = None
    license: Optional[DocumentDetails] = None

class ImageClassification(BaseModel):
    image_index: int = Field(description="Порядковый номер изображения или страницы PDF, начиная с 0")
    category: str = Field(
        description="Категория: 'passport_front', 'passport_back', 'license_front', 'license_back', 'truck_front', 'trailer_front', 'truck_back', 'trailer_back', 'other'"
    )

class FullCargoSubmission(BaseModel):
    truck: Optional[VehicleDetails] = None
    trailer: Optional[VehicleDetails] = None
    driver: Optional[DriverDetails] = None
    image_roles: Optional[list[ImageClassification]] = None
    

async def process_docs_with_ai(photos_file_ids, doc_file_ids, text_notes, is_polyethylene=False):
    """Распознает документы через Gemini и возвращает структурированный словарь + отсортированные файлы."""
    fallback_text = (
        "Тягач: Не распознан\n"
        "Прицеп: Не распознан\n"
        "Водитель: Не распознан\n"
        f"Номера телефонов: {text_notes or 'Не указан'}\n"
        "Паспорт: Не распознан\n"
        "Водительское: Не распознано"
    )

    all_files = (photos_file_ids or []) + (doc_file_ids or [])

    if not GEMINI_API_KEY or not gemini_client or not HAS_GENAI or not all_files:
        return fallback_text, all_files, {}

    contents = []

    if text_notes:
        contents.append(f"Заметки и номера от водителя: {text_notes}")

    for file_id in all_files:
        try:
            file_info = await bot.get_file(file_id)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            file_bytes = buf.getvalue()

            mime_type = "image/jpeg"
            if file_info.file_path:
                fp_lower = file_info.file_path.lower()
                if fp_lower.endswith('.png'):
                    mime_type = "image/png"
                elif fp_lower.endswith('.pdf'):
                    mime_type = "application/pdf"

            contents.append(genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        except Exception as e:
            logging.error(f"Error downloading document for AI: {e}")

    if not contents:
        return fallback_text, all_files, {}

    system_prompt = (
        "Ты — эксперт логистической компании по распознаванию международных документов водителей и ТС.\n"
        "1. Распознавай данные с 100% точностью!\n"
        "2. Марки ТС: строго разделяй марку (brand) и модель (model). Внимательно проверяй орфографию! "
        "Примеры марок прицепов: WIELTON (СТРОГО WIELTON, не Welton!), SCHMITZ CARGOBULL, KRONE, KÖGEL, KÄSSBOHRER, SCHWARZMÜLLER, FLIEGL, TONAR, BODEX, GRUNWALD, MAZ. "
        "Примеры марок тягачей: DAF, VOLVO, SCANIA, MAN, MERCEDES-BENZ, IVECO, RENAULT, SITRAK, FAW, HOWO, SHACMAN, KAMAZ, MAZ.\n"
        "3. Страны регистрации и страны выдачи: ВСЕГДА ПИШИ СТРОГО НА РУССКОМ ЯЗЫКЕ (например: Беларусь, Узбекистан, Казахстан, Россия, Кыргызстан, Грузия, Азербайджан, Армения).\n"
        "4. Даты окончания документов: Обязательно извлекай expiry_date (срок действия / дата окончания) для паспорта и водительских прав при наличии.\n"
        "5. Для КАЖДОГО фото или страницы PDF укажи image_index (0, 1, 2...) и категорию (category): "
        "'passport_front', 'passport_back', 'license_front', 'license_back', 'truck_front', 'trailer_front', 'truck_back', 'trailer_back', 'other'.\n"
        "6. ФИО водителя и орган выдачи паспорта пиши строго в оригинальном написании с документа."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=FullCargoSubmission,
        temperature=0.1
    )

    response = None
    models_to_try = [
        "gemini-3.6-flash",
        "gemini-3.0-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]

    for model_name in models_to_try:
        try:
            if hasattr(gemini_client, 'aio'):
                response = await gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
            else:
                response = await asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config
                )
            if response and response.text:
                logging.info(f"✅ Gemini ответил на модели {model_name}")
                break
        except Exception as e:
            logging.warning(f"⚠️ Модель {model_name} недоступна: {e}")

    if response and response.text:
        try:
            import json
            raw_json = json.loads(response.text)
            
            t = raw_json.get("truck") or {}
            tr = raw_json.get("trailer") or {}
            d = raw_json.get("driver") or {}
            p = d.get("passport") or {}
            l = d.get("license") or {}

            # Форматирование текста под категорию груза (Полиэтилен vs Обычный)
            if is_polyethylene:
                # ДЛЯ ПОЛИЭТИЛЕНА: Берём ТОЛЬКО марку (без модели!)
                truck_brand = (t.get('brand') or t.get('brand_model') or 'Не распознан').strip()
                trailer_brand = (tr.get('brand') or tr.get('brand_model') or 'Не распознан').strip()

                formatted_output = (
                    f"ТС (марка, г/н, страна регистрации): {truck_brand}, {t.get('plate') or 'Не распознан'}, {t.get('country') or 'Не распознана'}\n"
                    f"Прицеп (марка, г/н, страна регистрации): {trailer_brand}, {tr.get('plate') or 'Не распознан'}, {tr.get('country') or 'Не распознана'}\n"
                    f"ФИО водителя: {d.get('full_name') or 'Не распознан'}\n"
                    f"Тел (росс): {d.get('phones') or text_notes or 'Не указан'}\n"
                    f"Водительское удостоверение (№, когда и кем выдано): № {l.get('number') or 'Не распознан'} от {l.get('issue_date') or 'Не распознана'}г. {l.get('country') or 'Не распознана'}\n"
                    f"Паспорт (серия, №, когда и кем выдан): № {p.get('number') or 'Не распознан'} выдан {p.get('issue_date') or 'Не распознана'}г. {p.get('authority') or 'Не распознан'}"
                )
            else:
                truck_str = f"{t.get('brand_model') or 'Не распознан'}, {t.get('plate') or 'Не распознан'}, VIN: {t.get('vin') or 'Не распознан'}, {t.get('country') or 'Не распознана'}"
                trailer_str = f"{tr.get('brand_model') or 'Не распознан'}, {tr.get('plate') or 'Не распознан'}, VIN: {tr.get('vin') or 'Не распознан'}, {tr.get('country') or 'Не распознана'}"
                driver_str = f"{d.get('full_name') or 'Не распознан'}, дата рождения: {d.get('birth_date') or 'Не распознана'}"
                phones_str = d.get("phones") or text_notes or "Не указан"
                passport_str = f"№ {p.get('number') or 'Не распознан'}, выдан {p.get('issue_date') or 'Не распознана'}, {p.get('authority') or 'Не распознан'}, {p.get('country') or 'Не распознана'}"
                license_str = f"№ {l.get('number') or 'Не распознан'}, выдано {l.get('issue_date') or 'Не распознана'}, {l.get('authority') or 'Не распознан'}, {l.get('country') or 'Не распознана'}"

                formatted_output = (
                    f"Тягач: {truck_str}\n"
                    f"Прицеп: {trailer_str}\n"
                    f"Водитель: {driver_str}\n"
                    f"Номера телефонов: {phones_str}\n"
                    f"Паспорт: {passport_str}\n"
                    f"Водительское: {license_str}"
                )

            # ----- СОРТИРОВКА ФОТО/СТРАНИЦ ПО ВАШЕМУ ПОРЯДКУ -----
            priority_map = {
                "passport_front": 1,
                "passport_back": 2,
                "license_front": 3,
                "license_back": 4,
                "truck_front": 5,
                "trailer_front": 6,
                "truck_back": 7,
                "trailer_back": 8,
                "other": 99
            }

            classified_file_priority = {}
            for role in raw_json.get("image_roles") or []:
                idx = role.get("image_index")
                cat = role.get("category", "other")
                if idx is not None and 0 <= idx < len(all_files):
                    classified_file_priority[all_files[idx]] = priority_map.get(cat, 99)

            sorted_files = sorted(all_files, key=lambda fid: classified_file_priority.get(fid, 99))

            return formatted_output, sorted_files, raw_json
        except Exception as e:
            logging.error(f"Error parsing Gemini response JSON: {e}")

    return fallback_text, all_files, {}


async def sort_pdf_pages(doc_file_id, raw_json) -> io.BytesIO:
    """Сортирует страницы внутри исходного PDF по правильному порядку."""
    try:
        from pypdf import PdfReader, PdfWriter
        file_info = await bot.get_file(doc_file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)

        reader = PdfReader(buf)
        total_pages = len(reader.pages)

        priority_map = {
            "passport_front": 1, "passport_back": 2,
            "license_front": 3, "license_back": 4,
            "truck_front": 5, "trailer_front": 6,
            "truck_back": 7, "trailer_back": 8,
            "other": 99
        }

        page_priorities = {}
        image_roles = raw_json.get("image_roles") if isinstance(raw_json, dict) else []
        for role in (image_roles or []):
            if isinstance(role, dict):
                idx = role.get("image_index")
                cat = role.get("category", "other")
                if idx is not None and 0 <= idx < total_pages:
                    page_priorities[idx] = priority_map.get(cat, 99)

        sorted_indices = sorted(range(total_pages), key=lambda i: page_priorities.get(i, 99))

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


def crop_document_margins(img: Image.Image) -> Image.Image:
    """Обрезает лишний стол/фон вокруг фотографий документов."""
    try:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        from PIL import ImageChops
        # Сравниваем изображение с угловым фоном для выявления лишних полей
        bg = Image.new(img.mode, img.size, img.getpixel((0,0)))
        diff = ImageChops.difference(img, bg)
        diff = ImageChops.add(diff, diff, 2.0, -100)
        bbox = diff.getbbox()
        if bbox:
            w, h = img.size
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            # Обрезаем только если документ занимает разумную область и не сжимается слишком сильно
            if bw > w * 0.4 and bh > h * 0.4:
                return img.crop(bbox)
    except Exception:
        pass
    return img

async def create_pdf_report_with_images(route: str, date_str: str, price: str, carrier_info: str, ai_text: str, photo_ids: list) -> io.BytesIO:
    """Создает PDF из фотографий: правильно поворачивает и обрезает лишний стол/фон."""
    buffer = io.BytesIO()
    images = []

    for pid in photo_ids:
        try:
            file_info = await bot.get_file(pid)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            buf.seek(0)

            img = Image.open(buf)
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')

            # Для фото документов вырезаем лишний фон
            img = crop_document_margins(img)
            images.append(img)
        except Exception as e:
            logging.error(f"Error converting file {pid} for PDF: {e}")

    if images:
        images[0].save(
            buffer, 
            format="PDF", 
            save_all=True, 
            append_images=images[1:]
        )
        buffer.seek(0)

    return buffer


# ==================== АВТО-ОЧИСТКА ГРУЗОВ (ТОЛЬКО ПО ТАЙМЕРУ МСК) ====================
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
            INSERT INTO users (user_id, company, name, phone, subscriptions, status)
            VALUES (?, 'Не указана', ?, 'Не указан', '', 'ACTIVE')
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
        "Если вы хотите продолжить просто в самом чате — такая возможность тоже есть:",
        reply_markup=get_chat_menu_inline_markup()
    )

@dp.message(F.text == "📱 Вызвать меню")
@dp.message(F.state == None)
async def cmd_show_menu_button(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📋 **Главное меню:**", reply_markup=get_chat_menu_inline_markup(), parse_mode="Markdown")

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
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, date, route, price, cars_count, details FROM loads WHERE status = 'ACTIVE' ORDER BY load_id DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer("📦 Актуальных грузов пока нет.")
        await callback.answer()
        return

    await callback.message.answer("📦 **Список актуальных грузов:**", parse_mode="Markdown")
    for load_id, date_str, route_str, price_str, cars_str, details_str in rows:
        card = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_str)
        builder = InlineKeyboardBuilder()
        web_app_url = f"{RENDER_URL}/webapp"
        builder.row(types.InlineKeyboardButton(text="🚀 Открыть в Web App", web_app=WebAppInfo(url=web_app_url)))
        
        try:
            await callback.message.answer(card, reply_markup=builder.as_markup())
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await callback.answer()

@dp.callback_query(F.data == "menu_my_deals")
async def callback_menu_my_deals(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, route, cars, price, details FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC", (user_id,))
    confirmed_loads = cursor.fetchall()
    conn.close()
    
    if not confirmed_loads:
        await callback.answer("У вас пока нет забранных грузов.", show_alert=True)
        return
        
    await callback.message.answer("📦 **Ваши забранные грузы:**", parse_mode="Markdown")
    for date_str, route_str, cars_count, price_str, details_text in confirmed_loads:
        card_text = (
            f"📍 {date_str} | {route_str}\n"
            f"💰 {price_str} | 🚚 {cars_count} авто"
        )
        if details_text:
            card_text += f"\n📦 {details_text}"
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
    
    user_subs = row[0].split(",") if row and row[0] else []
    
    builder = InlineKeyboardBuilder()
    for direction in CHANNELS.keys():
        mark = "✅ " if direction in user_subs else "   "
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
    
    current_subs = row[0].split(",") if row and row[0] else []
    if direction in current_subs:
        current_subs.remove(direction)
    else:
        current_subs.append(direction)
        
    new_subs_str = ",".join(current_subs)
    cursor.execute("UPDATE users SET subscriptions = ? WHERE user_id = ?", (new_subs_str, user_id))
    conn.commit()
    conn.close()
    
    builder = InlineKeyboardBuilder()
    for d in CHANNELS.keys():
        mark = "✅ " if d in current_subs else "   "
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
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    comp = row[0] if row and row[0] else "Не указана"
    name = row[1] if row and row[1] else "Не указано"
    phone = row[2] if row and row[2] else "Не указан"

    text = (
        f"👤 **Личный кабинет**\n\n"
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
        reply_markup=get_chat_menu_inline_markup(),
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


# ==================== ХЭНДЛЕРЫ ПОДАЧИ ДОКУМЕНТОВ ДЛЯ ИИ ====================

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
    await message.answer("❌ Подача данных на загрузку отменена.", reply_markup=get_main_reply_markup())

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
        await message.answer("⚠️ Вы не прислали ни одного фото/файла или телефона.")
        return

# Проверяем, не полиэтилен ли это
    is_polyethylene = False
    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.cargo_type, l.details, l.text 
            FROM confirmed_deals cd 
            LEFT JOIN loads l ON cd.load_id = l.load_id 
            WHERE cd.id = ?
        """, (deal_id,))
        row = cursor.fetchone()
        if row:
            cargo_info = f"{row[0] or ''} {row[1] or ''} {row[2] or ''}".lower()
            if "полиэтилен" in cargo_info or "polyethylene" in cargo_info:
                is_polyethylene = True
        conn.close()

    # Gemini распознает и сортирует данные
    ai_formatted_data, sorted_files, raw_json = await process_docs_with_ai(photos, documents, notes, is_polyethylene=is_polyethylene)

    # Проверяем полноту внесенных данных
    def is_doc_expired_or_expiring_soon(expiry_str: str, threshold_days: int = 15) -> bool:
    """Возвращает True, если документ просрочен или до конца осталось менее threshold_days дней."""
    if not expiry_str or str(expiry_str).lower().strip() in ["не распознана", "не указана", "бессрочно", "бессрочный"]:
        return False
    
    # Регулярные выражения для формата ДД.ММ.ГГГГ / ДД-ММ-ГГГГ и ISO ГГГГ-ММ-ДД
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

    # Проверяем полноту и валидность внесенных данных
    missing_items = []
    d_data = raw_json.get("driver") if isinstance(raw_json.get("driver"), dict) else {}
    t_data = raw_json.get("truck") if isinstance(raw_json.get("truck"), dict) else {}
    tr_data = raw_json.get("trailer") if isinstance(raw_json.get("trailer"), dict) else {}
    p_data = d_data.get("passport") if isinstance(d_data.get("passport"), dict) else {}
    l_data = d_data.get("license") if isinstance(d_data.get("license"), dict) else {}

    # Проверка Паспорта
    p_num = p_data.get("number")
    p_exp = p_data.get("expiry_date")
    if not p_num or p_num == "Не распознан":
        missing_items.append("Паспорт водителя")
    elif is_doc_expired_or_expiring_soon(p_exp, 15):
        missing_items.append("Паспорт водителя (просрочен/истекает)")

    # Проверка Водительского удостоверения
    l_num = l_data.get("number")
    l_exp = l_data.get("expiry_date")
    if not l_num or l_num == "Не распознан":
        missing_items.append("Водительское удостоверение")
    elif is_doc_expired_or_expiring_soon(l_exp, 15):
        missing_items.append("Водительское удостоверение (просрочено/истекает)")

    # Проверка техпаспортов
    if not t_data.get("plate") or t_data.get("plate") == "Не распознан":
        missing_items.append("Техпаспорт тягача")
    if not tr_data.get("plate") or tr_data.get("plate") == "Не распознан":
        missing_items.append("Техпаспорт прицепа")
    
    # Проверка телефона
    phone_val = d_data.get("phones") or notes
    if not phone_val or phone_val == "Не указан":
        missing_items.append("Номер телефона")

    if not missing_items:
        docs_status = "FULL"
        missing_docs_str = ""
    elif len(missing_items) < 5:
        docs_status = "PARTIAL"
        missing_docs_str = ", ".join(missing_items)
    else:
        docs_status = "NONE"
        missing_docs_str = ""

    # Фиксируем отправку и статус строго для ТЕКУЩЕЙ конкретной сделки
    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE confirmed_deals 
            SET docs_submitted = 1, docs_status = ?, missing_docs = ? 
            WHERE id = ? AND user_id = ?
        """, (docs_status, missing_docs_str, deal_id, user_id))
        conn.commit()
        conn.close()
    carrier_text = format_carrier_info(user_id, user_obj.username, user_obj.full_name)

    admin_msg = (
        f"📅 {date_str} | 📍 {route_str}\n"
        f"💰 {price_str}\n\n"
        f"{carrier_text}\n\n"
        f"{ai_formatted_data}"
    )

    # Генерация наименования файла: (Фамилия Имя водителя - ГосТягач/ГосПрицеп.pdf)
    d_data = raw_json.get("driver") or {}
    t_data = raw_json.get("truck") or {}
    tr_data = raw_json.get("trailer") or {}

    driver_name = (d_data.get("full_name") or "Водитель").strip().upper()
    truck_plate = (t_data.get("plate") or "Тягач").strip().upper()
    trailer_plate = (tr_data.get("plate") or "Прицеп").strip().upper()

    clean_name = re.sub(r'[^\w\s-]', '', driver_name)
    clean_truck = re.sub(r'[^\w]', '', truck_plate)
    clean_trailer = re.sub(r'[^\w]', '', trailer_plate)

    pdf_filename = f"{clean_name} - {clean_truck}_{clean_trailer}.pdf"
    file_caption = f"📅 Дата загрузки: {date_str} | 📍 Направление: {route_str}"

    try:
        # 1. Отправляем текстовый отчет логисту
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_msg, parse_mode="Markdown")

        # 2. Если исходно выслали PDF
        if documents:
            sorted_pdf_buf = await sort_pdf_pages(documents[0], raw_json) if len(documents) == 1 else None
            if sorted_pdf_buf:
                pdf_file = types.BufferedInputFile(sorted_pdf_buf.getvalue(), filename=pdf_filename)
                await bot.send_document(chat_id=ADMIN_CHANNEL_ID, document=pdf_file, caption=file_caption)
            else:
                for doc_id in documents:
                    await bot.send_document(chat_id=ADMIN_CHANNEL_ID, document=doc_id, caption=file_caption)

        # 3. Если выслали фото — собираем отсортированный PDF
        elif photos:
            pdf_buf = await create_pdf_report_with_images(route_str, date_str, price_str, carrier_text, ai_formatted_data, sorted_files)
            pdf_bytes = pdf_buf.getvalue()
            if pdf_bytes:
                pdf_file = types.BufferedInputFile(pdf_bytes, filename=pdf_filename)
                await bot.send_document(
                    chat_id=ADMIN_CHANNEL_ID, 
                    document=pdf_file, 
                    caption=file_caption
                )

    except Exception as e:
        logging.error(f"Error forwarding docs to admin channel: {e}", exc_info=True)

    await state.clear()
    await message.answer("✅ Данные переданы логисту.", reply_markup=get_main_reply_markup())

@dp.message(DocUploadStates.waiting_for_docs, F.text)
async def handle_doc_text_notes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    old_notes = data.get("text_notes", "")
    new_notes = (old_notes + "\n" + message.text).strip()
    await state.update_data(text_notes=new_notes)


# ==================== ЛОГИКА ОБРАБОТКИ ЗАЯВОК В ЧАТЕ ====================

@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await callback.answer("Ваш аккаунт заблокирован администратором.", show_alert=True)
        return
    conn.close()

    cargo_id = int(callback.data.replace("confirm_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
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
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await callback.answer("Ваш аккаунт заблокирован администратором.", show_alert=True)
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
        await message.answer("Груз не найден или уже закрыт.", reply_markup=get_main_reply_markup())
        await state.clear()
        return
        
    current_cars_str, raw_cargo_text, price_str, date_str, route_str, details_text, status = load_row
    
    if status in ['CLOSED', 'EXPIRED']:
        conn.close()
        await message.answer("Этот груз уже закрыт или истек его срок.", reply_markup=get_main_reply_markup())
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
            types.InlineKeyboardButton(text="💡 Встречная ставка", callback_data=f"counter_bid_{bid_id}"),
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
        await message.answer(f"{warning_text}✅ Ваша ставка отправлена администратору на рассмотрение!", reply_markup=get_main_reply_markup())
        return

    if current_cars > requested_cars:
        left_cars = current_cars - requested_cars
        cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), cargo_id))
    else:
        cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))
        
    cursor.execute("""
        INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (cargo_id, user_id, date_str, route_str, requested_cars, price_str, details_text))
    
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
    await message.answer(f"{warning_text}✅ Груз закреплен за вами! Просмотреть его можно в разделе «Забранные грузы».", reply_markup=get_main_reply_markup())


# ==================== ОБРАБОТКА СТАВОК ЛОГИСТОМ В ТЕЛЕГРАМ ====================

@dp.callback_query(F.data.startswith("accept_bid_"))
async def admin_accept_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("accept_bid_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)
        return
        
    cargo_id, carrier_id, requested_qty, agreed_rate = bid

    cursor.execute("SELECT status FROM users WHERE user_id = ?", (carrier_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await callback.answer("Этот перевозчик заблокирован!", show_alert=True)
        return

    cursor.execute("SELECT cars_count, date, route, details, price, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    
    if row:
        current_cars_str, date_str, route_str, details_str, base_price, status = row
        if status in ['CLOSED', 'EXPIRED']:
            await callback.message.edit_text(callback.message.text + "\n\n[ОШИБКА: Груз уже закрыт]")
            conn.close()
            return
            
        current_cars = int(re.search(r'\d+', str(current_cars_str)).group(0)) if re.search(r'\d+', str(current_cars_str)) else 1
        if requested_qty > current_cars:
            requested_qty = current_cars

        if current_cars > requested_qty:
            left_cars = current_cars - requested_qty
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), cargo_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))
            
        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, requested_qty, agreed_rate, details_str))
        
        cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()

        add_notification(
            carrier_id, 
            "✅ Ставка подтверждена", 
            f"Администратор подтвердил вашу ставку ({agreed_rate}) по грузу {route_str}."
        )

        await update_cargo_messages_for_all_users(cargo_id)
    else:
        conn.close()
        return

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=(
                f"✅ Администратор подтвердил вашу заявку! Груз закреплен за вами.\n\n"
                f"📅 Дата: {row[1]}\n"
                f"📍 Маршрут: {row[2]}\n"
                f"💰 Цена: {agreed_rate}\n"
                f"🚛 Авто: {requested_qty}"
            )
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n[СТАТУС: Подтверждено ✅]", reply_markup=None)
    await callback.answer("Ставка подтверждена!")

@dp.callback_query(F.data.startswith("partial_bid_"))
async def admin_partial_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("partial_bid_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)
        return
        
    cargo_id, carrier_id, max_requested, agreed_rate = bid
    
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    current_cars = int(re.search(r'\d+', str(row[0])).group(0)) if row and re.search(r'\d+', str(row[0])) else 1
    allowed_max = min(current_cars, max_requested)
    
    if allowed_max < 1:
        await callback.answer("Свободных авто нет!", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for i in range(1, allowed_max + 1):
        builder.add(types.InlineKeyboardButton(text=f"{i} авто", callback_data=f"pconf_{bid_id}_{i}"))
    builder.adjust(3)
    
    await callback.message.edit_text(
        text=callback.message.text + f"\n\nВыберите количество авто (макс {allowed_max}):",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pconf_"))
async def admin_process_partial_confirm(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    bid_id = int(parts[1])
    confirmed_qty = int(parts[2])

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)
        return
        
    cargo_id, carrier_id, agreed_rate = bid

    cursor.execute("SELECT cars_count, date, route, details, price, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()

    if row:
        current_cars_str, date_str, route_str, details_str, base_price, status = row
        if status in ['CLOSED', 'EXPIRED']:
            await callback.message.edit_text(callback.message.text + "\n\n[ОШИБКА: Груз закрыт]", reply_markup=None)
            conn.close()
            return

        current_cars = int(re.search(r'\d+', str(current_cars_str)).group(0)) if re.search(r'\d+', str(current_cars_str)) else 1

        if current_cars > confirmed_qty:
            left_cars = current_cars - confirmed_qty
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), cargo_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))

        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, confirmed_qty, agreed_rate, details_str))

        cursor.execute("UPDATE bids SET status = 'PARTIAL' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()

        add_notification(
            carrier_id, 
            "🔀 Частичное подтверждение", 
            f"Администратор частично подтвердил вашу ставку ({agreed_rate}) на {confirmed_qty} авто по грузу {route_str}."
        )

        await update_cargo_messages_for_all_users(cargo_id)
    else:
        conn.close()
        return

    try:
        await bot.send_message(
            chat_id=carrier_id,
            text=(
                f"✅ Частичное подтверждение ставки!\n\n"
                f"📍 Маршрут: {row[2]}\n"
                f"💰 Цена: {agreed_rate}\n"
                f"🚛 Авто: {confirmed_qty}"
            )
        )
    except Exception:
        pass

    old_text = callback.message.text.split("Выберите количество")[0].strip()
    await callback.message.edit_text(
        old_text + f"\n\n[СТАТУС: Частично подтверждено ({confirmed_qty} авто) ✅]",
        reply_markup=None
    )
    await callback.answer("Успешно!")

@dp.callback_query(F.data.startswith("counter_bid_"))
async def admin_start_counter_bid(callback: types.CallbackQuery, state: FSMContext):
    bid_id = int(callback.data.replace("counter_bid_", ""))
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pending_counters (admin_chat_id, bid_id) VALUES (?, ?)", (chat_id, bid_id))
    conn.commit()
    conn.close()

    PENDING_COUNTER_OFFERS[user_id] = bid_id
    PENDING_COUNTER_OFFERS[chat_id] = bid_id

    await state.update_data(counter_bid_id=bid_id)
    await state.set_state(AdminCounterStates.waiting_for_counter_rate)
    
    try:
        await callback.message.reply(
            f"💡 **Встречная ставка по заявке #{bid_id}:**\n\n"
            f"Напишите желаемую цену сообщением в этот чат (например: `2600 USD`).\n"
            f"Также можно использовать команду:\n`/counter_{bid_id} 2600 USD`",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("carr_acc_count_"))
async def carrier_accept_counter(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("carr_acc_count_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, counter_rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid or not bid[3]:
        conn.close()
        await callback.answer("Сделка не найдена.")
        return
        
    cargo_id, carrier_id, cars_qty, counter_rate = bid
    cursor.execute("SELECT cars_count, date, route, details FROM loads WHERE load_id = ?", (cargo_id,))
    load_row = cursor.fetchone()
    
    if load_row:
        date_str, route_str, details_str = load_row[1], load_row[2], load_row[3]
        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, cars_qty, counter_rate, details_str))
        cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()
        
        await callback.message.edit_text(f"✅ Вы приняли встречную ставку ({counter_rate})! Груз закрепился за вами.")
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=f"🎉 Перевозчик ПРИНЯЛ встречную ставку ({counter_rate}) по грузу #{cargo_id}!")
    else:
        conn.close()
    await callback.answer()

@dp.callback_query(F.data.startswith("carr_dec_count_"))
async def carrier_decline_counter(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("carr_dec_count_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE bids SET status = 'DECLINED' WHERE bid_id = ?", (bid_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("❌ Вы отклонили встречную ставку.")
    await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=f"❌ Перевозчик отклонил встречную ставку по заявке #{bid_id}.")
    await callback.answer()

@dp.callback_query(F.data.startswith("decline_bid_"))
async def admin_decline_bid(callback: types.CallbackQuery):
    bid_id = int(callback.data.replace("decline_bid_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, rate FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid:
        conn.close()
        await callback.answer("Ставка не найдена.", show_alert=True)
        return
        
    cargo_id, carrier_id, rate = bid
    cursor.execute("UPDATE bids SET status = 'DECLINED' WHERE bid_id = ?", (bid_id,))
    
    cursor.execute("SELECT route FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.commit()
    conn.close()

    route_name = row[0] if row else "Груз"

    add_notification(
        carrier_id, 
        "❌ Ставка отклонена", 
        f"Ваша ставка ({rate}) по грузу {route_name} была отклонена администратором."
    )

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=f"❌ Ваша ставка ({rate}) по грузу {route_name} отклонена администратором."
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n[СТАТУС: Отклонено ❌]", reply_markup=None)
    await callback.answer("Отклонено.")


# ==================== АДМИН-ПАНЕЛЬ В TELEGRAM ====================
@dp.message(F.chat.id == ADMIN_CHANNEL_ID)
@dp.channel_post(F.chat.id == ADMIN_CHANNEL_ID)
async def handle_admin_messages_and_posts(event: types.Message, state: FSMContext = None):
    text = (event.text or event.caption or "").strip()
    if not text:
        return
        
    cleaned_text = text.strip()

    if cleaned_text.startswith("/setpass"):
        new_pass = cleaned_text.replace("/setpass", "").strip()
        if not new_pass:
            await event.answer("⚠️ Укажите новый пароль после команды, например: `/setpass 777888`", parse_mode="Markdown")
            return
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_password', ?)", (new_pass,))
        conn.commit()
        conn.close()
        await event.answer(f"✅ Пароль от Админ-меню Web App успешно изменён на: `{new_pass}`", parse_mode="Markdown")
        return

    if cleaned_text.lower() in ("/меню", "меню", "!меню"):
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
        builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
        builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
        builder.row(types.InlineKeyboardButton(text="🔐 Сменить пароль Web App", callback_data="adm_change_pass"))
        await event.answer("🎛 **Панель администратора**\nВыберите нужный раздел:", reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    if cleaned_text.startswith("!"):
        broadcast_text = cleaned_text[1:].lstrip()
        if not broadcast_text:
            return

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE status != 'BLOCKED'")
        all_users = cursor.fetchall()
        conn.close()
        
        success_count = 0
        for (u_id,) in all_users:
            try:
                await bot.send_message(chat_id=u_id, text=broadcast_text)
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await event.answer(f"✅ Рассылка завершена ({success_count} польз.).")
        return

    await process_admin_counter_offer(event, cleaned_text, state)

async def process_admin_counter_offer(event: types.Message, text: str, state: FSMContext = None):
    bid_id = None
    new_rate = ""

    cmd_match = re.match(r'/counter_(\d+)\s+(.+)', text)
    if cmd_match:
        bid_id = int(cmd_match.group(1))
        new_rate = format_custom_rate(cmd_match.group(2).strip())
    else:
        chat_id = event.chat.id
        user_id = event.from_user.id if event.from_user else 0

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT bid_id FROM pending_counters WHERE admin_chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            bid_id = row[0]
        else:
            bid_id = PENDING_COUNTER_OFFERS.get(chat_id) or PENDING_COUNTER_OFFERS.get(user_id)

        new_rate = format_custom_rate(text)

    if not bid_id or not new_rate:
        return

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars FROM bids WHERE bid_id = ?", (bid_id,))
    bid = cursor.fetchone()
    
    if not bid:
        conn.close()
        return
        
    cargo_id, carrier_id, cars_count = bid
    cursor.execute("UPDATE bids SET counter_rate = ?, status = 'COUNTER_OFFER' WHERE bid_id = ?", (new_rate, bid_id))
    cursor.execute("DELETE FROM pending_counters WHERE admin_chat_id = ?", (event.chat.id,))
    cursor.execute("SELECT route FROM loads WHERE load_id = ?", (cargo_id,))
    load_row = cursor.fetchone()
    conn.commit()
    conn.close()
    
    route = load_row[0] if load_row else "Груз"

    add_notification(
        carrier_id, 
        "💡 Встречная ставка", 
        f"Логист предложил встречную ставку {new_rate} по грузу {route} ({cars_count} авто)."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text=f"✅ Принять {new_rate}", callback_data=f"carr_acc_count_{bid_id}"),
        types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"carr_dec_count_{bid_id}")
    )

    try:
        await bot.send_message(
            chat_id=carrier_id,
            text=(
                f"💡 **Логист предлагает встречную ставку!**\n\n"
                f"📍 Маршрут: {route}\n"
                f"🚛 Авто: {cars_count}\n"
                f"💰 Встречная цена: **{new_rate}**\n\n"
                f"Вы согласны?"
            ),
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await event.answer(f"✅ Встречное предложение ({new_rate}) отправлено перевозчику!")
    except Exception as e:
        try:
            await event.answer(f"❌ Не удалось отправить сообщение перевозчику: {e}")
        except Exception:
            pass

    if state:
        await state.clear()
    PENDING_COUNTER_OFFERS.pop(event.chat.id, None)
    if event.from_user:
        PENDING_COUNTER_OFFERS.pop(event.from_user.id, None)

@dp.callback_query(F.data == "adm_change_pass")
async def admin_change_pass_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply(
        "🔐 **Смена пароля для Web App:**\n\n"
        "Отправьте новую команду с паролем в этот чат:\n"
        "`/setpass ваш_новый_пароль`\n\n"
        "Например:\n`/setpass 777888`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_start_del_"))
async def admin_delete_cargo(callback: types.CallbackQuery):
    cargo_id = int(callback.data.replace("adm_start_del_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))
    conn.commit()
    conn.close()
    
    await update_cargo_messages_for_all_users(cargo_id)
    await callback.message.edit_text(callback.message.text + "\n\n[СТАТУС: ГРУЗ ЗАКРЫТ 🚫]", reply_markup=None)
    await callback.answer("Груз закрыт!")

@dp.callback_query(F.data.startswith("adm_start_edit_"))
async def admin_start_edit_cargo(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("adm_start_edit_", ""))
    await state.update_data(editing_cargo_id=cargo_id)
    await callback.message.answer("✍️ Отправьте новый текст (карточку) для этого груза:")
    await state.set_state(AdminEditStates.waiting_for_new_cargo_text)
    await callback.answer()

@dp.message(AdminEditStates.waiting_for_new_cargo_text)
async def admin_save_edited_cargo(message: types.Message, state: FSMContext):
    new_raw_text = message.text
    if not new_raw_text:
        await message.answer("Текст не может быть пустым:")
        return
        
    data = await state.get_data()
    cargo_id = data.get("editing_cargo_id")
    date_str, route_str, price_str, cars_str, details_text, car_type, cargo_type, weight, expires_at = parse_cargo_raw(new_raw_text)
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE loads 
        SET date = ?, route = ?, price = ?, cars_count = ?, text = ?, details = ?, car_type = ?, cargo_type = ?, weight = ?, expires_at = ?
        WHERE load_id = ?
    """, (date_str, route_str, price_str, cars_str, new_raw_text, details_text, car_type, cargo_type, weight, expires_at, cargo_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ Груз обновлен!", reply_markup=get_main_reply_markup())
    await update_cargo_messages_for_all_users(cargo_id)

@dp.callback_query(F.data == "adm_menu_back")
async def admin_menu_back(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
    builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
    builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
    builder.row(types.InlineKeyboardButton(text="🔐 Сменить пароль Web App", callback_data="adm_change_pass"))
    await callback.message.edit_text("🎛 **Панель администратора**\nВыберите нужный раздел:", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_menu_active")
async def admin_menu_active(callback: types.CallbackQuery):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, text FROM loads WHERE status = 'ACTIVE' ORDER BY load_id DESC LIMIT 30")
    active_loads = cursor.fetchall()
    conn.close()
    
    if not active_loads:
        await callback.answer("Нет активных грузов.", show_alert=True)
        return
        
    await callback.answer("Загружаю...")
    await bot.send_message(callback.message.chat.id, "📋 **Актуальные грузы:**", parse_mode="Markdown")
    
    for load_id, load_text in active_loads:
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            types.InlineKeyboardButton(text="✏️ Изменить", callback_data=f"adm_start_edit_{load_id}"),
            types.InlineKeyboardButton(text="🗑 Закрыть", callback_data=f"adm_start_del_{load_id}")
        )
        try:
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=f"ID: #{load_id}\n\n{load_text}",
                reply_markup=admin_kb.as_markup()
            )
            await asyncio.sleep(0.05)
        except Exception:
            pass

@dp.callback_query(F.data == "adm_menu_confirmed")
async def admin_menu_confirmed(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for direction in CHANNELS.keys():
        builder.row(types.InlineKeyboardButton(text=direction, callback_data=f"adm_dir_{direction}"[:64]))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад в меню админа", callback_data="adm_menu_back"))
    await callback.message.edit_text("🤝 **Выберите направление:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_dir_"))
async def admin_show_confirmed_by_dir(callback: types.CallbackQuery):
    direction = callback.data.replace("adm_dir_", "")
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    query = """
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, 
               u.company, u.name, u.phone, u.user_id
        FROM confirmed_deals cd
        JOIN loads l ON cd.load_id = l.load_id
        LEFT JOIN users u ON cd.user_id = u.user_id
        WHERE l.destination_country = ?
        ORDER BY cd.id DESC LIMIT 20
    """
    cursor.execute(query, (direction,))
    deals = cursor.fetchall()
    conn.close()
    
    if not deals:
        await callback.answer(f"По направлению {direction} сделок нет.", show_alert=True)
        return
        
    await callback.answer("Загружаю...")
    for deal_id, load_id, date, route, cars, price, company, name, phone, user_id in deals:
        comp_str = company or "Не указана"
        name_str = name or "Пользователь"
        phone_str = phone or "Не указан"
        text = (
            f"🤝 Сделка #{deal_id} (Груз #{load_id})\n\n"
            f"📍 {route}\n📅 Дата: {date}\n🚛 Авто: {cars} | 💰 Ставка: {price}\n\n"
            f"👤 {comp_str} | {name_str} | {phone_str}\n💬 Написать: tg://user?id={user_id}"
        )
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_ask_cancel_qty_{deal_id}"))
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("adm_ask_cancel_qty_"))
async def admin_ask_cancel_qty(callback: types.CallbackQuery):
    deal_id = int(callback.data.replace("adm_ask_cancel_qty_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars FROM confirmed_deals WHERE id = ?", (deal_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] <= 1:
        await execute_cancel_deal(callback, deal_id, 1)
        return
        
    max_cars = row[0]
    builder = InlineKeyboardBuilder()
    for i in range(1, max_cars + 1):
        builder.add(types.InlineKeyboardButton(text=f"{i} авто", callback_data=f"adm_exec_cancel_{deal_id}_{i}"))
    builder.adjust(3)
    await callback.message.edit_text(text=callback.message.text + f"\n\nСколько авто отменить из {max_cars}?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_exec_cancel_"))
async def admin_exec_cancel_partial(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    deal_id, cancel_qty = int(parts[3]), int(parts[4])
    await execute_cancel_deal(callback, deal_id, cancel_qty)

async def execute_cancel_deal(callback: types.CallbackQuery, deal_id: int, cancel_qty: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, user_id, cars, route, price FROM confirmed_deals WHERE id = ?", (deal_id,))
    deal = cursor.fetchone()
    
    if not deal:
        conn.close()
        return
        
    load_id, carrier_id, current_deal_cars, route, price = deal
    
    if cancel_qty >= current_deal_cars:
        cursor.execute("DELETE FROM confirmed_deals WHERE id = ?", (deal_id,))
        conn.commit()
        conn.close()
        
        add_notification(carrier_id, "⚠️ Отмена сделки", f"Ваш груз по маршруту {route} отменен администратором.")
        try:
            await bot.send_message(chat_id=carrier_id, text=f"⚠️ Ваш груз по маршруту {route} отменен администратором.")
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n[ОТМЕНЕНО 🔒]", reply_markup=None)
    else:
        new_cars = current_deal_cars - cancel_qty
        cursor.execute("UPDATE confirmed_deals SET cars = ? WHERE id = ?", (new_cars, deal_id))
        conn.commit()
        conn.close()
        
        add_notification(carrier_id, "⚠️ Частичная отмена", f"По сделке ({route}) отменено {cancel_qty} авто.")
        try:
            await bot.send_message(chat_id=carrier_id, text=f"⚠️ По сделке ({route}) отменено {cancel_qty} авто. Осталось в сделке: {new_cars}.")
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + f"\n\n[Отменено авто: {cancel_qty}]", reply_markup=None)
    await callback.answer("Готово!")

@dp.callback_query(F.data == "adm_menu_carriers")
async def admin_menu_carriers(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🌐 По всем направлениям", callback_data="adm_car_dir_ALL"))
    for direction in CHANNELS.keys():
        builder.row(types.InlineKeyboardButton(text=direction, callback_data=f"adm_car_dir_{direction}"[:64]))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад в меню админа", callback_data="adm_menu_back"))
    await callback.message.edit_text("👥 **Выберите направление:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_car_dir_"))
async def admin_show_carriers_by_dir(callback: types.CallbackQuery):
    direction = callback.data.replace("adm_car_dir_", "")
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, company, name, phone, status, subscriptions FROM users")
    all_users = cursor.fetchall()
    conn.close()
    
    found_any = False
    for u_id, comp, name, phone, u_status, subs_str in all_users:
        subs = subs_str.split(",") if subs_str else []
        if direction == "ALL" or direction in subs:
            found_any = True
            comp_str = comp or "Не указана"
            name_str = name or "Не указано"
            phone_str = phone or "Не указан"
            status_label = "🟢 Активен" if u_status != 'BLOCKED' else "🔴 Заблокирован"
            text = f"🏢 {comp_str} | 👤 {name_str}\n📞 {phone_str}\nID: `{u_id}` | {status_label}"
            
            kb = InlineKeyboardBuilder()
            if u_status != 'BLOCKED':
                kb.row(types.InlineKeyboardButton(text="🔴 Заблокировать", callback_data=f"adm_block_user_{u_id}_{direction}"))
            else:
                kb.row(types.InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"adm_unblock_user_{u_id}_{direction}"))
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=text, reply_markup=kb.as_markup(), parse_mode="Markdown")
            await asyncio.sleep(0.05)
            
    if not found_any:
        await callback.answer("Перевозчики не найдены.", show_alert=True)
    else:
        await callback.answer("Список загружен")

@dp.callback_query(F.data.startswith("adm_block_user_"))
async def admin_block_user(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[3])
    direction = parts[4] if len(parts) > 4 else "ALL"

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'BLOCKED' WHERE user_id = ?", (user_id,))
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    try:
        await bot.send_message(chat_id=user_id, text="❌ Ваш аккаунт заблокирован администратором.")
    except Exception:
        pass

    comp_str = row[0] if row and row[0] else "Не указана"
    name_str = row[1] if row and row[1] else "Не указано"
    phone_str = row[2] if row and row[2] else "Не указан"
    
    new_text = f"🏢 {comp_str} | 👤 {name_str}\n📞 {phone_str}\nID: `{user_id}` | 🔴 Заблокирован"
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"adm_unblock_user_{user_id}_{direction}"))

    await callback.message.edit_text(text=new_text, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await callback.answer("Заблокирован!")

@dp.callback_query(F.data.startswith("adm_unblock_user_"))
async def admin_unblock_user(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[3])
    direction = parts[4] if len(parts) > 4 else "ALL"

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'ACTIVE' WHERE user_id = ?", (user_id,))
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    try:
        await bot.send_message(chat_id=user_id, text="✅ Ваш аккаунт разблокирован!")
    except Exception:
        pass

    comp_str = row[0] if row and row[0] else "Не указана"
    name_str = row[1] if row and row[1] else "Не указано"
    phone_str = row[2] if row and row[2] else "Не указан"

    new_text = f"🏢 {comp_str} | 👤 {name_str}\n📞 {phone_str}\nID: `{user_id}` | 🟢 Активен"
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔴 Заблокировать", callback_data=f"adm_block_user_{user_id}_{direction}"))

    await callback.message.edit_text(text=new_text, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await callback.answer("Разблокирован!")


# ==================== ПАРСИНГ ИЗ КАНАЛОВ ====================
@dp.channel_post(F.chat.id.in_(list(CHANNEL_TO_DIRECTION.keys())))
async def handle_channel_post(message: types.Message):
    chat_id = message.chat.id
    raw_text = message.text or message.caption
    if not raw_text:
        return
        
    direction = CHANNEL_TO_DIRECTION.get(chat_id)
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
    conn.close()
    
    for cargo_id in created_cargo_ids:
        for u_id, subs, u_status in all_users:
            if u_status == 'BLOCKED':
                continue
            if subs and direction in [s.strip() for s in subs.split(",")]:
                await send_cargo_to_user(u_id, cargo_id)
                await asyncio.sleep(0.05)

# ==================== WEB APP БЭКЕНД ====================

async def get_loads_api(request):
    country = request.query.get('country')
    raw_uid = request.query.get('user_id')
    user_id = int(raw_uid) if raw_uid and raw_uid.isdigit() else 0

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    user_subs = []
    if user_id:
        cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
        u_row = cursor.fetchone()
        if u_row and u_row[0]:
            user_subs = [s.strip() for s in u_row[0].split(',') if s.strip()]

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
        # Если фильтр по стране не задан или 'ALL' — отдаем все активные грузы биржи
        
    query += " ORDER BY load_id DESC"
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error reading loads: {e}")
        rows = []
    conn.close()
    
    loads = [{
        "id": r[0],
        "route": r[1] if r[1] else "Не указан",
        "date": r[2] if r[2] else "Срочно",
        "cars": r[3] if r[3] else "1",
        "price": r[4] if r[4] else "Торги",
        "raw_text": r[5],
        "cargo_type": r[6],
        "weight": r[7],
        "car_type": r[8],
        "admin_comment": r[9],
        "country": r[10]
    } for r in rows]
    return web.json_response({"loads": loads})

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
    
    cursor.execute("""
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, 
               COALESCE(cd.details, ''), 'CONFIRMED' as status,
               COALESCE(l.car_type, 'Тент/реф'),
               COALESCE(l.cargo_type, 'ТНП'),
               COALESCE(l.weight, 'до 22т'),
               COALESCE(cd.docs_submitted, 0),
               COALESCE(cd.docs_status, 'NONE'),
               COALESCE(cd.missing_docs, '')
        FROM confirmed_deals cd
        LEFT JOIN loads l ON cd.load_id = l.load_id
        WHERE cd.user_id = ?
        ORDER BY cd.id DESC
    """, (user_id,))
    confirmed_rows = cursor.fetchall()
    
    cursor.execute("""
        SELECT b.bid_id, b.load_id, l.date, l.route, b.cars, b.rate as price,
               COALESCE(l.details, ''), 'PENDING' as status,
               COALESCE(l.car_type, 'Тент/реф'),
               COALESCE(l.cargo_type, 'ТНП'),
               COALESCE(l.weight, 'до 22т'),
               0 as docs_submitted
        FROM bids b
        JOIN loads l ON b.load_id = l.load_id
        WHERE b.user_id = ? AND b.status = 'PENDING'
        ORDER BY b.bid_id DESC
    """, (user_id,))
    pending_rows = cursor.fetchall()
    
    conn.close()
    
    msk_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    deals = []
    
    for r in pending_rows:
        bid_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub = r
        try:
            qty = int(re.search(r'\d+', str(cars_count)).group(0))
        except Exception:
            qty = 1
            
        c_date = parse_cargo_date(date_str)
        is_today = (c_date and c_date == msk_today)
        is_archived = (c_date and msk_today > c_date)

        for i in range(qty):
            deals.append({
                "id": f"bid_{bid_id}_{i}",
                "load_id": load_id,
                "date": date_str,
                "route": route_str,
                "price": price_str,
                "status": "PENDING",
                "status_text": "⏳ (на согласовании)",
                "details": details_str,
                "car_type": car_type,
                "cargo_type": cargo_type,
                "weight": weight,
                "is_today": is_today,
                "is_archived": is_archived,
                "docs_submitted": False
            })

    for r in confirmed_rows:
        deal_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub = r
        try:
            qty = int(re.search(r'\d+', str(cars_count)).group(0))
        except Exception:
            qty = 1

        c_date = parse_cargo_date(date_str)
        is_today = (c_date and c_date == msk_today)
        is_archived = (c_date and msk_today > c_date)

        for r in confirmed_rows:
        deal_id, load_id, date_str, route_str, cars_count, price_str, details_str, status_str, car_type, cargo_type, weight, docs_sub, docs_stat, miss_docs = r
        try:
            qty = int(re.search(r'\d+', str(cars_count)).group(0))
        except Exception:
            qty = 1

        c_date = parse_cargo_date(date_str)
        is_today = (c_date and c_date == msk_today)
        is_archived = (c_date and msk_today > c_date)

        for i in range(qty):
            deals.append({
                "id": f"deal_{deal_id}_{i}",
                "deal_id": deal_id,
                "load_id": load_id,
                "date": date_str,
                "route": route_str,
                "price": price_str,
                "status": "CONFIRMED",
                "status_text": "✅ Подтвержден",
                "details": details_str,
                "car_type": car_type,
                "cargo_type": cargo_type,
                "weight": weight,
                "is_today": is_today,
                "is_archived": is_archived,
                "docs_submitted": bool(docs_sub),
                "docs_status": docs_stat,
                "missing_docs": miss_docs
            })
            
    return web.json_response({"deals": deals})

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

    notifs = [{
        "id": r[0],
        "title": r[1],
        "text": r[2],
        "is_read": r[3],
        "created_at": r[4]
    } for r in rows]

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
    cursor.execute("SELECT company, name, phone, subscriptions FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return web.json_response({
            "profile": {
                "company": row[0] or "",
                "name": row[1] or "",
                "phone": row[2] or "",
                "subscriptions": row[3] or ""
            }
        })
    return web.json_response({"profile": None})

async def profile_post_api(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    raw_uid = data.get('user_id')
    if not raw_uid:
        return web.json_response({"error": "No user_id"}, status=400)

    try:
        user_id = int(raw_uid)
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid user_id"}, status=400)

    company = data.get('company', '')
    name = data.get('name', '')
    phone = data.get('phone', '')
    subscriptions = data.get('subscriptions', '')

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, company, name, phone, subscriptions, status)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE')
        ON CONFLICT(user_id) DO UPDATE SET
            company = excluded.company,
            name = excluded.name,
            phone = excluded.phone,
            subscriptions = excluded.subscriptions
    """, (user_id, company, name, phone, subscriptions))
    conn.commit()
    conn.close()

    return web.json_response({"status": "success"})

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
        cursor.execute("SELECT date, route, price FROM confirmed_deals WHERE id = ? AND user_id = ?", (clean_deal_id, user_id))
        deal = cursor.fetchone()
        conn.close()

        date_str = deal[0] if deal else "Ближайшая"
        route_str = deal[1] if deal else "Маршрут"
        price_str = deal[2] if deal else "Ставка"

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

        prompt_text = (
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
        logging.error(f"Error in submit_docs_prompt_api: {e}")
        return web.json_response({"error": str(e)}, status=400)

async def book_load_api(request):
    raw_id = request.match_info.get('id', '')
    digits = re.findall(r'\d+', str(raw_id))
    if not digits:
        return web.json_response({"error": "Неверный ID груза"}, status=400)
    load_id = int(digits[0])

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Неверный формат данных"}, status=400)

    raw_uid = data.get('user_id')
    try:
        user_id = int(raw_uid)
    except (ValueError, TypeError):
        user_id = 0

    if not user_id:
        return web.json_response({"error": "Пользователь не определён. Переоткройте Web App из бота."}, status=400)

    first_name = data.get('first_name', '')
    username = data.get('username', '')
    action = data.get('action') 
    proposed_price_raw = data.get('proposed_price', '')
    proposed_price = format_custom_rate(proposed_price_raw)
    carrier_comment = data.get('comment', '')
    
    try:
        requested_cars = int(data.get('cars', 1))
    except (ValueError, TypeError):
        requested_cars = 1

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    cursor.execute("SELECT status, company, name, phone FROM users WHERE user_id = ?", (user_id,))
    user_row = cursor.fetchone()
    if not user_row:
        cursor.execute("""
            INSERT INTO users (user_id, company, name, phone, subscriptions, status)
            VALUES (?, 'Не указана', ?, 'Не указан', '', 'ACTIVE')
        """, (user_id, first_name or username or f"User_{user_id}"))
        conn.commit()
    else:
        u_status = user_row[0]
        if u_status == 'BLOCKED':
            conn.close()
            return web.json_response({"error": "Ваш аккаунт заблокирован"}, status=403)

    cursor.execute("SELECT status, route, date, price, cars_count, details, text FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()
    
    if not load or load[0] != 'ACTIVE':
        conn.close()
        return web.json_response({"error": "Груз недоступен или уже закрыт"}, status=400)
        
    status, route_str, date_str, price_str, cars_count_str, details_text, raw_cargo_text = load
    current_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if cars_count_str and re.search(r'\d+', str(cars_count_str)) else 1

    if current_cars <= 0:
        conn.close()
        return web.json_response({"error": "Все авто по этому грузу уже забронированы"}, status=400)

    carrier_text = format_carrier_info(user_id, username, first_name)

    if action == 'confirm':
        if requested_cars > current_cars:
            requested_cars = current_cars

        if current_cars > requested_cars:
            left_cars = current_cars - requested_cars
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), load_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))

        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (load_id, user_id, date_str, route_str, requested_cars, price_str, details_text))

        conn.commit()
        conn.close()

        add_notification(
            user_id, 
            "✅ Груз забронирован", 
            f"Вы забронировали {requested_cars} авто по маршруту {route_str} ({price_str})."
        )

        await update_cargo_messages_for_all_users(load_id)

        admin_notification = (
            f"🎯 **Груз забран перевозчиком из Web App!**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"📅 Дата: {date_str}\n"
            f"💰 Ставка: {price_str} | 🚛 Забрано авто: {requested_cars}\n"
            f"💬 Комментарий: {carrier_comment or 'Нет'}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error sending admin notification: {e}")

        return web.json_response({"status": "success"})

    elif action == 'bid':
        cursor.execute("""
            INSERT INTO bids (load_id, user_id, cars, rate, comment)
            VALUES (?, ?, ?, ?, ?)
        """, (load_id, user_id, requested_cars, proposed_price, carrier_comment or 'Своя ставка'))
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        add_notification(
            user_id, 
            "⏳ Ставка отправлена", 
            f"Ваша ставка {proposed_price} на {requested_cars} авто по грузу {route_str} отправлена логисту."
        )

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="🔀 Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(
            types.InlineKeyboardButton(text="💡 Встречная ставка", callback_data=f"counter_bid_{bid_id}"),
            types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{bid_id}")
        )

        admin_notification = (
            f"💰 **Новая ставка из Web App!**\n\n"
            f"🆔 Груз #{load_id} | Маршрут: {route_str}\n"
            f"📅 Дата: {date_str}\n"
            f"💵 Предложенная ставка: {proposed_price} | 🚛 Авто: {requested_cars}\n"
            f"💬 Комментарий: {carrier_comment or 'Нет'}\n\n"
            f"{carrier_text}"
        )
        try:
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID, 
                text=admin_notification, 
                reply_markup=admin_builder.as_markup(),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Error sending bid notification: {e}")

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
    carriers = [{
        "user_id": r[0], "company": r[1] or "Не указана", "name": r[2] or "Не указано",
        "phone": r[3] or "Не указан", "status": r[4] or "ACTIVE", "subscriptions": r[5] or ""
    } for r in rows]
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
        route = data.get('route', '')
        date_str = data.get('date', '')
        price = data.get('price', '')
        cars = data.get('cars', '1')
        car_type = data.get('car_type', 'Тент/реф')
        cargo_type = data.get('cargo_type', 'ТНП')
        weight = data.get('weight', 'до 22т')
        admin_comment = data.get('admin_comment', '')

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE loads 
            SET route = ?, date = ?, price = ?, cars_count = ?, car_type = ?, cargo_type = ?, weight = ?, admin_comment = ?
            WHERE load_id = ?
        """, (route, date_str, price, cars, car_type, cargo_type, weight, admin_comment, load_id))
        conn.commit()
        conn.close()

        await update_cargo_messages_for_all_users(load_id)
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def admin_delete_load_api(request):
    try:
        data = await request.json()
        load_id = int(data.get('id'))
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (load_id,))
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
        return web.json_response({"status": "success", "new_status": new_status})
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
    query = """
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, cd.details, cd.user_id,
               COALESCE(u.company, 'Не указана'), COALESCE(u.name, 'Пользователь'), COALESCE(u.phone, 'Не указан')
        FROM confirmed_deals cd
        LEFT JOIN users u ON cd.user_id = u.user_id
        ORDER BY cd.id DESC
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    deals = [{
        "deal_id": r[0],
        "load_id": r[1],
        "date": r[2],
        "route": r[3],
        "cars": r[4],
        "price": r[5],
        "details": r[6],
        "user_id": r[7],
        "company": r[8],
        "name": r[9],
        "phone": r[10]
    } for r in rows]
    return web.json_response({"deals": deals})

async def admin_edit_deal_api(request):
    try:
        data = await request.json()
        deal_id = int(data.get('deal_id'))
        new_route = data.get('route', '')
        new_date = data.get('date', '')
        new_price = format_custom_rate(data.get('price', ''))
        new_cars = int(data.get('cars', 1))

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM confirmed_deals WHERE id = ?", (deal_id,))
        row = cursor.fetchone()

        if row:
            carrier_id = row[0]
            cursor.execute("""
                UPDATE confirmed_deals 
                SET route = ?, date = ?, price = ?, cars = ?
                WHERE id = ?
            """, (new_route, new_date, new_price, new_cars, deal_id))
            conn.commit()
            conn.close()

            add_notification(
                carrier_id, 
                "⚠️ Сделка отредактирована", 
                f"Логист изменил параметры вашей сделки #{deal_id} по маршруту {new_route} (Ставка: {new_price}, Авто: {new_cars})."
            )

            try:
                await bot.send_message(
                    chat_id=carrier_id, 
                    text=f"⚠️ **Логист отредактировал вашу подтверждённую сделку!**\n\n📍 Маршрут: {new_route}\n📅 Дата: {new_date}\n💰 Ставка: {new_price}\n🚛 Авто: {new_cars}"
                )
            except Exception:
                pass

            return web.json_response({"status": "success"})
        else:
            conn.close()
            return web.json_response({"error": "Сделка не найдена"}, status=404)
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

            add_notification(carrier_id, "⚠️ Сделка отменена", f"Ваша подтверждённая сделка по маршруту {route} отменена логистом.")
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
    return web.Response(text=INDEX_HTML, content_type='text/html')

DIRECTIONS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Выбор направлений</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 15px; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #000); }
        h3 { margin-top: 0; font-size: 17px; text-align: center; }
        .item { font-size: 16px; margin: 12px 0; display: flex; align-items: center; gap: 10px; background: var(--tg-theme-secondary-bg-color, #f5f5f7); padding: 10px 14px; border-radius: 8px; }
        .item input { width: 18px; height: 18px; }
        button { width: 100%; padding: 12px; background: var(--tg-theme-button-color, #2481cc); color: var(--tg-theme-button-text-color, #fff); border: none; border-radius: 8px; font-size: 15px; font-weight: bold; margin-top: 20px; cursor: pointer; }
    </style>
</head>
<body>
    <h3>🌍 Выберите интересующие направления:</h3>
    <div class="item"><label><input type="checkbox" value="Казахстан 🇰🇿"> 🇰🇿 Казахстан</label></div>
    <div class="item"><label><input type="checkbox" value="Узбекистан 🇺🇿"> 🇺🇿 Узбекистан</label></div>
    <div class="item"><label><input type="checkbox" value="Кыргызстан 🇰🇬"> 🇰🇬 Кыргызстан</label></div>
    <div class="item"><label><input type="checkbox" value="Грузия 🇬🇪"> 🇬🇪 Грузия</label></div>
    <div class="item"><label><input type="checkbox" value="Азербайджан 🇦🇿"> 🇦🇿 Азербайджан</label></div>
    <div class="item"><label><input type="checkbox" value="Армения 🇦🇲"> 🇦🇲 Армения</label></div>

    <button onclick="save()">Сохранить подписки</button>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        function save() {
            let selected = [];
            document.querySelectorAll('input:checked').forEach(cb => selected.push(cb.value));
            tg.sendData(JSON.stringify(selected));
            tg.close();
        }
    </script>
</body>
</html>"""

async def serve_directions(request):
    return web.Response(text=DIRECTIONS_HTML, content_type='text/html')


# ==================== СЕРВЕР И САМОПИНГ (ЗАЩИТА ОТ СПЯЩЕГО РЕЖИМА) ====================
async def handle_ping(request):
    return web.Response(text="Bot is running!", status=200)

async def self_ping():
    await asyncio.sleep(10)
    import aiohttp
    url = RENDER_URL.rstrip('/') if RENDER_URL else None
    
    if not url or "your-app-name" in url:
        logging.warning("⚠️ RENDER_URL не настроен! Задайте переменную RENDER_URL в настройках хостинга.")
        return

    ping_url = f"{url}/ping"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    logging.info(f"Self-ping ({ping_url}) status: {response.status}")
        except Exception as e:
            logging.error(f"Self-ping error: {e}")
            
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
    app.router.add_get("/directions", serve_directions)
    app.router.add_get("/api/loads", get_loads_api)
    app.router.add_get("/api/my_loads", my_loads_api)
    app.router.add_get("/api/notifications", notifications_get_api)
    app.router.add_post("/api/notifications/read", notifications_read_api)
    app.router.add_get("/api/profile", profile_get_api)
    app.router.add_post("/api/profile", profile_post_api)
    app.router.add_post("/api/book/{id}", book_load_api)
    app.router.add_post("/api/submit_docs_prompt", submit_docs_prompt_api)

    # Админ бэкенд роуты
    app.router.add_post("/api/admin/verify_pass", admin_verify_pass_api)
    app.router.add_get("/api/admin/carriers", admin_get_carriers_api)
    app.router.add_post("/api/admin/carrier_status", admin_toggle_carrier_status_api)
    app.router.add_get("/api/admin/loads", admin_get_loads_api)
    app.router.add_post("/api/admin/edit_load", admin_edit_load_api)
    app.router.add_post("/api/admin/delete_load", admin_delete_load_api)
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
    await asyncio.gather(
        run_bot(),
        web_server()
    )

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    print("🚀 Запуск Telegram бота и Web App сервера...", flush=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен вручную.", flush=True)
    except Exception as e:
        print("💥 КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА:", flush=True)
        traceback.print_exc()
        sys.exit(1)

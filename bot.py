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
        "ALTER TABLE confirmed_deals ADD COLUMN docs_submitted INTEGER DEFAULT 0"
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
        if ':' not in raw_time:
            hours = int(raw_time)
            minutes = 0
        else:
            parts = raw_time.split(':')
            hours, minutes = int(parts[0]), int(parts[1])
            
        time_formatted = f"{hours:02d}:{minutes:02d}"
        msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
        expire_dt = msk_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
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

class VehicleDetails(BaseModel):
    brand_model: str = Field(default="Не распознан", description="Марка и модель (например: DAF XF 105)")
    plate: str = Field(default="Не распознан", description="Гос. номер (например: 1234 AB-7 или 777AAA01)")
    vin: str = Field(default="Не распознан", description="VIN номер (17 символов)")
    country: str = Field(default="Не распознана", description="Страна регистрации ТС")

class DocumentDetails(BaseModel):
    number: str = Field(default="Не распознан", description="Номер документа")
    issue_date: str = Field(default="Не распознана", description="Дата выдачи (ГГГГ-ММ-ДД или ДД.ММ.ГГГГ)")
    authority: str = Field(
        default="Не распознан", 
        description=(
            "Орган выдачи документа. Приоритет — РУССКИЙ ЯЗЫК. "
            "Для паспортов РБ — точное название (напр. СТАРОДОРОЖСКИЙ РОВД МИНСКОЙ ОБЛАСТИ). "
            "Для ID-карт РБ — 'Код органа выдачи: XXX'. "
            "Для Казахстана — 'МВД РК' или 'МВД РЕСПУБЛИКИ КАЗАХСТАН'. "
            "Для Узбекистана — 'MIA XXXXXX'. "
            "Для Кыргызстана — 'MIA' или 'PSC'. "
            "Для Азербайджана — 'MINISTRY OF INTERNAL AFFAIRS'."
        )
    )
    country: str = Field(default="Не распознана", description="Страна выдачи документа на русском языке")

class DriverDetails(BaseModel):
    full_name: str = Field(default="Не распознан", description="ФИО водителя. ПРИОРИТЕТ — РУССКИЙ ЯЗЫК (Кириллица). Если нет — латиница.")
    birth_date: str = Field(default="Не распознана", description="Дата рождения водителя")
    phones: str = Field(
        default="Не указан", 
        description="Номера телефонов водителя (только цифры и '+'). Российский номер (+7...) ВСЕГДА ПЕРВЫМ, остальные через слеш '/'"
    )
    passport: DocumentDetails = Field(default_factory=DocumentDetails)
    license: DocumentDetails = Field(default_factory=DocumentDetails)

class FullCargoSubmission(BaseModel):
    truck: VehicleDetails = Field(default_factory=VehicleDetails)
    trailer: VehicleDetails = Field(default_factory=VehicleDetails)
    driver: DriverDetails = Field(default_factory=DriverDetails)

# ==================== СХЕМЫ, ИИ-АГЕНТ И СОРТИРОВКА ФОТО ====================

class VehicleDetails(BaseModel):
    brand_model: str = Field(default="Не распознан", description="Марка и модель (например: DAF XF 105)")
    plate: str = Field(default="Не распознан", description="Гос. номер (например: 1234 AB-7 или 777AAA01)")
    vin: str = Field(default="Не распознан", description="VIN номер (17 символов)")
    country: str = Field(default="Не распознана", description="Страна регистрации ТС")

class DocumentDetails(BaseModel):
    number: str = Field(default="Не распознан", description="Номер документа")
    issue_date: str = Field(default="Не распознана", description="Дата выдачи")
    authority: str = Field(default="Не распознан", description="Орган выдачи документа (Приоритет - русский язык)")
    country: str = Field(default="Не распознана", description="Страна выдачи документа")

class DriverDetails(BaseModel):
    full_name: str = Field(default="Не распознан", description="ФИО водителя (Приоритет - русский язык)")
    birth_date: str = Field(default="Не распознана", description="Дата рождения водителя")
    phones: str = Field(default="Не указан", description="Номера телефонов (+7... первым, остальные через '/')")
    passport: DocumentDetails = Field(default_factory=DocumentDetails)
    license: DocumentDetails = Field(default_factory=DocumentDetails)

class ImageClassification(BaseModel):
    image_index: int = Field(description="Порядковый номер загруженного изображения, начиная с 0")
    category: str = Field(
        description=(
            "Категория и сторона фото: "
            "'passport_front' (Паспорт/ID лицевая), "
            "'passport_back' (Паспорт/ID обратная), "
            "'license_front' (Водительское лицевая), "
            "'license_back' (Водительское обратная), "
            "'truck_front' (Техпаспорт тягача лицевая), "
            "'trailer_front' (Техпаспорт прицепа лицевая), "
            "'truck_back' (Техпаспорт тягача обратная), "
            "'trailer_back' (Техпаспорт прицепа обратная), "
            "'other' (неизвестно/другое)"
        )
    )

class FullCargoSubmission(BaseModel):
    truck: VehicleDetails = Field(default_factory=VehicleDetails)
    trailer: VehicleDetails = Field(default_factory=VehicleDetails)
    driver: DriverDetails = Field(default_factory=DriverDetails)
    image_roles: list[ImageClassification] = Field(default_factory=list, description="Классификация каждого фото")


async def process_docs_with_ai(photos_file_ids, doc_file_ids, text_notes):
    """Распознает документы и автоматически сортирует фото в нужном порядке для PDF."""
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
        return fallback_text, all_files

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
        return fallback_text, all_files

    system_prompt = (
        "Ты — эксперт логистической компании по распознаванию международных документов водителей и ТС.\n"
        "1. Распознай данные с фото.\n"
        "2. Для КАЖДОГО фото укажи его image_index (0, 1, 2...) и определи категорию (category): "
        "'passport_front', 'passport_back', 'license_front', 'license_back', "
        "'truck_front', 'trailer_front', 'truck_back', 'trailer_back', 'other'.\n"
        "3. ПРИОРИТЕТ ЯЗЫКА: Все данные пиши на РУССКОМ ЯЗЫКЕ (кириллица). Только если его нет - латиница.\n"
        "4. ОРГАНЫ ВЫДАЧИ: РБ паспорт — точный текст ('СТАРОДОРОЖСКИЙ РОВД...'), РБ ID-карта — 'Код органа выдачи: XXX', "
        "Казахстан — 'МВД РК', Узбекистан — 'MIA 123456', Кыргызстан — 'MIA'/'PSC', Азербайджан — 'MINISTRY OF INTERNAL AFFAIRS'.\n"
        "5. ТЕЛЕФОНЫ: Российский номер (+7...) всегда первым."
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=FullCargoSubmission,
        temperature=0.1
    )

    try:
        if hasattr(gemini_client, 'aio'):
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=config
            )
        else:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model="gemini-2.5-flash",
                contents=contents,
                config=config
            )

        if response and response.text:
            import json
            raw_json = json.loads(response.text)
            
            t = raw_json.get("truck", {})
            tr = raw_json.get("trailer", {})
            d = raw_json.get("driver", {})
            p = d.get("passport", {})
            l = d.get("license", {})

            truck_str = f"{t.get('brand_model', 'Не распознан')}, {t.get('plate', 'Не распознан')}, VIN: {t.get('vin', 'Не распознан')}, {t.get('country', 'Не распознана')}"
            trailer_str = f"{tr.get('brand_model', 'Не распознан')}, {tr.get('plate', 'Не распознан')}, VIN: {tr.get('vin', 'Не распознан')}, {tr.get('country', 'Не распознана')}"
            driver_str = f"{d.get('full_name', 'Не распознан')}, дата рождения: {d.get('birth_date', 'Не распознана')}"
            phones_str = d.get("phones") or text_notes or "Не указан"
            passport_str = f"№ {p.get('number', 'Не распознан')}, выдан {p.get('issue_date', 'Не распознана')}, {p.get('authority', 'Не распознан')}, {p.get('country', 'Не распознана')}"
            license_str = f"№ {l.get('number', 'Не распознан')}, выдано {l.get('issue_date', 'Не распознана')}, {l.get('authority', 'Не распознан')}, {l.get('country', 'Не распознана')}"

            formatted_output = (
                f"Тягач: {truck_str}\n"
                f"Прицеп: {trailer_str}\n"
                f"Водитель: {driver_str}\n"
                f"Номера телефонов: {phones_str}\n"
                f"Паспорт: {passport_str}\n"
                f"Водительское: {license_str}"
            )

            # ----- СОРТИРОВКА ФОТО ДЛЯ PDF В СТРОГОМ ПОРЯДКЕ -----
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
            for role in raw_json.get("image_roles", []):
                idx = role.get("image_index")
                cat = role.get("category", "other")
                if idx is not None and 0 <= idx < len(all_files):
                    classified_file_priority[all_files[idx]] = priority_map.get(cat, 99)

            sorted_files = sorted(all_files, key=lambda fid: classified_file_priority.get(fid, 99))

            return formatted_output, sorted_files

    except Exception as e:
        logging.error(f"Gemini Processing Error: {e}")

    return fallback_text, all_files


async def create_pdf_report_with_images(route: str, date_str: str, price: str, carrier_info: str, ai_text: str, photo_ids: list) -> io.BytesIO:
    """Создает 1 чистый PDF-файл, содержащий ТОЛЬКО фотографии документов в правильном порядке."""
    buffer = io.BytesIO()
    images = []

    for pid in photo_ids:
        try:
            file_info = await bot.get_file(pid)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=buf)
            buf.seek(0)

            img = Image.open(buf)
            
            # Автоматический поворот перевернутых снимков
            img = ImageOps.exif_transpose(img)
            
            if img.mode != 'RGB':
                img = img.convert('RGB')

            images.append(img)
        except Exception as e:
            logging.error(f"Error processing image for PDF: {e}")

    if images:
        images[0].save(
            buffer, 
            format="PDF", 
            save_all=True, 
            append_images=images[1:]
        )
        buffer.seek(0)
    else:
        buffer.write(b"")
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
    await message.answer("❌ Загрузка отменена.", reply_markup=get_main_reply_markup())

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

    if deal_id:
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE confirmed_deals SET docs_submitted = 1 WHERE id = ? OR load_id = ?", (deal_id, deal_id))
        conn.commit()
        conn.close()

    # Распаковываем текст И отсортированный список файлов
    ai_formatted_data, sorted_files = await process_docs_with_ai(photos, documents, notes)
    carrier_text = format_carrier_info(user_id, user_obj.username, user_obj.full_name)

    admin_msg = (
        f"📅 {date_str} | 📍 {route_str}\n"
        f"💰 {price_str}\n\n"
        f"{carrier_text}\n\n"
        f"{ai_formatted_data}"
    )

    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_msg, parse_mode="Markdown")

        # Передаем отсортированный список страниц в PDF
        pdf_buf = await create_pdf_report_with_images(route_str, date_str, price_str, carrier_text, ai_formatted_data, sorted_files)
        pdf_file = types.BufferedInputFile(pdf_buf.getvalue(), filename=f"Docs_{date_str}.pdf")
        
        await bot.send_document(
            chat_id=ADMIN_CHANNEL_ID, 
            document=pdf_file, 
            caption=f"📄 **PDF-отчет по грузу** ({route_str})"
        )
    except Exception as e:
        logging.error(f"Error forwarding docs to admin channel: {e}")

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


# ==================== WEB APP HTML ====================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Биржа грузов</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg: var(--tg-theme-bg-color, #f8f9fa);
            --text: var(--tg-theme-text-color, #212529);
            --hint: var(--tg-theme-hint-color, #6c757d);
            --card: var(--tg-theme-secondary-bg-color, #ffffff);
            --border: rgba(0,0,0,0.08);
            --btn-green: #28a745;
            --btn-orange: #fd7e14;
            --btn-blue: #007aff;
            --active-tab: var(--tg-theme-button-color, #2481cc);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 10px;
            font-size: 13px;
            -webkit-user-select: none;
            user-select: none;
        }

        .header-top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 10px;
            padding: 0 2px;
        }

        .header-title {
            font-size: 15px;
            font-weight: 700;
            color: var(--text);
            letter-spacing: 0.3px;
            cursor: pointer;
        }

        .bell-icon {
            position: relative;
            font-size: 18px;
            cursor: pointer;
            padding: 6px 12px;
            background: var(--card);
            border-radius: 12px;
            border: 1px solid var(--border);
            box-shadow: 0 2px 5px rgba(0,0,0,0.03);
        }

        .bell-badge {
            position: absolute;
            top: -3px;
            right: -3px;
            background: #dc3545;
            color: #ffffff;
            font-size: 9px;
            font-weight: 700;
            border-radius: 10px;
            padding: 1px 5px;
            display: none;
        }

        .main-nav {
            display: flex;
            gap: 6px;
            margin-bottom: 12px;
            background: var(--card);
            padding: 4px;
            border-radius: 12px;
            border: 1px solid var(--border);
            box-shadow: 0 2px 6px rgba(0,0,0,0.02);
        }

        .nav-btn {
            flex: 1;
            padding: 9px 4px;
            text-align: center;
            font-weight: 600;
            font-size: 11px;
            border-radius: 9px;
            cursor: pointer;
            color: var(--hint);
            transition: all 0.2s;
            white-space: nowrap;
        }

        .nav-btn.active {
            background: var(--active-tab);
            color: #ffffff;
            box-shadow: 0 2px 6px rgba(0,0,0,0.15);
        }

        .filter-scroll {
            display: flex;
            gap: 6px;
            margin-bottom: 10px;
            overflow-x: auto;
            white-space: nowrap;
            scrollbar-width: none;
            -webkit-overflow-scrolling: touch;
        }
        .filter-scroll::-webkit-scrollbar { display: none; }

        @media (min-width: 600px) {
            .filter-scroll {
                flex-wrap: wrap;
                overflow-x: visible;
                white-space: normal;
            }
        }

        .chip {
            white-space: nowrap;
            padding: 7px 12px;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            color: var(--hint);
            cursor: pointer;
            transition: all 0.15s;
        }

        .chip.active {
            background: var(--active-tab);
            color: #fff;
            border-color: var(--active-tab);
        }

        .sub-nav {
            display: flex;
            gap: 6px;
            margin-bottom: 10px;
        }

        .sub-nav-btn {
            flex: 1;
            padding: 7px;
            text-align: center;
            font-weight: 600;
            font-size: 11px;
            border-radius: 10px;
            background: var(--card);
            border: 1px solid var(--border);
            color: var(--hint);
            cursor: pointer;
            transition: all 0.2s;
        }

        .sub-nav-btn.active {
            background: var(--active-tab);
            color: #fff;
            border-color: var(--active-tab);
        }

        .sort-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
            background: var(--card);
            padding: 6px 12px;
            border-radius: 12px;
            border: 1px solid var(--border);
            box-shadow: 0 2px 6px rgba(0,0,0,0.02);
        }

        .sort-bar label {
            font-size: 10px;
            font-weight: 700;
            color: var(--hint);
            text-transform: uppercase;
        }

        .sort-bar select, select {
            flex: 1;
            appearance: none;
            -webkit-appearance: none;
            background: var(--bg) url("data:image/svg+xml;utf8,<svg fill='%236c757d' height='16' viewBox='0 0 24 24' width='16' xmlns='http://www.w3.org/2000/svg'><path d='M7 10l5 5 5-5z'/></svg>") no-repeat right 10px center;
            border-radius: 10px;
            padding: 8px 28px 8px 12px;
            border: 1px solid var(--border);
            font-size: 11px;
            font-weight: 600;
            color: var(--text);
            outline: none;
            box-shadow: none;
        }

        .table-container {
            background: var(--card);
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--border);
            box-shadow: 0 2px 8px rgba(0,0,0,0.03);
        }

        .t-head {
            display: grid;
            grid-template-columns: 45px 1fr 145px 16px;
            background: rgba(0,0,0,0.03);
            padding: 9px 10px;
            font-size: 11px;
            font-weight: 700;
            color: var(--hint);
            border-bottom: 1px solid var(--border);
            text-transform: uppercase;
        }

        .t-row {
            display: grid;
            grid-template-columns: 45px 1fr 145px 16px;
            padding: 11px 10px;
            border-bottom: 1px solid var(--border);
            align-items: center;
            cursor: pointer;
            transition: background 0.15s;
            white-space: nowrap;
            overflow: hidden;
        }

        .t-row:active { background: rgba(0,0,0,0.04); }
        .t-row:last-child { border-bottom: none; }
        
        .col-date { font-weight: 500; color: var(--hint); font-size: 11px; white-space: nowrap; }
        .col-route { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 4px; }
        .col-price { font-weight: 700; color: var(--btn-green); text-align: right; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .col-arrow { text-align: right; color: var(--hint); font-size: 10px; }

        .t-details {
            display: none;
            padding: 12px 14px;
            background: var(--bg);
            border-bottom: 1px solid var(--border);
            animation: fadeIn 0.2s ease-in-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .t-details.active { display: block; }

        .info-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 10px;
            font-size: 12px;
        }

        .info-item span { color: var(--hint); font-size: 10px; display: block; text-transform: uppercase; font-weight: 600; }
        .info-item b { color: var(--text); }

        .today-badge {
            background: rgba(253, 126, 20, 0.15);
            color: #fd7e14;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            display: inline-block;
            margin-bottom: 8px;
        }

        .doc-badge {
            background: rgba(40, 167, 69, 0.15);
            color: #28a745;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            display: inline-block;
            margin-bottom: 8px;
        }

        .admin-comment {
            background: rgba(255, 193, 7, 0.15);
            color: var(--text);
            padding: 8px 10px;
            border-radius: 8px;
            margin-bottom: 10px;
            border-left: 3px solid #ffc107;
            font-size: 12px;
            line-height: 1.4;
        }

        .qty-picker {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
            background: var(--card);
            padding: 6px 10px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }

        .qty-picker label { font-size: 11px; color: var(--hint); font-weight: 600; flex: 1; }

        textarea, input[type="text"], input[type="password"] {
            width: 100%;
            box-sizing: border-box;
            background: var(--card);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 10px 12px;
            border-radius: 10px;
            font-size: 12px;
            margin-bottom: 10px;
            font-family: inherit;
        }

        textarea { resize: none; height: 46px; }

        .buttons {
            display: flex;
            gap: 8px;
        }

        .buttons button, .btn-rounded {
            flex: 1;
            padding: 11px;
            border: none;
            border-radius: 10px;
            font-weight: 600;
            font-size: 12px;
            color: #fff;
            cursor: pointer;
            transition: opacity 0.15s;
        }

        .buttons button:active, .btn-rounded:active { opacity: 0.8; }

        .btn-confirm { background: var(--btn-green); }
        .btn-offer { background: var(--btn-orange); }

        .loader { text-align: center; padding: 25px; color: var(--hint); font-size: 13px; }

        .profile-card {
            background: var(--card);
            border-radius: 12px;
            padding: 14px;
            border: 1px solid var(--border);
            margin-bottom: 12px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.02);
        }

        .profile-title {
            font-weight: 700;
            font-size: 13px;
            margin-bottom: 10px;
            color: var(--text);
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .form-group {
            margin-bottom: 10px;
        }

        .form-group label {
            display: block;
            font-size: 10px;
            font-weight: 700;
            color: var(--hint);
            margin-bottom: 4px;
            text-transform: uppercase;
        }

        .subs-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 12px;
        }

        .sub-chip {
            background: var(--bg);
            padding: 10px 12px;
            border-radius: 10px;
            border: 1.5px solid var(--border);
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
            user-select: none;
            color: var(--text);
        }

        .sub-chip.active {
            background: var(--active-tab, #2481cc);
            color: #ffffff;
            border-color: var(--active-tab, #2481cc);
            box-shadow: 0 2px 6px rgba(36, 129, 204, 0.3);
        }

        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.5);
            z-index: 999;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .modal-overlay.active { display: flex; }
        .modal-card {
            background: var(--card);
            border-radius: 16px;
            padding: 18px;
            width: 100%;
            max-width: 340px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.2);
            max-height: 80vh;
            overflow-y: auto;
        }
        .modal-title { font-weight: 700; font-size: 14px; margin-bottom: 12px; text-align: center; }

        .notif-item {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px;
            margin-bottom: 8px;
        }
        .notif-item.unread {
            border-left: 4px solid var(--active-tab);
        }
        .notif-title { font-weight: 700; font-size: 12px; margin-bottom: 2px; }
        .notif-text { font-size: 11px; color: var(--text); line-height: 1.3; }
        .notif-date { font-size: 9px; color: var(--hint); margin-top: 4px; text-align: right; }
    </style>
</head>
<body>

    <div class="header-top">
        <div class="header-title" onclick="handleHeaderClick()">ЧТУП «Белкаспиан» | Биржа</div>
        <div class="bell-icon" onclick="openNotifications()">
            🔔
            <div class="bell-badge" id="bellBadge">0</div>
        </div>
    </div>

    <div class="main-nav">
        <div class="nav-btn active" id="tab-catalog" onclick="switchTab('catalog')">📋 Биржа</div>
        <div class="nav-btn" id="tab-my" onclick="switchTab('my')">📦 Мои заявки</div>
        <div class="nav-btn" id="tab-profile" onclick="switchTab('profile')">👤 Личный кабинет</div>
    </div>

    <div class="filter-scroll" id="dir-filters">
        <div class="chip active" onclick="setFilter('ALL', this)">Все направления</div>
        <div class="chip" onclick="setFilter('Казахстан', this)">🇰🇿 Казахстан</div>
        <div class="chip" onclick="setFilter('Узбекистан', this)">🇺🇿 Узбекистан</div>
        <div class="chip" onclick="setFilter('Кыргызстан', this)">🇰🇬 Кыргызстан</div>
        <div class="chip" onclick="setFilter('Грузия', this)">🇬🇪 Грузия</div>
        <div class="chip" onclick="setFilter('Азербайджан', this)">🇦🇿 Азербайджан</div>
        <div class="chip" onclick="setFilter('Армения', this)">🇦🇲 Армения</div>
    </div>

    <div class="sub-nav" id="my-sub-nav" style="display: none;">
        <div class="sub-nav-btn active" id="subnav-active" onclick="switchMySubTab('active')">📋 Активные</div>
        <div class="sub-nav-btn" id="subnav-archive" onclick="switchMySubTab('archive')">🚚 Едущие авто (Архив)</div>
    </div>

    <div class="sort-bar" id="sort-container">
        <label>Сортировка:</label>
        <select id="sortSelect" onchange="loadData(false)">
            <option value="newest">📅 Сначала новые</option>
            <option value="date_asc">📅 По дате погрузки</option>
            <option value="route">📍 По направлению (А-Я)</option>
            <option value="price_desc">💰 По цене (сначала высокие)</option>
        </select>
    </div>

    <div class="table-container" id="main-table">
        <div class="t-head">
            <div>Дата</div>
            <div>Направление</div>
            <div style="text-align: right;">Ставка</div>
            <div></div>
        </div>
        <div id="loads-body"><div class="loader">Загрузка данных...</div></div>
    </div>

    <div id="profile-container" style="display: none;">
        <div class="profile-card">
            <div class="profile-title">👤 Данные компании (Автосохранение)</div>
            <div class="form-group">
                <label>Компания:</label>
                <input type="text" id="profCompany" placeholder="Название вашей компании..." oninput="autoSaveProfile()" />
            </div>
            <div class="form-group">
                <label>Имя контактного лица:</label>
                <input type="text" id="profName" placeholder="Ваше имя..." oninput="autoSaveProfile()" />
            </div>
            <div class="form-group">
                <label>Телефон:</label>
                <input type="text" id="profPhone" placeholder="+375 / +7..." oninput="autoSaveProfile()" />
            </div>
        </div>

        <div class="profile-card">
            <div class="profile-title">🔔 Подписки на направления</div>
            <div class="subs-grid" id="profile-subs-grid">
                <div class="sub-chip" data-val="Казахстан 🇰🇿" onclick="toggleSubChip(this)">🇰🇿 Казахстан</div>
                <div class="sub-chip" data-val="Узбекистан 🇺🇿" onclick="toggleSubChip(this)">🇺🇿 Узбекистан</div>
                <div class="sub-chip" data-val="Кыргызстан 🇰🇬" onclick="toggleSubChip(this)">🇰🇬 Кыргызстан</div>
                <div class="sub-chip" data-val="Грузия 🇬🇪" onclick="toggleSubChip(this)">🇬🇪 Грузия</div>
                <div class="sub-chip" data-val="Азербайджан 🇦🇿" onclick="toggleSubChip(this)">🇦🇿 Азербайджан</div>
                <div class="sub-chip" data-val="Армения 🇦🇲" onclick="toggleSubChip(this)">🇦🇲 Армения</div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="bidModal">
        <div class="modal-card">
            <div class="modal-title">💰 Предложить свою цену</div>
            <input type="text" id="modalPrice" placeholder="Желаемая ставка (напр., 2400 USD)" />
            <textarea id="modalComment" placeholder="Комментарий (условия, дата подачи)..."></textarea>
            <div class="buttons">
                <button class="btn-confirm" onclick="submitModalBid()">Отправить</button>
                <button style="background: var(--hint);" onclick="closeModal()">Отмена</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="notifModal">
        <div class="modal-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="modal-title" style="margin-bottom:0;">🔔 Уведомления</div>
                <div style="cursor: pointer; font-weight: 700; font-size: 16px; color: var(--hint); padding: 4px;" onclick="closeNotifModal()">✕</div>
            </div>
            <div id="notifList"><div class="loader">Загрузка...</div></div>
            <button class="btn-rounded" style="width:100%; margin-top:10px; background:var(--hint);" onclick="closeNotifModal()">Закрыть</button>
        </div>
    </div>

    <div class="modal-overlay" id="adminPassModal">
        <div class="modal-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="modal-title" style="margin-bottom:0;">🔐 Вход для администратора</div>
                <div style="cursor: pointer; font-weight: 700; font-size: 16px; color: var(--hint);" onclick="closeAdminPassModal()">✕</div>
            </div>
            <input type="password" id="adminPassInput" placeholder="Введите пароль..." />
            <div class="buttons">
                <button class="btn-confirm" onclick="submitAdminPassword()">Войти</button>
                <button style="background: var(--hint);" onclick="closeAdminPassModal()">Отмена</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="adminPanelModal">
        <div class="modal-card" style="max-width: 95%; width: 500px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="modal-title" style="margin-bottom:0;">🎛 Панель Администратора</div>
                <div style="cursor: pointer; font-weight: 700; font-size: 16px; color: var(--hint);" onclick="closeAdminPanelModal()">✕</div>
            </div>
            
            <div class="sub-nav" style="margin-bottom:12px;">
                <div class="sub-nav-btn active" id="admTabCarriers" onclick="switchAdminSubTab('carriers')">👥 Перевозчики</div>
                <div class="sub-nav-btn" id="admTabExchange" onclick="switchAdminSubTab('exchange')">📋 Биржа</div>
                <div class="sub-nav-btn" id="admTabConfirmed" onclick="switchAdminSubTab('confirmed')">🤝 Сделки</div>
            </div>

            <div id="adminSortBox" style="margin-bottom:10px;">
                <select id="adminSortSelect" onchange="applyAdminSort()">
                    <option value="name">👤 По названию/имени (А-Я)</option>
                    <option value="status">🟢 По статусу (Сначала активные)</option>
                </select>
            </div>

            <div id="adminCarrierFilterBox" style="display:none; margin-bottom:10px;">
                <select id="adminCarrierSelect" onchange="renderAdminConfirmedDeals()">
                    <option value="ALL">Все перевозчики</option>
                </select>
            </div>

            <div id="adminContentBody"><div class="loader">Загрузка...</div></div>
        </div>
    </div>

    <div class="modal-overlay" id="adminEditLoadModal">
        <div class="modal-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="modal-title" style="margin-bottom:0;">✏️ Редактирование груза</div>
                <div style="cursor: pointer; font-weight: 700; font-size: 16px; color: var(--hint);" onclick="closeAdminEditModal()">✕</div>
            </div>
            <input type="hidden" id="editLoadId" />
            <div class="form-group"><label>Маршрут:</label><input type="text" id="editRoute" /></div>
            <div class="form-group"><label>Дата:</label><input type="text" id="editDate" /></div>
            <div class="form-group"><label>Ставка:</label><input type="text" id="editPrice" /></div>
            <div class="form-group"><label>Доступно авто:</label><input type="text" id="editCars" /></div>
            <div class="form-group"><label>Тип ТС:</label><input type="text" id="editCarType" /></div>
            <div class="form-group"><label>Груз:</label><input type="text" id="editCargoType" /></div>
            <div class="form-group"><label>Вес:</label><input type="text" id="editWeight" /></div>
            <div class="form-group"><label>Комментарий логиста:</label><input type="text" id="editComment" /></div>
            <div class="buttons">
                <button class="btn-confirm" onclick="saveAdminLoadEdit()">💾 Сохранить</button>
                <button style="background: var(--hint);" onclick="closeAdminEditModal()">Отмена</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="adminEditDealModal">
        <div class="modal-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="modal-title" style="margin-bottom:0;">✏️ Редактирование сделки</div>
                <div style="cursor: pointer; font-weight: 700; font-size: 16px; color: var(--hint);" onclick="closeAdminEditDealModal()">✕</div>
            </div>
            <input type="hidden" id="editDealId" />
            <div class="form-group"><label>Маршрут:</label><input type="text" id="editDealRoute" /></div>
            <div class="form-group"><label>Дата:</label><input type="text" id="editDealDate" /></div>
            <div class="form-group"><label>Ставка:</label><input type="text" id="editDealPrice" /></div>
            <div class="form-group"><label>Забрано авто:</label><input type="text" id="editDealCars" /></div>
            <div class="buttons">
                <button class="btn-confirm" onclick="saveAdminDealEdit()">💾 Сохранить</button>
                <button style="background: var(--hint);" onclick="closeAdminEditDealModal()">Отмена</button>
            </div>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        try { tg.expand(); tg.ready(); tg.setHeaderColor('secondary_bg_color'); } catch(e) {}

        const tbody = document.getElementById('loads-body');
        let currentTab = 'catalog';
        let mySubTab = 'active';
        let currentCountry = 'ALL';
        let activeOfferLoadId = null;
        let currentDataHash = '';
        let saveProfileTimer = null;

        let headerClicks = 0;
        let headerTimer = null;
        let adminSubTab = 'carriers';

        let allAdminCarriersArray = [];
        let allAdminLoadsArray = [];
        let allAdminDeals = [];

        function handleHeaderClick() {
            headerClicks++;
            if (headerTimer) clearTimeout(headerTimer);
            if (headerClicks >= 5) {
                headerClicks = 0;
                if (sessionStorage.getItem('admin_authed') === 'true') {
                    openAdminPanel();
                } else {
                    document.getElementById('adminPassModal').classList.add('active');
                }
            } else {
                headerTimer = setTimeout(() => { headerClicks = 0; }, 2000);
            }
        }

        function closeAdminPassModal() {
            document.getElementById('adminPassModal').classList.remove('active');
        }

        async function submitAdminPassword() {
            let pass = document.getElementById('adminPassInput').value.trim();
            if (!pass) return;

            try {
                let res = await fetch('/api/admin/verify_pass', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: pass})
                });
                if (res.ok) {
                    sessionStorage.setItem('admin_authed', 'true');
                    closeAdminPassModal();
                    openAdminPanel();
                } else {
                    notify('❌ Неверный пароль администратора');
                }
            } catch(e) {
                notify('Ошибка проверки пароля');
            }
        }

        function openAdminPanel() {
            document.getElementById('adminPanelModal').classList.add('active');
            switchAdminSubTab(adminSubTab);
        }

        function closeAdminPanelModal() {
            document.getElementById('adminPanelModal').classList.remove('active');
        }

        function switchAdminSubTab(tab) {
            adminSubTab = tab;
            document.getElementById('admTabCarriers').classList.toggle('active', tab === 'carriers');
            document.getElementById('admTabExchange').classList.toggle('active', tab === 'exchange');
            document.getElementById('admTabConfirmed').classList.toggle('active', tab === 'confirmed');

            document.getElementById('adminSortBox').style.display = (tab !== 'confirmed') ? 'block' : 'none';
            document.getElementById('adminCarrierFilterBox').style.display = (tab === 'confirmed') ? 'block' : 'none';

            if (tab === 'carriers') {
                loadAdminCarriers();
            } else if (tab === 'exchange') {
                loadAdminLoads();
            } else if (tab === 'confirmed') {
                loadAdminConfirmedDeals();
            }
        }

        function applyAdminSort() {
            if (adminSubTab === 'carriers') renderAdminCarriers();
            else if (adminSubTab === 'exchange') renderAdminLoads();
        }

        async function loadAdminCarriers() {
            let body = document.getElementById('adminContentBody');
            body.innerHTML = '<div class="loader">Загрузка перевозчиков...</div>';
            try {
                let res = await fetch('/api/admin/carriers?t=' + Date.now());
                let data = await res.json();
                allAdminCarriersArray = data.carriers || [];
                renderAdminCarriers();
            } catch(e) { body.innerHTML = 'Ошибка загрузки'; }
        }

        function renderAdminCarriers() {
            let body = document.getElementById('adminContentBody');
            let sortType = document.getElementById('adminSortSelect')?.value || 'name';
            let list = [...allAdminCarriersArray];

            if (sortType === 'name') {
                list.sort((a, b) => (a.company || a.name).localeCompare(b.company || b.name));
            } else if (sortType === 'status') {
                list.sort((a, b) => (a.status === 'ACTIVE' ? -1 : 1));
            }

            body.innerHTML = list.map(c => `
                <div class="notif-item">
                    <div class="notif-title">🏢 ${c.company} | ${c.name}</div>
                    <div class="notif-text">📞 ${c.phone} | ID: ${c.user_id}</div>
                    <div class="notif-text" style="color:var(--hint); margin-top:2px;">Подписки: ${c.subscriptions || 'нет'}</div>
                    <button class="btn-rounded" style="margin-top:6px; background:${c.status === 'ACTIVE' ? '#dc3545' : '#28a745'}; padding:6px;" onclick="toggleCarrierStatus(${c.user_id})">
                        ${c.status === 'ACTIVE' ? '🔴 Заблокировать' : '🟢 Разблокировать'}
                    </button>
                </div>
            `).join('');
        }

        async function toggleCarrierStatus(uId) {
            await fetch('/api/admin/carrier_status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: uId})
            });
            loadAdminCarriers();
        }

        async function loadAdminLoads() {
            let body = document.getElementById('adminContentBody');
            body.innerHTML = '<div class="loader">Загрузка грузов...</div>';
            try {
                let res = await fetch('/api/admin/loads?t=' + Date.now());
                let data = await res.json();
                allAdminLoadsArray = data.loads || [];
                renderAdminLoads();
            } catch(e) { body.innerHTML = 'Ошибка загрузки'; }
        }

        function renderAdminLoads() {
            let body = document.getElementById('adminContentBody');
            let list = [...allAdminLoadsArray];

            body.innerHTML = list.map(l => {
                let isAct = (l.status === 'ACTIVE');
                return `
                <div class="notif-item">
                    <div class="notif-title">📍 ${l.date} | ${l.route} (${l.price})</div>
                    <div class="notif-text">🚚 Доступно: ${l.cars} авто | Статус: <b>${l.status}</b></div>
                    <div class="notif-text" style="color:#007aff;">👤 Перевозчик: ${l.carrier_info}</div>
                    <div class="buttons" style="margin-top:6px;">
                        <button class="btn-confirm" style="padding:6px; background:${isAct ? '#fd7e14' : '#28a745'};" onclick="toggleAdminLoadStatus(${l.id})">
                            ${isAct ? '🔴 Закрыть' : '🟢 Открыть'}
                        </button>
                        <button class="btn-confirm" style="padding:6px;" onclick="openAdminEditModalById(${l.id})">✏️ Изменить</button>
                        <button class="btn-offer" style="padding:6px; background:#dc3545;" onclick="hardDeleteAdminLoad(${l.id})">🗑 Удалить</button>
                    </div>
                </div>
            `}).join('');
        }

        function openAdminEditModalById(loadId) {
            let l = allAdminLoadsArray.find(x => String(x.id) === String(loadId));
            if (!l) return;

            document.getElementById('editLoadId').value = l.id;
            document.getElementById('editRoute').value = l.route;
            document.getElementById('editDate').value = l.date;
            document.getElementById('editPrice').value = l.price;
            document.getElementById('editCars').value = l.cars;
            document.getElementById('editCarType').value = l.car_type;
            document.getElementById('editCargoType').value = l.cargo_type;
            document.getElementById('editWeight').value = l.weight;
            document.getElementById('editComment').value = l.admin_comment;
            document.getElementById('adminEditLoadModal').classList.add('active');
        }

        function closeAdminEditModal() {
            document.getElementById('adminEditLoadModal').classList.remove('active');
        }

        async function saveAdminLoadEdit() {
            let data = {
                id: document.getElementById('editLoadId').value,
                route: document.getElementById('editRoute').value,
                date: document.getElementById('editDate').value,
                price: document.getElementById('editPrice').value,
                cars: document.getElementById('editCars').value,
                car_type: document.getElementById('editCarType').value,
                cargo_type: document.getElementById('editCargoType').value,
                weight: document.getElementById('editWeight').value,
                admin_comment: document.getElementById('editComment').value
            };
            await fetch('/api/admin/edit_load', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            closeAdminEditModal();
            loadAdminLoads();
            loadData(false);
        }

        async function toggleAdminLoadStatus(id) {
            await fetch('/api/admin/toggle_load_active', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id})
            });
            loadAdminLoads();
            loadData(false);
        }

        async function hardDeleteAdminLoad(id) {
            if (confirm('Удалить этот груз безвозвратно из базы?')) {
                await fetch('/api/admin/hard_delete_load', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: id})
                });
                loadAdminLoads();
                loadData(false);
            }
        }

        async function loadAdminConfirmedDeals() {
            let body = document.getElementById('adminContentBody');
            body.innerHTML = '<div class="loader">Загрузка сделок...</div>';
            try {
                let res = await fetch('/api/admin/confirmed_deals?t=' + Date.now());
                let data = await res.json();
                allAdminDeals = data.deals || [];

                let select = document.getElementById('adminCarrierSelect');
                let carriersMap = {};
                allAdminDeals.forEach(d => {
                    carriersMap[d.user_id] = `${d.company} (${d.name})`;
                });
                
                let selectHtml = '<option value="ALL">Все перевозчики</option>';
                for (let uid in carriersMap) {
                    selectHtml += `<option value="${uid}">${carriersMap[uid]}</option>`;
                }
                select.innerHTML = selectHtml;

                renderAdminConfirmedDeals();
            } catch(e) {
                body.innerHTML = 'Ошибка загрузки сделок';
            }
        }

        function renderAdminConfirmedDeals() {
            let body = document.getElementById('adminContentBody');
            let selectedUser = document.getElementById('adminCarrierSelect')?.value || 'ALL';

            let filtered = allAdminDeals.filter(d => {
                if (selectedUser !== 'ALL') return String(d.user_id) === String(selectedUser);
                return true;
            });

            if (filtered.length === 0) {
                body.innerHTML = '<div class="loader">Сделок по фильтру не найдено</div>';
                return;
            }

            body.innerHTML = filtered.map(d => `
                <div class="notif-item">
                    <div class="notif-title">🤝 Сделка #${d.deal_id} | 📍 ${d.route}</div>
                    <div class="notif-text">📅 Дата: ${d.date} | 💰 Ставка: ${d.price} | 🚛 Авто: ${d.cars}</div>
                    <div class="notif-text" style="color:#007aff; margin-top:2px;">
                        👤 <b>${d.company}</b> | ${d.name} (📞 ${d.phone})
                    </div>
                    <div class="buttons" style="margin-top:6px;">
                        <button class="btn-confirm" style="padding:6px;" onclick='openAdminEditDealModal(${JSON.stringify(d).replace(/'/g, "&#39;")})'>✏️ Изменить сделку</button>
                        <button class="btn-offer" style="padding:6px; background:#dc3545;" onclick="cancelAdminDeal(${d.deal_id})">❌ Отменить сделку</button>
                    </div>
                </div>
            `).join('');
        }

        function openAdminEditDealModal(d) {
            document.getElementById('editDealId').value = d.deal_id;
            document.getElementById('editDealRoute').value = d.route;
            document.getElementById('editDealDate').value = d.date;
            document.getElementById('editDealPrice').value = d.price;
            document.getElementById('editDealCars').value = d.cars;
            document.getElementById('adminEditDealModal').classList.add('active');
        }

        function closeAdminEditDealModal() {
            document.getElementById('adminEditDealModal').classList.remove('active');
        }

        async function saveAdminDealEdit() {
            let data = {
                deal_id: document.getElementById('editDealId').value,
                route: document.getElementById('editDealRoute').value,
                date: document.getElementById('editDealDate').value,
                price: document.getElementById('editDealPrice').value,
                cars: document.getElementById('editDealCars').value
            };
            await fetch('/api/admin/edit_deal', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            closeAdminEditDealModal();
            loadAdminConfirmedDeals();
            loadData(false);
        }

        async function cancelAdminDeal(dealId) {
            if (confirm('Отменить эту подтверждённую сделку? Перевозчику придет уведомление.')) {
                await fetch('/api/admin/cancel_deal', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({deal_id: dealId})
                });
                loadAdminConfirmedDeals();
                loadData(false);
            }
        }

        function getUserId() {
            let uid = tg.initDataUnsafe?.user?.id;
            if (!uid) {
                const params = new URLSearchParams(window.location.search);
                uid = params.get('user_id');
            }
            if (!uid && window.localStorage) {
                uid = localStorage.getItem('tg_user_id');
            } else if (uid && window.localStorage) {
                localStorage.setItem('tg_user_id', uid);
            }
            return uid ? parseInt(uid) : 0;
        }

        function notify(text) {
            if (tg && tg.showAlert) tg.showAlert(text);
            else alert(text);
        }

        function askConfirm(text, callback) {
            try {
                if (tg && tg.showConfirm) {
                    tg.showConfirm(text, (confirmed) => {
                        if (confirmed) callback(true);
                    });
                    return;
                }
            } catch(e) {}
            callback(confirm(text));
        }

        function toggleSubChip(el) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            el.classList.toggle('active');
            autoSaveProfile();
        }

        function autoSaveProfile() {
            if (saveProfileTimer) clearTimeout(saveProfileTimer);
            saveProfileTimer = setTimeout(() => {
                saveProfile();
            }, 400);
        }

        function switchTab(tab) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            currentTab = tab;
            currentDataHash = '';
            document.getElementById('tab-catalog').classList.toggle('active', tab === 'catalog');
            document.getElementById('tab-my').classList.toggle('active', tab === 'my');
            document.getElementById('tab-profile').classList.toggle('active', tab === 'profile');

            document.getElementById('dir-filters').style.display = (tab === 'catalog') ? 'flex' : 'none';
            document.getElementById('sort-container').style.display = (tab === 'catalog' || tab === 'my') ? 'flex' : 'none';
            document.getElementById('my-sub-nav').style.display = (tab === 'my') ? 'flex' : 'none';

            document.getElementById('main-table').style.display = (tab === 'profile') ? 'none' : 'block';
            document.getElementById('profile-container').style.display = (tab === 'profile') ? 'block' : 'none';

            if (tab === 'profile') {
                loadProfile();
            } else {
                loadData(false);
            }
        }

        function switchMySubTab(sub) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            mySubTab = sub;
            currentDataHash = '';
            document.getElementById('subnav-active').classList.toggle('active', sub === 'active');
            document.getElementById('subnav-archive').classList.toggle('active', sub === 'archive');
            loadData(false);
        }

        function setFilter(country, el) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            currentCountry = country;
            currentDataHash = '';
            document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
            el.classList.add('active');
            loadData(false);
        }

        async function checkNotifications() {
            let userId = getUserId();
            if (!userId) return;
            try {
                let res = await fetch(`/api/notifications?user_id=${userId}&t=` + Date.now());
                let data = await res.json();
                let badge = document.getElementById('bellBadge');
                if (data.unread_count > 0) {
                    badge.textContent = data.unread_count;
                    badge.style.display = 'block';
                } else {
                    badge.style.display = 'none';
                }
            } catch(e) {}
        }

        async function openNotifications() {
            let userId = getUserId();
            if (!userId) return;

            document.getElementById('notifModal').classList.add('active');
            let list = document.getElementById('notifList');
            list.innerHTML = '<div class="loader">Загрузка...</div>';

            try {
                let res = await fetch(`/api/notifications?user_id=${userId}&t=` + Date.now());
                let data = await res.json();

                if (!data.notifications || data.notifications.length === 0) {
                    list.innerHTML = '<div class="loader">Уведомлений пока нет</div>';
                } else {
                    list.innerHTML = data.notifications.map(n => `
                        <div class="notif-item ${n.is_read ? '' : 'unread'}">
                            <div class="notif-title">${n.title}</div>
                            <div class="notif-text">${n.text}</div>
                            <div class="notif-date">${n.created_at || ''}</div>
                        </div>
                    `).join('');
                }

                await fetch('/api/notifications/read', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: userId})
                });
                document.getElementById('bellBadge').style.display = 'none';
            } catch(e) {
                list.innerHTML = '<div class="loader">Ошибка загрузки</div>';
            }
        }

        function closeNotifModal() {
            document.getElementById('notifModal').classList.remove('active');
        }

        function sortItems(items) {
            let sortType = document.getElementById('sortSelect')?.value || 'newest';
            let list = [...items];
            
            if (sortType === 'date_asc') {
                list.sort((a, b) => (a.date || '').localeCompare(b.date || ''));
            } else if (sortType === 'route') {
                list.sort((a, b) => (a.route || '').localeCompare(b.route || ''));
            } else if (sortType === 'price_desc') {
                list.sort((a, b) => {
                    let numA = parseInt((a.price || '').replace(/\D/g, '')) || 0;
                    let numB = parseInt((b.price || '').replace(/\D/g, '')) || 0;
                    return numB - numA;
                });
            }
            return list;
        }

        async function loadData(isSilent = false) {
            if (!isSilent) tbody.innerHTML = '<div class="loader">Загрузка данных...</div>';
            let userId = getUserId();

            checkNotifications();

            try {
                if (currentTab === 'catalog') {
                    let url = `/api/loads?user_id=${userId}&t=` + Date.now();
                    if (currentCountry !== 'ALL') url += '&country=' + encodeURIComponent(currentCountry);
                    
                    let res = await fetch(url);
                    let data = await res.json();
                    
                    let sortedLoads = sortItems(data.loads || []);
                    let newHash = JSON.stringify(sortedLoads);
                    if (isSilent && newHash === currentDataHash) {
                        return;
                    }
                    
                    let openDetailsId = document.querySelector('.t-details.active')?.id;
                    currentDataHash = newHash;

                    if (!sortedLoads || sortedLoads.length === 0) {
                        tbody.innerHTML = '<div class="loader">Активных заявок пока нет</div>';
                        return;
                    }
                    
                    tbody.innerHTML = sortedLoads.map(l => {
                        let carsNum = parseInt(l.cars) || 1;
                        let carsSelect = '';
                        if (carsNum > 1) {
                            let options = '';
                            for (let i = 1; i <= carsNum; i++) {
                                options += `<option value="${i}">${i} авто</option>`;
                            }
                            carsSelect = `
                                <div class="qty-picker">
                                    <label>Количество авто:</label>
                                    <select id="qty-${l.id}">${options}</select>
                                </div>
                            `;
                        } else {
                            carsSelect = `<input type="hidden" id="qty-${l.id}" value="1">`;
                        }

                        let isAuction = !l.price || /торг/i.test(l.price);

                        let actionButtons = '';
                        if (isAuction) {
                            actionButtons = `<button class="btn-offer" style="width:100%;" onclick="openOfferModal(${l.id}, event)">💰 Предложить авто по цене</button>`;
                        } else {
                            actionButtons = `
                                <button class="btn-confirm" onclick="sendAction(${l.id}, 'confirm', event)">✅ Подтвердить</button>
                                <button class="btn-offer" onclick="openOfferModal(${l.id}, event)">💰 Своя цена</button>
                            `;
                        }

                        return `
                        <div class="t-row" onclick="toggleRow('${l.id}')">
                            <div class="col-date">${l.date}</div>
                            <div class="col-route" title="${l.route}">${l.route}</div>
                            <div class="col-price">${l.price}</div>
                            <div class="col-arrow" id="arrow-${l.id}">▼</div>
                        </div>
                        <div class="t-details" id="details-${l.id}">
                            <div class="info-grid">
                                <div class="info-item"><span>Тип ТС:</span><b>${l.car_type || 'Тент/реф'}</b></div>
                                <div class="info-item"><span>Тип груза:</span><b>${l.cargo_type || 'ТНП'}</b></div>
                                <div class="info-item"><span>Вес / Объем:</span><b>${l.weight || 'до 22т'}</b></div>
                                <div class="info-item"><span>Доступно авто:</span><b>${l.cars} авто</b></div>
                            </div>
                            
                            ${l.admin_comment ? `<div class="admin-comment"><b>💡 От логиста:</b> ${l.admin_comment}</div>` : ''}
                            
                            ${carsSelect}
                            
                            <textarea id="comment-${l.id}" placeholder="Комментарий / Заметка к заявке..."></textarea>
                            
                            <div class="buttons">
                                ${actionButtons}
                            </div>
                        </div>
                    `}).join('');

                    if (openDetailsId) {
                        let el = document.getElementById(openDetailsId);
                        if (el) el.classList.add('active');
                    }

                } else if (currentTab === 'my') {
                    let res = await fetch(`/api/my_loads?user_id=${userId}&t=` + Date.now());
                    let data = await res.json();

                    let filteredDeals = (data.deals || []).filter(d => {
                        if (mySubTab === 'archive') return d.is_archived;
                        return !d.is_archived;
                    });

                    let sortedDeals = sortItems(filteredDeals);

                    let newHash = JSON.stringify(sortedDeals);
                    if (isSilent && newHash === currentDataHash) {
                        return;
                    }
                    
                    let openDetailsId = document.querySelector('.t-details.active')?.id;
                    currentDataHash = newHash;

                    if (!sortedDeals || sortedDeals.length === 0) {
                        tbody.innerHTML = `<div class="loader">${mySubTab === 'archive' ? 'В архиве пока нет едущих авто' : 'У вас пока нет активных заявок'}</div>`;
                        return;
                    }

                    tbody.innerHTML = sortedDeals.map(d => {
                        let statusStyle = d.status === 'CONFIRMED' ? 'color:#28a745;' : 'color:#fd7e14;';
                        
                        let highlightStyle = '';
                        let badgeHtml = '';

                        if (d.docs_submitted) {
                            highlightStyle = 'background: rgba(40, 167, 69, 0.10); border-left: 3px solid #28a745;';
                            badgeHtml = '<div class="doc-badge">✅ Данные поданы</div>';
                        } else if (d.is_today) {
                            highlightStyle = 'background: rgba(253, 126, 20, 0.10); border-left: 3px solid #fd7e14;';
                            badgeHtml = '<div class="today-badge">📌 Погрузка сегодня!</div>';
                        }

                        let docBtnText = d.docs_submitted ? '📄 Данные поданы (обновить)' : '📄 Подать данные (техпаспорт / водитель)';

                        let docButton = '';
                        if (d.status === 'CONFIRMED') {
                            docButton = `<button class="btn-confirm" style="background:var(--active-tab); width:100%; margin-top:8px; padding:8px;" onclick="requestDocUpload('${d.load_id || d.id}', event)">${docBtnText}</button>`;
                        }

                        return `
                        <div class="t-row" style="${highlightStyle}" onclick="toggleRow('${d.id}')">
                            <div class="col-date">${d.date}</div>
                            <div class="col-route" title="${d.route}">${d.route}</div>
                            <div class="col-price">${d.price}</div>
                            <div class="col-arrow" id="arrow-${d.id}">▼</div>
                        </div>
                        <div class="t-details" id="details-${d.id}">
                            ${badgeHtml}
                            <div style="font-weight:700; margin-bottom:8px; ${statusStyle}">
                                Статус: ${d.status_text}
                            </div>
                            <div class="info-grid">
                                <div class="info-item"><span>Тип ТС:</span><b>${d.car_type || 'Тент/реф'}</b></div>
                                <div class="info-item"><span>Тип груза:</span><b>${d.cargo_type || 'ТНП'}</b></div>
                                <div class="info-item"><span>Вес / Объем:</span><b>${d.weight || 'до 22т'}</b></div>
                                <div class="info-item"><span>Маршрут:</span><b>${d.route}</b></div>
                            </div>
                            ${d.details ? `<div style="font-size:11px; color:var(--hint); margin-top:4px;"><b>Детали:</b> ${d.details}</div>` : ''}
                            ${docButton}
                        </div>
                    `}).join('');

                    if (openDetailsId) {
                        let el = document.getElementById(openDetailsId);
                        if (el) el.classList.add('active');
                    }
                }

            } catch(e) {
                if (!isSilent) tbody.innerHTML = `<div class="loader" style="color:#dc3545;">Ошибка загрузки данных</div>`;
            }
        }

        async function requestDocUpload(dealId, event) {
            event.stopPropagation();
            let userId = getUserId();
            if (!userId) return;

            try {
                let res = await fetch('/api/submit_docs_prompt', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: userId, deal_id: dealId})
                });
                if (res.ok) {
                    notify('✅ Инструкция по отправке документов отправлена вам в чат с ботом!');
                    if (tg && tg.close) tg.close();
                } else {
                    notify('❌ Ошибка инициализации сбора документов.');
                }
            } catch(e) {
                notify('⚠️ Ошибка соединения с сервером.');
            }
        }

        async function loadProfile() {
            let userId = getUserId();
            if (!userId) return;

            try {
                let res = await fetch(`/api/profile?user_id=${userId}&t=` + Date.now());
                let data = await res.json();
                if (data.profile) {
                    document.getElementById('profCompany').value = data.profile.company || '';
                    document.getElementById('profName').value = data.profile.name || tg.initDataUnsafe?.user?.first_name || '';
                    document.getElementById('profPhone').value = data.profile.phone || '';

                    let subs = (data.profile.subscriptions || '').split(',');
                    document.querySelectorAll('.sub-chip').forEach(chip => {
                        let val = chip.dataset.val;
                        let cleanCountry = val.split(' ')[0];
                        let isMatched = subs.some(s => s.trim() && (s.includes(cleanCountry) || val.includes(s.trim())));
                        if (isMatched) {
                            chip.classList.add('active');
                        } else {
                            chip.classList.remove('active');
                        }
                    });
                }
            } catch(e) {}
        }

        async function saveProfile() {
            let userId = getUserId();
            if (!userId) return;

            let company = document.getElementById('profCompany').value.trim();
            let name = document.getElementById('profName').value.trim();
            let phone = document.getElementById('profPhone').value.trim();

            let selectedSubs = [];
            document.querySelectorAll('.sub-chip.active').forEach(chip => selectedSubs.push(chip.dataset.val));

            try {
                await fetch('/api/profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user_id: userId,
                        company: company,
                        name: name,
                        phone: phone,
                        subscriptions: selectedSubs.join(',')
                    })
                });
            } catch(e) {}
        }

        function toggleRow(id) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            const detailsBlock = document.getElementById(`details-${id}`);
            const arrow = document.getElementById(`arrow-${id}`);
            
            document.querySelectorAll('.t-details').forEach(el => {
                if(el.id !== `details-${id}`) el.classList.remove('active');
            });
            
            if (detailsBlock) {
                detailsBlock.classList.toggle('active');
                if (arrow) arrow.textContent = detailsBlock.classList.contains('active') ? '▲' : '▼';
            }
        }

        function openOfferModal(id, event) {
            event.stopPropagation();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
            activeOfferLoadId = parseInt(id);
            document.getElementById('modalPrice').value = '';
            const commEl = document.getElementById(`comment-${id}`);
            document.getElementById('modalComment').value = commEl ? commEl.value : '';
            document.getElementById('bidModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('bidModal').classList.remove('active');
            activeOfferLoadId = null;
        }

        async function submitModalBid() {
            let price = document.getElementById('modalPrice').value.trim();
            let comment = document.getElementById('modalComment').value.trim();

            if (!price) {
                notify('⚠️ Пожалуйста, укажите вашу ставку!');
                return;
            }

            let id = activeOfferLoadId;
            closeModal();

            if (!id || isNaN(id)) {
                notify('❌ Ошибка: не найден ID груза.');
                return;
            }

            let qty = document.getElementById(`qty-${id}`)?.value || '1';
            await performBooking(id, 'bid', price, comment, qty);
        }

        async function sendAction(id, actionType, event) {
            event.stopPropagation();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');

            let qty = document.getElementById(`qty-${id}`)?.value || '1';
            let commEl = document.getElementById(`comment-${id}`);
            let carrierComment = commEl ? commEl.value : '';

            let confirmMsg = `Подтвердить забор груза (${qty} авто) по указанной ставке?`;

            askConfirm(confirmMsg, async (confirmed) => {
                if (confirmed) {
                    await performBooking(id, 'confirm', '', carrierComment, qty);
                }
            });
        }

        async function performBooking(id, actionType, customPrice, comment, qty) {
            let user = tg.initDataUnsafe?.user || {};
            let userId = getUserId();
            let cleanId = parseInt(id);

            try {
                let res = await fetch(`/api/book/${cleanId}`, { 
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user_id: userId,
                        first_name: user.first_name || '',
                        username: user.username || '',
                        action: actionType,
                        proposed_price: customPrice,
                        comment: comment,
                        cars: qty
                    })
                });
                
                let respData = await res.json();

                if (res.ok && respData.status === 'success') {
                    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                    notify('✅ Заявка отправлена!');
                    loadData(false); 
                } else {
                    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
                    notify('❌ ' + (respData.error || 'Ошибка. Возможно, груз уже занят.'));
                    loadData(false);
                }
            } catch(e) {
                notify('⚠️ Ошибка соединения с сервером.');
            }
        }

        loadData(false);

        setInterval(() => {
            if (currentTab === 'catalog' || currentTab === 'my') {
                loadData(true);
            }
        }, 3000);
    </script>
</body>
</html>"""

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
    elif user_subs:
        sub_conditions = []
        for sub in user_subs:
            clean_sub = sub.split(' ')[0]
            sub_conditions.append("(destination_country LIKE ? OR route LIKE ?)")
            params.extend([f"%{clean_sub}%", f"%{clean_sub}%"])
        query += " AND (" + " OR ".join(sub_conditions) + ")"
    elif user_id:
        conn.close()
        return web.json_response({"loads": []})
        
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
               COALESCE(cd.docs_submitted, 0)
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

        for i in range(qty):
            deals.append({
                "id": f"deal_{deal_id}_{i}",
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
                "docs_submitted": bool(docs_sub)
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
        cursor.execute("SELECT date, route, price FROM confirmed_deals WHERE id = ? OR load_id = ?", (clean_deal_id, clean_deal_id))
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
            text_notes=""
        )

        prompt_text = (
            f"📍 {route_str} ({date_str})\n"
            f"💰 Ставка: {price_str}\n\n"
            f"Пожалуйста, отправьте в этот чат:\n"
            f"1) Техпаспорт тягача (с 2х сторон)\n"
            f"2) Техпаспорт прицепа (с 2х сторон)\n"
            f"3) Паспорт водителя\n"
            f"4) Водительское удостоверение (с 2х сторон)\n"
            f"5) Номера телефонов водителя (российский обязательно)\n\n"
            f"Вы можете прислать фото или PDF-файлы. Когда закончите, нажмите кнопку **«✅ Отправить данные логисту»**."
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

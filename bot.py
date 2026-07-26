import os
import logging
import sqlite3
import asyncio
import re
import json
from datetime import datetime, date, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types.web_app_info import WebAppInfo

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не задан токен бота в переменных окружения BOT_TOKEN!")

RENDER_URL = os.getenv("RENDER_URL", "https://your-app-name.onrender.com")
ADMIN_ID = os.getenv("ADMIN_ID")

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
            details TEXT
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
        "ALTER TABLE loads ADD COLUMN expires_at TEXT"
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

    # Очистка маршрута от цен
    route_str = re.sub(r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|UZS|сум|руб|долл|\$|€|тг).*$', '', route_str, flags=re.IGNORECASE)
    route_str = re.sub(r'\s*,\s*,\s*', ', ', route_str)
    route_str = re.sub(r'^\s*,\s*|\s*,\s*$', '', route_str).strip()

    # Анализ остальных элементов по запятым (Тип ТС, Груз, Вес, Доп условия)
    car_type = "Тент/реф"
    cargo_type = "ТНП"
    weight = "до 22т"
    details_list = []

    vehicle_keywords = ['тент', 'реф', 'мега', 'сцепка', 'тандем', 'изотерм', 'площадка', 'контейнер', 'автовоз', 'цистерна']
    req_keywords = {
        'адр': 'АДР',
        'бок': 'Бок погрузка',
        'верх': 'Верх погрузка',
        'гидроборт': 'Гидроборт',
        'пневмо': 'Пневмоход'
    }

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
        p_lower = part.lower()

        # Поиск Веса
        w_match = re.search(r'(\d{1,2}\s*т|до\s*\d{1,2}\s*т)', part, re.IGNORECASE)
        if w_match:
            weight = w_match.group(1).strip()
            part_no_weight = re.sub(r'(\d{1,2}\s*т|до\s*\d{1,2}\s*т)', '', part, flags=re.IGNORECASE).strip(' ,.-')
            if part_no_weight and not found_cargo:
                cargo_type = part_no_weight.capitalize()
                found_cargo = True
            continue

        # Поиск Типа ТС
        if not found_vehicle and any(vk in p_lower for vk in vehicle_keywords):
            car_type = part.capitalize()
            found_vehicle = True
            continue

        # Поиск спец. условий
        matched_req = False
        for r_key, r_label in req_keywords.items():
            if r_key in p_lower:
                if r_label not in details_list:
                    details_list.append(r_label)
                matched_req = True
                break
        if matched_req:
            continue

        # В остатке — наименование груза
        if not found_cargo and len(part) > 1:
            cargo_type = part.capitalize()
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
                if not is_auction_price(price_str):
                    builder.row(types.InlineKeyboardButton(
                        text=f"✅ Подтвердить за {price_str}",
                        callback_data=f"confirm_{cargo_id}"
                    ))
                builder.row(types.InlineKeyboardButton(
                    text="💰 Своя ставка",
                    callback_data=f"bid_{cargo_id}"
                ))
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
        if not is_auction_price(price_str):
            builder.row(types.InlineKeyboardButton(
                text=f"✅ Подтвердить за {price_str}",
                callback_data=f"confirm_{cargo_id}"
            ))
        builder.row(types.InlineKeyboardButton(
            text="💰 Своя ставка",
            callback_data=f"bid_{cargo_id}"
        ))
    
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

    # Сообщение 1
    await message.answer(
        "Приветствую!\nДля удобства использования нашего бота есть Web-App 👇",
        reply_markup=inline_builder1.as_markup()
    )

    # Сообщение 2 (включает клавиатуру постоянной кнопки)
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
        if not is_auction_price(price_str):
            builder.row(types.InlineKeyboardButton(text=f"✅ Подтвердить за {price_str}", callback_data=f"confirm_{load_id}"))
        builder.row(types.InlineKeyboardButton(text="💰 Своя ставка", callback_data=f"bid_{load_id}"))
        
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


# ==================== ЛОГИКА ОБРАБОТКИ ЗАЯВОК И СДЕЛОК ====================
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

    # Ставка из чата
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

    # Прямое подтверждение по ставке логиста
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

    # 1. Вызов меню админа
    if cleaned_text.lower() in ("/меню", "меню", "!меню"):
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
        builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
        builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
        builder.row(types.InlineKeyboardButton(text="🔐 Сменить пароль Web App", callback_data="adm_change_pass"))
        await event.answer("🎛 **Панель администратора**\nВыберите нужный раздел:", reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    # 2. Массовая рассылка (!текст)
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

    # 3. Встречная ставка от логиста
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
    await callback.message.answer("🔐 Введите новый пароль для входа в Админ-панель в Web App:")
    await state.set_state(AdminPassState.waiting_for_new_pass)
    await callback.answer()

@dp.message(AdminPassState.waiting_for_new_pass)
async def admin_save_new_pass(message: types.Message, state: FSMContext):
    new_pass = message.text.strip()
    if not new_pass:
        await message.answer("Пароль не может быть пустым!")
        return
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_password', ?)", (new_pass,))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Пароль для Web App успешно изменён на: `{new_pass}`", parse_mode="Markdown")

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

        /* Адаптивный фильтр направлений: 1 строка с свайпом на телефоне, сетка на ПК */
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

        /* Подразделы для Моих заявок */
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

        .sort-bar select {
            flex: 1;
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 600;
            outline: none;
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
        .qty-picker select {
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 700;
        }

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

    <!-- Адаптивный фильтр направлений -->
    <div class="filter-scroll" id="dir-filters">
        <div class="chip active" onclick="setFilter('ALL', this)">Все направления</div>
        <div class="chip" onclick="setFilter('Казахстан', this)">🇰🇿 Казахстан</div>
        <div class="chip" onclick="setFilter('Узбекистан', this)">🇺🇿 Узбекистан</div>
        <div class="chip" onclick="setFilter('Кыргызстан', this)">🇰🇬 Кыргызстан</div>
        <div class="chip" onclick="setFilter('Грузия', this)">🇬🇪 Грузия</div>
        <div class="chip" onclick="setFilter('Азербайджан', this)">🇦🇿 Азербайджан</div>
        <div class="chip" onclick="setFilter('Армения', this)">🇦🇲 Армения</div>
    </div>

    <!-- Переключатель подраздела "Мои заявки" -->
    <div class="sub-nav" id="my-sub-nav" style="display: none;">
        <div class="sub-nav-btn active" id="subnav-active" onclick="switchMySubTab('active')">📋 Активные</div>
        <div class="sub-nav-btn" id="subnav-archive" onclick="switchMySubTab('archive')">🚚 Едущие авто (Архив)</div>
    </div>

    <!-- Блок сортировки -->
    <div class="sort-bar" id="sort-container">
        <label>Сортировка:</label>
        <select id="sortSelect" onchange="loadData(false)">
            <option value="newest">📅 Сначала новые</option>
            <option value="date_asc">📅 По дате погрузки</option>
            <option value="route">📍 По направлению (А-Я)</option>
            <option value="price_desc">💰 По цене (сначала высокие)</option>
        </select>
    </div>

    <!-- Список грузов / Таблица -->
    <div class="table-container" id="main-table">
        <div class="t-head">
            <div>Дата</div>
            <div>Направление</div>
            <div style="text-align: right;">Ставка</div>
            <div></div>
        </div>
        <div id="loads-body"><div class="loader">Загрузка данных...</div></div>
    </div>

    <!-- Блок личного кабинета (АВТОСОХРАНЕНИЕ) -->
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

    <!-- Модальное окно "Своя ставка" -->
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

    <!-- Модальное окно уведомлений -->
    <div class="modal-overlay" id="notifModal">
        <div class="modal-card">
            <div class="modal-title">🔔 Уведомления</div>
            <div id="notifList"><div class="loader">Загрузка...</div></div>
            <button class="btn-rounded" style="width:100%; margin-top:10px; background:var(--hint);" onclick="closeNotifModal()">Закрыть</button>
        </div>
    </div>

    <!-- Модалка ввода пароля Админа -->
    <div class="modal-overlay" id="adminPassModal">
        <div class="modal-card">
            <div class="modal-title">🔐 Вход для администратора</div>
            <input type="password" id="adminPassInput" placeholder="Введите пароль..." />
            <div class="buttons">
                <button class="btn-confirm" onclick="submitAdminPassword()">Войти</button>
                <button style="background: var(--hint);" onclick="closeAdminPassModal()">Отмена</button>
            </div>
        </div>
    </div>

    <!-- Модалка Панели Администратора -->
    <div class="modal-overlay" id="adminPanelModal">
        <div class="modal-card" style="max-width: 95%; width: 500px;">
            <div class="modal-title">🎛 Панель Администратора</div>
            <div class="sub-nav" style="margin-bottom:12px;">
                <div class="sub-nav-btn active" id="admTabCarriers" onclick="switchAdminSubTab('carriers')">👥 Перевозчики</div>
                <div class="sub-nav-btn" id="admTabLoads" onclick="switchAdminSubTab('loads')">📦 Все грузы</div>
            </div>
            <div id="adminContentBody"><div class="loader">Загрузка...</div></div>
            <button class="btn-rounded" style="width:100%; margin-top:12px; background:var(--hint);" onclick="closeAdminPanelModal()">Закрыть Админку</button>
        </div>
    </div>

    <!-- Модалка редактирования груза Админом -->
    <div class="modal-overlay" id="adminEditLoadModal">
        <div class="modal-card">
            <div class="modal-title">✏️ Редактирование груза</div>
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

        // Секретный триггер кликов для входа в Админку
        let headerClicks = 0;
        let headerTimer = null;
        let adminSubTab = 'carriers';

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
            document.getElementById('admTabLoads').classList.toggle('active', tab === 'loads');

            if (tab === 'carriers') {
                loadAdminCarriers();
            } else {
                loadAdminLoads();
            }
        }

        async function loadAdminCarriers() {
            let body = document.getElementById('adminContentBody');
            body.innerHTML = '<div class="loader">Загрузка перевозчиков...</div>';
            try {
                let res = await fetch('/api/admin/carriers?t=' + Date.now());
                let data = await res.json();
                body.innerHTML = data.carriers.map(c => `
                    <div class="notif-item">
                        <div class="notif-title">🏢 ${c.company} | ${c.name}</div>
                        <div class="notif-text">📞 ${c.phone} | ID: ${c.user_id}</div>
                        <div class="notif-text" style="color:var(--hint); margin-top:2px;">Подписки: ${c.subscriptions || 'нет'}</div>
                        <button class="btn-rounded" style="margin-top:6px; background:${c.status === 'ACTIVE' ? '#dc3545' : '#28a745'}; padding:6px;" onclick="toggleCarrierStatus(${c.user_id})">
                            ${c.status === 'ACTIVE' ? '🔴 Заблокировать' : '🟢 Разблокировать'}
                        </button>
                    </div>
                `).join('');
            } catch(e) { body.innerHTML = 'Ошибка загрузки'; }
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
                body.innerHTML = data.loads.map(l => `
                    <div class="notif-item">
                        <div class="notif-title">📍 ${l.date} | ${l.route} (${l.price})</div>
                        <div class="notif-text">🚚 Доступно: ${l.cars} авто | Статус: <b>${l.status}</b></div>
                        <div class="notif-text" style="color:#007aff;">👤 Перевозчик: ${l.carrier_info}</div>
                        <div class="buttons" style="margin-top:6px;">
                            <button class="btn-confirm" style="padding:6px;" onclick='openAdminEditModal(${JSON.stringify(l).replace(/'/g, "&#39;")})'>✏️ Изменить</button>
                            <button class="btn-offer" style="padding:6px; background:#dc3545;" onclick="deleteAdminLoad(${l.id})">🗑 Закрыть/Удалить</button>
                        </div>
                    </div>
                `).join('');
            } catch(e) { body.innerHTML = 'Ошибка загрузки'; }
        }

        function openAdminEditModal(l) {
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

        async function deleteAdminLoad(id) {
            if (confirm('Закрыть/удалить этот груз для всех?')) {
                await fetch('/api/admin/delete_load', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: id})
                });
                loadAdminLoads();
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
                        let highlightStyle = d.is_today ? 'background: rgba(40, 167, 69, 0.08); border-left: 3px solid #28a745;' : '';

                        return `
                        <div class="t-row" style="${highlightStyle}" onclick="toggleRow('${d.id}')">
                            <div class="col-date">${d.date}</div>
                            <div class="col-route" title="${d.route}">${d.route}</div>
                            <div class="col-price">${d.price}</div>
                            <div class="col-arrow" id="arrow-${d.id}">▼</div>
                        </div>
                        <div class="t-details" id="details-${d.id}">
                            ${d.is_today ? `<div class="today-badge">📌 Погрузка сегодня!</div>` : ''}
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

import os
import logging
import sqlite3
import asyncio
import re
import json
from datetime import datetime, date, timedelta
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
ADMIN_CHANNEL_ID = -1004271518848

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
            text TEXT,
            details TEXT,
            status TEXT DEFAULT 'ACTIVE',
            cargo_type TEXT,
            weight TEXT,
            admin_comment TEXT,
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
            status TEXT DEFAULT 'PENDING'
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
        "ALTER TABLE loads ADD COLUMN admin_comment TEXT",
        "ALTER TABLE loads ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE bids ADD COLUMN comment TEXT"
    ]
    for migration in migrations:
        try:
            cursor.execute(migration)
        except sqlite3.OperationalError:
            pass 
    
    conn.commit()
    conn.close()

init_db()


# ==================== СОСТОЯНИЯ ====================
class RegistrationStates(StatesGroup):
    waiting_for_company = State()
    waiting_for_name = State()
    waiting_for_phone = State()

class DealStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_custom_rate = State()
    waiting_for_comment = State()

class AdminEditStates(StatesGroup):
    waiting_for_new_cargo_text = State()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ДАТЫ ====================
def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    web_app_url = f"{RENDER_URL}/webapp" 
    builder.add(types.KeyboardButton(
        text="🌐 Открыть каталог грузов", 
        web_app=WebAppInfo(url=web_app_url)
    ))
    builder.add(types.KeyboardButton(text="🏠 Меню и направления"))
    builder.add(types.KeyboardButton(text="📦 Мои подтвержденные грузы"))
    builder.adjust(1, 2)
    return builder.as_markup(resize_keyboard=True)

def is_auction_price(price_str: str) -> bool:
    if not price_str:
        return True
    p = str(price_str).lower().strip()
    return any(w in p for w in ['торг', 'запрос', 'договор', 'аукцион', 'по запросу']) or not any(c.isdigit() for c in p)

def extract_price(text: str) -> str:
    price_pattern = re.compile(
        r'([\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб|долл|доллар|долларов|\$|€|тг))', 
        re.IGNORECASE
    )
    match = price_pattern.search(text)
    if match:
        return match.group(1).strip(' ,.')
    
    match_num = re.search(r'\d{2,}\s*[\.,]\d{3}', text)
    if match_num:
        return match_num.group(0).strip(' ,.')
        
    return ""

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
    clean_lines = []
    for line in raw_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('📍') or line.startswith('💰') or line.startswith('📦') or line.startswith('🚚'):
            parts = line.split('|')
            for p in parts:
                p_clean = p.replace('📍', '').replace('💰', '').replace('📦', '').replace('🚚', '').strip()
                if p_clean:
                    clean_lines.append(p_clean)
            continue
        clean_lines.append(line)

    date_str = ""
    route_str = ""
    price_str = extract_price(raw_text)
    cars_str = ""
    details_list = []
    
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
                r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб|долл|доллар|\$|€|тг)', 
                '', 
                clean_route, 
                flags=re.IGNORECASE
            )
            clean_route = re.sub(r'\d{2,}\s*[\.,]\d{3}', '', clean_route)
            clean_route = clean_route.strip(' ,.-')
            if clean_route:
                route_str = clean_route.replace(' - ', ' → ').replace('-', '→')
        
        is_price_line = price_str and price_str.lower() in line.lower()
        if not is_price_line and not re.search(r'(RUB|USD|EUR|KZT|сум|руб|долл|доллар|\$|€|тг)', line, re.IGNORECASE) and not date_pattern.search(line) and not cars_pattern.search(line) and '-' not in line and '→' not in line:
            if line not in details_list:
                details_list.append(line)

    if not date_str:
        date_str = "Дата не указана"
    if not route_str:
        route_str = clean_lines[0] if clean_lines else "Маршрут не указан"
    if not price_str:
        price_str = "Торги"
    if not cars_str:
        cars_str = "1"

    details_text = ", ".join(details_list) if details_list else ""
    return date_str, route_str, price_str, cars_str, details_text

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


# ==================== АВТО-ОЧИСТКА ГРУЗОВ ПО ДАТЕ ====================
async def auto_clean_expired_cargos():
    """Удаляет/закрывает грузы только когда прошла дата погрузки включительно."""
    while True:
        try:
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("SELECT load_id, date, created_at FROM loads WHERE status = 'ACTIVE'")
            rows = cursor.fetchall()
            
            today = datetime.now().date()
            expired_ids = []
            
            for load_id, date_str, created_at in rows:
                cargo_d = parse_cargo_date(date_str)
                if cargo_d:
                    if today > cargo_d:
                        expired_ids.append(load_id)
                else:
                    if created_at:
                        try:
                            c_time = datetime.strptime(created_at.split('.')[0], "%Y-%m-%d %H:%M:%S")
                            if datetime.now() - c_time > timedelta(days=7):
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
            
        await asyncio.sleep(900)


# ==================== ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ====================

@dp.message(F.web_app_data)
async def handle_webapp_data(message: types.Message):
    user_id = message.from_user.id
    try:
        data = json.loads(message.web_app_data.data)
        if isinstance(data, list):
            new_subs = ",".join(data)
            conn = sqlite3.connect("cargo_bot.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET subscriptions = ? WHERE user_id = ?", (new_subs, user_id))
            conn.commit()
            conn.close()
            await message.answer(
                f"✅ **Подписки успешно обновлены!**\n\nВыбранные направления:\n{', '.join(data) if data else 'Ничего не выбрано'}",
                parse_mode="Markdown",
                reply_markup=get_main_reply_markup()
            )
    except Exception as e:
        logging.error(f"Error webapp data: {e}")


@dp.message(Command("start"))
@dp.message(F.text == "🏠 Меню и направления")
async def cmd_start_or_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, status FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()

    if user and user[1] == 'BLOCKED':
        await message.answer("Ваш аккаунт заблокирован администратором.")
        return

    if not user:
        await message.answer(
            "Здравствуйте! Для доступа к системе необходима регистрация.\n\nШаг 1 из 3: Введите название вашей компании:",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(RegistrationStates.waiting_for_company)
    else:
        await show_main_menu(message)

@dp.message(RegistrationStates.waiting_for_company)
async def process_company(message: types.Message, state: FSMContext):
    await state.update_data(company=message.text)
    await message.answer("Шаг 2 из 3: Введите ваше имя:")
    await state.set_state(RegistrationStates.waiting_for_name)

@dp.message(RegistrationStates.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True))
    await message.answer(
        "Шаг 3 из 3: Нажмите кнопку ниже для отправки номера в 1 клик, либо введите текстом:",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.set_state(RegistrationStates.waiting_for_phone)

@dp.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    data = await state.get_data()
    company, name = data.get("company"), data.get("name")
    user_id = message.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, company, name, phone, subscriptions, status)
        VALUES (?, ?, ?, ?, COALESCE((SELECT subscriptions FROM users WHERE user_id = ?), ''), COALESCE((SELECT status FROM users WHERE user_id = ?), 'ACTIVE'))
    """, (user_id, company, name, phone, user_id, user_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("Регистрация успешно завершена! 🎉", reply_markup=get_main_reply_markup())
    await show_main_menu(message)

async def show_main_menu(message: types.Message):
    user_id = message.from_user.id
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
    
    text = (
        "⚙️ **Главное меню и направления**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
        f"Ваши текущие подписки: {', '.join(user_subs) if user_subs else 'ничего не выбрано'}"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await message.answer("Выберите нужное действие:", reply_markup=get_main_reply_markup())

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
        
    text = (
        "⚙️ **Главное меню и направления**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
        f"Ваши текущие подписки: {', '.join(current_subs) if current_subs else 'ничего не выбрано'}"
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.message(F.text == "📦 Мои подтвержденные грузы")
async def show_confirmed_cargos_handler(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, route, cars, price, details FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC", (user_id,))
    confirmed_loads = cursor.fetchall()
    conn.close()
    
    if not confirmed_loads:
        await message.answer("У вас пока нет подтвержденных грузов.", reply_markup=get_main_reply_markup())
        return
        
    await message.answer("📦 **Ваши подтвержденные грузы:**", parse_mode="Markdown", reply_markup=get_main_reply_markup())
    for date_str, route_str, cars_count, price_str, details_text in confirmed_loads:
        card_text = (
            f"📍 {date_str} | {route_str}\n"
            f"💰 {price_str} | 🚚 {cars_count} авто"
        )
        if details_text:
            card_text += f"\n📦 {details_text}"
        try:
            await message.answer(card_text)
            await asyncio.sleep(0.05)
        except Exception:
            pass

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
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `250.000 руб` или `2000 долл`):")
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

    rate = message.text.strip()
    await state.update_data(custom_rate=rate)
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    await message.answer(f"Сколько грузов вы можете поставить по этой ставке? (доступно машин: {row[0] if row else '?'})")
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
    
    if action_type == "bid":
        await state.update_data(requested_cars=qty_input)
        await message.answer("Введите комментарий к ставке (например, сроки подачи машины, особенности оплаты).\n\nОтправьте `-` для пропуска:")
        await state.set_state(DealStates.waiting_for_comment)
        return

    cargo_id = data.get("cargo_id")
    user_obj = message.from_user
    user_link = f"@{user_obj.username}" if user_obj.username else f"{user_obj.full_name} (ID: {user_id})"

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()
    
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
        warning_text = f"⚠️ Столько грузов нет, доступно только {current_cars} авто. Берем в работу {current_cars} авто.\n\n"

    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    carrier_info = f"👤 Перевозчик: {user_link} | Компания: {company} | Имя: {name} | Тел: {phone}"
    
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
        f"🎯 Заявка на груз!\n\n"
        f"📦 Описание:\n{raw_cargo_text}\n\n"
        f"🚛 Забирает авто: {requested_cars}\n"
        f"{carrier_info}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification)
    except Exception:
        pass
        
    await state.clear()
    await message.answer(f"{warning_text}✅ Заявка принята! Менеджер свяжется с вами.", reply_markup=get_main_reply_markup())

@dp.message(DealStates.waiting_for_comment)
async def process_deal_comment(message: types.Message, state: FSMContext):
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

    comment_input = message.text.strip()
    comment_text = "-" if comment_input == "-" else comment_input

    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    rate = data.get("custom_rate")
    qty_input = data.get("requested_cars", "1")
    
    user_obj = message.from_user
    user_link = f"@{user_obj.username}" if user_obj.username else f"{user_obj.full_name} (ID: {user_id})"

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()
    
    cursor.execute("SELECT cars_count, text, status FROM loads WHERE load_id = ?", (cargo_id,))
    load_row = cursor.fetchone()
    
    if not load_row:
        conn.close()
        await message.answer("Груз не найден.", reply_markup=get_main_reply_markup())
        await state.clear()
        return
        
    current_cars_str, raw_cargo_text, status = load_row
    if status in ['CLOSED', 'EXPIRED']:
        conn.close()
        await message.answer("Этот груз уже закрыт или истек.", reply_markup=get_main_reply_markup())
        await state.clear()
        return

    current_cars = int(re.search(r'\d+', str(current_cars_str)).group(0)) if re.search(r'\d+', str(current_cars_str)) else 1
    requested_cars = int(re.search(r'\d+', qty_input).group(0)) if re.search(r'\d+', qty_input) else 1
    
    warning_text = ""
    if requested_cars > current_cars:
        requested_cars = current_cars
        warning_text = f"⚠️ Запрошено больше, чем доступно. Передаем ставку на {current_cars} авто.\n\n"

    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    carrier_info = f"👤 Перевозчик: {user_link} | Компания: {company} | Имя: {name} | Тел: {phone}"

    cursor.execute("""
        INSERT INTO bids (load_id, user_id, cars, rate, comment)
        VALUES (?, ?, ?, ?, ?)
    """, (cargo_id, user_id, requested_cars, rate, comment_text))
    bid_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    bid_notification = (
        f"💰 Новая ставка от перевозчика!\n\n"
        f"📦 Груз:\n{raw_cargo_text}\n\n"
        f"💵 Ставка: {rate} | 🚛 Авто: {requested_cars}\n"
        f"💬 Комментарий: {comment_text}\n"
        f"{carrier_info}\n\n"
        f"(Груз остается активным)"
    )
    
    admin_builder = InlineKeyboardBuilder()
    admin_builder.row(
        types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{bid_id}"),
        types.InlineKeyboardButton(text="🔀 Часть", callback_data=f"partial_bid_{bid_id}")
    )
    admin_builder.row(types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{bid_id}"))

    try:
        await bot.send_message(
            chat_id=ADMIN_CHANNEL_ID, 
            text=bid_notification, 
            reply_markup=admin_builder.as_markup()
        )
    except Exception:
        pass
            
    await state.clear()
    await message.answer(f"{warning_text}✅ Ваша ставка и комментарий отправлены администратору на рассмотрение. Ожидайте обратной связи!", reply_markup=get_main_reply_markup())

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
            cursor.execute("UPDATE loads SET cars_count = ?, price = ? WHERE load_id = ?", (str(left_cars), agreed_rate, cargo_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0', price = ? WHERE load_id = ?", (agreed_rate, cargo_id))
            
        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, requested_qty, agreed_rate, details_str))
        
        cursor.execute("UPDATE bids SET status = 'ACCEPTED' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(cargo_id)
    else:
        conn.close()
        return

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=(
                f"✅ Администратор подтвердил вашу ставку! Груз закреплен за вами.\n\n"
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
            cursor.execute("UPDATE loads SET cars_count = ?, price = ? WHERE load_id = ?", (str(left_cars), agreed_rate, cargo_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0', price = ? WHERE load_id = ?", (agreed_rate, cargo_id))

        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, confirmed_qty, agreed_rate, details_str))

        cursor.execute("UPDATE bids SET status = 'PARTIAL' WHERE bid_id = ?", (bid_id,))
        conn.commit()
        conn.close()
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
    
    cursor.execute("SELECT date, route, details, price FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=f"❌ Ваша ставка ({rate}) по грузу отклонена администратором."
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n[СТАТУС: Отклонено ❌]", reply_markup=None)
    await callback.answer("Отклонено.")


# ==================== АДМИН-ПАНЕЛЬ ====================
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
    date_str, route_str, price_str, cars_str, details_text = parse_cargo_raw(new_raw_text)
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE loads 
        SET date = ?, route = ?, price = ?, cars_count = ?, text = ?, details = ?
        WHERE load_id = ?
    """, (date_str, route_str, price_str, cars_str, new_raw_text, details_text, cargo_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ Груз обновлен!", reply_markup=get_main_reply_markup())
    await update_cargo_messages_for_all_users(cargo_id)

@dp.message(F.chat.id == ADMIN_CHANNEL_ID)
@dp.channel_post(F.chat.id == ADMIN_CHANNEL_ID)
async def handle_admin_messages_and_posts(event: types.Message):
    text = event.text or event.caption
    if not text:
        return
        
    cleaned_text = text.strip()
    if cleaned_text.lower() in ("/меню", "меню", "!меню"):
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
        builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
        builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
        await event.answer("🎛 **Панель администратора**\nВыберите нужный раздел:", reply_markup=builder.as_markup(), parse_mode="Markdown")
        return

    if cleaned_text.startswith("!"):
        broadcast_text = cleaned_text[1:].lstrip()
        if not broadcast_text:
            return

        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
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

@dp.callback_query(F.data == "adm_menu_back")
async def admin_menu_back(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
    builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
    builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
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
        date_str, route_str, price_str, cars_str, details_text = parse_cargo_raw(single_text)
        cursor.execute("""
            INSERT INTO loads (destination_country, date, route, cars_count, price, text, details, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """, (direction, date_str, route_str, cars_str, price_str, single_text, details_text))
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


# ==================== WEB APP HTML (ВСТРОЕННЫЙ) ====================
INDEX_HTML = """<!DOCTYPE html>
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
            padding: 8px;
            font-size: 13px;
            -webkit-user-select: none;
            user-select: none;
        }

        .header-title {
            text-align: center;
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text);
            letter-spacing: 0.3px;
        }

        .main-nav {
            display: flex;
            gap: 4px;
            margin-bottom: 10px;
            background: var(--card);
            padding: 4px;
            border-radius: 10px;
            border: 1px solid var(--border);
        }

        .nav-btn {
            flex: 1;
            padding: 8px 4px;
            text-align: center;
            font-weight: 600;
            font-size: 11px;
            border-radius: 7px;
            cursor: pointer;
            color: var(--hint);
            transition: all 0.2s;
            white-space: nowrap;
        }

        .nav-btn.active {
            background: var(--active-tab);
            color: #ffffff;
        }

        .filter-scroll {
            display: flex;
            gap: 6px;
            overflow-x: auto;
            padding-bottom: 8px;
            margin-bottom: 8px;
            scrollbar-width: none;
        }
        .filter-scroll::-webkit-scrollbar { display: none; }

        .chip {
            white-space: nowrap;
            padding: 6px 12px;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 20px;
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

        .table-container {
            background: var(--card);
            border-radius: 10px;
            overflow: hidden;
            border: 1px solid var(--border);
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }

        .t-head {
            display: grid;
            grid-template-columns: 50px 1fr 85px 20px;
            background: rgba(0,0,0,0.03);
            padding: 8px 10px;
            font-size: 11px;
            font-weight: 700;
            color: var(--hint);
            border-bottom: 1px solid var(--border);
            text-transform: uppercase;
        }

        .t-row {
            display: grid;
            grid-template-columns: 50px 1fr 85px 20px;
            padding: 10px;
            border-bottom: 1px solid var(--border);
            align-items: center;
            cursor: pointer;
            transition: background 0.15s;
        }

        .t-row:active { background: rgba(0,0,0,0.04); }
        .t-row:last-child { border-bottom: none; }
        
        .col-date { font-weight: 500; color: var(--hint); font-size: 11px; }
        .col-route { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 6px; }
        .col-price { font-weight: 700; color: var(--btn-green); text-align: right; font-size: 12px; }
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
        
        .admin-comment {
            background: rgba(255, 193, 7, 0.15);
            color: var(--text);
            padding: 8px 10px;
            border-radius: 6px;
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
            border-radius: 6px;
            border: 1px solid var(--border);
        }

        .qty-picker label { font-size: 11px; color: var(--hint); font-weight: 600; flex: 1; }
        .qty-picker select {
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 700;
        }

        textarea, input[type="text"] {
            width: 100%;
            box-sizing: border-box;
            background: var(--card);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 12px;
            margin-bottom: 10px;
            font-family: inherit;
        }

        textarea { resize: none; height: 42px; }

        .buttons {
            display: flex;
            gap: 8px;
        }

        .buttons button {
            flex: 1;
            padding: 10px;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            font-size: 12px;
            color: #fff;
            cursor: pointer;
            transition: opacity 0.15s;
        }

        .buttons button:active { opacity: 0.8; }

        .btn-confirm { background: var(--btn-green); }
        .btn-offer { background: var(--btn-orange); }

        .loader { text-align: center; padding: 25px; color: var(--hint); font-size: 13px; }

        .profile-card {
            background: var(--card);
            border-radius: 10px;
            padding: 14px;
            border: 1px solid var(--border);
            margin-bottom: 12px;
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

        .sub-item {
            background: var(--bg);
            padding: 8px 10px;
            border-radius: 6px;
            border: 1px solid var(--border);
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .sub-item input { width: 16px; height: 16px; margin: 0; }

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
            border-radius: 12px;
            padding: 16px;
            width: 100%;
            max-width: 340px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }
        .modal-title { font-weight: 700; font-size: 14px; margin-bottom: 12px; text-align: center; }
    </style>
</head>
<body>

    <div class="header-title">ЧТУП «Белкаспиан» | Биржа</div>

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
            <div class="profile-title">👤 Данные компании</div>
            <div class="form-group">
                <label>Компания:</label>
                <input type="text" id="profCompany" placeholder="Название вашей компании..." />
            </div>
            <div class="form-group">
                <label>Имя контактного лица:</label>
                <input type="text" id="profName" placeholder="Ваше имя..." />
            </div>
            <div class="form-group">
                <label>Телефон:</label>
                <input type="text" id="profPhone" placeholder="+375 / +7..." />
            </div>
        </div>

        <div class="profile-card">
            <div class="profile-title">🔔 Подписки на направления</div>
            <div class="subs-grid">
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Казахстан 🇰🇿"> 🇰🇿 Казахстан</div>
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Узбекистан 🇺🇿"> 🇺🇿 Узбекистан</div>
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Кыргызстан 🇰🇬"> 🇰🇬 Кыргызстан</div>
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Грузия 🇬🇪"> 🇬🇪 Грузия</div>
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Азербайджан 🇦🇿"> 🇦🇿 Азербайджан</div>
                <div class="sub-item"><input type="checkbox" class="sub-cb" value="Армения 🇦🇲"> 🇦🇲 Армения</div>
            </div>
        </div>

        <button class="btn-confirm" style="width: 100%; padding: 12px; font-size: 13px;" onclick="saveProfile()">💾 Сохранить данные профиля</button>
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

    <script>
        const tg = window.Telegram.WebApp;
        try { tg.expand(); tg.ready(); tg.setHeaderColor('secondary_bg_color'); } catch(e) {}

        const tbody = document.getElementById('loads-body');
        let currentTab = 'catalog';
        let currentCountry = 'ALL';
        let activeOfferLoadId = null;

        function notify(text) {
            if (tg && tg.showAlert) tg.showAlert(text);
            else alert(text);
        }

        function askConfirm(text, callback) {
            if (tg && tg.showConfirm) {
                tg.showConfirm(text, callback);
            } else {
                callback(confirm(text));
            }
        }

        function switchTab(tab) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            currentTab = tab;
            document.getElementById('tab-catalog').classList.toggle('active', tab === 'catalog');
            document.getElementById('tab-my').classList.toggle('active', tab === 'my');
            document.getElementById('tab-profile').classList.toggle('active', tab === 'profile');

            document.getElementById('dir-filters').style.display = (tab === 'catalog') ? 'flex' : 'none';
            document.getElementById('main-table').style.display = (tab === 'profile') ? 'none' : 'block';
            document.getElementById('profile-container').style.display = (tab === 'profile') ? 'block' : 'none';

            if (tab === 'profile') {
                loadProfile();
            } else {
                loadData();
            }
        }

        function setFilter(country, el) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            currentCountry = country;
            document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
            el.classList.add('active');
            loadData();
        }

        async function loadData() {
            tbody.innerHTML = '<div class="loader">Загрузка...</div>';
            let user = tg.initDataUnsafe?.user || {};
            let userId = user.id || 0;

            try {
                if (currentTab === 'catalog') {
                    let url = '/api/loads?t=' + Date.now();
                    if (currentCountry !== 'ALL') url += '&country=' + encodeURIComponent(currentCountry);
                    
                    let res = await fetch(url);
                    let data = await res.json();
                    
                    if (!data.loads || data.loads.length === 0) {
                        tbody.innerHTML = '<div class="loader">Активных заявок пока нет</div>';
                        return;
                    }
                    
                    tbody.innerHTML = data.loads.map(l => {
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

                        let isAuction = !l.price || /торг|запрос|договор|по запросу/i.test(l.price);

                        let actionButtons = '';
                        if (isAuction) {
                            actionButtons = `<button class="btn-offer" style="width:100%;" onclick="openOfferModal('${l.id}', event)">💰 Предложить авто по цене</button>`;
                        } else {
                            actionButtons = `
                                <button class="btn-confirm" onclick="sendAction('${l.id}', 'confirm', event)">✅ Подтвердить</button>
                                <button class="btn-offer" onclick="openOfferModal('${l.id}', event)">💰 Своя цена</button>
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
                                <div class="info-item"><span>Тип груза:</span><b>${l.cargo_type || 'ТНП'}</b></div>
                                <div class="info-item"><span>Вес / Объем:</span><b>${l.weight || '20т'}</b></div>
                                <div class="info-item"><span>Доступно авто:</span><b>${l.cars} авто</b></div>
                                <div class="info-item"><span>Направление:</span><b>${l.country || 'Все'}</b></div>
                            </div>
                            
                            ${l.admin_comment ? `<div class="admin-comment"><b>💡 От логиста:</b> ${l.admin_comment}</div>` : ''}
                            
                            ${carsSelect}
                            
                            <textarea id="comment-${l.id}" placeholder="Комментарий / Заметка к заявке..."></textarea>
                            
                            <div class="buttons">
                                ${actionButtons}
                            </div>
                        </div>
                    `}).join('');

                } else if (currentTab === 'my') {
                    let res = await fetch(`/api/my_loads?user_id=${userId}&t=` + Date.now());
                    let data = await res.json();

                    if (!data.deals || data.deals.length === 0) {
                        tbody.innerHTML = '<div class="loader">У вас пока нет подтвержденных заявок</div>';
                        return;
                    }

                    tbody.innerHTML = data.deals.map(d => `
                        <div class="t-row" style="cursor:default;">
                            <div class="col-date">${d.date}</div>
                            <div class="col-route">${d.route}</div>
                            <div class="col-price">${d.price} (${d.cars} авто)</div>
                            <div class="col-arrow">✅</div>
                        </div>
                    `).join('');
                }

            } catch(e) {
                tbody.innerHTML = `<div class="loader" style="color:#dc3545;">Ошибка загрузки данных</div>`;
            }
        }

        async function loadProfile() {
            let user = tg.initDataUnsafe?.user || {};
            let userId = user.id || 0;
            if (!userId) return;

            try {
                let res = await fetch(`/api/profile?user_id=${userId}&t=` + Date.now());
                let data = await res.json();
                if (data.profile) {
                    document.getElementById('profCompany').value = data.profile.company || '';
                    document.getElementById('profName').value = data.profile.name || user.first_name || '';
                    document.getElementById('profPhone').value = data.profile.phone || '';

                    let subs = (data.profile.subscriptions || '').split(',');
                    document.querySelectorAll('.sub-cb').forEach(cb => {
                        cb.checked = subs.some(s => s.trim() && cb.value.includes(s.trim()));
                    });
                }
            } catch(e) {}
        }

        async function saveProfile() {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
            let user = tg.initDataUnsafe?.user || {};
            let userId = user.id || 11111111;

            let company = document.getElementById('profCompany').value.trim();
            let name = document.getElementById('profName').value.trim();
            let phone = document.getElementById('profPhone').value.trim();

            let selectedSubs = [];
            document.querySelectorAll('.sub-cb:checked').forEach(cb => selectedSubs.push(cb.value));

            try {
                let res = await fetch('/api/profile', {
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

                if (res.ok) {
                    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                    notify('✅ Данные профиля и подписки успешно сохранены!');
                } else {
                    notify('❌ Ошибка сохранения данных.');
                }
            } catch(e) {
                notify('⚠️ Ошибка соединения с сервером.');
            }
        }

        function toggleRow(id) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            const detailsBlock = document.getElementById(`details-${id}`);
            const arrow = document.getElementById(`arrow-${id}`);
            
            document.querySelectorAll('.t-details').forEach(el => {
                if(el.id !== `details-${id}`) el.classList.remove('active');
            });
            
            detailsBlock.classList.toggle('active');
            arrow.textContent = detailsBlock.classList.contains('active') ? '▲' : '▼';
        }

        function openOfferModal(id, event) {
            event.stopPropagation();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
            activeOfferLoadId = id;
            document.getElementById('modalPrice').value = '';
            document.getElementById('modalComment').value = document.getElementById(`comment-${id}`).value || '';
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

            closeModal();
            let id = activeOfferLoadId;
            let qty = document.getElementById(`qty-${id}`)?.value || '1';
            await performBooking(id, 'bid', price, comment, qty);
        }

        async function sendAction(id, actionType, event) {
            event.stopPropagation();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');

            let qty = document.getElementById(`qty-${id}`)?.value || '1';
            let carrierComment = document.getElementById(`comment-${id}`).value;

            let confirmMsg = `Подтвердить забор груза (${qty} авто) по указанной ставке?`;

            askConfirm(confirmMsg, async (confirmed) => {
                if (confirmed) {
                    await performBooking(id, 'confirm', '', carrierComment, qty);
                }
            });
        }

        async function performBooking(id, actionType, customPrice, comment, qty) {
            let user = tg.initDataUnsafe?.user || {};
            let userId = user.id || 11111111;

            try {
                let res = await fetch(`/api/book/${id}`, { 
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
                    notify('✅ Заявка успешно отправлена логисту!');
                    loadData(); 
                } else {
                    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
                    notify('❌ ' + (respData.error || 'Ошибка. Возможно, груз уже занят.'));
                    loadData();
                }
            } catch(e) {
                notify('⚠️ Ошибка соединения с сервером.');
            }
        }

        loadData();
    </script>
</body>
</html>"""

# ==================== WEB APP БЭКЕНД ====================
async def get_loads_api(request):
    country = request.query.get('country')
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    query = """
        SELECT load_id, route, date, cars_count, price, text, 
               COALESCE(cargo_type, 'ТНП'), 
               COALESCE(weight, '20т'), 
               COALESCE(admin_comment, ''),
               COALESCE(destination_country, 'Все')
        FROM loads 
        WHERE status = 'ACTIVE'
    """
    params = []
    if country and country != 'ALL':
        query += " AND (destination_country LIKE ? OR route LIKE ?)"
        params.extend([f"%{country}%", f"%{country}%"])
        
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
        "admin_comment": r[8],
        "country": r[9]
    } for r in rows]
    return web.json_response({"loads": loads})

async def my_loads_api(request):
    user_id = request.query.get('user_id')
    if not user_id:
        return web.json_response({"deals": []})
        
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, date, route, cars, price 
        FROM confirmed_deals 
        WHERE user_id = ? 
        ORDER BY id DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    deals = [{
        "id": r[0],
        "date": r[1],
        "route": r[2],
        "cars": r[3],
        "price": r[4]
    } for r in rows]
    return web.json_response({"deals": deals})

async def profile_get_api(request):
    user_id = request.query.get('user_id')
    if not user_id:
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

    user_id = data.get('user_id')
    if not user_id:
        return web.json_response({"error": "No user_id"}, status=400)

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

async def book_load_api(request):
    load_id = int(request.match_info.get('id'))
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Неверный формат данных"}, status=400)

    user_id = data.get('user_id')
    if not user_id:
        return web.json_response({"error": "Пользователь не определён"}, status=400)

    first_name = data.get('first_name', '')
    username = data.get('username', '')
    action = data.get('action') 
    proposed_price = data.get('proposed_price', '')
    carrier_comment = data.get('comment', '')
    requested_cars = int(data.get('cars', 1))

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
        company_name, u_name, u_phone = "Не указана", first_name or f"User_{user_id}", "Не указан"
    else:
        u_status, company_name, u_name, u_phone = user_row
        if u_status == 'BLOCKED':
            conn.close()
            return web.json_response({"error": "Ваш аккаунт заблокирован"}, status=403)

    cursor.execute("SELECT status, route, date, price, cars_count, details, text FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()
    
    if not load or load[0] != 'ACTIVE':
        conn.close()
        return web.json_response({"error": "Груз недоступен или уже закрыт"}, status=400)
        
    status, route, date_str, price_str, cars_count_str, details_text, raw_cargo_text = load
    current_cars = int(re.search(r'\d+', str(cars_count_str)).group(0)) if re.search(r'\d+', str(cars_count_str)) else 1

    user_link = f"@{username}" if username else f"{u_name} (ID: {user_id})"
    carrier_info = f"👤 Перевозчик: {user_link} | Компания: {company_name or 'Не указана'} | Имя: {u_name} | Тел: {u_phone or 'Не указан'}"

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

        await update_cargo_messages_for_all_users(load_id)

        admin_notification = (
            f"🎯 Заявка на груз из Web App!\n\n"
            f"📦 Описание:\n{raw_cargo_text or route}\n\n"
            f"🚛 Забирает авто: {requested_cars}\n"
            f"💬 Комментарий: {carrier_comment or 'Нет'}\n"
            f"{carrier_info}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification)
        except Exception as e:
            logging.error(f"Error sending admin notification: {e}")

        return web.json_response({"status": "success"})

    elif action == 'bid':
        cursor.execute("""
            INSERT INTO bids (load_id, user_id, cars, rate, comment)
            VALUES (?, ?, ?, ?, ?)
        """, (load_id, user_id, requested_cars, proposed_price, carrier_comment))
        bid_id = cursor.lastrowid
        conn.commit()
        conn.close()

        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{bid_id}"),
            types.InlineKeyboardButton(text="🔀 Часть", callback_data=f"partial_bid_{bid_id}")
        )
        admin_builder.row(types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{bid_id}"))

        try:
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=(
                    f"💰 Новая ставка через Web App!\n\n"
                    f"🆔 Груз #{load_id} | Маршрут: {route}\n"
                    f"💵 Ставка: {proposed_price} | 🚛 Авто: {requested_cars}\n"
                    f"💬 Комментарий: {carrier_comment or 'Нет'}\n"
                    f"{carrier_info}"
                ),
                reply_markup=admin_builder.as_markup()
            )
        except Exception as e:
            logging.error(f"Error sending bid notification: {e}")

        return web.json_response({"status": "success"})

    conn.close()
    return web.json_response({"error": "Неизвестное действие"}, status=400)

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
    """Пингует веб-сервер каждые 3 минуты, чтобы Render/хостинг не усыплял бота."""
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
    app.router.add_get("/api/profile", profile_get_api)
    app.router.add_post("/api/profile", profile_post_api)
    app.router.add_post("/api/book/{id}", book_load_api)
    
    app.on_startup.append(webserver_on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def main():
    await asyncio.gather(
        run_bot(),
        web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())

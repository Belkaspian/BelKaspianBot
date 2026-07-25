import os
import logging
import sqlite3
import asyncio
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не задан токен бота в переменных окружения BOT_TOKEN!")

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
            status TEXT DEFAULT 'ACTIVE'
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
    
    try:
        cursor.execute("ALTER TABLE confirmed_deals ADD COLUMN load_id INTEGER")
    except sqlite3.OperationalError:
        pass 

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'ACTIVE'")
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

class AdminEditStates(StatesGroup):
    waiting_for_new_cargo_text = State()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="🏠 Меню и направления"))
    builder.add(types.KeyboardButton(text="📋 Посмотреть актуальные грузы"))
    builder.add(types.KeyboardButton(text="📦 Мои подтвержденные грузы"))
    builder.adjust(1, 2)
    return builder.as_markup(resize_keyboard=True)

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
                r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб|долл|доллар|долларов|\$|€|тг)', 
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
        price_str = "По запросу"
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
    is_closed = (status == 'CLOSED')
    
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
                    reply_markup=None,
                    parse_mode="Markdown"
                )
            else:
                builder = InlineKeyboardBuilder()
                btn_confirm = types.InlineKeyboardButton(
                    text=f"✅ Подтвердить за {price_str}",
                    callback_data=f"confirm_{cargo_id}"
                )
                btn_bid = types.InlineKeyboardButton(
                    text="💰 Своя ставка",
                    callback_data=f"bid_{cargo_id}"
                )
                builder.row(btn_confirm)
                builder.row(btn_bid)
                
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=builder.as_markup(),
                    parse_mode="Markdown"
                )
        except Exception as e:
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
    is_closed = (status == 'CLOSED')
    
    formatted_text = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, is_closed=is_closed)
    
    builder = InlineKeyboardBuilder()
    if not is_closed:
        btn_confirm = types.InlineKeyboardButton(
            text=f"✅ Подтвердить за {price_str}",
            callback_data=f"confirm_{cargo_id}"
        )
        btn_bid = types.InlineKeyboardButton(
            text="💰 Своя ставка",
            callback_data=f"bid_{cargo_id}"
        )
        builder.row(btn_confirm)
        builder.row(btn_bid)
    
    try:
        msg = await bot.send_message(
            chat_id=user_id, 
            text=formatted_text, 
            reply_markup=builder.as_markup() if not is_closed else None, 
            parse_mode="Markdown"
        )
        
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO user_messages (cargo_id, user_id, message_id) VALUES (?, ?, ?)", (cargo_id, user_id, msg.message_id))
        conn.commit()
        conn.close()
    except Exception as e:
        pass


# ==================== ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ====================
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
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()

    data = await state.get_data()
    company = data.get("company")
    name = data.get("name")
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
        is_selected = direction in user_subs
        mark = "✅ " if is_selected else "   "
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
        is_selected = d in current_subs
        mark = "✅ " if is_selected else "   "
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

@dp.message(F.text == "📋 Посмотреть актуальные грузы")
async def show_cargo_handler(message: types.Message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    u_status = cursor.fetchone()
    if u_status and u_status[0] == 'BLOCKED':
        conn.close()
        await message.answer("Ваш аккаунт заблокирован администратором.")
        return

    cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    user_subs = row[0].split(",") if row and row[0] else []
    
    if not user_subs:
        conn.close()
        await message.answer("У вас не выбрано ни одно направление. Выберите их в меню выше ⬆️", reply_markup=get_main_reply_markup())
        return
        
    placeholders = ",".join(["?"] * len(user_subs))
    cursor.execute(f"SELECT load_id FROM loads WHERE destination_country IN ({placeholders}) AND status = 'ACTIVE' ORDER BY load_id DESC LIMIT 10", user_subs)
    cargos = cursor.fetchall()
    conn.close()
    
    if not cargos:
        await message.answer("📦 В данный момент активных грузов по вашим направлениям нет.", reply_markup=get_main_reply_markup())
    else:
        await message.answer("📦 **Актуальные грузы:**", parse_mode="Markdown", reply_markup=get_main_reply_markup())
        for (load_id,) in cargos:
            await send_cargo_to_user(user_id, load_id)

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
            await message.answer(card_text, parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except Exception as e:
            pass

@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
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
    cargo_id = int(callback.data.replace("bid_", ""))
    await state.update_data(cargo_id=cargo_id, action_type="bid")
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `250.000 руб` или `2000 долл`):", parse_mode="Markdown")
    await state.set_state(DealStates.waiting_for_custom_rate)
    await callback.answer()

@dp.message(DealStates.waiting_for_custom_rate)
async def process_custom_rate(message: types.Message, state: FSMContext):
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
    qty_input = message.text.strip()
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    action_type = data.get("action_type", "confirm")
    
    user_id = message.from_user.id
    user_obj = message.from_user
    
    if user_obj.username:
        user_link = f"@{user_obj.username}"
    else:
        user_link = f"[{user_obj.full_name}](tg://user?id={user_id})"

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
    
    if status == 'CLOSED':
        conn.close()
        await message.answer("Этот груз уже закрыт.", reply_markup=get_main_reply_markup())
        await state.clear()
        return

    current_cars_match = re.search(r'\d+', str(current_cars_str))
    total_cars = int(current_cars_match.group(0)) if current_cars_match else 1
    
    requested_cars_match = re.search(r'\d+', qty_input)
    requested_cars = int(requested_cars_match.group(0)) if requested_cars_match else 1
    
    warning_text = ""
    if requested_cars > total_cars:
        requested_cars = total_cars
        warning_text = f"⚠️ Столько грузов нет, доступно только {total_cars} авто. Берем в работу {total_cars} авто.\n\n"

    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    carrier_info = f"👤 Перевозчик: {user_link} | {company} | {name} | {phone}"
    
    if action_type == "confirm":
        if total_cars > requested_cars:
            left_cars = total_cars - requested_cars
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
            f"🎯 **Заявка на груз!**\n\n"
            f"📦 Описание:\n{raw_cargo_text}\n\n"
            f"🚛 Забирает грузов: **{requested_cars}**\n"
            f"{carrier_info}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass
            
        await state.clear()
        await message.answer(f"{warning_text}✅ Заявка принята! Менеджер свяжется с вами.", reply_markup=get_main_reply_markup())
        
    elif action_type == "bid":
        rate = data.get("custom_rate")
        conn.close()
        
        bid_notification = (
            f"💰 **Новая ставка от перевозчика!**\n\n"
            f"📦 Груз:\n{raw_cargo_text}\n\n"
            f"💵 Ставка перевозчика: **{rate}** | 🚛 Забирает грузов: **{requested_cars}**\n"
            f"{carrier_info}\n\n"
            f"*(Груз остается активным для других участников)*"
        )
        
        safe_rate = rate.replace(" ", "_")
        
        admin_builder = InlineKeyboardBuilder()
        btn_accept = types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"accept_bid_{cargo_id}_{user_id}_{requested_cars}_{safe_rate}")
        btn_partial = types.InlineKeyboardButton(text="🔀 Подтвердить часть", callback_data=f"partial_bid_{cargo_id}_{user_id}_{requested_cars}_{safe_rate}")
        btn_decline = types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"decline_bid_{cargo_id}_{user_id}")
        
        admin_builder.row(btn_accept, btn_partial)
        admin_builder.row(btn_decline)

        try:
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID, 
                text=bid_notification, 
                reply_markup=admin_builder.as_markup(), 
                parse_mode="Markdown"
            )
        except Exception:
            pass
                
        await state.clear()
        await message.answer(f"{warning_text}✅ Ваша ставка и количество грузов ({requested_cars}) отправлены администратору на рассмотрение. Ожидайте обратной связи!", reply_markup=get_main_reply_markup())

@dp.callback_query(F.data.startswith("accept_bid_"))
async def admin_accept_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[2])
    carrier_id = int(parts[3])
    accepted_qty = int(parts[4])
    agreed_rate = "_".join(parts[5:]).replace("_", " ")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count, date, route, details, price, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    
    if row:
        current_cars_str, date_str, route_str, details_str, base_price, status = row
        
        if status == 'CLOSED':
            await callback.message.edit_text(callback.message.text + "\n\n**[ОШИБКА: Груз уже закрыт другим участником]**", parse_mode="Markdown")
            conn.close()
            return
            
        current_cars = int(re.search(r'\d+', str(current_cars_str)).group(0)) if re.search(r'\d+', str(current_cars_str)) else 1
        
        if accepted_qty > current_cars:
            accepted_qty = current_cars

        if current_cars > accepted_qty:
            left_cars = current_cars - accepted_qty
            cursor.execute("UPDATE loads SET cars_count = ?, price = ? WHERE load_id = ?", (str(left_cars), agreed_rate, cargo_id))
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0', price = ? WHERE load_id = ?", (agreed_rate, cargo_id))
            
        cursor.execute("""
            INSERT INTO confirmed_deals (load_id, user_id, date, route, cars, price, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cargo_id, carrier_id, date_str, route_str, accepted_qty, agreed_rate, details_str))
        
        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(cargo_id)
            
    final_price = agreed_rate if agreed_rate else (row[4] if row else "По запросу")
    load_date = row[1] if row else "Не указана"
    load_route = row[2] if row else "Не указан"
    load_details = row[3] if row else "Нет дополнительных данных"

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=(
                f"✅ **Администратор подтвердил вашу ставку! Груз закреплен за вами.**\n\n"
                f"📅 Дата загрузки: *{load_date}*\n"
                f"📍 Маршрут: *{load_route}*\n"
                f"📦 Информация по грузу: *{load_details}*\n"
                f"💰 Согласованная цена: *{final_price}*\n"
                f"🚛 Количество грузов: *{accepted_qty}*"
            ), 
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: Ставка подтверждена администратором ✅]**", reply_markup=None, parse_mode="Markdown")
    await callback.answer("Ставка подтверждена!")

@dp.callback_query(F.data.startswith("partial_bid_"))
async def admin_partial_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[2])
    carrier_id = int(parts[3])
    max_requested = int(parts[4])
    agreed_rate = "_".join(parts[5:]).replace("_", " ")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    current_cars_str = row[0] if row else "1"
    match = re.search(r'\d+', str(current_cars_str))
    current_cars = int(match.group(0)) if match else 1
    
    allowed_max = min(current_cars, max_requested)
    
    if allowed_max < 1:
        await callback.answer("Свободных авто по этому грузу не осталось!", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    safe_rate = agreed_rate.replace(" ", "_")
    
    for i in range(1, allowed_max + 1):
        cb_data = f"pconf_{cargo_id}_{carrier_id}_{i}_{safe_rate}"
        builder.add(types.InlineKeyboardButton(text=f"{i} авто", callback_data=cb_data[:64]))
    
    builder.adjust(3)
    
    await callback.message.edit_text(
        text=callback.message.text + f"\n\n**Выберите количество авто для подтверждения (макс {allowed_max}):**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pconf_"))
async def admin_process_partial_confirm(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[1])
    carrier_id = int(parts[2])
    confirmed_qty = int(parts[3])
    agreed_rate = "_".join(parts[4:]).replace("_", " ")

    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count, date, route, details, price, status FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()

    if row:
        current_cars_str, date_str, route_str, details_str, base_price, status = row

        if status == 'CLOSED':
            await callback.message.edit_text(callback.message.text + "\n\n**[ОШИБКА: Груз уже закрыт]**", reply_markup=None, parse_mode="Markdown")
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

        conn.commit()
        conn.close()
        await update_cargo_messages_for_all_users(cargo_id)
    else:
        conn.close()
        return

    final_price = agreed_rate if agreed_rate else "По запросу"
    load_date = row[1] if row else "Не указана"
    load_route = row[2] if row else "Не указан"
    load_details = row[3] if row else "Нет данных"

    try:
        await bot.send_message(
            chat_id=carrier_id,
            text=(
                f"✅ **Администратор подтвердил часть вашей ставки! Груз закреплен за вами.**\n\n"
                f"📅 Дата загрузки: *{load_date}*\n"
                f"📍 Маршрут: *{load_route}*\n"
                f"📦 Информация по грузу: *{load_details}*\n"
                f"💰 Согласованная цена: *{final_price}*\n"
                f"🚛 Количество грузов: *{confirmed_qty}*"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    old_text = callback.message.text.split("**Выберите количество авто")[0].strip()
    
    await callback.message.edit_text(
        old_text + f"\n\n**[СТАТУС: Частично подтверждено ({confirmed_qty} авто) ✅]**",
        reply_markup=None,
        parse_mode="Markdown"
    )
    await callback.answer("Успешно подтверждено!")

@dp.callback_query(F.data.startswith("decline_bid_"))
async def admin_decline_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[2])
    carrier_id = int(parts[3])
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, route, details, price FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    load_date = row[0] if row else "Не указана"
    load_route = row[1] if row else "Не указан"
    load_details = row[2] if row else "Нет данных"
    price = row[3] if row else "По запросу"

    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text=(
                f"❌ **К сожалению, ваша ставка по грузу была отклонена администратором.**\n\n"
                f"📅 Дата загрузки: *{load_date}*\n"
                f"📍 Маршрут: *{load_route}*\n"
                f"📦 Информация по грузу: *{load_details}*\n"
                f"💰 Запрошенная цена: *{price}*"
            ), 
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: Ставка отклонена ❌]**", reply_markup=None, parse_mode="Markdown")
    await callback.answer("Ставка отклонена.")


# ==================== АДМИН-ПАНЕЛЬ И УПРАВЛЕНИЕ ГРУЗАМИ (/МЕНЮ) ====================
@dp.callback_query(F.data.startswith("adm_start_del_"))
async def admin_delete_cargo(callback: types.CallbackQuery):
    cargo_id = int(callback.data.replace("adm_start_del_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0' WHERE load_id = ?", (cargo_id,))
    conn.commit()
    conn.close()
    
    await update_cargo_messages_for_all_users(cargo_id)
    
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: ГРУЗ ЗАКРЫТ 🚫]**", reply_markup=None, parse_mode="Markdown")
    await callback.answer("Груз успешно закрыт!")

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
        await message.answer("Текст не может быть пустым. Попробуйте еще раз:")
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
    await message.answer("✅ Груз успешно обновлен в базе! Рассылаю изменения...", reply_markup=get_main_reply_markup())
    
    await update_cargo_messages_for_all_users(cargo_id)

    try:
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            types.InlineKeyboardButton(text="✏️ Изменить", callback_data=f"adm_start_edit_{cargo_id}"),
            types.InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_start_del_{cargo_id}")
        )
        
        formatted_card = build_cargo_card_text(date_str, route_str, price_str, cars_str, details_text, is_closed=False)
        
        await bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=f"✏️ **Обновленный груз #{cargo_id}:**\n\n{formatted_card}",
            reply_markup=admin_kb.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление об изменении в админ-канал: {e}")

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
                    
        await event.answer(f"✅ Сообщение ({success_count} шт.) успешно разослано пользователям в чистом виде.")
    else:
        return

# --- Вкладка "Актуальные грузы" ---
@dp.callback_query(F.data == "adm_menu_active")
async def admin_menu_active(callback: types.CallbackQuery):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, text, status FROM loads WHERE status = 'ACTIVE' ORDER BY load_id DESC LIMIT 15")
    active_loads = cursor.fetchall()
    conn.close()
    
    if not active_loads:
        await callback.answer("В системе нет активных грузов.", show_alert=True)
        return
        
    await callback.answer("Загружаю активные грузы...")
    await bot.send_message(callback.message.chat.id, "📋 **Актуальные грузы (управление):**", parse_mode="Markdown")
    
    for load_id, load_text, status in active_loads:
        admin_kb = InlineKeyboardBuilder()
        admin_kb.row(
            types.InlineKeyboardButton(text="✏️ Изменить", callback_data=f"adm_start_edit_{load_id}"),
            types.InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_start_del_{load_id}")
        )
        try:
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=f"ID: #{load_id}\n\n{load_text}",
                reply_markup=admin_kb.as_markup(),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.05)
        except Exception:
            pass

# --- Вкладка "Подтвержденные грузы" (в админ-меню) ---
@dp.callback_query(F.data == "adm_menu_confirmed")
async def admin_menu_confirmed(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for direction in CHANNELS.keys():
        builder.row(types.InlineKeyboardButton(text=direction, callback_data=f"adm_dir_{direction}"[:64]))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_menu_back"))
    
    await callback.message.edit_text("🤝 **Выберите направление для просмотра сделок:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_menu_back")
async def admin_menu_back(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📦 Актуальные грузы", callback_data="adm_menu_active"))
    builder.row(types.InlineKeyboardButton(text="🤝 Подтвержденные грузы", callback_data="adm_menu_confirmed"))
    builder.row(types.InlineKeyboardButton(text="👥 Перевозчики", callback_data="adm_menu_carriers"))
    await callback.message.edit_text("🎛 **Панель администратора**\nВыберите нужный раздел:", reply_markup=builder.as_markup(), parse_mode="Markdown")

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
        JOIN users u ON cd.user_id = u.user_id
        WHERE l.destination_country = ?
        ORDER BY cd.id DESC LIMIT 15
    """
    cursor.execute(query, (direction,))
    deals = cursor.fetchall()
    conn.close()
    
    if not deals:
        await callback.answer(f"По направлению {direction} пока нет подтвержденных грузов.", show_alert=True)
        return
        
    await callback.answer(f"Загружаю грузы по: {direction}")
    
    for deal_id, load_id, date, route, cars, price, company, name, phone, user_id in deals:
        text = (
            f"🤝 **Сделка #{deal_id} (Груз #{load_id})**\n\n"
            f"📍 {route}\n"
            f"📅 Дата: {date}\n"
            f"🚛 Забрано машин: **{cars}**\n"
            f"💰 Ставка: **{price}**\n\n"
            f"👤 **Перевозчик:**\n"
            f"🏢 {company} | 👤 {name} | 📞 {phone}\n"
            f"💬 [Написать перевозчику](tg://user?id={user_id})"
        )
        
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="❌ Отменить груз перевозчику", callback_data=f"adm_ask_cancel_qty_{deal_id}"))
        
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=text, reply_markup=kb.as_markup(), parse_mode="Markdown")

# --- ВЫБОР КОЛИЧЕСТВА МАШИН ДЛЯ ОТМЕНЫ ПЕРЕВОЗЧИКУ ---
@dp.callback_query(F.data.startswith("adm_ask_cancel_qty_"))
async def admin_ask_cancel_qty(callback: types.CallbackQuery):
    deal_id = int(callback.data.replace("adm_ask_cancel_qty_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars FROM confirmed_deals WHERE id = ?", (deal_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
        
    max_cars = row[0]
    
    if max_cars <= 1:
        # Если была всего 1 машина, сразу отменяем полностью
        await execute_cancel_deal(callback, deal_id, 1)
        return
        
    builder = InlineKeyboardBuilder()
    for i in range(1, max_cars + 1):
        builder.add(types.InlineKeyboardButton(text=f"{i} авто", callback_data=f"adm_exec_cancel_{deal_id}_{i}"))
    builder.adjust(3)
    builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=f"adm_cancel_back_{deal_id}"))
    
    await callback.message.edit_text(
        text=callback.message.text + f"\n\n**Сколько авто нужно отменить из {max_cars}?**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_cancel_back_"))
async def admin_cancel_back(callback: types.CallbackQuery):
    deal_id = int(callback.data.replace("adm_cancel_back_", ""))
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, 
               u.company, u.name, u.phone, u.user_id
        FROM confirmed_deals cd
        JOIN loads l ON cd.load_id = l.load_id
        JOIN users u ON cd.user_id = u.user_id
        WHERE cd.id = ?
    """, (deal_id,))
    deal = cursor.fetchone()
    conn.close()
    
    if not deal:
        await callback.message.edit_text("Сделка уже удалена.", reply_markup=None)
        return
        
    deal_id, load_id, date, route, cars, price, company, name, phone, user_id = deal
    text = (
        f"🤝 **Сделка #{deal_id} (Груз #{load_id})**\n\n"
        f"📍 {route}\n"
        f"📅 Дата: {date}\n"
        f"🚛 Забрано машин: **{cars}**\n"
        f"💰 Ставка: **{price}**\n\n"
        f"👤 **Перевозчик:**\n"
        f"🏢 {company} | 👤 {name} | 📞 {phone}\n"
        f"💬 [Написать перевозчику](tg://user?id={user_id})"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="❌ Отменить груз перевозчику", callback_data=f"adm_ask_cancel_qty_{deal_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_exec_cancel_"))
async def admin_exec_cancel_partial(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    deal_id = int(parts[3])
    cancel_qty = int(parts[4])
    
    await execute_cancel_deal(callback, deal_id, cancel_qty)

async def execute_cancel_deal(callback: types.CallbackQuery, deal_id: int, cancel_qty: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT load_id, user_id, cars, route, price FROM confirmed_deals WHERE id = ?", (deal_id,))
    deal = cursor.fetchone()
    
    if not deal:
        await callback.answer("Сделка не найдена.", show_alert=True)
        conn.close()
        return
        
    load_id, carrier_id, current_deal_cars, route, price = deal
    
    if cancel_qty >= current_deal_cars:
        # Полная отмена сделки
        cursor.execute("DELETE FROM confirmed_deals WHERE id = ?", (deal_id,))
        conn.commit()
        conn.close()
        
        try:
            await bot.send_message(
                chat_id=carrier_id,
                text=(
                    f"⚠️ **Внимание! Ваш груз отменен администратором.**\n\n"
                    f"📍 Маршрут: {route}\n"
                    f"💰 Ставка: {price}\n"
                    f"🚛 Количество авто: {current_deal_cars}\n\n"
                    f"🔒 Груз закрыт."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
            
        old_text = callback.message.text.split("**Сколько авто")[0].strip()
        await callback.message.edit_text(
            old_text + f"\n\n**[СТАТУС: ПОЛНОСТЬЮ ОТМЕНЕНО 🔒]**",
            reply_markup=None, 
            parse_mode="Markdown"
        )
    else:
        # Частичная отмена сделки (уменьшаем кол-во авто в подтвержденной сделке)
        new_deal_cars = current_deal_cars - cancel_qty
        cursor.execute("UPDATE confirmed_deals SET cars = ? WHERE id = ?", (new_deal_cars, deal_id))
        conn.commit()
        conn.close()
        
        try:
            await bot.send_message(
                chat_id=carrier_id,
                text=(
                    f"⚠️ **Внимание! Часть вашего груза отменена администратором.**\n\n"
                    f"📍 Маршрут: {route}\n"
                    f"💰 Ставка: {price}\n"
                    f"🚛 Отменено авто: {cancel_qty} | Осталось ваших авто в сделке: {new_deal_cars}"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
            
        # Обновляем карточку в админ-канале
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cd.id, cd.load_id, cd.date, cd.route, cd.cars, cd.price, 
                   u.company, u.name, u.phone, u.user_id
            FROM confirmed_deals cd
            JOIN loads l ON cd.load_id = l.load_id
            JOIN users u ON cd.user_id = u.user_id
            WHERE cd.id = ?
        """, (deal_id,))
        updated_deal = cursor.fetchone()
        conn.close()
        
        if updated_deal:
            d_id, l_id, date, route, cars, price, company, name, phone, user_id = updated_deal
            text = (
                f"🤝 **Сделка #{d_id} (Груз #{l_id})**\n\n"
                f"📍 {route}\n"
                f"📅 Дата: {date}\n"
                f"🚛 Забрано машин: **{cars}**\n"
                f"💰 Ставка: **{price}**\n\n"
                f"👤 **Перевозчик:**\n"
                f"🏢 {company} | 👤 {name} | 📞 {phone}\n"
                f"💬 [Написать перевозчику](tg://user?id={user_id})\n\n"
                f"**[СТАТУС: Отменено {cancel_qty} авто ✅]**"
            )
            kb = InlineKeyboardBuilder()
            kb.row(types.InlineKeyboardButton(text="❌ Отменить груз перевозчику", callback_data=f"adm_ask_cancel_qty_{d_id}"))
            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

    await callback.answer("Успешно обработано!")

# ==================== РАЗДЕЛ "ПЕРЕВОЗЧИКИ" В АДМИН-МЕНЮ ====================
@dp.callback_query(F.data == "adm_menu_carriers")
async def admin_menu_carriers(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for direction in CHANNELS.keys():
        builder.row(types.InlineKeyboardButton(text=direction, callback_data=f"adm_car_dir_{direction}"[:64]))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_menu_back"))
    
    await callback.message.edit_text("👥 **Выберите направление для просмотра перевозчиков:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_car_dir_"))
async def admin_show_carriers_by_dir(callback: types.CallbackQuery):
    direction = callback.data.replace("adm_car_dir_", "")
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    # Ищем пользователей, у которых в подписках есть это направление
    cursor.execute("SELECT user_id, company, name, phone, status FROM users")
    all_users = cursor.fetchall()
    conn.close()
    
    direction_users = []
    for u in all_users:
        u_id, comp, name, phone, u_status = u
        # Проверим подписки в базе
        conn = sqlite3.connect("cargo_bot.db")
        cur = conn.cursor()
        cur.execute("SELECT subscriptions FROM users WHERE user_id = ?", (u_id,))
        sub_row = cur.fetchone()
        conn.close()
        
        subs = sub_row[0].split(",") if sub_row and sub_row[0] else []
        if direction in subs:
            direction_users.append((u_id, comp, name, phone, u_status))
            
    if not direction_users:
        await callback.answer(f"По направлению {direction} нет подписанных перевозчиков.", show_alert=True)
        return
        
    await callback.answer(f"Перевозчики по направлению: {direction}")
    
    for u_id, comp, name, phone, u_status in direction_users:
        status_label = "🟢 Активен" if u_status != 'BLOCKED' else "🔴 Заблокирован"
        text = (
            f"🏢 **Компания:** {comp}\n"
            f"👤 **Имя:** {name}\n"
            f"📞 **Телефон:** {phone}\n"
            f"ID: `{u_id}` | Статус: {status_label}"
        )
        
        kb = InlineKeyboardBuilder()
        if u_status != 'BLOCKED':
            kb.row(types.InlineKeyboardButton(text="🔴 Заблокировать", callback_data=f"adm_block_user_{u_id}_{direction}"))
        else:
            kb.row(types.InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"adm_unblock_user_{u_id}_{direction}"))
            
        kb.row(types.InlineKeyboardButton(text="📦 Подтвержденные грузы", callback_data=f"adm_user_deals_{u_id}"))
        
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_block_user_"))
async def admin_block_user(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[3])
    direction = "_".join(parts[4:])
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'BLOCKED' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    try:
        await bot.send_message(chat_id=user_id, text="❌ Ваш аккаунт был заблокирован администратором.")
    except Exception:
        pass
        
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"adm_unblock_user_{user_id}_{direction}"))
    kb.row(types.InlineKeyboardButton(text="📦 Подтвержденные грузы", callback_data=f"adm_user_deals_{user_id}"))
    
    await callback.message.edit_text(callback.message.text.replace("🟢 Активен", "🔴 Заблокирован"), reply_markup=kb.as_markup(), parse_mode="Markdown")
    await callback.answer("Перевозчик заблокирован!")

@dp.callback_query(F.data.startswith("adm_unblock_user_"))
async def admin_unblock_user(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[3])
    direction = "_".join(parts[4:])
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'ACTIVE' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    try:
        await bot.send_message(chat_id=user_id, text="✅ Ваш аккаунт был разблокирован администратором!")
    except Exception:
        pass
        
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔴 Заблокировать", callback_data=f"adm_block_user_{user_id}_{direction}"))
    kb.row(types.InlineKeyboardButton(text="📦 Подтвержденные грузы", callback_data=f"adm_user_deals_{user_id}"))
    
    await callback.message.edit_text(callback.message.text.replace("🔴 Заблокирован", "🟢 Активен"), reply_markup=kb.as_markup(), parse_mode="Markdown")
    await callback.answer("Перевозчик разблокирован!")

@dp.callback_query(F.data.startswith("adm_user_deals_"))
async def admin_show_user_confirmed_deals(callback: types.CallbackQuery):
    user_id = int(callback.data.replace("adm_user_deals_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    u_info = cursor.fetchone()
    
    cursor.execute("SELECT id, load_id, date, route, cars, price, details FROM confirmed_deals WHERE user_id = ? ORDER BY id DESC", (user_id,))
    deals = cursor.fetchall()
    conn.close()
    
    comp, name, phone = u_info if u_info else ("Не указана", "Не указан", "Не указан")
    
    if not deals:
        await callback.answer(f"У перевозчика {comp} пока нет подтвержденных грузов.", show_alert=True)
        return
        
    await callback.answer("Загружаю подтвержденные грузы перевозчика...")
    await bot.send_message(
        chat_id=ADMIN_CHANNEL_ID,
        text=f"📦 **Подтвержденные грузы перевозчика:**\n🏢 {comp} | 👤 {name} | 📞 {phone}",
        parse_mode="Markdown"
    )
    
    for deal_id, load_id, date, route, cars, price, details in deals:
        card_text = (
            f"🤝 Сделка #{deal_id} (Груз #{load_id})\n"
            f"📍 {date} | {route}\n"
            f"💰 {price} | 🚚 {cars} авто"
        )
        if details:
            card_text += f"\n📦 {details}"
            
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=card_text, parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except Exception:
            pass


# ==================== ПАРСИНГ ГРУЗОВ ИЗ КАНАЛОВ ====================
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
            if subs:
                user_subs = [s.strip() for s in subs.split(",")]
                if direction in user_subs:
                    await send_cargo_to_user(u_id, cargo_id)
                    await asyncio.sleep(0.05)


# ==================== ВЕБ-СЕРВЕР RENDER И ЗАПУСК ====================
async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
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

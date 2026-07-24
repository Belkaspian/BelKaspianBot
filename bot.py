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

# Настройка логирования
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

def init_db():
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            company TEXT,
            name TEXT,
            phone TEXT,
            subscriptions TEXT
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
            status TEXT DEFAULT 'ACTIVE',
            taken_by INTEGER
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
    
    conn.commit()
    conn.close()

init_db()

class RegistrationStates(StatesGroup):
    waiting_for_company = State()
    waiting_for_name = State()
    waiting_for_phone = State()

class DealStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_custom_rate = State()


def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="🏠 Меню и направления"))
    return builder.as_markup(resize_keyboard=True)

def extract_price(text: str) -> str:
    match = re.search(r'([\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб))', text, re.IGNORECASE)
    if match:
        return match.group(1).strip(' ,.')
    match_num = re.search(r'\d{2,}\s*[\.,]\d{3}', text)
    if match_num:
        return match_num.group(0).strip(' ,.')
    return ""

def format_cargo_text(raw_text: str, override_qty: str = None) -> str:
    clean_lines = []
    for line in raw_text.split('\n'):
        line = line.strip()
        if line.startswith('📍') or line.startswith('💰') or line.startswith('📦') or line.startswith('🚚'):
            parts = line.split('|')
            for p in parts:
                p_clean = p.replace('📍', '').replace('💰', '').replace('📦', '').replace('🚚', '').strip()
                if p_clean and not p_clean.endswith('авто') and 'руб' not in p_clean and 'USD' not in p_clean and 'EUR' not in p_clean:
                    clean_lines.append(p_clean)
            continue
        if line:
            clean_lines.append(line)

    date_str = ""
    route_str = ""
    price_str = ""
    cars_str = ""
    details = []
    
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
            clean_route = re.sub(r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб)', '', clean_route, flags=re.IGNORECASE)
            clean_route = re.sub(r'\d{2,}\s*[\.,]\d{3}', '', clean_route)
            clean_route = clean_route.strip(' ,.-')
            if clean_route:
                route_str = clean_route.replace(' - ', ' → ').replace('-', '→')
        
        if re.search(r'(RUB|USD|EUR|KZT|сум|руб)', line, re.IGNORECASE) and not price_str:
            price_str = line.strip(' ,.')
        elif not price_str and re.search(r'\d{2,}\s*[\.,]\d{3}', line):
            price_str = line.strip(' ,.')

    if override_qty is not None:
        cars_str = override_qty
    elif not cars_str:
        full_match = cars_pattern.search(raw_text)
        if full_match:
            cars_str = full_match.group(1)

    for line in clean_lines:
        if date_pattern.search(line) and ('-' in line or '→' in line or 'авт' in line):
            continue
        if re.search(r'(RUB|USD|EUR|KZT|сум|руб)', line, re.IGNORECASE) and len(line) < 25:
            continue
        if cars_pattern.search(line) and len(line) < 15 and ('-' not in line and '→' not in line):
            continue
        if line not in details:
            details.append(line)

    if not date_str:
        date_str = "Дата не указана"
    if not route_str:
        route_str = clean_lines[0] if clean_lines else "Маршрут не указан"
    if not price_str:
        price_str = "По запросу"
    
    if not cars_str:
        cars_str = "1"

    if not cars_str.endswith("авто") and not cars_str.endswith("машин"):
        cars_formatted = f"{cars_str} авто"
    else:
        cars_formatted = cars_str

    details_text = ", ".join(details) if details else "Уточняйте детали"

    return (
        f"📍 {date_str} | {route_str}\n"
        f"💰 {price_str} | 🚚 {cars_formatted}\n"
        f"📦 {details_text}"
    )

async def update_cargo_messages_for_all_users(cargo_id: int, new_text: str, price: str, is_closed: bool = False):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, message_id FROM user_messages WHERE cargo_id = ?", (cargo_id,))
    messages_to_edit = cursor.fetchall()
    conn.close()

    for u_id, msg_id in messages_to_edit:
        try:
            if is_closed:
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=f"🚫 **Груз закрыт**\n\n{new_text}",
                    reply_markup=None,
                    parse_mode="Markdown"
                )
            else:
                builder = InlineKeyboardBuilder()
                if price:
                    builder.row(types.InlineKeyboardButton(
                        text=f"✅ Подтвердить авто за {price}",
                        callback_data=f"confirm_{cargo_id}"
                    ))
                builder.row(types.InlineKeyboardButton(
                    text="💰 Предложить авто по своей ставке",
                    callback_data=f"bid_{cargo_id}"
                ))
                await bot.edit_message_text(
                    chat_id=u_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=builder.as_markup(),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logging.error(f"Не удалось обновить сообщение у пользователя {u_id}: {e}")

async def send_cargo_to_user(user_id: int, cargo_id: int, text: str, price: str):
    formatted_text = format_cargo_text(text)
    
    builder = InlineKeyboardBuilder()
    if price:
        builder.row(types.InlineKeyboardButton(
            text=f"✅ Подтвердить авто за {price}",
            callback_data=f"confirm_{cargo_id}"
        ))
    builder.row(types.InlineKeyboardButton(
        text="💰 Предложить авто по своей ставке",
        callback_data=f"bid_{cargo_id}"
    ))
    
    try:
        msg = await bot.send_message(chat_id=user_id, text=formatted_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        
        conn = sqlite3.connect("cargo_bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO user_messages (cargo_id, user_id, message_id) VALUES (?, ?, ?)", (cargo_id, user_id, msg.message_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Не удалось отправить груз пользователю {user_id}: {e}")

@dp.message(Command("start"))
@dp.message(F.text == "🏠 Меню и направления")
async def cmd_start_or_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT company FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()

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
        "Шаг 3 из 3: Нажмите кнопку ниже для отправки номера в 1 клик, "
        "либо просто введите его текстом (или отправьте `-`, чтобы пропустить):",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.set_state(RegistrationStates.waiting_for_phone)

@dp.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    else:
        text_input = message.text.strip()
        phone = "Не указан" if text_input == "-" else text_input

    data = await state.get_data()
    company = data.get("company")
    name = data.get("name")
    user_id = message.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, company, name, phone, subscriptions)
        VALUES (?, ?, ?, ?, COALESCE((SELECT subscriptions FROM users WHERE user_id = ?), ''))
    """, (user_id, company, name, phone, user_id))
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
    
    builder.row(types.InlineKeyboardButton(text="📋 Посмотреть актуальные грузы", callback_data="show_cargo"))
    
    text = (
        "⚙️ **Главное меню и направления**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться от них:\n\n"
        f"Ваши текущие подписки: {', '.join(user_subs) if user_subs else 'ничего не выбрано'}"
    )
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

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
        
    builder.row(types.InlineKeyboardButton(text="📋 Посмотреть актуальные грузы", callback_data="show_cargo"))
    
    text = (
        "⚙️ **Главное меню и направления**\n\n"
        "Нажимайте на направления ниже, чтобы подписаться или отписаться от них:\n\n"
        f"Ваши текущие подписки: {', '.join(current_subs) if current_subs else 'ничего не выбрано'}"
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "show_cargo")
async def callback_show_cargo(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT subscriptions FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    user_subs = row[0].split(",") if row and row[0] else []
    
    if not user_subs:
        conn.close()
        await callback.message.answer("У вас не выбрано ни одно направление. Выберите их в меню выше ⬆️")
        await callback.answer()
        return
        
    placeholders = ",".join(["?"] * len(user_subs))
    cursor.execute(f"SELECT load_id, text, price FROM loads WHERE destination_country IN ({placeholders}) AND status = 'ACTIVE' ORDER BY load_id DESC LIMIT 10", user_subs)
    cargos = cursor.fetchall()
    conn.close()
    
    if not cargos:
        await callback.message.answer("📦 В данный момент активных грузов по вашим направлениям нет.")
    else:
        await callback.message.answer("📦 **Актуальные грузы:**", parse_mode="Markdown")
        for cargo_id, text, price in cargos:
            await send_cargo_to_user(user_id, cargo_id, text, price)
            
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("confirm_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT text, price FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    cargo_text = row[0] if row else callback.message.text
    price = row[1] if row else ""
        
    await state.update_data(cargo_id=cargo_id, cargo_text=cargo_text, cargo_price=price, action_type="confirm")
    await callback.message.answer("Напишите, сколько авто вы забираете?")
    await state.set_state(DealStates.waiting_for_quantity)
    await callback.answer()

@dp.message(DealStates.waiting_for_quantity)
async def process_deal_quantity(message: types.Message, state: FSMContext):
    qty_input = message.text.strip()
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    cargo_text = data.get("cargo_text")
    price = data.get("cargo_price")
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
    
    current_cars_match = re.search(r'(\d+)\s*(?:авт[оа]|машин[аы]?[е]?[е]?)', cargo_text, re.IGNORECASE)
    total_cars = int(current_cars_match.group(1)) if current_cars_match else None
    
    requested_cars_match = re.search(r'\d+', qty_input)
    requested_cars = int(requested_cars_match.group(0)) if requested_cars_match else 1
    
    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    carrier_info = f"👤 Перевозчик: {user_link} | {company} | {name} | {phone}"
    
    if action_type == "confirm":
        if total_cars and total_cars > requested_cars:
            left_cars = total_cars - requested_cars
            new_cars_str = f"{left_cars} авто"
            new_formatted_text = format_cargo_text(cargo_text, override_qty=new_cars_str)
            
            cursor.execute("UPDATE loads SET text = ? WHERE load_id = ?", (new_formatted_text, cargo_id))
            conn.commit()
            conn.close()
            
            await update_cargo_messages_for_all_users(cargo_id, new_formatted_text, price, is_closed=False)
        else:
            cursor.execute("UPDATE loads SET status = 'BOOKED', taken_by = ? WHERE load_id = ?", (user_id, cargo_id))
            conn.commit()
            conn.close()
            
            await update_cargo_messages_for_all_users(cargo_id, cargo_text, price, is_closed=True)
            
        admin_notification = (
            f"🎯 **Заявка на груз!**\n\n"
            f"📦 Описание:\n{cargo_text}\n\n"
            f"🚛 Забирает авто: **{qty_input}**\n"
            f"{carrier_info}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
        except Exception:
            pass
            
        await state.clear()
        await message.answer("✅ Заявка принята! Менеджер свяжется с вами.", reply_markup=get_main_reply_markup())
        
    elif action_type == "bid":
        rate = data.get("custom_rate")
        conn.close()
        
        if total_cars and total_cars > requested_cars:
            left_cars = total_cars - requested_cars
            new_cars_str = f"{left_cars} авто"
            new_formatted_text = format_cargo_text(cargo_text, override_qty=new_cars_str)
            
            conn_u = sqlite3.connect("cargo_bot.db")
            cur_u = conn_u.cursor()
            cur_u.execute("UPDATE loads SET text = ? WHERE load_id = ?", (new_formatted_text, cargo_id))
            conn_u.commit()
            conn_u.close()
            
            await update_cargo_messages_for_all_users(cargo_id, new_formatted_text, price, is_closed=False)
        else:
            conn_u = sqlite3.connect("cargo_bot.db")
            cur_u = conn_u.cursor()
            cur_u.execute("UPDATE loads SET status = 'BOOKED', taken_by = ? WHERE load_id = ?", (user_id, cargo_id))
            conn_u.commit()
            conn_u.close()
            
            await update_cargo_messages_for_all_users(cargo_id, cargo_text, price, is_closed=True)

        bid_notification = (
            f"💰 **Новая ставка от перевозчика!**\n\n"
            f"📦 Груз:\n{cargo_text}\n\n"
            f"💵 Ставка: **{rate}** | 🚛 Авто: **{qty_input}**\n"
            f"{carrier_info}\n\n"
            f"*(Груз остается активным для других участников)*"
        )
        
        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_accept_{cargo_id}_{user_id}"),
            types.InlineKeyboardButton(text="❌ Отказать", callback_data=f"adm_decline_{cargo_id}_{user_id}")
        )

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
        await message.answer("✅ Ваша ставка и количество авто отправлены администратору на рассмотрение. Ожидайте обратной связи!", reply_markup=get_main_reply_markup())

@dp.callback_query(F.data.startswith("bid_"))
async def callback_custom_bid(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("bid_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT text, price FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    cargo_text = row[0] if row else callback.message.text
    price = row[1] if row else ""
        
    await state.update_data(cargo_id=cargo_id, cargo_text=cargo_text, cargo_price=price, action_type="bid")
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `125.000 руб`):", parse_mode="Markdown")
    await state.set_state(DealStates.waiting_for_custom_rate)
    await callback.answer()

@dp.message(DealStates.waiting_for_custom_rate)
async def process_custom_rate(message: types.Message, state: FSMContext):
    rate = message.text.strip()
    await state.update_data(custom_rate=rate)
    
    await message.answer("Сколько авто вы можете поставить по этой ставке?")
    await state.set_state(DealStates.waiting_for_quantity)

@dp.callback_query(F.data.startswith("adm_accept_"))
async def admin_accept_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[2])
    carrier_id = int(parts[3])
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE loads SET status = 'BOOKED', taken_by = ? WHERE load_id = ?", (carrier_id, cargo_id))
    cursor.execute("SELECT text, price FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    
    cargo_text = row[0] if row else ""
    price = row[1] if row else ""

    await update_cargo_messages_for_all_users(cargo_id, cargo_text, price, is_closed=True)
    
    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text="✅ Администратор подтвердил вашу ставку! Груз закреплен за вами.", 
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: Ставка подтверждена администратором ✅]**", reply_markup=None, parse_mode="Markdown")
    await callback.answer("Ставка подтверждена!")

@dp.callback_query(F.data.startswith("adm_decline_"))
async def admin_decline_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    carrier_id = int(parts[3])
    
    try:
        await bot.send_message(
            chat_id=carrier_id, 
            text="❌ К сожалению, ваша ставка по грузу была отклонена администратором. Груз остается доступным в системе.", 
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: Ставка отклонена ❌]**", reply_markup=None, parse_mode="Markdown")
    await callback.answer("Ставка отклонена.")

@dp.channel_post(F.chat.id.in_(list(CHANNEL_TO_DIRECTION.keys()) + [ADMIN_CHANNEL_ID]))
async def handle_channel_post(message: types.Message):
    chat_id = message.chat.id
    raw_text = message.text or message.caption
    if not raw_text:
        return
        
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()

    if chat_id == ADMIN_CHANNEL_ID:
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        for u in all_users:
            try:
                await bot.send_message(chat_id=u[0], text=raw_text, parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception as e:
                logging.error(f"Не удалось отправить новость пользователю {u[0]}: {e}")

    elif chat_id in CHANNEL_TO_DIRECTION:
        price = extract_price(raw_text)
        direction = CHANNEL_TO_DIRECTION.get(chat_id)
        
        formatted_initial_text = format_cargo_text(raw_text)
        
        cursor.execute("INSERT INTO loads (destination_country, text, price, status, route, cars_count, car_type) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?)", 
                       (direction, formatted_initial_text, price, formatted_initial_text, "2", "Тент"))
        cargo_id = cursor.lastrowid
        
        cursor.execute("SELECT user_id, subscriptions FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        for u_id, subs_str in all_users:
            if subs_str and direction in subs_str.split(","):
                await send_cargo_to_user(u_id, cargo_id, formatted_initial_text, price)
                await asyncio.sleep(0.05)

async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

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
            details TEXT,
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
            clean_route = re.sub(r'[\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб)', '', clean_route, flags=re.IGNORECASE)
            clean_route = re.sub(r'\d{2,}\s*[\.,]\d{3}', '', clean_route)
            clean_route = clean_route.strip(' ,.-')
            if clean_route:
                route_str = clean_route.replace(' - ', ' → ').replace('-', '→')
        
        if not re.search(r'(RUB|USD|EUR|KZT|сум|руб)', line, re.IGNORECASE) and not date_pattern.search(line) and not cars_pattern.search(line) and '-' not in line and '→' not in line:
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
                # Кнопки друг под другом
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
            logging.error(f"Не удалось обновить сообщение у пользователя {u_id}: {e}")

async def send_cargo_to_user(user_id: int, cargo_id: int):
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
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
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
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
        "Нажимайте на направления ниже, чтобы подписаться или отписаться:\n\n"
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
    cursor.execute(f"SELECT load_id FROM loads WHERE destination_country IN ({placeholders}) AND status = 'ACTIVE' ORDER BY load_id DESC LIMIT 10", user_subs)
    cargos = cursor.fetchall()
    conn.close()
    
    if not cargos:
        await callback.message.answer("📦 В данный момент активных грузов по вашим направлениям нет.")
    else:
        await callback.message.answer("📦 **Актуальные грузы:**", parse_mode="Markdown")
        for (load_id,) in cargos:
            await send_cargo_to_user(user_id, load_id)
            
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("confirm_", ""))
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    await state.update_data(cargo_id=cargo_id, action_type="confirm")
    await callback.message.answer(f"Напишите, сколько авто вы забираете? (доступно машин: {row[0] if row else '?'})")
    await state.set_state(DealStates.waiting_for_quantity)
    await callback.answer()

@dp.callback_query(F.data.startswith("bid_"))
async def callback_custom_bid(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("bid_", ""))
    await state.update_data(cargo_id=cargo_id, action_type="bid")
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `250.000 руб`):", parse_mode="Markdown")
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
    
    await message.answer(f"Сколько авто вы можете поставить по этой ставке? (доступно машин: {row[0] if row else '?'})")
    await state.set_state(DealStates.waiting_for_quantity)

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
    
    cursor.execute("SELECT cars_count, text, price FROM loads WHERE load_id = ?", (cargo_id,))
    load_row = cursor.fetchone()
    
    if not load_row:
        conn.close()
        await message.answer("Груз не найден или был удален.")
        await state.clear()
        return
        
    current_cars_str, raw_cargo_text, price_str = load_row
    
    current_cars_match = re.search(r'\d+', str(current_cars_str))
    total_cars = int(current_cars_match.group(0)) if current_cars_match else 1
    
    requested_cars_match = re.search(r'\d+', qty_input)
    requested_cars = int(requested_cars_match.group(0)) if requested_cars_match else 1
    
    # Проверка на превышение доступного лимита машин
    warning_text = ""
    if requested_cars > total_cars:
        requested_cars = total_cars
        warning_text = f"⚠️ Столько грузов нет, доступно только {total_cars} авто. Берем в работу {total_cars} авто.\n\n"

    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    carrier_info = f"👤 Перевозчик: {user_link} | {company} | {name} | {phone}"
    
    if action_type == "confirm":
        if total_cars > requested_cars:
            left_cars = total_cars - requested_cars
            new_cars_str = str(left_cars)
            
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (new_cars_str, cargo_id))
            conn.commit()
            conn.close()
            
            await update_cargo_messages_for_all_users(cargo_id)
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0', taken_by = ? WHERE load_id = ?", (user_id, cargo_id))
            conn.commit()
            conn.close()
            
            await update_cargo_messages_for_all_users(cargo_id)
            
        admin_notification = (
            f"🎯 **Заявка на груз!**\n\n"
            f"📦 Описание:\n{raw_cargo_text}\n\n"
            f"🚛 Забирает авто: **{requested_cars}**\n"
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
            f"💵 Ставка: **{rate}** | 🚛 Авто: **{requested_cars}**\n"
            f"{carrier_info}\n\n"
            f"*(Груз остается активным для других участников)*"
        )
        
        admin_builder = InlineKeyboardBuilder()
        admin_builder.row(
            types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_accept_{cargo_id}_{user_id}_{requested_cars}")
        )
        admin_builder.row(
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
        await message.answer(f"{warning_text}✅ Ваша ставка и количество авто ({requested_cars}) отправлены администратору на рассмотрение. Ожидайте обратной связи!", reply_markup=get_main_reply_markup())

@dp.callback_query(F.data.startswith("adm_accept_"))
async def admin_accept_bid(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    cargo_id = int(parts[2])
    carrier_id = int(parts[3])
    accepted_qty = int(parts[4])
    
    conn = sqlite3.connect("cargo_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cars_count FROM loads WHERE load_id = ?", (cargo_id,))
    row = cursor.fetchone()
    
    if row:
        current_cars = int(re.search(r'\d+', str(row[0])).group(0)) if re.search(r'\d+', str(row[0])) else 1
        if current_cars > accepted_qty:
            left_cars = current_cars - accepted_qty
            cursor.execute("UPDATE loads SET cars_count = ? WHERE load_id = ?", (str(left_cars), cargo_id))
            conn.commit()
            conn.close()
            await update_cargo_messages_for_all_users(cargo_id)
        else:
            cursor.execute("UPDATE loads SET status = 'CLOSED', cars_count = '0', taken_by = ? WHERE load_id = ?", (carrier_id, cargo_id))
            conn.commit()
            conn.close()
            await update_cargo_messages_for_all_users(cargo_id)
            
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
            text="❌ К сожалению, ваша ставка по грузу была отклонена администратором.", 
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
        direction = CHANNEL_TO_DIRECTION.get(chat_id)
        
        date_str, route_str, price_str, cars_str, details_text = parse_cargo_raw(raw_text)
        
        cursor.execute("""
            INSERT INTO loads (destination_country, date, route, cars_count, price, text, details, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """, (direction, date_str, route_str, cars_str, price_str, raw_text, details_text))
        cargo_id = cursor.lastrowid
        conn.commit()
        
        cursor.execute("SELECT user_id, subscriptions FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        for u_id, subs_str in all_users:
            if subs_str and direction in subs_str.split(","):
                await send_cargo_to_user(u_id, cargo_id)
                await asyncio.sleep(0.05)

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

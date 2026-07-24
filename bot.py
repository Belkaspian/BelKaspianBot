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
    conn = sqlite3.connect("bot_database.db")
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
        CREATE TABLE IF NOT EXISTS cargo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT,
            text TEXT,
            price TEXT,
            status TEXT DEFAULT 'active'
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
    waiting_for_custom_quantity = State()


def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="🏠 Меню и направления"))
    return builder.as_markup(resize_keyboard=True)

def extract_price(text: str) -> str:
    match = re.search(r'([\d\.\,\s]+(?:RUB|USD|EUR|KZT|сум|руб))', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""

async def send_cargo_to_user(user_id: int, cargo_id: int, text: str, price: str):
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
        msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO user_messages (cargo_id, user_id, message_id) VALUES (?, ?, ?)", (cargo_id, user_id, msg.message_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Не удалось отправить груз пользователю {user_id}: {e}")


# --- РЕГИСТРАЦИЯ И СТАРТ ---
@dp.message(Command("start"))
@dp.message(F.text == "🏠 Меню и направления")
async def cmd_start_or_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_database.db")
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
        "либо просто введите его текстом (or отправьте `-`, чтобы пропустить):",
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
    
    conn = sqlite3.connect("bot_database.db")
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
    
    conn = sqlite3.connect("bot_database.db")
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
    
    conn = sqlite3.connect("bot_database.db")
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
    
    conn = sqlite3.connect("bot_database.db")
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
    cursor.execute(f"SELECT id, text, price FROM cargo WHERE direction IN ({placeholders}) AND status = 'active' ORDER BY id DESC LIMIT 10", user_subs)
    cargos = cursor.fetchall()
    conn.close()
    
    if not cargos:
        await callback.message.answer("📦 В данный момент активных грузов по вашим направлениям нет.")
    else:
        await callback.message.answer("📦 **Актуальные грузы:**", parse_mode="Markdown")
        for cargo_id, text, price in cargos:
            await send_cargo_to_user(user_id, cargo_id, text, price)
            
    await callback.answer()


# --- ШАГ 1: ПОДТВЕРЖДЕНИЕ ГРУЗА ПО ФИКСИРОВАННОЙ ЦЕНЕ ---
@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_cargo(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("confirm_", ""))
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, text FROM cargo WHERE id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] != 'active':
        await callback.answer("⚠️ Этот груз уже закрыт или неактуален.", show_alert=True)
        return
        
    await state.update_data(cargo_id=cargo_id, cargo_text=row[1])
    await callback.message.answer("Напишите, сколько авто у вас?")
    await state.set_state(DealStates.waiting_for_quantity)
    await callback.answer()


@dp.message(DealStates.waiting_for_quantity)
async def process_deal_quantity(message: types.Message, state: FSMContext):
    qty = message.text.strip()
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    
    user_id = message.from_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT status FROM cargo WHERE id = ?", (cargo_id,))
    row = cursor.fetchone()
    if not row or row[0] != 'active':
        conn.close()
        await state.clear()
        await message.answer("⚠️ К сожалению, этот груз уже был разобран другим перевозчиком.", reply_markup=get_main_reply_markup())
        return

    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()
    
    # Закрываем груз по фикс. цене только здесь при окончательном подтверждении
    cursor.execute("UPDATE cargo SET status = 'closed' WHERE id = ?", (cargo_id,))
    
    cursor.execute("SELECT user_id, message_id FROM user_messages WHERE cargo_id = ?", (cargo_id,))
    messages_to_edit = cursor.fetchall()
    conn.commit()
    conn.close()
    
    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    
    admin_notification = (
        f"🎯 **Груз успешно забронирован!**\n\n"
        f"📦 Описание:\n{data.get('cargo_text')}\n\n"
        f"🚛 Перевозчик:\n"
        f"Компания: {company}\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Количество авто: {qty}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_notification, parse_mode="Markdown")
    except Exception:
        pass
        
    for u_id, msg_id in messages_to_edit:
        try:
            await bot.edit_message_text(
                chat_id=u_id,
                message_id=msg_id,
                text=f"🚫 **Груз закрыт** (уже взят перевозчиком)\n\n{data.get('cargo_text')}",
                reply_markup=None,
                parse_mode="Markdown"
            )
        except Exception:
            pass
            
    await state.clear()
    await message.answer("✅ Заявка принята! Менеджер свяжется с вами.", reply_markup=get_main_reply_markup())


# --- ШАГ 2: ПРЕДЛОЖИТЬ АВТО ПО СВОЕЙ СТАВКЕ ---
@dp.callback_query(F.data.startswith("bid_"))
async def callback_custom_bid(callback: types.CallbackQuery, state: FSMContext):
    cargo_id = int(callback.data.replace("bid_", ""))
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT status, text FROM cargo WHERE id = ?", (cargo_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] != 'active':
        await callback.answer("⚠️ Этот груз уже закрыт или неактуален.", show_alert=True)
        return
        
    await state.update_data(cargo_id=cargo_id, cargo_text=row[1])
    await callback.message.answer("Введите вашу цену / ставку за этот рейс (например: `125.000 руб`):", parse_mode="Markdown")
    await state.set_state(DealStates.waiting_for_custom_rate)
    await callback.answer()


@dp.message(DealStates.waiting_for_custom_rate)
async def process_custom_rate(message: types.Message, state: FSMContext):
    rate = message.text.strip()
    await state.update_data(custom_rate=rate)
    
    await message.answer("Сколько авто вы можете поставить по этой ставке?")
    await state.set_state(DealStates.waiting_for_custom_quantity)


@dp.message(DealStates.waiting_for_custom_quantity)
async def process_custom_quantity(message: types.Message, state: FSMContext):
    qty = message.text.strip()
    data = await state.get_data()
    cargo_id = data.get("cargo_id")
    rate = data.get("custom_rate")
    
    user_id = message.from_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT status FROM cargo WHERE id = ?", (cargo_id,))
    row = cursor.fetchone()
    if not row or row[0] != 'active':
        conn.close()
        await state.clear()
        await message.answer("⚠️ К сожалению, этот груз уже был закрыт.", reply_markup=get_main_reply_markup())
        return

    # Предложение своей ставки НЕ закрывает груз автоматически для других,
    # он остается активным, пока вы сами не решите подтвердить сделку или пока его не заберут.
    
    cursor.execute("SELECT company, name, phone FROM users WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()
    conn.close()
    
    company, name, phone = user_info if user_info else ("Не указана", "Не указано", "Не указан")
    
    bid_notification = (
        f"💰 **Новая ставка от перевозчика!**\n\n"
        f"📦 Груз:\n{data.get('cargo_text')}\n\n"
        f"💵 Предложенная ставка: **{rate}**\n"
        f"🚛 Количество авто: **{qty}**\n\n"
        f"👤 Перевозчик:\n"
        f"Компания: {company}\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n\n"
        f"*(Груз остается активным для других участников)*"
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=bid_notification, parse_mode="Markdown")
    except Exception:
        pass
            
    await state.clear()
    await message.answer("✅ Ваша ставка и количество авто отправлены администратору на рассмотрение. Ожидайте обратной связи!", reply_markup=get_main_reply_markup())


# --- АВТОМАТИЧЕСКИЙ ПЕРЕХВАТ ПОСТОВ ИЗ КАНАЛОВ ---
@dp.channel_post(F.chat.id.in_(list(CHANNEL_TO_DIRECTION.keys()) + [ADMIN_CHANNEL_ID]))
async def handle_channel_post(message: types.Message):
    chat_id = message.chat.id
    cargo_text = message.text or message.caption
    if not cargo_text:
        return
        
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()

    # Случай 1: Канал новостей (админ)
    if chat_id == ADMIN_CHANNEL_ID:
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        for u in all_users:
            try:
                await bot.send_message(chat_id=u[0], text=cargo_text, parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception as e:
                logging.error(f"Не удалось отправить новость пользователю {u[0]}: {e}")

    # Случай 2: Страновые каналы (грузы)
    elif chat_id in CHANNEL_TO_DIRECTION:
        price = extract_price(cargo_text)
        direction = CHANNEL_TO_DIRECTION.get(chat_id)
        
        cursor.execute("INSERT INTO cargo (direction, text, price, status) VALUES (?, ?, ?, 'active')", (direction, cargo_text, price))
        cargo_id = cursor.lastrowid
        
        cursor.execute("SELECT user_id, subscriptions FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        for u_id, subs_str in all_users:
            if subs_str and direction in subs_str.split(","):
                await send_cargo_to_user(u_id, cargo_id, cargo_text, price)
                await asyncio.sleep(0.05)


async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Dummy web server started on port {port}")

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())

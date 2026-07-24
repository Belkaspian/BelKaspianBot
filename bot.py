import os
import logging
import sqlite3
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получаем токен из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не задан токен бота в переменных окружения BOT_TOKEN!")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Список страновых направлений и их привязка к ID каналов
CHANNELS = {
    "Казахстан 🇰🇿": -1004309918435,
    "Узбекистан 🇺🇿": -1003470705929,
    "Кыргызстан 🇰🇬": -1004470387295,
    "Азербайджан 🇦🇿": -1004483200216,
    "Грузия 🇬🇪": -1004340496095,
    "Армения 🇦🇲": -1004335138909
}

# Обратный словарь: по ID странового канала узнаем название страны
CHANNEL_TO_DIRECTION = {v: k for k, v in CHANNELS.items()}

# ID канала-админа для общей рассылки всем пользователям
ADMIN_CHANNEL_ID = -1004271518848

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            company TEXT,
            name TEXT,
            phone TEXT,
            subscriptions TEXT
        )
    """)
    # Таблица грузов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cargo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT,
            text TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Состояния для регистрации
class RegistrationStates(StatesGroup):
    waiting_for_company = State()
    waiting_for_name = State()
    waiting_for_phone = State()


# Постоянная клавиатура (меню внизу)
def get_main_reply_markup():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="🏠 Меню и направления"))
    return builder.as_markup(resize_keyboard=True)


# --- КОМАНДА /START И РЕГИСТРАЦИЯ ---
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


# --- ГЛАВНОЕ МЕНЮ И НАСТРОЙКА НАПРАВЛЕНИЙ ---
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
        "Нажимайте на направления ниже, чтобы подписаться или отписаться от них. "
        "В ленте будут отображаться только выбранные варианты:\n\n"
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


# --- ПРОСМОТР ГРУЗОВ ПО ПОДПИСКАМ ---
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
    cursor.execute(f"SELECT direction, text FROM cargo WHERE direction IN ({placeholders}) ORDER BY id DESC LIMIT 10", user_subs)
    cargos = cursor.fetchall()
    conn.close()
    
    if not cargos:
        await callback.message.answer("📦 В данный момент активных грузов по вашим направлениям нет.")
    else:
        response_text = "📦 **Актуальные грузы по вашим направлениям:**\n\n"
        for direction, text in cargos:
            response_text += f"🔹 **{direction}**\n{text}\n\n-------------------\n"
        await callback.message.answer(response_text, parse_mode="Markdown")
        
    await callback.answer()


# --- АВТОМАТИЧЕСКИЙ ПЕРЕХВАТ ПОСТОВ ИЗ КАНАЛОВ ---
@dp.channel_post(F.chat.id.in_(list(CHANNEL_TO_DIRECTION.keys()) + [ADMIN_CHANNEL_ID]))
async def handle_channel_post(message: types.Message):
    chat_id = message.chat.id
    cargo_text = message.text or message.caption
    if not cargo_text:
        return
        
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()

    # Случай 1: Пост из общего канала-админа (рассылка всем)
    if chat_id == ADMIN_CHANNEL_ID:
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        target_users = [u[0] for u in all_users]
        if target_users:
            notification_text = f"🚨 **Общее объявление / Важный груз:**\n\n{cargo_text}"
            for u_id in target_users:
                try:
                    await bot.send_message(chat_id=u_id, text=notification_text, parse_mode="Markdown")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить общую рассылку пользователю {u_id}: {e}")
            logging.info(f"Общая рассылка выполнена. Получателей: {len(target_users)}")

    # Случай 2: Пост из одного из 6 страновых каналов (рассылка по подпискам)
    elif chat_id in CHANNEL_TO_DIRECTION:
        direction = CHANNEL_TO_DIRECTION.get(chat_id)
        
        # Сохраняем в базу данных
        cursor.execute("INSERT INTO cargo (direction, text) VALUES (?, ?)", (direction, cargo_text))
        conn.commit()
        
        cursor.execute("SELECT user_id, subscriptions FROM users")
        all_users = cursor.fetchall()
        conn.close()
        
        target_users = []
        for u_id, subs_str in all_users:
            if subs_str:
                user_subs = subs_str.split(",")
                if direction in user_subs:
                    target_users.append(u_id)
                    
        if target_users:
            notification_text = f"🚨 **Новый груз по вашему направлению!**\n\n🔹 **{direction}**\n{cargo_text}"
            for u_id in target_users:
                try:
                    await bot.send_message(chat_id=u_id, text=notification_text, parse_mode="Markdown")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю {u_id}: {e}")
            logging.info(f"Рассылка по направлению {direction} выполнена. Получателей: {len(target_users)}")


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER И ЗАПУСК ---
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

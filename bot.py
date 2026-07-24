import asyncio
import sqlite3
import os
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

router = Router()
bot = Bot(token=TOKEN)
COUNTRIES = ["Казахстан", "Узбекистан", "Кыргызстан", "Грузия", "Азербайджан", "Армения"]

def init_db():
    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER, country TEXT, PRIMARY KEY (user_id, country))')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loads (
            load_id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT,
            destination_country TEXT,
            date TEXT,
            cars_count INTEGER,
            price TEXT,
            car_type TEXT,
            status TEXT DEFAULT 'ACTIVE',
            taken_by INTEGER
        )
    ''')
    conn.commit()
    conn.close()

@router.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎛 Выбрать направления", callback_data="set_countries")],
        [InlineKeyboardButton(text="📋 Актуальные грузы (Каталог)", callback_data="show_catalog")]
    ])
    await message.answer(
        "👋 Добро пожаловать!\n\nВыберите интересующие направления для подписки или откройте каталог актуальных грузов.",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "show_catalog")
async def show_catalog(callback: CallbackQuery):
    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, route, date, cars_count, price, car_type FROM loads WHERE status = 'ACTIVE'")
    loads = cursor.fetchall()
    conn.close()

    if not loads:
        await callback.message.answer("📭 Сейчас нет активных грузов.")
        await callback.answer()
        return

    for l in loads:
        load_id, route, date, cars, price, car_type = l
        text = f"📍 **{route}**\n📅 Дата: {date} | 🚚 Авто: {cars}\n💰 Ставка: **{price}**\n📝 {car_type}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🟢 Взять за {price}", callback_data=f"take_{load_id}")]
        ])
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("take_"))
async def process_take_load(callback: CallbackQuery):
    load_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name
    username = callback.from_user.username

    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT status, route, price FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()

    if not load or load[0] != 'ACTIVE':
        conn.close()
        await callback.answer("❌ Груз уже занят!", show_alert=True)
        return

    cursor.execute("UPDATE loads SET status = 'BOOKED', taken_by = ? WHERE load_id = ?", (user_id, load_id))
    conn.commit()
    conn.close()

    await callback.answer("✅ Вы забрали груз!", show_alert=True)
    await callback.message.edit_text(f"✅ Вы забронировали: {load[1]} за {load[2]}.")
    await bot.send_message(ADMIN_ID, f"🚨 Груз #{load_id} ({load[1]}) забрал перевозчик {user_name} (@{username})")

async def main():
    init_db()
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

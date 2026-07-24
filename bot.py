import asyncio
import json
import sqlite3
import os
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from database import init_db

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-app.onrender.com")

router = Router()
bot = Bot(token=TOKEN)
COUNTRIES = ["Казахстан", "Узбекистан", "Кыргызстан", "Грузия", "Азербайджан", "Армения"]

@router.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎛 Выбрать направления", web_app=WebAppInfo(url=f"{WEBAPP_URL}/settings.html"))],
        [InlineKeyboardButton(text="📋 Каталог грузов (Логинет)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/index.html"))]
    ])
    await message.answer(
        "👋 Добро пожаловать!\n\nИспользуйте меню ниже для настройки подписок и просмотра актуальных грузов.",
        reply_markup=keyboard
    )

@router.message(F.web_app_data)
async def web_app_data_handler(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
        user_id = message.from_user.id
        conn = sqlite3.connect('cargo_bot.db')
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)", (user_id, message.from_user.full_name))
        cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        for country in data:
            if country in COUNTRIES:
                cursor.execute("INSERT INTO subscriptions (user_id, country) VALUES (?, ?)", (user_id, country))
        conn.commit()
        conn.close()
        await message.answer("✅ Настройки подписок успешно сохранены!")
    except:
        await message.answer("❌ Ошибка при сохранении.")

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

async def run_bot():
    init_db()
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)
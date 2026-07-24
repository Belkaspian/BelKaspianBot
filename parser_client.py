import sqlite3
import asyncio
import os
from telethon import TelegramClient, events
from database import parse_cargo_text

# Получите на my.telegram.org (бесплатно)
API_ID = int(os.getenv("TG_API_ID", "123456"))
API_HASH = os.getenv("TG_API_HASH", "your_api_hash")

client = TelegramClient('session_name', API_ID, API_HASH)

# ID или юзернеймы ваших 6 групп/каналов
TARGET_CHATS = [-100123456789, -100987654321] # Замените на ваши ID групп

@client.on(events.NewMessage(chats=TARGET_CHATS))
async def handle_new_cargo(event):
    text = event.raw_text
    parsed = parse_cargo_text(text)
    
    if parsed:
        conn = sqlite3.connect('cargo_bot.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO loads (raw_text, destination_country, date, route, cars_count, price, car_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (parsed['raw_text'], parsed['destination_country'], parsed['date'], parsed['route'], parsed['cars_count'], parsed['price'], parsed['car_type']))
        conn.commit()
        conn.close()
        print(f"✅ Добавлен новый груз: {parsed['route']} -> {parsed['destination_country']}")

async def main():
    await client.start()
    print("Юзербот-парсер запущен и слушает группы...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
import sqlite3
import asyncio
import threading
from bot import run_bot

app = FastAPI()

# Подключаем папку со статикой (наш фронтенд Web App)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/api/loads")
def get_loads():
    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT load_id, destination_country, date, route, cars_count, price, car_type FROM loads WHERE status = 'ACTIVE'")
    rows = cursor.fetchall()
    conn.close()
    
    loads = [{"id": r[0], "country": r[1], "date": r[2], "route": r[3], "cars": r[4], "price": r[5], "car_type": r[6]} for r in rows]
    return {"loads": loads}

@app.post("/api/book/{load_id}")
def book_load(load_id: int, user_id: int):
    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM loads WHERE load_id = ?", (load_id,))
    load = cursor.fetchone()
    if not load or load[0] != 'ACTIVE':
        conn.close()
        raise HTTPException(status_code=400, detail="Груз недоступен")
    cursor.execute("UPDATE loads SET status = 'BOOKED', taken_by = ? WHERE load_id = ?", (user_id, load_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

# Запуск Телеграм-бота в отдельном потоке вместе с сервером
@app.on_event("startup")
def startup_event():
    threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True).threading_start() if hasattr(threading.Thread, "threading_start") else threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True).start()

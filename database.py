import sqlite3
import re

def init_db():
    conn = sqlite3.connect('cargo_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER,
            country TEXT,
            PRIMARY KEY (user_id, country)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loads (
            load_id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_text TEXT,
            destination_country TEXT,
            date TEXT,
            route TEXT,
            cars_count INTEGER,
            price TEXT,
            car_type TEXT,
            status TEXT DEFAULT 'ACTIVE',
            taken_by INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def parse_cargo_text(text):
    lines = text.strip().split('\n')
    if len(lines) < 2:
        return None
    
    line1 = lines[0]
    line2 = lines[1]

    date_match = re.search(r'(\d{2}\.\d{2})', line1)
    date = date_match.group(1) if date_match else "Срочно"

    route_match = re.search(r'\d{2}\.\d{2}\s+(.*?),', line1)
    route = route_match.group(1).strip() if route_match else line1

    cars_match = re.search(r'(\d+)\s*авто', line1)
    cars_count = int(cars_match.group(1)) if cars_match else 1

    price_match = re.search(r',\s*([\d\s]+\b(?:долл|USD|руб|EUR)\b)', line1, re.IGNORECASE)
    price = price_match.group(1).strip() if price_match else "По запросу"

    dest_country = detect_country(route)

    return {
        "destination_country": dest_country,
        "date": date,
        "route": route,
        "cars_count": cars_count,
        "price": price,
        "car_type": line2,
        "raw_text": text
    }

def detect_country(route):
    route_lower = route.lower()
    if 'казахстан' in route_lower or any(c in route_lower for c in ['алматы', 'астана', 'шымкент', 'караганда']):
        return 'Казахстан'
    elif 'узбекистан' in route_lower or any(c in route_lower for c in ['ташкент', 'самарканд', 'бухара', 'навои']):
        return 'Узбекистан'
    elif 'кыргызстан' in route_lower or 'киргизия' in route_lower or 'бишкек' in route_lower:
        return 'Кыргызстан'
    elif 'грузия' in route_lower or 'тбилиси' in route_lower:
        return 'Грузия'
    elif 'азербайджан' in route_lower or 'баку' in route_lower:
        return 'Азербайджан'
    elif 'армения' in route_lower or 'ереван' in route_lower:
        return 'Армения'
    return 'Другое'
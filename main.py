import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime

import aiohttp
from aiohttp import web  # Добавили для веб-сервера
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

# Получаем переменные окружения
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
CHANNEL_ID = "@kinoshiza_channel"

# --- ВАЖНО: ПРОВЕРКА ТОКЕНА ---
# Если ключа нет, бот сразу скажет об этом в логах, а не упадет молча
if not API_TOKEN:
    print("ОШИБКА: Не найден TELEGRAM_BOT_TOKEN в переменных окружения!")
    # Для теста локально можно раскомментировать и вставить токен вручную, но лучше через ENV
    # API_TOKEN = "ВАШ_ТОКЕН_СЮДА"

# Настройки
logging.basicConfig(level=logging.INFO)
# Инициализируем объекты только если токен есть
bot = Bot(token=API_TOKEN) if API_TOKEN else None
dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()

# --- ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def health_check(request):
    """Просто отвечает Render'у, что бот жив."""
    return web.Response(text="Bot is running! Киношиза на связи.")

async def start_web_server():
    """Запускает веб-сервер на порту, который дает Render."""
    port = int(os.getenv("PORT", 8080)) # Render сам передаст порт сюда
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")

# --- РАБОТА С TMDB (ИСТОЧНИК ДАННЫХ) ---

async def get_tmdb_data(endpoint, params=None):
    if not TMDB_API_KEY: return None
    base_url = "https://api.themoviedb.org/3"
    default_params = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
    if params: default_params.update(params)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}{endpoint}", params=default_params) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

async def fetch_movie_poster(poster_path):
    if not poster_path: return None
    return f"https://image.tmdb.org/t/p/w780{poster_path}"

# --- БАЗА ДАННЫХ ---

def init_db():
    conn = sqlite3.connect('bot_memory.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY)''')
    conn.commit()
    conn.close()

def check_history(item_id):
    conn = sqlite3.connect('bot_memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM history WHERE id = ?', (str(item_id),))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def add_to_history(item_id):
    conn = sqlite3.connect('bot_memory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO history (id) VALUES (?)', (str(item_id),))
    conn.commit()
    conn.close()

# --- ГЕНЕРАТОРЫ ---

async def create_news_post():
    category = random.choice(["movie/now_playing", "movie/upcoming", "tv/popular"])
    data = await get_tmdb_data(f"/{category}")
    if not data or 'results' not in data: return None, None

    selected_item = None
    for item in data['results']:
        item_id = item['id']
        if not check_history(item_id):
            selected_item = item
            add_to_history(item_id)
            break
    
    if not selected_item: return None, None

    title = selected_item.get('title') or selected_item.get('name')
    overview = selected_item.get('overview', 'Описание отсутствует.')
    poster_path = selected_item.get('poster_path')
    release_date = selected_item.get('release_date') or selected_item.get('first_air_date')

    prompt = (
        f"Ты админ канала 'Киношиза'. Напиши короткий пост (макс 300 знаков) про: {title}.\n"
        f"Инфо: {overview}. Дата: {release_date}.\n"
        f"Стиль: Сарказм, молодежный сленг, хайп. Без приветствий."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8
        )
        text = response.choices[0].message.content
        image_url = await fetch_movie_poster(poster_path)
        return text, image_url
    except Exception as e:
        logging.error(f"Error AI: {e}")
        return None, None

async def create_random_fact_post():
    prompt = "Напиши безумный, короткий факт о кино в стиле 'Киношиза'. 1 предложение."
    try:
        text_resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        text = text_resp.choices[0].message.content
        img_resp = await client.images.generate(
            model="dall-e-3",
            prompt=f"Abstract movie art: {text[:50]}",
            size="1024x1024"
        )
        return text, img_resp.data[0].url
    except Exception:
        return "Киношиза на связи! Смотрите хорошее кино.", None

# --- ЛОГИКА ПУБЛИКАЦИИ ---

async def master_poster():
    logging.info("Генерация поста...")
    if not bot: return # Если нет токена, не работаем

    try:
        if random.random() < 0.7:
            caption, image = await create_news_post()
            if not caption: caption, image = await create_random_fact_post()
        else:
            caption, image = await create_random_fact_post()

        final_caption = f"{caption}\n\n🍿 <b>Киношиза подпишись</b>"

        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=final_caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=final_caption, parse_mode="HTML")
        logging.info("Пост готов!")
    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")

# --- ГЛАВНЫЙ ЗАПУСК ---

async def main():
    if not bot:
        logging.error("БОТ НЕ ЗАПУЩЕН: НЕТ ТОКЕНА")
        return

    init_db()
    
    # Запускаем "фейковый" сервер для Render (это решит проблему портов)
    await start_web_server()

    # Запускаем расписание
    scheduler.add_job(master_poster, 'interval', minutes=60) # Пост раз в час (для примера)
    scheduler.start()
    
    # Запускаем бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

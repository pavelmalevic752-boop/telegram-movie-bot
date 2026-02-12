import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
CHANNEL_ID = "@kinoshiza_channel"  # Замените на ваш канал

# Настройки
logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()


# --- РАБОТА С TMDB (ИСТОЧНИК РЕАЛЬНЫХ ДАННЫХ) ---

async def get_tmdb_data(endpoint, params=None):
    """Базовая функция запроса к TMDB."""
    base_url = "https://api.themoviedb.org/3"
    default_params = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
    if params:
        default_params.update(params)

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}{endpoint}", params=default_params) as resp:
            if resp.status == 200:
                return await resp.json()
            return None


async def fetch_movie_poster(poster_path):
    """Формирует ссылку на постер."""
    if not poster_path:
        return None
    return f"https://image.tmdb.org/t/p/w780{poster_path}"


# --- БАЗА ДАННЫХ (ПАМЯТЬ БОТА) ---
# Примечание: На бесплатном Render SQLite сбрасывается при деплое.
# Для продакшена лучше подключить PostgreSQL.

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


# --- ГЕНЕРАТОРЫ КОНТЕНТА ---

async def create_news_post():
    """Берет реальную новинку/популярное и делает пост."""
    # Выбираем категорию: сейчас в кино, скоро выйдет, или популярные сериалы
    category = random.choice(["movie/now_playing", "movie/upcoming", "tv/popular"])

    data = await get_tmdb_data(f"/{category}")
    if not data or 'results' not in data:
        return None, None

    # Ищем фильм, про который еще не писали
    selected_item = None
    for item in data['results']:
        item_id = item['id']
        if not check_history(item_id):
            selected_item = item
            add_to_history(item_id)
            break

    if not selected_item:
        return None, None  # Все из этого списка уже постили

    # Данные фильма
    title = selected_item.get('title') or selected_item.get('name')
    overview = selected_item.get('overview', 'Описание отсутствует.')
    poster_path = selected_item.get('poster_path')
    release_date = selected_item.get('release_date') or selected_item.get('first_air_date')

    # Промпт для GPT
    prompt = (
        f"Ты админ канала 'Киношиза'. Напиши пост про этот фильм/сериал.\n"
        f"Название: {title}\n"
        f"Дата выхода: {release_date}\n"
        f"Описание: {overview}\n\n"
        f"Задача: Перепиши это описание в стиле 'Киношиза'. Это должно быть смешно, дерзко, "
        f"с использованием сленга. Если описание скучное — высмей его. Если крутое — хайпани. "
        f"Не пиши 'Всем привет', сразу к делу. Используй эмодзи."
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
    """Генерирует рандомный факт (когда нет новостей)."""
    prompt = "Расскажи безумный факт из истории кино или о съемках известного блокбастера. Коротко и смешно. Стиль 'Киношиза'."

    text_resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    text = text_resp.choices[0].message.content

    # Тут генерируем картинку, так как у факта нет постера
    img_resp = await client.images.generate(
        model="dall-e-3",
        prompt=f"Abstract movie art, crazy style: {text[:50]}",
        size="1024x1024"
    )

    return text, img_resp.data[0].url


# --- ГЛАВНАЯ ЛОГИКА ---

async def master_poster():
    """Решает, какой тип поста сделать."""
    logging.info("Генерация поста...")

    # 70% шанс на реальную новинку/фильм, 30% на рандомный факт
    if random.random() < 0.7:
        try:
            caption, image = await create_news_post()
            if not caption:  # Если вдруг не нашли новых фильмов
                caption, image = await create_random_fact_post()
        except Exception as e:
            logging.error(f"Ошибка TMDB: {e}, переключаюсь на факт")
            caption, image = await create_random_fact_post()
    else:
        caption, image = await create_random_fact_post()

    # Финальная подпись
    final_caption = f"{caption}\n\n🍿 <b>Киношиза подпишись</b>"

    try:
        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=final_caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=final_caption, parse_mode="HTML")
        logging.info("Пост готов!")
    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")


# --- ЗАПУСК ДЛЯ RENDER ---

async def main():
    init_db()

    # Частые посты (каждые 2 часа примерно, или точное время)
    # Используем интервал для простоты на сервере
    scheduler.add_job(master_poster, 'cron', hour='8-23', minute=0)  # Каждый час с 8 до 23

    # !!! ВАЖНО ДЛЯ RENDER !!!
    # Render может убить процесс, если не будет сетевой активности.
    # Но для Worker (Background Worker) это не нужно.
    # Если деплоите как Web Service, добавьте маленький aiohttp сервер.

    scheduler.start()

    # Удаляем вебхук
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

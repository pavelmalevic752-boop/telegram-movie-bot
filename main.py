import logging
import asyncio
import sys
import os
import random
import sqlite3
import requests
from datetime import datetime

# Настройка логов
logging.basicConfig(level=logging.INFO)

try:
    from aiogram import Bot, Dispatcher
    from aiogram.types import FSInputFile
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    # Библиотека Гугла
    import google.generativeai as genai
except ImportError:
    print("❌ ОШИБКА: Зайди в Pip и установи: google-generativeai aiogram apscheduler requests")
    sys.exit()

# ==========================================
# 👇 ТВОИ КЛЮЧИ (ВНУТРИ КАВЫЧЕК!) 👇
# ==========================================

# Токен от BotFather
TELEGRAM_TOKEN = "7470711434:AAFfgojLu4NB_Vy42576Bstfajcd66EAHnQ"

# Ключ от Google (AIza...)
GOOGLE_API_KEY = "AIzaSyDK3QOEnvUFGFL5lAwnvqedGkkRmYHEAn4" 

# Ключ от TMDB (для постеров)
TMDB_API_KEY = "efccbf9e43526f8bfe58e1ddb1f65c35" 

# ID твоего канала
CHANNEL_ID = "@kinoshizik"

# ==========================================

# Настройка Google Gemini
try:
    genai.configure(api_key=GOOGLE_API_KEY)
    # Используем быструю и бесплатную модель Flash
    model = genai.GenerativeModel('gemini-2.0-flash')
except Exception as e:
    print(f"❌ Ошибка настройки Google: {e}")
    sys.exit()

# Настройка Telegram бота
try:
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()
    # Ставим UTC, чтобы Pydroid не ругался на часовые пояса
    scheduler = AsyncIOScheduler(timezone="UTC")
except Exception as e:
    print(f"❌ Ошибка бота: {e}")
    sys.exit()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('history.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY)')
    conn.commit()
    conn.close()

def is_duplicate(item_id):
    conn = sqlite3.connect('history.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM posted WHERE id = ?', (str(item_id),))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def save_to_history(item_id):
    conn = sqlite3.connect('history.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO posted (id) VALUES (?)', (str(item_id),))
    conn.commit()
    conn.close()

# --- ПОИСК ФИЛЬМОВ (TMDB) ---
def get_movie_data():
    """Ищет популярный фильм, которого еще не было в канале."""
    # Чередуем: Популярное сейчас / Скоро в кино
    category = random.choice(["movie/now_playing", "movie/popular", "tv/top_rated"])
    url = f"https://api.themoviedb.org/3/{category}"
    params = {"api_key": TMDB_API_KEY, "language": "ru-RU", "page": random.randint(1, 5)}

    try:
        # Тайм-аут 20 сек, так как мы через VPN
        response = requests.get(url, params=params, timeout=20)
        data = response.json()
        
        if 'results' in data:
            for item in data['results']:
                if not is_duplicate(item['id']):
                    title = item.get('title') or item.get('name')
                    overview = item.get('overview', '')
                    poster = item.get('poster_path')
                    
                    if poster: # Берем только если есть постер
                        return {
                            'id': item['id'],
                            'title': title,
                            'overview': overview,
                            'poster_url': f"https://image.tmdb.org/t/p/w780{poster}"
                        }
    except Exception as e:
        logging.error(f"Ошибка TMDB: {e}")
    return None

# --- ГЕНЕРАЦИЯ ПОСТА (GOOGLE GEMINI) ---
async def make_post():
    logging.info("🚀 (Google) Начинаю создание поста...")

    movie = get_movie_data()
    
    if movie:
        # Если нашли фильм
        save_to_history(movie['id'])
        logging.info(f"✅ Фильм найден: {movie['title']}")
        
        prompt = (
            f"Ты безумный админ телеграм-канала 'Киношиза'. "
            f"Напиши короткий, дерзкий, хайповый пост про фильм '{movie['title']}'. "
            f"Вот о чем он: {movie['overview']}. "
            f"Используй сленг, эмодзи 🎬🔥🍿. Не пиши 'Всем привет'. "
            f"В конце призыва к подписке НЕ делай (я добавлю сам)."
        )
        image_url = movie['poster_url']
    else:
        # Если фильмы кончились или ошибка сети - генерим факт
        logging.info("⚠️ Фильм не найден, генерирую факт...")
        prompt = (
            "Ты админ канала 'Киношиза'. "
            "Напиши один короткий, шокирующий факт о кино или актерах. "
            "Стиль: треш, угар, сленг."
        )
        image_url = None

    try:
        # ЗАПРОС К ГУГЛУ
        response = await model.generate_content_async(prompt)
        text = response.text
        
        # Формируем пост
        final_caption = f"{text}\n\n👉 <b>Киношиза подпишись</b>"

        # Отправляем
        if image_url:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image_url, caption=final_caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=final_caption, parse_mode="HTML")
            
        logging.info("🎉 Пост опубликован успешно!")

    except Exception as e:
        logging.error(f"Ошибка Google Gemini или Telegram: {e}")
        logging.error("Возможно, ты забыл включить VPN или ключ Google неверный.")

# --- ЗАПУСК ---
async def main():
    init_db()
    
    # Расписание: каждые 2 часа
    scheduler.add_job(make_post, 'interval', hours=2)
    scheduler.start()
    
    print("⏳ Делаю тестовый пост через Google Gemini...")
    await make_post()
    
    print("🤖 Бот запущен! НЕ ВЫКЛЮЧАЙ VPN.")
    # Удаляем вебхук и запускаем
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен.")

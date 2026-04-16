"""
AI Search Agent - Telegram Bot
Финальная версия: OpenRouter + Serper.dev
"""

import os
import sys
import asyncio
import logging
import aiohttp
from typing import List, Dict, Any

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("search_bot")

# === Импорты ===
try:
    from dotenv import load_dotenv
    from aiogram import Bot, Dispatcher, types
    from aiogram.filters import Command
    from aiogram.types import Message, BotCommand
    from aiogram.enums import ParseMode
    from aiogram.client.default import DefaultBotProperties
    from openai import AsyncOpenAI
except ImportError as e:
    logger.error(f"Ошибка импорта: {e}")
    sys.exit(1)

load_dotenv()

# === Конфигурация ===
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not all([OPENROUTER_API_KEY, SERPER_API_KEY, BOT_TOKEN]):
    logger.error("Отсутствуют необходимые ключи API!")
    logger.error(f"OPENROUTER: {'✓' if OPENROUTER_API_KEY else '✗'}")
    logger.error(f"SERPER: {'✓' if SERPER_API_KEY else '✗'}")
    logger.error(f"BOT_TOKEN: {'✓' if BOT_TOKEN else '✗'}")
    sys.exit(1)

# === Настройка OpenRouter ===
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "https://t.me/p01sK0vikbot",
        "X-Title": "SearchBot"
    }
)
MODEL_NAME = "google/gemini-2.0-flash-exp:free"
logger.info(f"OpenRouter настроен с моделью {MODEL_NAME}")
logger.info("Serper API настроен")


# ========== ЛОГИКА АГЕНТА ==========

async def text_to_search_query(user_input: str) -> str:
    """Превращает запрос в поисковую фразу через OpenRouter."""
    prompt = f"""
Преврати запрос пользователя в поисковую фразу для Google.
Выдели товар, бренд, модель. Добавь "купить" или "цена".
Верни ТОЛЬКО поисковую фразу, без кавычек и пояснений.

Пример: "найди айфон 15 про" → iPhone 15 Pro купить цена

Запрос: "{user_input}"
Поисковая фраза:
"""
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100
        )
        query = response.choices[0].message.content.strip()
        logger.info(f"Поисковый запрос: {query}")
        return query
    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        return user_input


async def search_serper(query: str, num_results: int = 5) -> List[Dict[str, str]]:
    """Поиск через Serper.dev API (Google Search)."""
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "q": query,
        "gl": "ru",
        "hl": "ru",
        "num": num_results
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                data = await response.json()
        
        results = []
        for item in data.get("organic", [])[:num_results]:
            results.append({
                "title": item.get("title", "Без названия"),
                "snippet": item.get("snippet", ""),
                "link": item.get("link", ""),
                "source": item.get("source", "неизвестно")
            })
        
        logger.info(f"Serper: найдено {len(results)} результатов")
        return results
        
    except Exception as e:
        logger.error(f"Ошибка Serper: {e}")
        return []


async def analyze_results(
    user_query: str,
    search_query: str,
    results: List[Dict[str, str]]
) -> str:
    """Анализирует результаты через OpenRouter."""
    if not results:
        return "❌ Ничего не найдено. Попробуй переформулировать запрос."

    results_text = ""
    for i, r in enumerate(results, 1):
        results_text += f"""
{i}. **{r['title']}**
   📝 {r['snippet'][:150]}...
   🔗 {r['link']}
"""

    prompt = f"""
Ты — ассистент по поиску выгодных покупок.

Пользователь искал: "{user_query}"
Поисковый запрос: "{search_query}"

Результаты поиска Google:
{results_text}

Найди предложение с самой низкой ценой (если цены указаны).
Если цен нет, выбери самое надёжное предложение (известный магазин).

Ответь в формате:

🔥 **Лучшее предложение:**
[Название и цена, если есть]
🔗 [Ссылка]

💡 **Альтернатива:**
[Название и ссылка] (если есть)

Если цен нет: "Цены не указаны, вот наиболее релевантное предложение: [ссылка]"
"""
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        first = results[0]
        return f"""
🔥 **Лучшее предложение:**
{first['title']}
🔗 {first['link']}
"""


async def process_user_request(user_text: str) -> str:
    """Главная функция обработки запроса."""
    logger.info(f"Запрос: {user_text[:50]}...")

    try:
        # 1. Формируем поисковый запрос
        search_query = await text_to_search_query(user_text)

        # 2. Ищем через Serper
        results = await search_serper(search_query, 5)

        # 3. Анализируем
        best_offer = await analyze_results(user_text, search_query, results)

        # 4. Формируем ответ
        header = f"🔍 **Поиск:** `{search_query}`"
        footer = f"\n\n📊 *Найдено: {len(results)}*" if results else ""

        return f"{header}\n\n{best_offer}{footer}"

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return f"⚠️ Ошибка. Попробуй позже."


def get_stats() -> Dict[str, str]:
    return {
        "model": MODEL_NAME,
        "search": "Serper.dev (Google)",
        "status": "active"
    }


# ========== ТЕЛЕГРАМ БОТ ==========

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()


async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="help", description="❓ Помощь"),
        BotCommand(command="stats", description="📊 Статистика"),
    ]
    await bot.set_my_commands(commands)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("""
👋 **Привет! Я AI-агент для поиска товаров.**

🔍 Нахожу товары по описанию
💰 Сравниваю цены
🎯 Выбираю лучшее предложение

**Примеры:**
• `найди iphone 15 pro`
• `робот пылесос xiaomi`
• `утюг philips цена`

Просто напиши, что хочешь найти!
""")
    logger.info(f"Пользователь {message.from_user.id} запустил бота")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("""
❓ **Как пользоваться**

1️⃣ Напиши, что хочешь найти
2️⃣ Я найду результаты через Google
3️⃣ Проанализирую с помощью ИИ
4️⃣ Пришлю лучшее предложение

**Советы:**
• Указывай бренд и модель
• Добавляй "цена" или "купить"

*Время ответа: 5-10 секунд*
""")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_stats()
    await message.answer(f"""
📊 **Статистика**

🔧 Модель: `{stats['model']}`
🌐 Поиск: `{stats['search']}`
✅ Статус: `{stats['status']}`
""")


@dp.message()
async def handle_text(message: Message):
    user_text = message.text.strip()

    if len(user_text) < 3:
        await message.answer("⚠️ Слишком коротко. Напиши, что искать.")
        return

    await bot.send_chat_action(message.chat.id, action="typing")
    status_msg = await message.answer("🔍 *Ищу информацию...*")

    try:
        response = await process_user_request(user_text)
        await status_msg.edit_text(
            response,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False
        )
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await status_msg.edit_text("❌ Ошибка. Попробуй позже.")


# ========== ЗАПУСК ==========

async def main():
    logger.info("Запуск бота...")
    await set_commands(bot)
    try:
        logger.info("Бот запущен!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")

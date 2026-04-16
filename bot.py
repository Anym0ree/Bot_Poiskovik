"""
AI Search Agent - Telegram Bot
Исправленная версия: gemini-2.5-flash + обход rate limit DuckDuckGo
"""

import os
import sys
import asyncio
import logging
import time
import random
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
    import google.generativeai as genai
    from duckduckgo_search import DDGS
except ImportError as e:
    logger.error(f"Ошибка импорта: {e}")
    sys.exit(1)

load_dotenv()

# === Конфигурация ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not GEMINI_API_KEY or not BOT_TOKEN:
    logger.error("Отсутствуют ключи API!")
    sys.exit(1)

# === Настройка Gemini (ИСПРАВЛЕНО: новая модель) ===
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash"  # <-- ВОТ ГЛАВНОЕ ИСПРАВЛЕНИЕ
MODEL = genai.GenerativeModel(MODEL_NAME)
logger.info(f"Gemini API настроен с моделью {MODEL_NAME}")


# ========== ЛОГИКА АГЕНТА ==========

async def text_to_search_query(user_input: str) -> str:
    """Превращает естественный язык в поисковый запрос."""
    prompt = f"""
Преврати запрос в поисковую фразу для поиска товара.
Выдели товар, бренд, модель. Добавь "купить" или "цена".
Верни ТОЛЬКО поисковую фразу, без пояснений.

Пример: "найди айфон 15 про" → iPhone 15 Pro купить цена

Запрос: "{user_input}"
Поисковая фраза:
"""
    try:
        response = await MODEL.generate_content_async(prompt)
        query = response.text.strip()
        logger.info(f"Поисковый запрос: {query}")
        return query
    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}")
        return user_input


def search_duckduckgo(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Поиск через DuckDuckGo с обходом rate limit.
    Добавлена задержка и смена региона при ошибке.
    """
    results = []
    regions = ['wt-wt', 'ru-ru', 'us-en']  # Пробуем разные регионы
    
    for region in regions:
        try:
            # Небольшая задержка перед запросом (обходит rate limit)
            time.sleep(random.uniform(1.0, 2.0))
            
            with DDGS() as ddgs:
                for r in ddgs.text(
                    query,
                    region=region,
                    safesearch='moderate',
                    max_results=max_results
                ):
                    results.append({
                        'title': r.get('title', 'Без названия'),
                        'snippet': r.get('body', ''),
                        'link': r.get('href', ''),
                        'source': r.get('source', 'неизвестно')
                    })
            
            if results:
                logger.info(f"Найдено результатов: {len(results)} (регион {region})")
                break
                
        except Exception as e:
            logger.warning(f"Попытка с регионом {region} не удалась: {e}")
            continue
    
    return results


async def analyze_results(
    user_query: str,
    search_query: str,
    results: List[Dict[str, str]]
) -> str:
    """Анализирует результаты и находит лучшее предложение."""
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

Результаты:
{results_text}

Найди предложение с самой низкой ценой (если цены указаны).
Если цен нет, выбери самое надёжное предложение.

Ответь в формате:

🔥 **Лучшее предложение:**
[Название и цена]
🔗 [Ссылка]

💡 **Альтернатива:**
[Название и ссылка] (если есть)
"""
    try:
        response = await MODEL.generate_content_async(prompt)
        return response.text.strip()
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
        # 1. Понимание запроса
        search_query = await text_to_search_query(user_text)

        # 2. Поиск (в отдельном потоке)
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, search_duckduckgo, search_query, 5)

        # 3. Анализ
        best_offer = await analyze_results(user_text, search_query, results)

        # 4. Ответ
        header = f"🔍 **Поиск:** `{search_query}`"
        footer = f"\n\n📊 *Найдено: {len(results)}*" if results else ""

        return f"{header}\n\n{best_offer}{footer}"

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return f"⚠️ Ошибка. Попробуй позже."


def get_stats() -> Dict[str, str]:
    return {
        "model": MODEL_NAME,
        "search": "DuckDuckGo",
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
2️⃣ Я найду результаты
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

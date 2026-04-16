"""
AI Search Agent - Telegram Bot
Продвинутая версия: прозрачный поиск, объяснение выбора, высокая стабильность
"""

import os
import sys
import asyncio
import logging
import aiohttp
import json
from typing import List, Dict, Any, Optional

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("smart_search_bot")

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
    sys.exit(1)

# === Настройка OpenRouter ===
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "https://t.me/p01sK0vikbot",
        "X-Title": "SmartSearchBot"
    }
)
PRIMARY_MODEL = "google/gemini-2.0-flash-exp:free"
FALLBACK_MODEL = "openai/gpt-4o-mini" # Платная, но дешёвая модель для подстраховки

logger.info(f"Основная ИИ-модель: {PRIMARY_MODEL}")
logger.info(f"Резервная ИИ-модель: {FALLBACK_MODEL}")
logger.info("Serper API настроен")

# ========== УМНАЯ ЛОГИКА АГЕНТА ==========

async def call_llm(prompt: str, temperature: float = 0.1, max_tokens: int = 100) -> str:
    """Вызов LLM с автоматическим переключением на резервную модель при ошибке."""
    try:
        response = await client.chat.completions.create(
            model=PRIMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Ошибка при вызове {PRIMARY_MODEL}: {e}. Переключаюсь на {FALLBACK_MODEL}...")
        try:
            response = await client.chat.completions.create(
                model=FALLBACK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens
            )
            return response.choices[0].message.content.strip()
        except Exception as fallback_e:
            logger.error(f"Ошибка и у резервной модели: {fallback_e}")
            raise fallback_e

async def text_to_search_query(user_input: str) -> str:
    """Превращает запрос в оптимизированную поисковую фразу."""
    prompt = f"""
Преврати запрос пользователя в поисковую фразу для Google Shopping.
Выдели товар, бренд, модель. Добавь "купить" или "цена".
Верни ТОЛЬКО поисковую фразу, без кавычек и пояснений.

Пример: "найди айфон 15 про" → iPhone 15 Pro купить цена

Запрос: "{user_input}"
Поисковая фраза:
"""
    try:
        query = await call_llm(prompt, temperature=0.1, max_tokens=100)
        logger.info(f"Поисковый запрос: {query}")
        return query
    except Exception as e:
        logger.error(f"Ошибка формирования запроса: {e}")
        return user_input

async def search_serper(query: str, num_results: int = 5) -> List[Dict[str, Any]]:
    """Поиск через Serper.dev с оптимизацией."""
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "q": query + " цена купить",
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
            # Исключаем заведомо нерелевантные сайты
            if "pinterest.com" in item.get("link", "") or "youtube.com" in item.get("link", ""):
                continue
                
            results.append({
                "title": item.get("title", "Без названия"),
                "snippet": item.get("snippet", ""),
                "link": item.get("link", ""),
                "source": item.get("source", item.get("displayLink", "неизвестно")),
                "price": item.get("price") # Serper иногда отдаёт цену отдельно
            })
        
        logger.info(f"Serper: найдено {len(results)} результатов по запросу '{query}'")
        return results
        
    except Exception as e:
        logger.error(f"Ошибка Serper: {e}")
        return []

async def analyze_results(
    user_query: str,
    search_query: str,
    results: List[Dict[str, Any]]
) -> Dict[str, str]:
    """
    Анализирует результаты, возвращая не только ответ, но и его обоснование.
    """
    if not results:
        return {"answer": "❌ Ничего не найдено.", "reason": ""}

    results_text = ""
    for i, r in enumerate(results, 1):
        price_info = f" (Цена: {r['price']})" if r.get('price') else ""
        results_text += f"""
{i}. **{r['title']}**{price_info}
   📝 {r['snippet'][:150]}...
   🔗 {r['link']}
   📦 Источник: {r['source']}
"""

    prompt = f"""
Ты — эксперт по поиску выгодных покупок. Проанализируй результаты поиска Google и выбери лучшее предложение для пользователя.

Пользователь искал: "{user_query}"
Поисковый запрос: "{search_query}"

Результаты поиска:
{results_text}

Твоя задача:
1. Внимательно проанализируй сниппеты. Найди в них упоминания цен.
2. Если цены указаны, выбери предложение с самой низкой ценой.
3. Если цен нет, выбери самое надёжное предложение от известного магазина (например, DNS, Ситилинк, М.Видео, Ozon, Wildberries, официальный сайт бренда).
4. ОБЯЗАТЕЛЬНО объясни свой выбор.

Верни ответ СТРОГО в формате JSON:
{{
  "best_offer_title": "Название лучшего предложения",
  "best_offer_link": "Ссылка",
  "best_offer_price": "Цена, если найдена",
  "reason": "Чёткое и краткое объяснение: почему выбрано именно это. Например: 'Найдена самая низкая цена — 7990 руб.' или 'Цены не указаны, но это официальный сайт бренда, которому можно доверять.'"
}}
"""
    try:
        response = await call_llm(prompt, temperature=0.3, max_tokens=500)
        
        # Пытаемся распарсить JSON
        try:
            # Находим JSON в ответе
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            json_str = response[json_start:json_end]
            analysis = json.loads(json_str)
            logger.info(f"Анализ выполнен успешно: {analysis.get('reason')}")
            return analysis
        except json.JSONDecodeError:
            logger.warning("Не удалось распарсить JSON, использую fallback")
            # Fallback: забираем первый результат
            first = results[0]
            return {
                "best_offer_title": first['title'],
                "best_offer_link": first['link'],
                "best_offer_price": first.get('price', 'Цена не указана'),
                "reason": "Автоматический выбор: первое и наиболее релевантное предложение в выдаче Google."
            }
            
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        first = results[0]
        return {
            "best_offer_title": first['title'],
            "best_offer_link": first['link'],
            "best_offer_price": first.get('price', 'Цена не указана'),
            "reason": "Автоматический выбор из-за технической ошибки анализа."
        }

async def process_user_request(user_text: str) -> str:
    """Главная функция обработки запроса с расширенным ответом."""
    logger.info(f"Запрос: {user_text[:50]}...")

    try:
        # 1. Формируем поисковый запрос
        search_query = await text_to_search_query(user_text)

        # 2. Ищем через Serper
        results = await search_serper(search_query, 5)

        # 3. Анализируем
        analysis = await analyze_results(user_text, search_query, results)

        # 4. Формируем красивый ответ
        header = f"🔍 **Поисковый запрос:** `{search_query}`"
        
        offer_block = f"""
🔥 **Лучшее предложение:**
**{analysis.get('best_offer_title', 'Не найдено')}**
💰 {analysis.get('best_offer_price', 'Цена не указана')}
🔗 {analysis.get('best_offer_link', '')}
"""
        reason_block = f"""
🧠 **Почему выбрано это:**
{analysis.get('reason', 'Анализ не предоставлен.')}
"""
        footer = f"📊 *Проанализировано результатов: {len(results)}*"

        full_response = f"{header}\n{offer_block}\n{reason_block}\n{footer}"
        
        logger.info("Ответ сформирован")
        return full_response

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}", exc_info=True)
        return f"⚠️ Внутренняя ошибка. Попробуй позже."

def get_stats() -> Dict[str, str]:
    return {
        "model": f"{PRIMARY_MODEL} (резерв: {FALLBACK_MODEL})",
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
👋 **Привет! Я умный AI-агент для поиска товаров.**

🔍 Нахожу товары по описанию
💰 Сравниваю цены
🎯 Выбираю лучшее предложение
🧠 Объясняю свой выбор

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
4️⃣ Пришлю лучшее предложение и объясню, почему выбрал его

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
    logger.info("Запуск продвинутого бота...")
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

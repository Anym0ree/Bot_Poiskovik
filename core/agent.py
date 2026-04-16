import os
import asyncio
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

import google.generativeai as genai
from duckduckgo_search import DDGS
from pydantic import BaseModel, Field

from utils.logger import setup_logger

logger = setup_logger("agent")

# === Настройка Gemini ===
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_SEARCH = genai.GenerativeModel("gemini-1.5-flash")
MODEL_ANALYZE = genai.GenerativeModel("gemini-1.5-flash")


class SearchResult(BaseModel):
    title: str
    snippet: str
    link: str
    source: Optional[str] = None


class AgentResponse(BaseModel):
    success: bool
    search_query: str
    results_count: int
    best_offer: str
    raw_results: Optional[List[Dict[str, str]]] = Field(default_factory=list)


async def text_to_search_query(user_input: str) -> str:
    """
    Превращает естественный язык в поисковый запрос.
    """
    prompt = f"""
    Ты — поисковый ассистент. Преврати запрос пользователя в оптимальную поисковую фразу 
    для поиска товара с целью найти лучшую цену.
    
    Правила:
    1. Выдели название товара, бренд, модель.
    2. Если есть характеристики (объём, цвет, размер) — включи их.
    3. Добавь слово "купить" или "цена".
    4. Верни ТОЛЬКО поисковую фразу, без кавычек и лишних слов.
    
    Примеры:
    "найди айфон 15 про 256 гб серый" → iPhone 15 Pro 256GB Space Gray купить
    "утюг филипс недорого" → Philips утюг купить цена
    "хочу робот пылесос сяоми" → Xiaomi робот пылесос купить
    
    Запрос: "{user_input}"
    Поисковая фраза:
    """
    
    try:
        response = await MODEL_SEARCH.generate_content_async(prompt)
        query = response.text.strip()
        logger.info(f"Поисковый запрос сформирован: {query}")
        return query
    except Exception as e:
        logger.error(f"Ошибка формирования запроса: {e}")
        return user_input  # Fallback на оригинальный текст


def search_duckduckgo(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Поиск через DuckDuckGo. Синхронная функция.
    """
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(
                query, 
                region='wt-wt', 
                safesearch='moderate',
                max_results=max_results
            ):
                results.append({
                    'title': r.get('title', 'Без названия'),
                    'snippet': r.get('body', ''),
                    'link': r.get('href', ''),
                    'source': r.get('source', 'неизвестно')
                })
        logger.info(f"DuckDuckGo: найдено {len(results)} результатов")
    except Exception as e:
        logger.error(f"Ошибка поиска DuckDuckGo: {e}")
    
    return results


async def analyze_and_find_best(
    user_query: str, 
    search_query: str, 
    results: List[Dict[str, str]]
) -> str:
    """
    Анализирует результаты поиска и находит лучшее предложение.
    """
    if not results:
        return "❌ К сожалению, ничего не найдено. Попробуй переформулировать запрос."
    
    # Формируем текст для анализа
    results_text = ""
    for i, r in enumerate(results, 1):
        results_text += f"""
{i}. **{r['title']}**
   📝 {r['snippet'][:200]}...
   🔗 {r['link']}
   📦 {r.get('source', 'неизвестный источник')}
"""
    
    prompt = f"""
    Ты — ассистент по поиску выгодных покупок.
    
    Пользователь хотел найти: "{user_query}"
    Мы искали по запросу: "{search_query}"
    
    Вот результаты поиска:
    {results_text}
    
    Задача:
    1. Найди предложение с самой низкой ценой (если цены указаны).
    2. Если цен нет в сниппетах, выбери самое надёжное предложение (известный магазин, официальный сайт).
    3. Если есть несколько вариантов, укажи это.
    
    Формат ответа:
    
    🔥 **Лучшее предложение:**
    [Название товара и цена, если есть]
    🔗 [Прямая ссылка]
    
    💡 **Альтернатива:**
    [Название и ссылка на второй вариант, если есть]
    
    Если цен нигде нет, напиши:
    "Цены в сниппетах не указаны. Вот наиболее релевантное предложение: [ссылка]"
    
    Будь конкретным и полезным.
    """
    
    try:
        response = await MODEL_ANALYZE.generate_content_async(prompt)
        answer = response.text.strip()
        logger.info("Анализ результатов выполнен успешно")
        return answer
    except Exception as e:
        logger.error(f"Ошибка анализа результатов: {e}")
        # Fallback: просто отдаём первый результат
        first = results[0]
        return f"""
🔥 **Лучшее предложение (автовыбор):**
{first['title']}
🔗 {first['link']}
"""


async def process_user_request(user_text: str) -> str:
    """
    Главная функция обработки запроса пользователя.
    Принимает текст, возвращает форматированный ответ.
    """
    logger.info(f"Получен запрос: {user_text[:50]}...")
    
    try:
        # Шаг 1: Понимание запроса
        search_query = await text_to_search_query(user_text)
        
        # Шаг 2: Поиск
        # Выполняем синхронный поиск в отдельном потоке чтобы не блокировать event loop
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, search_duckduckgo, search_query, 5)
        
        # Шаг 3: Анализ и выбор лучшего
        best_offer = await analyze_and_find_best(user_text, search_query, results)
        
        # Шаг 4: Формирование финального ответа
        header = f"🔍 **Поисковый запрос:** `{search_query}`"
        
        if len(results) > 0:
            footer = f"\n\n📊 *Найдено результатов: {len(results)}*"
        else:
            footer = ""
        
        full_response = f"{header}\n\n{best_offer}{footer}"
        
        logger.info(f"Ответ сформирован, длина: {len(full_response)} символов")
        return full_response
        
    except Exception as e:
        logger.error(f"Критическая ошибка в process_user_request: {e}", exc_info=True)
        return f"""
⚠️ **Произошла ошибка при обработке запроса**

Пожалуйста, попробуй:
• Переформулировать запрос короче
• Написать только название товара
• Попробовать позже

*Техническая информация:* `{str(e)[:100]}`
"""


# === Дополнительные функции для расширения ===

async def search_with_filters(
    user_text: str, 
    max_price: Optional[int] = None,
    preferred_shops: Optional[List[str]] = None
) -> str:
    """
    Расширенный поиск с фильтрами (задел на будущее).
    """
    # TODO: добавить фильтрацию по цене и магазинам
    return await process_user_request(user_text)


def get_stats() -> Dict[str, Any]:
    """
    Возвращает статистику использования (для админ-панели).
    """
    return {
        "model": "gemini-1.5-flash",
        "search_engine": "DuckDuckGo",
        "status": "active"
    }

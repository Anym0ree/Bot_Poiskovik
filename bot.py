import re
import logging
from typing import List, Dict, Any, Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from tavily import TavilyClient
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

if not TELEGRAM_TOKEN or not TAVILY_API_KEY:
    raise ValueError("Не заданы переменные окружения TELEGRAM_TOKEN и TAVILY_API_KEY")
# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Список городов (можно расширить)
CITIES = {
    "москва", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар"
}

# Регулярки для извлечения данных
PRICE_REGEX = re.compile(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|rub|р)')
RATING_REGEX = re.compile(r'(\d(?:[.,]\d)?)\s*[/]?\s*5')
DELIVERY_COST_REGEX = re.compile(r'доставк[ае]\s*[: ]?\s*(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб)')
DELIVERY_DAYS_REGEX = re.compile(r'(\d{1,3})\s*(?:дн|день|дня|дней|day|days)')

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

def is_valid_query(text: str) -> bool:
    if len(text.strip()) < 3:
        return False
    if not re.search(r'[a-zA-Zа-яА-Я]', text):
        return False
    # Простейшая проверка на мат (можно расширить)
    bad_words = ['хер', 'хуй', 'пизд', 'ебан', 'fuck', 'shit']
    for bw in bad_words:
        if bw in text.lower():
            return False
    return True

def parse_number(s: str) -> float:
    s = s.replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except:
        return 0.0

def extract_product_info(snippet: Dict[str, Any]) -> Optional[Dict]:
    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None

    title = snippet.get('title', '')
    url = snippet.get('url', '')

    price_match = PRICE_REGEX.search(content)
    price = parse_number(price_match.group(1)) if price_match else 0.0

    rating_match = RATING_REGEX.search(content)
    rating = parse_number(rating_match.group(1)) if rating_match else 0.0
    if rating > 5:
        rating = 5.0

    delivery_cost_match = DELIVERY_COST_REGEX.search(content)
    delivery_cost = parse_number(delivery_cost_match.group(1)) if delivery_cost_match else 0.0

    delivery_days_match = DELIVERY_DAYS_REGEX.search(content)
    delivery_days = int(delivery_days_match.group(1)) if delivery_days_match else 0

    if price == 0 and rating == 0 and delivery_cost == 0 and delivery_days == 0:
        return None

    return {
        'title': title,
        'price': price,
        'rating': rating,
        'delivery_cost': delivery_cost,
        'delivery_days': delivery_days,
        'url': url
    }

def normalize_list(values: List[float]) -> List[float]:
    if not values:
        return []
    min_v, max_v = min(values), max(values)
    if max_v == min_v:
        return [0.5] * len(values)
    return [(max_v - v) / (max_v - min_v) for v in values]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я ищу товары в интернете.\n"
        "Напиши, что хочешь найти, например: «игровой ноутбук москва»\n"
        "Если город не укажешь — спрошу отдельно."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    state = context.user_data.get('state', 'new')

    # --- Этап: спрашиваем город, если его нет ---
    if state == 'new':
        city = extract_city(user_text)
        if city:
            context.user_data['city'] = city
            query = user_text
        else:
            context.user_data['pending_query'] = user_text
            context.user_data['state'] = 'awaiting_city'
            await update.message.reply_text("В каком городе ищем товар?")
            return
    elif state == 'awaiting_city':
        city = extract_city(user_text) or user_text
        context.user_data['city'] = city
        query = context.user_data.get('pending_query', '')
        context.user_data['state'] = 'done'
    else:
        query = user_text
        city = context.user_data.get('city', '')

    # --- Валидация запроса ---
    if not is_valid_query(query):
        await update.message.reply_text(
            "❌ Запрос слишком короткий, содержит неприемлемые слова или не похож на поиск товара.\n"
            "Пожалуйста, перефразируйте."
        )
        return

    city = context.user_data.get('city', '')
    search_query = f"{query} цена рейтинг доставка {city}" if city else query

    # --- Поиск через Tavily ---
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        results = client.search(search_query, max_results=8)
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("Ошибка при поиске. Проверьте ключ Tavily.")
        return

    # --- Извлекаем данные ---
    products_raw = []
    for item in results.get('results', []):
        info = extract_product_info(item)
        if info:
            products_raw.append(info)

    if not products_raw:
        await update.message.reply_text("Не удалось найти товары с достаточной информацией.")
        return

    # --- Нормализация и расчёт баллов ---
    prices = [p['price'] for p in products_raw]
    ratings = [p['rating'] / 5 for p in products_raw]
    delivery_costs = [p['delivery_cost'] for p in products_raw]
    delivery_days = [p['delivery_days'] for p in products_raw]

    norm_prices = normalize_list(prices)
    norm_ratings = normalize_list(ratings)  # нормализация рейтинга в 0-1 (чем выше, тем лучше)
    norm_costs = normalize_list(delivery_costs)
    norm_days = normalize_list(delivery_days)

    for i, prod in enumerate(products_raw):
        score = (0.4 * norm_prices[i] +
                 0.3 * norm_ratings[i] +
                 0.15 * norm_costs[i] +
                 0.15 * norm_days[i])
        prod['score'] = score

    # Сортировка и топ-3
    products_raw.sort(key=lambda x: x['score'], reverse=True)
    top3 = products_raw[:3]

    # --- Формируем ответ ---
    answer = "🔍 **ТОП-3 товаров**\n\n"
    for idx, prod in enumerate(top3, 1):
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else "🥉")
        answer += f"{medal} **{prod['title']}**\n"
        answer += f"💰 Цена: {prod['price']:.2f} ₽\n"
        answer += f"⭐ Рейтинг: {prod['rating']:.1f}/5\n"
        answer += f"📦 Доставка: {prod['delivery_cost']:.2f} ₽, {prod['delivery_days']} дн.\n"
        answer += f"📊 Балл: {prod['score']:.2f}\n"
        answer += f"[Ссылка]({prod['url']})\n\n"

    # Обоснование для первого места
    if top3:
        best = top3[0]
        reasons = []
        if best['score'] > 0.7:
            reasons.append("высокий рейтинг")
        if best['price'] < min(p['price'] for p in products_raw):
            reasons.append("самая низкая цена")
        if best['delivery_days'] < min(p['delivery_days'] for p in products_raw):
            reasons.append("быстрая доставка")
        if not reasons:
            reasons.append("сбалансированные характеристики")
        answer += f"\n💡 **Почему первый товар лучший?** {', '.join(reasons)}."

    await update.message.reply_text(answer, parse_mode='Markdown')

    # Сброс состояния
    context.user_data.clear()

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling()

if __name__ == '__main__':
    main()

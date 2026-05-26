import re
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from tavily import TavilyClient
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

if not TELEGRAM_TOKEN or not TAVILY_API_KEY:
    raise ValueError("Не заданы переменные окружения TELEGRAM_TOKEN и TAVILY_API_KEY")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния диалога
AWAITING_QUERY, AWAITING_CITY = range(2)

# Список городов
CITIES = {
    "москва", "мск", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар"
}

# Регулярки — улучшенные
PRICE_REGEX = re.compile(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|rub|р|RUB)', re.IGNORECASE)
DELIVERY_COST_REGEX = re.compile(r'доставк[ае]\s*[:]?\s*(?:бесплатно|(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб))', re.IGNORECASE)
DELIVERY_DAYS_REGEX = re.compile(r'(\d{1,3})\s*(?:дн|день|дня|дней|day|days)', re.IGNORECASE)
RATING_REGEX = re.compile(r'(\d(?:[.,]\d)?)\s*[/]?\s*5')

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

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

    # Цена
    price_match = PRICE_REGEX.search(content)
    price = parse_number(price_match.group(1)) if price_match else None
    if price is None or price == 0:
        return None  # товар без цены не рассматриваем

    # Рейтинг
    rating_match = RATING_REGEX.search(content)
    rating = parse_number(rating_match.group(1)) if rating_match else 0.0
    if rating > 5:
        rating = 5.0

    # Стоимость доставки
    delivery_cost_match = DELIVERY_COST_REGEX.search(content)
    delivery_cost = parse_number(delivery_cost_match.group(1)) if delivery_cost_match and delivery_cost_match.group(1) else 0.0
    # Если в тексте "доставка бесплатно" — ставим 0
    if delivery_cost_match and "бесплатно" in delivery_cost_match.group(0).lower():
        delivery_cost = 0.0

    # Срок доставки
    delivery_days_match = DELIVERY_DAYS_REGEX.search(content)
    delivery_days = int(delivery_days_match.group(1)) if delivery_days_match else None

    # Если срок не указан — ставим большой, чтобы не попадал в топ
    if delivery_days is None:
        delivery_days = 999

    return {
        'title': title,
        'price': price,
        'rating': rating,
        'delivery_cost': delivery_cost,
        'delivery_days': delivery_days,
        'url': url
    }

def normalize_list(values: List[float]) -> List[float]:
    """Обратная нормализация: чем меньше исходное значение, тем выше результат."""
    if not values:
        return []
    min_v, max_v = min(values), max(values)
    if max_v == min_v:
        return [0.5] * len(values)
    return [(max_v - v) / (max_v - min_v) for v in values]

def generate_justification(best: Dict, products: List[Dict]) -> str:
    """Сравнивает лучший товар с остальными и возвращает понятное обоснование."""
    reasons = []
    # Цена
    prices = [p['price'] for p in products]
    if best['price'] == min(prices):
        reasons.append("✅ самая низкая цена")
    else:
        diff = best['price'] - min(prices)
        reasons.append(f"💰 цена выше на {diff:.0f} ₽ по сравнению с самым дешёвым")

    # Рейтинг
    ratings = [p['rating'] for p in products]
    if best['rating'] == max(ratings):
        reasons.append("⭐ самый высокий рейтинг")
    elif best['rating'] > 0:
        reasons.append(f"⭐ рейтинг {best['rating']:.1f} (максимум {max(ratings):.1f})")

    # Доставка
    costs = [p['delivery_cost'] for p in products]
    if best['delivery_cost'] == min(costs):
        reasons.append("🚚 самая низкая стоимость доставки")
    if best['delivery_days'] == min(p['delivery_days'] for p in products):
        reasons.append("⚡ самая быстрая доставка")

    if not reasons:
        reasons.append("сбалансированные характеристики")
    return "📌 " + ". ".join(reasons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Привет! Я помогу найти товары.\n"
        "Напиши, что хочешь найти, например:\n"
        "«игровой ноутбук москва»\n"
        "Если город не укажешь — спрошу отдельно.\n\n"
        "Команды:\n/help — справка\n/reset — начать заново"
    )
    return AWAITING_QUERY

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как я работаю:\n"
        "1. Ты вводишь запрос с товаром, например «ноутбук москва»\n"
        "2. Если город не указан — я спрошу.\n"
        "3. Ищу в интернете через Tavily API.\n"
        "4. Отбираю товары с ценой и доставкой, рассчитываю балл.\n"
        "5. Показываю ТОП-3 с объяснением, почему первый лучший."
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Диалог сброшен. Напиши /start для нового поиска.")
    return AWAITING_QUERY

async def receive_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    city = extract_city(user_text)
    if city:
        context.user_data['city'] = city
        context.user_data['query'] = user_text
        return await perform_search(update, context)
    else:
        context.user_data['pending_query'] = user_text
        await update.message.reply_text("В каком городе ищем товар? (например, Москва, СПБ)")
        return AWAITING_CITY

async def receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    context.user_data['city'] = city
    context.user_data['query'] = context.user_data.get('pending_query', '')
    return await perform_search(update, context)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = context.user_data.get('query')
    city = context.user_data.get('city')
    if not query:
        await update.message.reply_text("Произошла ошибка. Напиши /reset и попробуй снова.")
        return ConversationHandler.END

    search_query = f"{query} {city}" if city else query
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        results = client.search(search_query, max_results=10, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("Ошибка при поиске. Проверьте ключ Tavily.")
        return ConversationHandler.END

    products_raw = []
    for item in results.get('results', []):
        info = extract_product_info(item)
        if info:
            products_raw.append(info)

    if not products_raw:
        await update.message.reply_text("Не удалось найти товары с ценой и доставкой. Попробуйте другой запрос.")
        return ConversationHandler.END

    # Нормализация
    prices = [p['price'] for p in products_raw]
    ratings = [p['rating'] / 5 for p in products_raw]
    delivery_costs = [p['delivery_cost'] for p in products_raw]
    delivery_days = [p['delivery_days'] for p in products_raw]

    norm_prices = normalize_list(prices)
    norm_ratings = normalize_list(ratings)
    norm_costs = normalize_list(delivery_costs)
    norm_days = normalize_list(delivery_days)

    for i, prod in enumerate(products_raw):
        score = (0.4 * norm_prices[i] +
                 0.3 * norm_ratings[i] +
                 0.15 * norm_costs[i] +
                 0.15 * norm_days[i])
        prod['score'] = score

    products_raw.sort(key=lambda x: x['score'], reverse=True)
    top3 = products_raw[:3]

    answer = f"🔍 **Результаты для «{query}»** (город: {city if city else 'не указан'})\n\n"
    for idx, prod in enumerate(top3, 1):
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else "🥉")
        answer += f"{medal} **{prod['title'][:60]}**\n"
        answer += f"💰 Цена: {prod['price']:.0f} ₽\n"
        answer += f"⭐ Рейтинг: {prod['rating']:.1f}/5\n"
        answer += f"🚚 Доставка: {prod['delivery_cost']:.0f} ₽, {prod['delivery_days']} дн.\n"
        answer += f"📊 Итоговый балл: {prod['score']:.2f}\n"
        answer += f"[Ссылка]({prod['url']})\n\n"

    if top3:
        best = top3[0]
        justification = generate_justification(best, products_raw)
        answer += f"💡 **Почему первое место?**\n{justification}\n\n"
        answer += "ℹ️ Учтите: доставка и цена могут быть указаны не точно — проверяйте на сайте."

    await update.message.reply_text(answer, parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('reset', reset)],
        states={
            AWAITING_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_query)],
            AWAITING_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city)],
        },
        fallbacks=[CommandHandler('reset', reset)],
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('help', help_command))
    print("Бот запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()

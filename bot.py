import re
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ReplyKeyboardRemove
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

# Список городов (можно расширять)
CITIES = {
    "москва", "мск", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар",
    "киров", "тула", "ярославль", "иркутск", "хабаровск", "владивосток"
}

# Белый список доменов магазинов (только их будем показывать)
ALLOWED_DOMAINS = [
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "citilink.ru", "dns-shop.ru",
    "mvideo.ru", "eldorado.ru", "technopark.ru", "onlinetrade.ru", "regard.ru",
    "xcom-shop.ru", "computeruniverse.ru", "holodilnik.ru", "e96.ru", "shop.mts.ru",
    "beeline.ru", "megafon.ru", "tele2.ru", "avito.ru"  # если всё же хочешь Avito, оставь, но по желанию можно убрать
]

def is_allowed_url(url: str) -> bool:
    """Проверяет, принадлежит ли URL разрешённому домену магазина."""
    for domain in ALLOWED_DOMAINS:
        if domain in url.lower():
            return True
    return False

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

def is_valid_query(text: str) -> bool:
    if len(text.strip()) < 2:
        return False
    # Простейшая проверка на мат
    bad_words = ['хер', 'хуй', 'пизд', 'ебан', 'fuck', 'shit']
    for bw in bad_words:
        if bw in text.lower():
            return False
    return True

def parse_price(s: str) -> float:
    """Парсит цену из строки, убирая пробелы и заменяя запятую на точку."""
    s = s.replace(' ', '').replace(',', '.')
    try:
        price = float(s)
        # Отбрасываем явно нереалистичные цены (меньше 500 руб. для ПК-комплектующих/мониторов)
        if price < 500:
            return 0.0
        return price
    except:
        return 0.0

def extract_product_info(snippet: Dict[str, Any]) -> Optional[Dict]:
    """Извлекает информацию о товаре, только если URL из разрешённых доменов."""
    url = snippet.get('url', '')
    if not is_allowed_url(url):
        return None

    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None

    title = snippet.get('title', '')

    # Ищем цену: числа рядом с ₽, руб, RUB, р (не в новостях)
    price_match = re.search(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|р|RUB)', content, re.IGNORECASE)
    if not price_match:
        return None
    price = parse_price(price_match.group(1))
    if price == 0:
        return None

    # Рейтинг (необязательно, но попробуем найти)
    rating_match = re.search(r'(\d(?:[.,]\d)?)\s*[/]?\s*5', content)
    rating = float(rating_match.group(1).replace(',', '.')) if rating_match else 0.0
    if rating > 5:
        rating = 5.0

    # Доставка: ищем "доставка" и число или слово "бесплатно"
    delivery_cost = 0.0
    delivery_days = 0
    delivery_text = re.search(r'доставк[ае][:]*\s*([\d\s.,]+)\s*(?:₽|руб|р)?', content, re.IGNORECASE)
    if delivery_text:
        cost_str = re.sub(r'[^\d.,]', '', delivery_text.group(1))
        if cost_str:
            delivery_cost = parse_price(cost_str)
    # Если в тексте есть "бесплатно"
    if 'бесплатно' in content.lower():
        delivery_cost = 0.0

    days_match = re.search(r'(\d{1,2})\s*(?:дн|день|дня|дней)', content, re.IGNORECASE)
    if days_match:
        delivery_days = int(days_match.group(1))
    else:
        delivery_days = 5  # значение по умолчанию, если не указано

    return {
        'title': title,
        'price': price,
        'rating': rating,
        'delivery_cost': delivery_cost,
        'delivery_days': delivery_days,
        'url': url
    }

def normalize_list(values: List[float]) -> List[float]:
    """Обратная нормализация: чем меньше, тем лучше."""
    if not values:
        return []
    min_v, max_v = min(values), max(values)
    if max_v == min_v:
        return [0.5] * len(values)
    return [(max_v - v) / (max_v - min_v) for v in values]

def generate_justification(best: Dict, products: List[Dict]) -> str:
    reasons = []
    prices = [p['price'] for p in products]
    if best['price'] == min(prices):
        reasons.append("✅ самая низкая цена")
    else:
        diff = best['price'] - min(prices)
        reasons.append(f"💰 цена всего на {diff:.0f} ₽ выше минимальной")

    ratings = [p['rating'] for p in products]
    if best['rating'] == max(ratings) and best['rating'] > 0:
        reasons.append(f"⭐ наивысший рейтинг ({best['rating']:.1f})")

    costs = [p['delivery_cost'] for p in products]
    if best['delivery_cost'] == min(costs):
        reasons.append("🚚 самая дешёвая доставка")
    days = [p['delivery_days'] for p in products]
    if best['delivery_days'] == min(days):
        reasons.append("⚡ самая быстрая доставка")

    if not reasons:
        reasons.append("сбалансированные характеристики")
    return "📌 " + ". ".join(reasons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 **Помощник по поиску товаров для ПК**\n\n"
        "Я ищу только в проверенных магазинах России:\n"
        "Ozon, Wildberries, Яндекс.Маркет, DNS, Citilink, M.Video и другие.\n\n"
        "**Как использовать:**\n"
        "• Просто напиши, что хочешь найти (например, `видеокарта москва`)\n"
        "• Если город не укажешь — я спрошу отдельно.\n"
        "• Я покажу ТОП-3 товара с ценами, рейтингом и доставкой.\n"
        "• После ответа можешь отправить новый запрос — и я снова помогу.\n\n"
        "Команды:\n"
        "/help — подробная справка\n"
        "/reset — начать заново\n"
        "/cancel — отменить текущий поиск",
        parse_mode='Markdown'
    )
    return AWAITING_QUERY

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **Как я работаю**\n"
        "1. Вы вводите запрос, например `монитор 240hz москва`\n"
        "2. Если город не указан — я спрошу.\n"
        "3. Ищу товары на сайтах-магазинах (см. список выше).\n"
        "4. Извлекаю цену, рейтинг, стоимость и срок доставки.\n"
        "5. Рассчитываю интегральный балл (низкая цена, высокий рейтинг, быстрая и дешёвая доставка).\n"
        "6. Показываю ТОП-3 с понятным обоснованием.\n\n"
        "Просто напишите новый запрос после ответа."
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔄 Диалог сброшен. Напиши /start для начала нового поиска.")
    return AWAITING_QUERY

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Поиск отменён. Чтобы начать заново, напиши /start.")
    return ConversationHandler.END

async def receive_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    city = extract_city(user_text)
    if city:
        context.user_data['city'] = city
        context.user_data['query'] = user_text
        return await perform_search(update, context)
    else:
        context.user_data['pending_query'] = user_text
        await update.message.reply_text("🌆 В каком городе ищем товар? (например, Москва, СПБ, Казань)")
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
        await update.message.reply_text("Ошибка. Напиши /start заново.")
        return ConversationHandler.END

    # Формируем поисковый запрос: добавляем город, а также ограничение по доменам через site:
    site_query = ' OR '.join([f'site:{domain}' for domain in ALLOWED_DOMAINS[:5]])  # Tavily не любит очень длинные, возьмём топ-5
    search_query = f"{query} {city} ({site_query})" if city else f"{query} ({site_query})"

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        results = client.search(search_query, max_results=15, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("Ошибка при поиске. Попробуйте позже.")
        return ConversationHandler.END

    products_raw = []
    for item in results.get('results', []):
        info = extract_product_info(item)
        if info:
            products_raw.append(info)

    if not products_raw:
        await update.message.reply_text("Не найдено товаров в магазинах. Попробуйте изменить запрос или город.")
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

    answer = f"🔍 **Результаты для «{query}»** (город: {city})\n\n"
    for idx, prod in enumerate(top3, 1):
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else "🥉")
        answer += f"{medal} **{prod['title'][:60]}**\n"
        answer += f"💰 Цена: {prod['price']:.0f} ₽\n"
        answer += f"⭐ Рейтинг: {prod['rating']:.1f}/5\n"
        answer += f"🚚 Доставка: {prod['delivery_cost']:.0f} ₽, {prod['delivery_days']} дн.\n"
        answer += f"📊 Балл: {prod['score']:.2f}\n"
        answer += f"[Ссылка]({prod['url']})\n\n"

    if top3:
        justification = generate_justification(top3[0], products_raw)
        answer += f"💡 **Почему первое место?**\n{justification}\n\n"
        answer += "ℹ️ Цены и доставка могут быть примерными. Для точной информации перейдите по ссылке."

    await update.message.reply_text(answer, parse_mode='Markdown')
    # После ответа не завершаем диалог, а возвращаемся к AWAITING_QUERY
    context.user_data.clear()
    await update.message.reply_text("✅ Поиск завершён. Если хотите найти ещё что-то, просто напишите запрос (или /start для начала).")
    return AWAITING_QUERY

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            AWAITING_QUERY: [
                CommandHandler('help', help_command),
                CommandHandler('reset', reset),
                CommandHandler('cancel', cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_query)
            ],
            AWAITING_CITY: [
                CommandHandler('cancel', cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city)
            ],
        },
        fallbacks=[CommandHandler('reset', reset)],
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('reset', reset))
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == '__main__':
    main()

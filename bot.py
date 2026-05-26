import re
import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ChatAction
from telegram.ext import (
    ApplicationBuilder,
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

MIN_PRICE = 2000

CITIES = {
    "москва", "мск", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар"
}

ALLOWED_DOMAINS = [
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "citilink.ru", "dns-shop.ru",
    "mvideo.ru", "eldorado.ru", "technopark.ru", "onlinetrade.ru", "regard.ru",
    "xcom-shop.ru", "computeruniverse.ru", "pcshop.ru", "compyou.ru"
]

def is_allowed_url(url: str) -> bool:
    return any(domain in url.lower() for domain in ALLOWED_DOMAINS)

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

def extract_product_query(text: str, city: str = None) -> str:
    """Оставляет только ключевые слова товара (модель), убирая город и лишние слова."""
    query = text
    if city:
        query = re.sub(r'\b' + re.escape(city) + r'\b', '', query, flags=re.IGNORECASE)
    # Удаляем типичные слова-паразиты
    query = re.sub(r'\b(в|во|на|для|купить|цена|с доставкой|материнская плата|материнку|материнка|процессор|видеокарта|оперативная память|ssd|блок питания)\b', '', query, flags=re.IGNORECASE)
    query = re.sub(r'\s+', ' ', query).strip()
    return query if query else text

def parse_price(s: str) -> float:
    s = s.replace(' ', '').replace(',', '.')
    try:
        price = float(s)
        return price if price >= MIN_PRICE else 0.0
    except:
        return 0.0

def extract_product_info(snippet: Dict[str, Any]) -> Optional[Dict]:
    url = snippet.get('url', '')
    if not is_allowed_url(url):
        return None
    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None
    title = snippet.get('title', '')

    price_match = re.search(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|р|RUB)', content, re.IGNORECASE)
    if not price_match:
        return None
    price = parse_price(price_match.group(1))
    if price == 0:
        return None

    rating_match = re.search(r'(\d(?:[.,]\d)?)\s*[/]?\s*5', content)
    rating = float(rating_match.group(1).replace(',', '.')) if rating_match else 0.0
    rating = min(rating, 5.0)

    delivery_cost = 0.0
    delivery_text = re.search(r'доставк[ае][:]*\s*([\d\s.,]+)\s*(?:₽|руб|р)?', content, re.IGNORECASE)
    if delivery_text:
        cost_str = re.sub(r'[^\d.,]', '', delivery_text.group(1))
        if cost_str:
            delivery_cost = parse_price(cost_str)
    if 'бесплатно' in content.lower():
        delivery_cost = 0.0

    days_match = re.search(r'(\d{1,2})\s*(?:дн|день|дня|дней)', content, re.IGNORECASE)
    delivery_days = int(days_match.group(1)) if days_match else 5

    return {
        'title': title,
        'price': price,
        'rating': rating,
        'delivery_cost': delivery_cost,
        'delivery_days': delivery_days,
        'url': url
    }

def normalize_list(values):
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    return [(mx - v) / (mx - mn) for v in values]

def generate_justification(best, products):
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    state = context.user_data.get('state', '')
    pending_query = context.user_data.get('pending_query', '')

    if state == 'awaiting_city':
        city = extract_city(user_text) or user_text
        context.user_data['city'] = city
        query = pending_query
        context.user_data.clear()
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text("🔍 Ищу товары...")
        await perform_search(update, context, query, city)
        return

    city = extract_city(user_text)
    if city:
        context.user_data.clear()
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text("🔍 Ищу товары...")
        await perform_search(update, context, user_text, city)
    else:
        context.user_data['pending_query'] = user_text
        context.user_data['state'] = 'awaiting_city'
        await update.message.reply_text("🌆 В каком городе ищем? Напиши название (Москва, Казань и т.д.)")

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, full_query: str, city: str):
    # Очищаем запрос от лишних слов, оставляем модель товара
    product_query = extract_product_query(full_query, city)
    search_query = f"{product_query} {city} купить"
    logger.info(f"Search query: {search_query}")

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        site_query = ' OR '.join([f'site:{d}' for d in ALLOWED_DOMAINS[:6]])
        results = client.search(f"{search_query} ({site_query})", max_results=20, search_depth="advanced")
        if len(results.get('results', [])) < 3:
            results = client.search(search_query, max_results=20, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("❌ Ошибка при поиске. Попробуй позже.")
        return

    products = []
    for item in results.get('results', []):
        info = extract_product_info(item)
        if info:
            products.append(info)

    if not products:
        await update.message.reply_text("😕 Не нашёл товары в магазинах. Попробуй изменить запрос или город.")
        return

    # Нормализация и подсчёт баллов
    prices = [p['price'] for p in products]
    ratings = [p['rating']/5 for p in products]
    costs = [p['delivery_cost'] for p in products]
    days = [p['delivery_days'] for p in products]

    norm_prices = normalize_list(prices)
    norm_ratings = normalize_list(ratings)
    norm_costs = normalize_list(costs)
    norm_days = normalize_list(days)

    for i, p in enumerate(products):
        p['score'] = 0.4*norm_prices[i] + 0.3*norm_ratings[i] + 0.15*norm_costs[i] + 0.15*norm_days[i]

    products.sort(key=lambda x: x['score'], reverse=True)
    top3 = products[:3]

    answer = f"🔍 **Результаты для «{full_query}»** (город: {city})\n\n"
    for idx, p in enumerate(top3, 1):
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else "🥉")
        answer += f"{medal} **{p['title'][:60]}**\n"
        answer += f"💰 Цена: {p['price']:.0f} ₽\n"
        answer += f"⭐ Рейтинг: {p['rating']:.1f}/5\n"
        answer += f"🚚 Доставка: {p['delivery_cost']:.0f} ₽, {p['delivery_days']} дн.\n"
        answer += f"📊 Балл: {p['score']:.2f}\n"
        answer += f"[Ссылка]({p['url']})\n\n"

    if top3:
        justification = generate_justification(top3[0], products)
        answer += f"💡 **Почему первое место?**\n{justification}\n\n"
        answer += "ℹ️ Цены и доставка примерные. Уточняйте на сайте."

    await update.message.reply_text(answer, parse_mode='Markdown')
    await update.message.reply_text("✅ Готово! Напиши новый запрос, например: `видеокарта москва`")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()

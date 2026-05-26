import re
import logging
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, constants
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Минимальные реальные цены
GPU_MIN_PRICE = 15000
CPU_MIN_PRICE = 5000
DEFAULT_MIN_PRICE = 2000

CITIES = {
    "москва", "мск", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар"
}

ALLOWED_DOMAINS = [
    "dns-shop.ru", "citilink.ru", "mvideo.ru", "eldorado.ru", "technopark.ru",
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "megamarket.ru", "regard.ru",
    "onlinetrade.ru", "xcom-shop.ru", "pcshop.ru", "compyou.ru", "nix.ru"
]

# Паттерны товарных URL
PRODUCT_URL_PATTERNS = [
    r'/product/', r'/item/', r'/p/', r'/goods/', r'/card/', r'/offer/',
    r'/show/', r'/catalog/.*/product/', r'/tovar/', r'/model/', r'/sku/',
    r'/\d+\.html', r'/\d+/', r'/dp/', r'/gp/'
]

def is_product_url(url: str) -> bool:
    url_lower = url.lower()
    for pattern in PRODUCT_URL_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    return False

def is_allowed_url(url: str) -> bool:
    return is_product_url(url) and any(domain in url.lower() for domain in ALLOWED_DOMAINS)

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

def extract_keywords(text: str, city: str = None) -> str:
    query = text.lower()
    if city:
        query = re.sub(r'\b' + re.escape(city.lower()) + r'\b', '', query)
    # удаляем стоп-слова
    stop_words = ["материнская плата", "мат плата", "материнка", "купить", "цена", "доставка", "в", "на", "для", "с", "и", "за"]
    for w in stop_words:
        query = re.sub(r'\b' + re.escape(w) + r'\b', '', query, flags=re.IGNORECASE)
    query = re.sub(r'[^\w\s]', '', query)
    query = re.sub(r'\s+', ' ', query).strip()
    return query if query else text.lower()

def detect_product_type(query: str) -> str:
    q = query.lower()
    if 'видеокарт' in q or 'rtx' in q or 'gpu' in q:
        return 'gpu'
    if 'процессор' in q or 'cpu' in q:
        return 'cpu'
    return 'other'

def parse_price(price_str: str, product_type: str) -> float:
    price_str = price_str.replace(' ', '').replace(',', '.')
    try:
        price = float(price_str)
        if price <= 0:
            return 0.0
        if product_type == 'gpu' and price < GPU_MIN_PRICE:
            return 0.0
        if product_type == 'cpu' and price < CPU_MIN_PRICE:
            return 0.0
        if product_type == 'other' and price < DEFAULT_MIN_PRICE:
            return 0.0
        return price
    except:
        return 0.0

def extract_availability(content: str) -> str:
    c = content.lower()
    if any(phrase in c for phrase in ['нет в наличии', 'ожидается', 'под заказ', 'распродано', 'заканчивается', 'предзаказ']):
        return "❌ Нет в наличии"
    if 'в наличии' in c:
        return "✅ В наличии"
    return "❔ Наличие не указано"

def is_relevant(title: str, keywords: str) -> bool:
    if not keywords:
        return True
    title_lower = title.lower()
    parts = keywords.split()
    if len(parts) <= 2:
        return any(p in title_lower for p in parts)
    else:
        matched = sum(1 for p in parts if p in title_lower)
        return matched >= len(parts) // 2

def extract_product_info(snippet: Dict[str, Any], keywords: str, product_type: str) -> Optional[Dict]:
    url = snippet.get('url', '')
    if not is_allowed_url(url):
        return None

    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None

    title = snippet.get('title', '')
    if keywords and not is_relevant(title, keywords):
        return None

    # Усиленный поиск цены: ищем любую цифру с валютой
    price_match = re.search(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|р|RUB)', content, re.IGNORECASE)
    price = 0.0
    price_str = "❓ Цена не указана"
    if price_match:
        price = parse_price(price_match.group(1), product_type)
        if price > 0:
            price_str = f"{price:.0f} ₽"

    # Рейтинг (если есть)
    rating_str = "нет рейтинга"
    rating_match = re.search(r'(\d(?:[.,]\d)?)\s*[/]?\s*5', content)
    if rating_match:
        rating = float(rating_match.group(1).replace(',', '.'))
        rating = min(rating, 5.0)
        if rating > 0:
            rating_str = f"{rating:.1f}/5"

    # Доставка (стоимость и срок)
    delivery_cost_str = "❓ не указана"
    delivery_days_str = "❓ не указан"
    deliv_cost_match = re.search(r'доставк[ае][:]*\s*(\d[\d\s.,]*)\s*(?:₽|руб|р)?', content, re.IGNORECASE)
    if deliv_cost_match:
        cost_clean = re.sub(r'[^\d.,]', '', deliv_cost_match.group(1))
        if cost_clean:
            cost = parse_price(cost_clean, 'other')
            if cost > 0:
                delivery_cost_str = f"{cost:.0f} ₽"
            elif cost == 0:
                delivery_cost_str = "бесплатно"
    if 'бесплатно' in content.lower() and delivery_cost_str == "❓ не указана":
        delivery_cost_str = "бесплатно"

    days_match = re.search(r'(\d{1,2})\s*(?:дн|день|дня|дней)', content, re.IGNORECASE)
    if days_match:
        delivery_days_str = f"{int(days_match.group(1))} дн."

    availability = extract_availability(content)

    return {
        'title': title,
        'price': price,
        'price_str': price_str,
        'rating_str': rating_str,
        'delivery_cost_str': delivery_cost_str,
        'delivery_days_str': delivery_days_str,
        'availability': availability,
        'url': url,
        'score': 0.0
    }

def normalize_list(values: List[float]) -> List[float]:
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    return [(mx - v) / (mx - mn) for v in values]

def generate_justification(best: Dict, all_products: List[Dict]) -> str:
    reasons = []
    priced = [p for p in all_products if p['price'] > 0]
    if priced and best['price'] > 0:
        prices = [p['price'] for p in priced]
        if best['price'] == min(prices):
            reasons.append("✅ самая низкая цена")
        else:
            diff = best['price'] - min(prices)
            reasons.append(f"💰 цена всего на {diff:.0f} ₽ выше минимальной")
    if best['availability'] == "✅ В наличии":
        reasons.append("📦 есть в наличии")
    if "бесплатно" in best['delivery_cost_str']:
        reasons.append("🚚 бесплатная доставка")
    if best['rating_str'] != "нет рейтинга":
        reasons.append(f"⭐ рейтинг {best['rating_str']}")
    if not reasons:
        reasons.append("сбалансированные характеристики")
    return "📌 " + ". ".join(reasons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 **Помощник по поиску комплектующих**\n\n"
        "Напиши запрос с городом, например:\n"
        "`rtx 3060 москва`\n"
        "`материнская плата asus prime z370 казань`\n\n"
        "Если город не укажешь — спрошу отдельно.\n"
        "Я покажу карточки товаров с реальных магазинов.\n\n"
        "Команды: /help, /start",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **Как я работаю**\n"
        "1. Отправь запрос: `модель город`\n"
        "2. Я ищу страницы товаров на DNS, Citilink, Ozon и других.\n"
        "3. Извлекаю цену, рейтинг, доставку и наличие из короткого описания.\n"
        "4. Если данных нет — пишу «не указано».\n"
        "5. Показываю ТОП-3, где первый товар — с обоснованием.\n\n"
        "Просто отправь новый запрос после ответа."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    state = context.user_data.get('state', '')
    pending_query = context.user_data.get('pending_query', '')

    if state == 'awaiting_city':
        city = extract_city(user_text) or user_text
        context.user_data['city'] = city
        query = pending_query
        context.user_data.clear()
        await update.message.chat.send_action(constants.ChatAction.TYPING)
        await update.message.reply_text("🔍 Ищу товары...")
        await perform_search(update, context, query, city)
        return

    city = extract_city(user_text)
    if city:
        context.user_data.clear()
        await update.message.chat.send_action(constants.ChatAction.TYPING)
        await update.message.reply_text("🔍 Ищу товары...")
        await perform_search(update, context, user_text, city)
    else:
        context.user_data['pending_query'] = user_text
        context.user_data['state'] = 'awaiting_city'
        await update.message.reply_text("🌆 В каком городе ищем? Напиши название (Москва, Казань и т.д.)")

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, full_query: str, city: str):
    keywords = extract_keywords(full_query, city)
    product_type = detect_product_type(full_query)
    search_query = f"{keywords} {city} купить" if keywords else f"{full_query} {city} купить"
    logger.info(f"Search: {search_query} | Keywords: {keywords}")

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        site_query = ' OR '.join([f'site:{d}' for d in ALLOWED_DOMAINS[:10]])
        results = client.search(f"{search_query} ({site_query})", max_results=30, search_depth="advanced")
        if len(results.get('results', [])) < 3:
            results = client.search(search_query, max_results=30, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("❌ Ошибка при поиске. Попробуйте позже.")
        return

    products = []
    for item in results.get('results', []):
        info = extract_product_info(item, keywords, product_type)
        if info:
            products.append(info)

    if not products:
        await update.message.reply_text("😕 Не удалось найти подходящие товары. Попробуйте изменить запрос или город.")
        return

    # Нормализация цен (только если есть цена)
    prices = [p['price'] for p in products if p['price'] > 0]
    norm_prices = normalize_list(prices) if prices else []
    price_idx = 0
    for p in products:
        if p['price'] > 0:
            p['norm_price'] = norm_prices[price_idx]
            price_idx += 1
        else:
            p['norm_price'] = 0.0
        availability_bonus = 0.3 if p['availability'] == "✅ В наличии" else 0.0
        p['score'] = p['norm_price'] + availability_bonus

    products.sort(key=lambda x: x['score'], reverse=True)
    top3 = products[:3]

    answer = f"🔍 **Результаты для «{full_query}»** (город: {city})\n\n"
    for idx, p in enumerate(top3, 1):
        if idx == 1:
            answer += f"🥇 **{p['title'][:80]}**\n"
            answer += f"💰 Цена: {p['price_str']}\n"
            answer += f"⭐ Рейтинг: {p['rating_str']}\n"
            answer += f"🚚 Доставка: {p['delivery_cost_str']}, {p['delivery_days_str']}\n"
            answer += f"📦 Наличие: {p['availability']}\n"
            answer += f"[Ссылка]({p['url']})\n\n"
            justification = generate_justification(p, products)
            answer += f"💡 **Почему первое место?**\n{justification}\n\n"
        else:
            medal = "🥈" if idx == 2 else "🥉"
            answer += f"{medal} **{p['title'][:60]}** — Цена: {p['price_str']}, Доставка: {p['delivery_cost_str']} / {p['delivery_days_str']}, Наличие: {p['availability']}\n[Ссылка]({p['url']})\n\n"

    answer += "ℹ️ Информация о ценах и доставке может быть примерной. Уточняйте на сайте."
    await update.message.reply_text(answer, parse_mode='Markdown')
    await update.message.reply_text("✅ Готово! Отправьте новый запрос.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()

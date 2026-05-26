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

MIN_PRICE = 500  # минимальная цена для фильтрации совсем мусора

# Список городов
CITIES = {
    "москва", "мск", "санкт-петербург", "спб", "новосибирск", "екатеринбург",
    "казань", "нижний новгород", "челябинск", "омск", "самара", "ростов-на-дону",
    "уфа", "красноярск", "пермь", "воронеж", "волгоград", "краснодар"
}

# Белый список доменов магазинов (можно расширять)
ALLOWED_DOMAINS = [
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "citilink.ru", "dns-shop.ru",
    "mvideo.ru", "eldorado.ru", "technopark.ru", "onlinetrade.ru", "regard.ru",
    "xcom-shop.ru", "computeruniverse.ru", "pcshop.ru", "compyou.ru",
    "megamarket.ru", "holodilnik.ru", "re-store.ru", "nix.ru", "knsneva.ru",
    "avito.ru", "goods.ru", "tehnosila.ru", "electrozone.ru", "ultracomp.ru"
]

# Слова-паразиты, которые удаляем при извлечении модели
STOP_WORDS = [
    "материнская плата", "мат плата", "материнка", "мать", "motherboard",
    "купить", "цена", "стоимость", "доставка", "в", "на", "для", "с", "и", "за",
    "процессор", "видеокарта", "оперативная память", "ssd", "блок питания",
    "материнскую", "плату", "лучший", "топ", "дешево"
]

def extract_keywords(text: str, city: str = None) -> str:
    """Извлекает ключевые слова товара (модель), убирая город и стоп-слова."""
    query = text.lower()
    if city:
        query = re.sub(r'\b' + re.escape(city.lower()) + r'\b', '', query)
    for word in STOP_WORDS:
        query = re.sub(r'\b' + re.escape(word) + r'\b', '', query, flags=re.IGNORECASE)
    # Убираем лишние символы и пробелы
    query = re.sub(r'[^\w\s]', '', query)
    query = re.sub(r'\s+', ' ', query).strip()
    # Если после чистки осталось пусто, возвращаем исходный запрос (хотя бы что-то)
    return query if query else text.lower()

def is_relevant(product_title: str, keywords: str) -> bool:
    """Проверяет, содержит ли название товара основные ключевые слова модели."""
    if not keywords:
        return True
    title_lower = product_title.lower()
    keyword_parts = keywords.split()
    # Для коротких запросов (1-2 слова) достаточно одного совпадения
    if len(keyword_parts) <= 2:
        return any(part in title_lower for part in keyword_parts)
    else:
        # Для длинных – хотя бы половина слов должна быть
        matched = sum(1 for part in keyword_parts if part in title_lower)
        return matched >= len(keyword_parts) // 2

def is_allowed_url(url: str) -> bool:
    return any(domain in url.lower() for domain in ALLOWED_DOMAINS)

def extract_city(text: str) -> Optional[str]:
    text_lower = text.lower()
    for city in CITIES:
        if city in text_lower:
            return city
    return None

def parse_price(s: str) -> float:
    s = s.replace(' ', '').replace(',', '.')
    try:
        price = float(s)
        if price < MIN_PRICE:
            return 0.0
        return price
    except:
        return 0.0

def extract_availability(content: str) -> str:
    """Определяет статус наличия товара."""
    content_lower = content.lower()
    if "нет в наличии" in content_lower or "ожидается" in content_lower or "под заказ" in content_lower:
        return "❌ Нет в наличии"
    if "в наличии" in content_lower:
        return "✅ В наличии"
    return "❔ Наличие не указано"

def extract_product_info(snippet: Dict[str, Any], keywords: str) -> Optional[Dict]:
    url = snippet.get('url', '')
    if not is_allowed_url(url):
        return None

    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None

    title = snippet.get('title', '')

    # Фильтрация по ключевым словам модели
    if keywords and not is_relevant(title, keywords):
        return None

    # Цена
    price_match = re.search(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|р|RUB)', content, re.IGNORECASE)
    price = parse_price(price_match.group(1)) if price_match else 0.0
    # Если цена не найдена – все равно показываем, но с пометкой
    price_str = f"{price:.0f} ₽" if price > 0 else "❓ Цена не указана"

    # Рейтинг (если есть)
    rating_match = re.search(r'(\d(?:[.,]\d)?)\s*[/]?\s*5', content)
    rating = float(rating_match.group(1).replace(',', '.')) if rating_match else 0.0
    rating = min(rating, 5.0)
    rating_str = f"{rating:.1f}/5" if rating > 0 else "нет рейтинга"

    # Доставка
    delivery_cost = 0.0
    delivery_cost_str = "❓ не указана"
    delivery_days_str = "❓ не указан"
    delivery_text = re.search(r'доставк[ае][:]*\s*([\d\s.,]+)\s*(?:₽|руб|р)?', content, re.IGNORECASE)
    if delivery_text:
        cost_str = re.sub(r'[^\d.,]', '', delivery_text.group(1))
        if cost_str:
            delivery_cost = parse_price(cost_str)
            if delivery_cost > 0:
                delivery_cost_str = f"{delivery_cost:.0f} ₽"
            elif delivery_cost == 0:
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
        'score': 0.0  # будет заполнено позже
    }

def normalize_list(values: List[float]) -> List[float]:
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    return [(mx - v) / (mx - mn) for v in values]

def generate_justification(best: Dict, products: List[Dict]) -> str:
    reasons = []
    # Сравниваем только те товары, у которых есть цена
    priced_products = [p for p in products if p['price'] > 0]
    if priced_products and best.get('price', 0) > 0:
        prices = [p['price'] for p in priced_products]
        if best['price'] == min(prices):
            reasons.append("✅ самая низкая цена")
        else:
            diff = best['price'] - min(prices)
            reasons.append(f"💰 цена всего на {diff:.0f} ₽ выше минимальной")
    if best.get('availability') == "✅ В наличии":
        reasons.append("📦 есть в наличии")
    if "бесплатно" in best.get('delivery_cost_str', ''):
        reasons.append("🚚 бесплатная доставка")
    # про рейтинг добавим только если он есть
    if best.get('rating_str') and best['rating_str'] != "нет рейтинга":
        reasons.append(f"⭐ рейтинг {best['rating_str']}")
    if not reasons:
        reasons.append("сбалансированные характеристики")
    return "📌 " + ". ".join(reasons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 **Помощник по поиску комплектующих**\n\n"
        "Я ищу процессоры, видеокарты, материнские платы и другие компоненты.\n"
        "Просто напиши запрос, например:\n"
        "`материнская плата asus prime z370 казань`\n"
        "`rtx 3060 москва`\n\n"
        "Если город не укажешь — спрошу отдельно.\n"
        "Показываю ТОП-3 с ценами, наличием, доставкой и ссылками.\n\n"
        "Команды: /help, /start",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **Как я работаю**\n"
        "1. Ты отправляешь запрос: `модель город`\n"
        "2. Я ищу на популярных маркетплейсах РФ.\n"
        "3. Показываю реальное наличие, цену, стоимость и срок доставки.\n"
        "4. Первый товар – с подробным обоснованием, остальные – кратко со ссылками.\n"
        "5. Если какой-то информации нет – пишу «не указано».\n\n"
        "Просто отправь новый запрос, чтобы продолжить."
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
    # Извлекаем ключевые слова модели
    keywords = extract_keywords(full_query, city)
    search_query = f"{keywords} {city} купить" if keywords else f"{full_query} {city} купить"
    logger.info(f"Search query: {search_query} | Keywords: {keywords}")

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        # Сначала пробуем искать с ограничением по магазинам
        site_query = ' OR '.join([f'site:{d}' for d in ALLOWED_DOMAINS[:10]])
        results = client.search(f"{search_query} ({site_query})", max_results=30, search_depth="advanced")
        if len(results.get('results', [])) < 3:
            # Если мало результатов, ищем без site:
            results = client.search(search_query, max_results=30, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("❌ Ошибка при поиске. Попробуйте позже.")
        return

    # Извлекаем информацию о товарах
    products = []
    for item in results.get('results', []):
        info = extract_product_info(item, keywords)
        if info:
            products.append(info)

    if not products:
        await update.message.reply_text("😕 Не удалось найти товары по вашему запросу. Попробуйте изменить модель или город.")
        return

    # Сортировка по баллу (учитываем только цену и наличие)
    for p in products:
        # Балл: учитываем цену (чем ниже, тем лучше) и наличие (в наличии + балл)
        price_norm = 0.0
        if p['price'] > 0:
            # временно для сортировки – позже пересчитаем нормализованно
            p['temp_price'] = p['price']
        else:
            p['temp_price'] = 1e9  # большая цена, чтобы уходил вниз
    # Нормализуем цены среди тех, у кого цена >0
    prices = [p['price'] for p in products if p['price'] > 0]
    norm_prices = normalize_list(prices) if prices else []
    price_idx = 0
    for p in products:
        if p['price'] > 0:
            p['norm_price'] = norm_prices[price_idx]
            price_idx += 1
        else:
            p['norm_price'] = 0.0
        # Бонус за наличие
        availability_bonus = 0.3 if p['availability'] == "✅ В наличии" else 0.0
        p['score'] = p['norm_price'] + availability_bonus

    products.sort(key=lambda x: x['score'], reverse=True)
    top3 = products[:3]

    # Формируем ответ
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

    answer += "ℹ️ Информация о ценах и доставке может быть примерной. Уточняйте на сайте магазина."
    await update.message.reply_text(answer, parse_mode='Markdown')
    await update.message.reply_text("✅ Готово! Отправьте новый запрос.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен и готов к работе.")
    app.run_polling()

if __name__ == '__main__':
    main()

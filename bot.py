import re
import logging
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# Только нужные магазины
ALLOWED_DOMAINS = ["dns-shop.ru", "citilink.ru", "mvideo.ru"]

# Паттерны товарных URL для этих магазинов
PRODUCT_URL_PATTERNS = [
    r'/product/', r'/products/', r'/catalog/product/', r'/item/',
    r'/p/', r'/goods/', r'/card/', r'/offer/',
    r'/tovar/', r'/model/', r'/sku/', r'\d+\.html', r'/dp/', r'/gp/'
]

# Состояния диалога
CITY, ACCURACY, QUERY = range(3)

# Кнопки городов
CITY_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Москва", callback_data="city_moscow"),
     InlineKeyboardButton("Казань", callback_data="city_kazan")]
])

# Кнопки точности
ACCURACY_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Точная модель", callback_data="accuracy_exact"),
     InlineKeyboardButton("💸 Подходящий товар (по описанию/цене)", callback_data="accuracy_general")]
])

# --- Вспомогательные функции (улучшенные) ---

def is_product_url(url: str) -> bool:
    url_lower = url.lower()
    for pattern in PRODUCT_URL_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    return False

def is_allowed_url(url: str) -> bool:
    return is_product_url(url) and any(domain in url.lower() for domain in ALLOWED_DOMAINS)

def extract_keywords(text: str) -> str:
    """Очистка запроса от стоп-слов для фильтрации по ключевым словам"""
    query = text.lower()
    # добавляем специфичные для техники стоп-слова
    stop_words = ["купить", "цена", "доставка", "в", "на", "для", "с", "и", "за",
                  "москва", "казань", "мск"]
    for w in stop_words:
        query = re.sub(r'\b' + re.escape(w) + r'\b', '', query)
    query = re.sub(r'[^\w\s]', '', query)
    query = re.sub(r'\s+', ' ', query).strip()
    return query if query else text.lower()

def is_relevant(title: str, keywords: str, exact_mode: bool) -> bool:
    """Проверка релевантности: в точном режиме все слова должны быть в заголовке,
       в общем – хотя бы половина."""
    if not keywords:
        return True
    title_lower = title.lower()
    parts = keywords.split()
    if exact_mode:
        # Все ключевые слова должны присутствовать (можно ослабить до большинства)
        return all(p in title_lower for p in parts)
    else:
        if len(parts) <= 2:
            return any(p in title_lower for p in parts)
        matched = sum(1 for p in parts if p in title_lower)
        return matched >= len(parts) // 2

def parse_price(price_str: str) -> float:
    """Извлечение цены и проверка на реалистичность (минимум 100 руб.)"""
    price_str = price_str.replace(' ', '').replace(',', '.')
    try:
        price = float(price_str)
        return price if price >= 100 else 0.0
    except:
        return 0.0

def extract_availability(content: str) -> str:
    c = content.lower()
    if any(phrase in c for phrase in ['нет в наличии', 'ожидается', 'под заказ', 'распродано', 'заканчивается', 'предзаказ']):
        return "❌ Нет в наличии"
    if 'в наличии' in c:
        return "✅ В наличии"
    return "❔ Наличие не указано"

def extract_product_info(snippet: Dict[str, Any], keywords: str, exact_mode: bool) -> Optional[Dict]:
    url = snippet.get('url', '')
    if not is_allowed_url(url):
        return None

    content = snippet.get('content', '') or snippet.get('raw_content', '')
    if not content:
        return None

    title = snippet.get('title', '')
    if keywords and not is_relevant(title, keywords, exact_mode):
        return None

    # Поиск цены (усиленный regex)
    price_match = re.search(r'(\d{1,3}(?:[ \d]{0,3})?(?:[.,]\d{2})?)\s*(?:₽|руб|р|RUB)', content, re.IGNORECASE)
    price = 0.0
    price_str = "❓ Цена не указана"
    if price_match:
        price = parse_price(price_match.group(1))
        if price > 0:
            price_str = f"{price:.0f} ₽"

    # Рейтинг
    rating_str = "нет рейтинга"
    rating_match = re.search(r'(\d(?:[.,]\d)?)\s*[/]?\s*5', content)
    if rating_match:
        rating = float(rating_match.group(1).replace(',', '.'))
        if 0 < rating <= 5:
            rating_str = f"{rating:.1f}/5"

    # Доставка
    delivery_cost_str = "❓ не указана"
    delivery_days_str = "❓ не указан"
    deliv_cost_match = re.search(r'доставк[ае][:]*\s*(\d[\d\s.,]*)\s*(?:₽|руб|р)?', content, re.IGNORECASE)
    if deliv_cost_match:
        cost_clean = re.sub(r'[^\d.,]', '', deliv_cost_match.group(1))
        if cost_clean:
            cost = parse_price(cost_clean)
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
        'url': url
    }

def generate_justification(best: Dict, all_products: List[Dict], mode_exact: bool) -> str:
    reasons = []
    if not best['price'] or best['price'] == 0:
        reasons.append("⚠️ цена не указана")
    else:
        if mode_exact:
            # Для точной модели важна цена, наличие, доставка
            priced = [p for p in all_products if p['price'] > 0]
            if priced:
                if best['price'] == min(p['price'] for p in priced):
                    reasons.append("✅ самая низкая цена")
                else:
                    reasons.append("💰 цена близка к минимальной")
        else:
            # Общий поиск – просто подчёркиваем, что выбрали самый дешёвый подходящий
            reasons.append("🏷️ минимальная цена среди подходящих")

    if best['availability'] == "✅ В наличии":
        reasons.append("📦 есть в наличии")
    if "бесплатно" in best['delivery_cost_str']:
        reasons.append("🚚 бесплатная доставка")
    if best['rating_str'] != "нет рейтинга":
        reasons.append(f"⭐ рейтинг {best['rating_str']}")
    if not reasons:
        reasons.append("сбалансированные характеристики")
    return "📌 " + ". ".join(reasons)

# --- Обработчики команд и диалога ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 **Поиск техники по Москве и Казани**\n\n"
        "Выберите город:",
        reply_markup=CITY_KEYBOARD,
        parse_mode='Markdown'
    )
    return CITY

async def city_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city = "москва" if query.data == "city_moscow" else "казань"
    context.user_data['city'] = city
    await query.edit_message_text(f"🏙️ Город: **{city.capitalize()}**\n\n"
                                  "Теперь уточните, что ищете:",
                                  reply_markup=ACCURACY_KEYBOARD,
                                  parse_mode='Markdown')
    return ACCURACY

async def accuracy_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    exact = query.data == "accuracy_exact"
    context.user_data['exact'] = exact
    if exact:
        text = "🔎 Введите точную модель товара (например, `Samsung Galaxy S23` или `RTX 3060`):"
    else:
        text = "💸 Опишите, что хотите найти (например, `дешёвая ТВ-приставка`, `монитор до 10000 рублей`):"
    await query.edit_message_text(text, parse_mode='Markdown')
    return QUERY

async def search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    city = context.user_data['city']
    exact = context.user_data.get('exact', False)
    await update.message.chat.send_action("typing")
    await update.message.reply_text("🔍 Ищу товары...")
    await perform_search(update, context, user_text, city, exact)
    # После завершения предлагаем начать заново
    await update.message.reply_text(
        "✅ Готово! Для нового поиска нажмите /start или просто введите любой запрос.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новый поиск", callback_data="new_search")]])
    )
    return ConversationHandler.END

async def new_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await start(update, context)
    return CITY

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, user_query: str, city: str, exact: bool):
    keywords = extract_keywords(user_query)
    # Формируем поисковый запрос
    if exact:
        search_query = f"{user_query} {city} купить"
    else:
        search_query = f"{user_query} дешево {city} купить"

    logger.info(f"Search: {search_query} | Exact: {exact}")

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        # Ищем только по трём магазинам
        site_filter = ' OR '.join([f'site:{d}' for d in ALLOWED_DOMAINS])
        results = client.search(f"{search_query} ({site_filter})", max_results=30, search_depth="advanced")
        if not results.get('results'):
            # Если совсем пусто, попробуем без фильтра
            results = client.search(search_query, max_results=30, search_depth="advanced")
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        await update.message.reply_text("❌ Ошибка при поиске. Попробуйте позже.")
        return

    products = []
    for item in results.get('results', []):
        info = extract_product_info(item, keywords, exact)
        if info:
            products.append(info)

    if not products:
        await update.message.reply_text("😕 Товары не найдены. Уточните запрос или попробуйте другую модель.")
        return

    # Сортируем: для общего поиска – по возрастанию цены, для точного – тоже, но с учётом наличия
    if exact:
        # Сначала с ценой, потом без; внутри по цене
        priced = [p for p in products if p['price'] > 0]
        unpriced = [p for p in products if p['price'] == 0]
        priced.sort(key=lambda x: x['price'])
        products = priced + unpriced
    else:
        # Общий поиск: самые дешёвые вперёд, товары без цены в конец
        products.sort(key=lambda x: (x['price'] == 0, x['price']))

    # Лучший – первый (если с ценой), иначе первый с ценой
    best = None
    for p in products:
        if p['price'] > 0:
            best = p
            break
    if not best:
        best = products[0]  # если совсем без цен

    # Формируем ответ
    answer = f"🔍 **Результаты для «{user_query}»** ({city.capitalize()})\n"
    answer += f"🎯 Режим: {'точная модель' if exact else 'подходящий товар по описанию'}\n\n"

    # Лучший вариант
    answer += f"🏆 **Лучший вариант:**\n"
    answer += f"📌 {best['title'][:80]}\n"
    answer += f"💰 Цена: {best['price_str']}\n"
    answer += f"⭐ Рейтинг: {best['rating_str']}\n"
    answer += f"🚚 Доставка: {best['delivery_cost_str']}, {best['delivery_days_str']}\n"
    answer += f"📦 Наличие: {best['availability']}\n"
    answer += f"🔗 [Ссылка]({best['url']})\n\n"
    justification = generate_justification(best, products, exact)
    answer += f"💡 {justification}\n\n"

    # Все цены (до 10)
    all_with_prices = [p for p in products if p['price'] > 0][:10]
    if all_with_prices:
        answer += "📊 **Все найденные цены (до 10):**\n"
        for i, p in enumerate(all_with_prices, 1):
            answer += f"{i}. {p['title'][:50]} — {p['price_str']} | {p['availability']}\n   {p['url']}\n"
    else:
        answer += "⚠️ Цены не найдены ни в одном товаре.\n"

    answer += "\nℹ️ Информация о ценах и доставке может быть примерной. Уточняйте на сайте магазина."
    await update.message.reply_text(answer, parse_mode='Markdown', disable_web_page_preview=True)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Поиск отменён. Для начала нового нажмите /start.")
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CITY: [CallbackQueryHandler(city_choice, pattern='^city_')],
            ACCURACY: [CallbackQueryHandler(accuracy_choice, pattern='^accuracy_')],
            QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )
    app.add_handler(conv_handler)
    # Для кнопки "новый поиск" после завершения
    app.add_handler(CallbackQueryHandler(new_search_callback, pattern='^new_search$'))
    # На случай, если кто-то напишет текст вне диалога – направим на /start
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    print("Бот запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()

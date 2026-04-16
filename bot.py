import asyncio
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BotCommand
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Загружаем .env для локальной разработки
load_dotenv()

# Добавляем путь для импортов
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.agent import process_user_request, get_stats
from utils.logger import setup_logger

logger = setup_logger("bot")

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в переменных окружения!")
    sys.exit(1)

# === Инициализация бота ===
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()


# === Команды ===

async def set_commands(bot: Bot):
    """Устанавливает список команд в меню бота."""
    commands = [
        BotCommand(command="start", description="🚀 Начать работу"),
        BotCommand(command="help", description="❓ Как пользоваться"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="about", description="ℹ️ О боте"),
    ]
    await bot.set_my_commands(commands)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Приветственное сообщение."""
    welcome_text = """
👋 **Привет! Я AI-агент для поиска выгодных покупок.**

Я умею:
🔍 Находить товары по твоему описанию
💰 Сравнивать цены в разных магазинах
🎯 Выбирать самое выгодное предложение

**Как пользоваться:**
Просто напиши мне, что хочешь найти.

*Примеры запросов:*
• `найди iphone 15 pro 256gb`
• `робот пылесос xiaomi недорого`
• `утюг philips с паром цена`
• `беспроводные наушники sony wh-1000xm5`

Я проанализирую рынок и пришлю лучшее предложение! 🚀
"""
    await message.answer(welcome_text)
    logger.info(f"Пользователь {message.from_user.id} запустил бота")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по использованию."""
    help_text = """
❓ **Как мной пользоваться**

1️⃣ Напиши, что хочешь найти
2️⃣ Я преобразую запрос в поисковую фразу
3️⃣ Найду результаты через DuckDuckGo
4️⃣ Проанализирую с помощью ИИ
5️⃣ Пришлю лучшее предложение

**Советы для лучшего результата:**
• Указывай бренд и модель
• Пиши ключевые характеристики
• Добавляй "цена" или "купить"

**Ограничения:**
• Бесплатный поиск через DuckDuckGo
• Анализ 5 первых результатов
• Время ответа: 5-10 секунд

*По вопросам и предложениям: @your_username*
"""
    await message.answer(help_text)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показывает статистику работы."""
    stats = get_stats()
    stats_text = f"""
📊 **Статистика бота**

🔧 Модель ИИ: `{stats['model']}`
🌐 Поисковая система: `{stats['search_engine']}`
✅ Статус: `{stats['status']}`

*Бот работает стабильно.*
"""
    await message.answer(stats_text)


@dp.message(Command("about"))
async def cmd_about(message: Message):
    """Информация о боте."""
    about_text = """
ℹ️ **О боте**

**AI Search Agent v1.0**

Создан как демонстрация возможностей AI-агентов для автоматизации поиска и сравнения цен.

**Технологии:**
• Google Gemini 1.5 Flash (анализ)
• DuckDuckGo (поиск)
• Aiogram 3.x (Telegram API)

**Возможности:**
• Понимание естественного языка
• Умный анализ выдачи
• Выбор оптимального предложения

*Бот работает 24/7*
"""
    await message.answer(about_text)


# === Обработка текстовых сообщений ===

@dp.message()
async def handle_text(message: Message):
    """Обработка любого текста как поискового запроса."""
    user_id = message.from_user.id
    user_text = message.text.strip()
    
    # Игнорируем слишком короткие запросы
    if len(user_text) < 3:
        await message.answer("⚠️ Слишком короткий запрос. Напиши, что именно хочешь найти.")
        return
    
    logger.info(f"Запрос от {user_id}: {user_text[:50]}...")
    
    # Отправляем "печатает..."
    await bot.send_chat_action(message.chat.id, action="typing")
    
    # Отправляем промежуточное сообщение
    status_msg = await message.answer("🔍 *Ищу информацию...*")
    
    try:
        # Вызываем агента
        response = await process_user_request(user_text)
        
        # Редактируем сообщение с результатом
        await status_msg.edit_text(
            response, 
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False
        )
        
    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ *Произошла ошибка.*\n\n"
            "Пожалуйста, попробуй позже или переформулируй запрос.",
            parse_mode=ParseMode.MARKDOWN
        )


# === Запуск ===

async def main():
    """Точка входа."""
    logger.info("Запуск бота...")
    
    # Устанавливаем команды
    await set_commands(bot)
    
    # Запускаем поллинг
    try:
        logger.info("Бот запущен и готов к работе!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    except Exception as e:
        logger.error(f"Необработанная ошибка: {e}", exc_info=True)

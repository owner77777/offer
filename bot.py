import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import SETTINGS, setup_logging
from database import DatabaseManager
from handlers import router

async def main():
    setup_logging()
    
    # Инициализация БД
    await DatabaseManager.init_db()

    default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(token=SETTINGS.BOT_TOKEN, default=default_props)
    dp = Dispatcher()

    # Подключаем роутер с хэндлерами
    dp.include_router(router)

    print("🤖 Бот запущен на Render...")
    try:
        await dp.start_polling(bot)
    finally:
        await DatabaseManager.close_connection()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Бот остановлен.")

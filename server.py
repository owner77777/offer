from aiohttp import web
import asyncio
from bot import main as bot_main


async def health_check(request):
    return web.Response(text="Bot is running")


async def start_bot():
    """Запуск бота в фоновом режиме"""
    await bot_main()


async def init_app():
    app = web.Application()
    app.router.add_get('/health', health_check)
    
    # Запускаем бота в фоне
    asyncio.create_task(start_bot())
    
    return app


if __name__ == "__main__":
    web.run_app(init_app(), port=8080, host='0.0.0.0')

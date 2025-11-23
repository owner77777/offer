from aiohttp import web
import os

async def handle_health_check(request):
    return web.Response(text="Bot is running")

def create_web_app():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    app.router.add_get('/health', handle_health_check)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(create_web_app(), host='0.0.0.0', port=port)

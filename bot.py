import asyncio
import logging
from aiohttp import web
import threading
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.client.default import DefaultBotProperties

from config import SETTINGS
from database import DatabaseManager
from handlers import (
    # Основные команды и отмена
    command_start, cmd_cancel, cmd_cancel_callback_handler,
    
    # Команды владельца
    cmd_help_owner, cmd_stats, cmd_ban, cmd_unban,
    
    # Хендлеры рассылки
    cmd_broadcast, process_broadcast_message, callback_broadcast_confirm,
    
    # Шаги FSM: Подача
    process_item_description, process_price, process_contact,
    
    # Редактирование
    process_single_edit,
    
    # Callbacks
    callback_start_submit, callback_final_send,
    
    # Хендлеры редактирования
    callback_edit_desc, callback_edit_price, callback_edit_contact,
    
    # Хендлеры статистики
    callback_stats_today, callback_stats_all, callback_stats_back, callback_stats_show_menu,
    
    # Хендлер модерации
    callback_moderation
)
from handlers import (
    AdSubmission, Broadcast, Stats
)

async def main():
    # Запускаем веб-сервер в отдельном потоке
    start_web_server()
    
    await DatabaseManager.init_db()

    default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(SETTINGS.BOT_TOKEN, default=default_props)
    dp = Dispatcher()

    # Основные команды и отмена
    dp.message.register(command_start, CommandStart(), F.chat.type.in_({ChatType.PRIVATE}))
    dp.message.register(cmd_cancel, Command("cancel"), F.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(cmd_cancel_callback_handler, F.data == "cancel_fsm",
                               F.message.chat.type.in_({ChatType.PRIVATE}))

    # Команды Владельца
    dp.message.register(cmd_help_owner, Command("help"), F.from_user.id == SETTINGS.OWNER_ID)
    dp.message.register(cmd_stats, Command("stats"), F.from_user.id == SETTINGS.OWNER_ID,
                        F.chat.type.in_({ChatType.PRIVATE}))
    dp.message.register(cmd_ban, Command("ban"), F.from_user.id == SETTINGS.OWNER_ID)
    dp.message.register(cmd_unban, Command("unban"), F.from_user.id == SETTINGS.OWNER_ID)

    # Хендлеры рассылки
    dp.message.register(cmd_broadcast, Command("broadcast"), F.from_user.id == SETTINGS.OWNER_ID,
                        F.chat.type.in_({ChatType.PRIVATE}))
    dp.message.register(process_broadcast_message, StateFilter(Broadcast.waiting_for_message),
                        F.chat.type.in_({ChatType.PRIVATE}),
                        F.text | F.photo | F.sticker | F.animation | F.video | F.document | F.caption)
    dp.callback_query.register(callback_broadcast_confirm, F.data == "bc_confirm",
                               StateFilter(Broadcast.waiting_for_confirmation),
                               F.message.chat.type.in_({ChatType.PRIVATE}))

    # Шаги FSM: Подача
    dp.message.register(process_item_description, StateFilter(AdSubmission.waiting_for_item_desc),
                        F.chat.type.in_({ChatType.PRIVATE}), F.caption | F.text)
    dp.message.register(process_price, StateFilter(AdSubmission.waiting_for_price), F.chat.type.in_({ChatType.PRIVATE}),
                        F.text)
    dp.message.register(process_contact, StateFilter(AdSubmission.waiting_for_contact),
                        F.chat.type.in_({ChatType.PRIVATE}),
                        F.text)

    # Редактирование
    dp.message.register(process_single_edit,
                        StateFilter(AdSubmission.waiting_for_edit_desc, AdSubmission.waiting_for_edit_price,
                                    AdSubmission.waiting_for_edit_contact),
                        F.chat.type.in_({ChatType.PRIVATE}), F.text | F.caption)

    # Callbacks
    dp.callback_query.register(callback_start_submit, F.data == "start_submit",
                               StateFilter(AdSubmission.waiting_for_start_button),
                               F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_final_send, F.data == "final_send",
                               StateFilter(AdSubmission.waiting_for_confirmation),
                               F.message.chat.type.in_({ChatType.PRIVATE}))

    # Хендлеры редактирования
    dp.callback_query.register(callback_edit_desc, F.data == "edit_desc",
                               StateFilter(AdSubmission.waiting_for_confirmation),
                               F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_edit_price, F.data == "edit_price",
                               StateFilter(AdSubmission.waiting_for_confirmation),
                               F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_edit_contact, F.data == "edit_contact",
                               StateFilter(AdSubmission.waiting_for_confirmation),
                               F.message.chat.type.in_({ChatType.PRIVATE}))

    # Хендлеры Статистики
    dp.callback_query.register(callback_stats_today, F.data == "stats_today", F.from_user.id == SETTINGS.OWNER_ID,
                               StateFilter(Stats.initial), F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_stats_all, F.data == "stats_all", F.from_user.id == SETTINGS.OWNER_ID,
                               StateFilter(Stats.initial), F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_stats_back, F.data == "stats_back", F.from_user.id == SETTINGS.OWNER_ID,
                               StateFilter(Stats.initial), F.message.chat.type.in_({ChatType.PRIVATE}))
    dp.callback_query.register(callback_stats_show_menu, F.data == "stats_show_menu",
                               F.from_user.id == SETTINGS.OWNER_ID, F.message.chat.type.in_({ChatType.PRIVATE}))

    # Хендлер модерации
    dp.callback_query.register(callback_moderation, F.data.startswith("mod_"), F.from_user.id == SETTINGS.OWNER_ID)

    print("🤖 Бот запущен...")
    try:
        await dp.start_polling(bot)
    finally:
        await DatabaseManager.close_connection()

# Веб-сервер для Render
async def handle_health_check(request):
    return web.Response(text="Bot is running")

def run_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    app.router.add_get('/health', handle_health_check)
    
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host='0.0.0.0', port=port)

def start_web_server():
    thread = threading.Thread(target=run_web_server, daemon=True)
    thread.start()

 print("🤖 Бот запущен...")
    try:
        await dp.start_polling(bot)
    finally:
        await DatabaseManager.close_connection()

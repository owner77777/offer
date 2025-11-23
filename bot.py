import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.client.default import DefaultBotProperties
from config import SETTINGS
from database import DatabaseManager
from handlers import (
    command_start, cmd_cancel, cmd_cancel_callback_handler,
    process_item_description, process_price, process_contact, process_single_edit,
    callback_start_submit, callback_final_send,
    callback_edit_desc, callback_edit_price, callback_edit_contact,
    cmd_help_owner, cmd_ban, cmd_unban, cmd_stats,
    cmd_broadcast, process_broadcast_message, callback_broadcast_confirm,
    callback_stats_today, callback_stats_all, callback_stats_show_menu, callback_stats_back,
    callback_moderation,
    AdSubmission, Broadcast, Stats
)


async def main():
    # Инициализация базы данных
    await DatabaseManager.init_db()

    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(SETTINGS.LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    # Создание бота и диспетчера
    default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(SETTINGS.BOT_TOKEN, default=default_props)
    dp = Dispatcher()

    # Регистрация хэндлеров
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Бот остановлен.")

import asyncio
import logging
import re
import os
from datetime import datetime
import pytz
from typing import Optional, Dict, Any, Tuple, List, Union
# Используем aiosqlite
import aiosqlite

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, InputMedia
from pydantic import BaseModel


# --- КОНФИГУРАЦИЯ ---
class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8346884521:AAGvOZdAJA4O3ohHzB2lFI5oTZnz3lWyxLY")
    OWNER_ID: int = 6493670021
    CHANNEL_PREDLOZHKA_ID: Union[int, str] = -1003287891557
    CHANNEL_FINAL_ID: Union[int, str] = -1003479497567
    CHANNEL_LOG_ID: Union[int, str] = -1003494833745
    MAX_POSTS_PER_DAY: int = 5
    TIMEZONE_NAME: str = "Europe/Moscow"
    DB_NAME: str = "bot_data.db"
    LOG_FILE: str = "bot_log.log"


SETTINGS = Config()
TIMEZONE = pytz.timezone(SETTINGS.TIMEZONE_NAME)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(SETTINGS.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Шаблон для удаления служебной информации
AUTHOR_SIG_PATTERN = re.compile(r'\n+— ID Автора:.*?—\s*$', re.DOTALL)


# --- АСИНХРОННЫЙ МЕНЕДЖЕР БАЗЫ ДАННЫХ (СИНГЛТОН) ---

class DatabaseManager:
    """Управляет единственным асинхронным подключением к aiosqlite."""
    _connection: Optional[aiosqlite.Connection] = None

    @classmethod
    async def get_connection(cls) -> aiosqlite.Connection:
        """Получает или создает одно подключение к БД."""
        if cls._connection is None:
            # Установим более длительный таймаут для предотвращения блокировок
            cls._connection = await aiosqlite.connect(SETTINGS.DB_NAME, timeout=10)
            cls._connection.row_factory = aiosqlite.Row  # Удобно для именованных столбцов
        return cls._connection

    @classmethod
    async def close_connection(cls):
        """Закрывает подключение."""
        if cls._connection:
            await cls._connection.close()
            cls._connection = None

    @classmethod
    async def init_db(cls):
        """Создает таблицы, если их нет."""
        db = await cls.get_connection()
        await db.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                created_at DATETIME, -- UTC ISO
                moderated_at DATETIME, -- UTC ISO
                moderated_date_str TEXT -- YYYY-MM-DD в локальной TZ
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER,
                date_str TEXT, -- YYYY-MM-DD в локальной TZ
                count INTEGER,
                PRIMARY KEY (user_id, date_str)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_by INTEGER,
                banned_at DATETIME, -- UTC ISO
                reason TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pending_posts (
                message_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                submitted_at DATETIME -- UTC ISO
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await db.commit()


# --- Вспомогательные функции для работы со временем ---

def _get_datetime_now_utc_str() -> str:
    """Получает текущее время в UTC в формате ISO для хранения в БД."""
    return datetime.now(pytz.utc).isoformat()


def _get_limit_date_str() -> str:
    """Получает строку с датой для лимита (по настроенной TIMEZONE)."""
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _to_tz_datetime(iso_utc_str: str) -> datetime:
    """Преобразует ISO UTC строку из БД в объект datetime в настроенной TIMEZONE."""
    dt_utc = datetime.fromisoformat(iso_utc_str).astimezone(pytz.utc)
    return dt_utc.astimezone(TIMEZONE)


# --- Функции для бана/лимитов/статистики (Асинхронные, используем DatabaseManager) ---

async def async_db_is_banned(user_id: int) -> bool:
    """Проверяет, забанен ли пользователь (асинхронно)."""
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone() is not None


async def async_db_ban_user(user_id: int, moderator_id: int, reason: str = "Не указана"):
    """Банит пользователя (асинхронно)."""
    now_utc_str = _get_datetime_now_utc_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, banned_by, banned_at, reason) VALUES (?, ?, ?, ?)",
        (user_id, moderator_id, now_utc_str, reason)
    )
    await db.commit()


async def async_db_unban_user(user_id: int):
    """Разбанивает пользователя (асинхронно)."""
    db = await DatabaseManager.get_connection()
    await db.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    await db.commit()


async def async_db_get_current_limit_count(user_id: int) -> int:
    """Получает текущее количество поданных постов за сегодня (асинхронно)."""
    if user_id == SETTINGS.OWNER_ID: return 0
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT COALESCE(count, 0) FROM user_limits WHERE user_id = ? AND date_str = ?",
                          (user_id, today_str)) as cursor:
        result = await cursor.fetchone()
        return result[0] if result else 0


async def async_db_increment_limit(user_id: int):
    """Увеличивает счетчик лимита на сегодня (асинхронно)."""
    if user_id == SETTINGS.OWNER_ID: return
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT INTO user_limits (user_id, date_str, count) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, date_str) DO UPDATE SET count = count + 1",
        (user_id, today_str)
    )
    await db.commit()


async def async_db_decrement_limit(user_id: int):
    """Уменьшает счетчик лимита на сегодня (асинхронно)."""
    if user_id == SETTINGS.OWNER_ID: return
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "UPDATE user_limits SET count = count - 1 WHERE user_id = ? AND date_str = ? AND count > 0",
        (user_id, today_str)
    )
    await db.commit()


# --- ФУНКЦИИ PENDING POSTS/BROADCAST ---
async def async_db_record_pending_post(message_id: int, user_id: int):
    """Записывает ID сообщения предложки и ID пользователя (асинхронно)."""
    submitted_at_utc_str = _get_datetime_now_utc_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT INTO pending_posts (message_id, user_id, submitted_at) VALUES (?, ?, ?)",
        (message_id, user_id, submitted_at_utc_str)
    )
    await db.commit()


async def async_db_add_broadcast_user(user_id: int):
    """Добавляет пользователя в список для рассылки (асинхронно)."""
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT OR IGNORE INTO broadcast_users (user_id) VALUES (?)",
        (user_id,)
    )
    await db.commit()


async def async_db_get_all_broadcast_users() -> List[int]:
    """Возвращает список ID всех пользователей для рассылки (асинхронно)."""
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT user_id FROM broadcast_users") as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def async_db_get_pending_post_data(message_id: int) -> Optional[Tuple[int, datetime]]:
    """Получает ID пользователя и время подачи (локализованное) (асинхронно)."""
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT user_id, submitted_at FROM pending_posts WHERE message_id = ?",
                          (message_id,)) as cursor:
        result = await cursor.fetchone()
        if result:
            user_id = result['user_id']
            submitted_at_utc_str = result['submitted_at']
            submitted_at_tz = _to_tz_datetime(submitted_at_utc_str)
            return user_id, submitted_at_tz
        return None


async def async_db_delete_pending_post(message_id: int):
    """Удаляет запись о посте в предложке (асинхронно)."""
    db = await DatabaseManager.get_connection()
    await db.execute("DELETE FROM pending_posts WHERE message_id = ?", (message_id,))
    await db.commit()


async def async_db_add_stat(event_type: str, submitted_at_tz: Optional[datetime], message_id: Optional[int] = None):
    """Добавление записи о модерации и удаление из pending_posts (асинхронно)."""
    now_utc_str = _get_datetime_now_utc_str()
    now_tz = datetime.now(TIMEZONE)
    moderated_date_str = now_tz.strftime("%Y-%m-%d")

    submitted_utc_str = submitted_at_tz.astimezone(pytz.utc).isoformat() if submitted_at_tz else now_utc_str

    db = await DatabaseManager.get_connection()

    await db.execute(
        "INSERT INTO stats (event_type, created_at, moderated_at, moderated_date_str) VALUES (?, ?, ?, ?)",
        (event_type, submitted_utc_str, now_utc_str, moderated_date_str))

    if message_id:
        await db.execute("DELETE FROM pending_posts WHERE message_id = ?", (message_id,))

    await db.commit()


async def async_db_get_stats_counts(period: str = 'all') -> Tuple[int, int]:
    """Получает количество опубликованных и отклоненных постов (асинхронно)."""
    db = await DatabaseManager.get_connection()

    params = []

    if period == 'today':
        today_str = _get_limit_date_str()
        condition = "WHERE moderated_date_str = ?"
        params = [today_str]
    else:
        condition = ""

    query_pub = f"SELECT COUNT(*) FROM stats {condition} AND event_type = 'published'"
    query_rej = f"SELECT COUNT(*) FROM stats {condition} AND event_type = 'rejected'"

    # Убираем "AND" если нет condition
    if not condition:
        query_pub = query_pub.replace("AND ", "WHERE ", 1)
        query_rej = query_rej.replace("AND ", "WHERE ", 1)

    # Обработка случая, когда нет where (для 'all')
    if period == 'all':
        query_pub = "SELECT COUNT(*) FROM stats WHERE event_type = 'published'"
        query_rej = "SELECT COUNT(*) FROM stats WHERE event_type = 'rejected'"
        params = []

    async with db.execute(query_pub, params) as cursor_pub:
        pub_count = (await cursor_pub.fetchone())[0]

    async with db.execute(query_rej, params) as cursor_rej:
        rej_count = (await cursor_rej.fetchone())[0]

    return pub_count, rej_count


# --- FSM СОСТОЯНИЯ, ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ, КЛАВИАТУРЫ ---

class AdSubmission(StatesGroup):
    waiting_for_start_button = State()
    waiting_for_item_desc = State()
    waiting_for_price = State()
    waiting_for_contact = State()
    waiting_for_confirmation = State()
    waiting_for_edit_desc = State()
    waiting_for_edit_price = State()
    waiting_for_edit_contact = State()


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


class Stats(StatesGroup):
    initial = State()


def escape_html(text: Optional[str]) -> str:
    """Экранирование HTML-спецсимволов."""
    if text is None:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def format_ad_text(data: Dict[str, Any], parse_mode: ParseMode = ParseMode.HTML) -> str:
    """Форматирование текста объявления для превью и отправки (минималистичный стиль)."""
    description = escape_html(data.get('description', 'Описание не указано'))
    price = escape_html(data.get('price', 'Цена не указана'))
    contact = escape_html(data.get('contact', 'Контакт не указан'))

    if parse_mode == ParseMode.HTML:
        return (
            f"📝 <b>Описание:</b>\n{description}\n\n"
            f"💰 <b>Цена:</b> {price}\n"
            f"📞 <b>Контакт:</b> {contact}"
        )
    else:
        return (
            f"📝 **Описание:**\n{description}\n\n"
            f"💰 **Цена:** {price}\n"
            f"📞 **Контакт:** {contact}"
        )


async def send_log(bot: Bot, message: str):
    """Отправка сообщения в лог-канал."""
    try:
        await bot.send_message(SETTINGS.CHANNEL_LOG_ID, f"📋 **LOG:** {message}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Failed to send log message to channel: {e}")


async def safe_delete_message(bot: Bot, chat_id: int, message_id: Optional[int]):
    """Безопасное удаление сообщения."""
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass


async def delete_instruction_message(bot: Bot, chat_id: int, state: FSMContext):
    """Удаление сообщения-инструкции и сброс его ID в FSM."""
    data = await state.get_data()
    message_id = data.get('instruction_message_id')
    await safe_delete_message(bot, chat_id, message_id)
    if message_id is not None:
        await state.update_data(instruction_message_id=None)


async def delete_user_draft(bot: Bot, chat_id: int, state: FSMContext):
    """Удаление сообщения-черновика и сброс его ID в FSM."""
    data = await state.get_data()
    message_id = data.get('draft_message_id')
    await safe_delete_message(bot, chat_id, message_id)
    if message_id is not None:
        await state.update_data(draft_message_id=None)


def kb_start_submit():
    builder = InlineKeyboardBuilder()
    builder.button(text="📤 Предложить пост", callback_data="start_submit")
    return builder.as_markup()


def kb_ad_submission_cancel():
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_fsm")
    return builder.as_markup()


def kb_ad_submission_edit():
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Описание", callback_data="edit_desc")
    builder.button(text="💰 Цена", callback_data="edit_price")
    builder.button(text="📞 Контакт", callback_data="edit_contact")
    builder.button(text="✅ Отправить на модерацию", callback_data="final_send")
    builder.button(text="❌ Отменить подачу", callback_data="cancel_fsm")
    builder.adjust(3, 1, 1)
    return builder.as_markup()


def kb_moderation_main(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"mod_pub:{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"mod_rej:{user_id}")
    builder.adjust(2)
    return builder.as_markup()


def kb_stats_options():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Сегодня", callback_data="stats_today")
    builder.button(text="📈 Все время", callback_data="stats_all")
    builder.button(text="🔙 Главное меню", callback_data="stats_back")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def kb_stats_back_only():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к меню статистики", callback_data="stats_show_menu")
    return builder.as_markup()


# --- ХЭНДЛЕРЫ: ПОЛЬЗОВАТЕЛЬ (START/CANCEL/SUBMISSION) ---

async def cmd_cancel(entity: Union[Message, CallbackQuery], state: FSMContext):
    """Обработчик команды или кнопки отмены /cancel и cancel_fsm."""
    if isinstance(entity, CallbackQuery):
        chat_id = entity.message.chat.id
        bot = entity.bot
        is_callback = True
    else:
        chat_id = entity.chat.id
        bot = entity.bot
        is_callback = False

    await delete_instruction_message(bot, chat_id, state)
    await delete_user_draft(bot, chat_id, state)
    await state.clear()

    response_text = "❌ <b>Действие отменено.</b>\n\nНачните с /start."

    if is_callback:
        await entity.answer("Действие отменено.")
        try:
            await entity.message.edit_text(response_text, reply_markup=None)
        except TelegramBadRequest:
            await bot.send_message(chat_id, response_text)
    else:
        await entity.answer(response_text, reply_markup=types.ReplyKeyboardRemove())


async def cmd_cancel_callback_handler(callback: CallbackQuery, state: FSMContext):
    await cmd_cancel(callback, state)


async def process_item_description(message: Message, state: FSMContext, bot: Bot):
    """Шаг 1: Описание и Фото."""
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    description = message.caption if message.caption else message.text
    photo_id = message.photo[-1].file_id if message.photo else None

    if not description or len(description.strip()) < 10:
        instruction_message = await message.answer(
            "❌ <b>Ошибка:</b> Описание должно быть не менее 10 символов.\n\nПопробуйте снова.",
            reply_markup=kb_ad_submission_cancel()
        )
        await state.update_data(instruction_message_id=instruction_message.message_id)
        return

    await state.update_data(photo_id=photo_id, description=description.strip())
    await state.set_state(AdSubmission.waiting_for_price)

    instruction_message = await message.answer(
        "💰 <b>Шаг 2 из 3: Цена</b>\n\n"
        "Укажите цену:\n"
        "• Например: <code>500.000</code>\n"
        "• <code>Договорная</code>",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=instruction_message.message_id)


async def process_price(message: Message, state: FSMContext, bot: Bot):
    """Шаг 2: Цена."""
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    price_text = message.text.strip()
    if not price_text or len(price_text) < 2:
        instruction_message = await message.answer(
            "❌ <b>Ошибка:</b> Цена не может быть такой короткой или пустой.\n\nПопробуйте снова.",
            reply_markup=kb_ad_submission_cancel()
        )
        await state.update_data(instruction_message_id=instruction_message.message_id)
        return

    await state.update_data(price=price_text)
    await state.set_state(AdSubmission.waiting_for_contact)

    instruction_message = await message.answer(
        "📞 <b>Шаг 3 из 3: Контакт</b>\n\n"
        "Укажите контакт для связи:\n"
        "• Телеграм: <code>@username</code>\n",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=instruction_message.message_id)


async def process_contact(message: Message, state: FSMContext, bot: Bot):
    """Шаг 3: Контакт (предварительный просмотр)."""
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    contact_text = message.text.strip()
    if not contact_text or len(contact_text) < 3:
        instruction_message = await message.answer(
            "❌ <b>Ошибка:</b> Контакт не может быть такой короткой или пустой.\n\nПопробуйте снова.",
            reply_markup=kb_ad_submission_cancel()
        )
        await state.update_data(instruction_message_id=instruction_message.message_id)
        return

    await state.update_data(contact=contact_text)

    data = await state.get_data()
    ad_text = format_ad_text(data, parse_mode=ParseMode.HTML)

    await state.set_state(AdSubmission.waiting_for_confirmation)

    caption = f"📋 <b>ПРЕДПРОСМОТР:</b>\n\n{ad_text}\n\n✅ <b>Проверьте данные перед отправкой</b>"

    await delete_user_draft(bot, message.chat.id, state)

    if data.get('photo_id'):
        preview_message = await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=data['photo_id'],
            caption=caption,
            reply_markup=kb_ad_submission_edit()
        )
    else:
        preview_message = await message.answer(
            caption,
            reply_markup=kb_ad_submission_edit()
        )
    await state.update_data(draft_message_id=preview_message.message_id)


async def process_single_edit(message: Message, state: FSMContext, bot: Bot):
    """Обработчик для редактирования."""
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    current_state = await state.get_state()
    data = await state.get_data()
    draft_message_id = data.get('draft_message_id')
    chat_id = message.chat.id

    new_data = {}

    if current_state == AdSubmission.waiting_for_edit_desc.state:
        new_desc = message.caption if message.caption else message.text
        new_photo_id = data.get('photo_id')
        if message.photo:
            new_photo_id = message.photo[-1].file_id
        elif not message.caption and message.text:
            new_photo_id = None

        if not new_desc or len(new_desc.strip()) < 10:
            instruction_message = await message.answer(
                "❌ <b>Ошибка:</b> Описание должно быть не менее 10 символов.",
                reply_markup=kb_ad_submission_cancel()
            )
            await state.update_data(instruction_message_id=instruction_message.message_id)
            return

        new_data['description'] = new_desc.strip()
        new_data['photo_id'] = new_photo_id

    elif current_state == AdSubmission.waiting_for_edit_price.state:
        new_price = message.text.strip()
        if not new_price or len(new_price) < 2:
            instruction_message = await message.answer(
                "❌ <b>Ошибка:</b> Цена не может быть такой короткой или пустой.",
                reply_markup=kb_ad_submission_cancel()
            )
            await state.update_data(instruction_message_id=instruction_message.message_id)
            return
        new_data['price'] = new_price

    elif current_state == AdSubmission.waiting_for_edit_contact.state:
        new_contact = message.text.strip()
        if not new_contact or len(new_contact) < 3:
            instruction_message = await message.answer(
                "❌ <b>Ошибка:</b> Контакт не может быть такой короткой или пустой.",
                reply_markup=kb_ad_submission_cancel()
            )
            await state.update_data(instruction_message_id=instruction_message.message_id)
            return
        new_data['contact'] = new_contact

    await state.update_data(**new_data)
    data.update(new_data)

    ad_text = format_ad_text(data, parse_mode=ParseMode.HTML)
    caption_text = f"📋 <b>ПРЕДПРОСМОТР:</b>\n\n{ad_text}\n\n✅ <b>Проверьте данные перед отправкой</b>"

    new_draft_message_id = draft_message_id
    is_photo_in_data = bool(data.get('photo_id'))

    if draft_message_id:
        try:
            if is_photo_in_data:
                input_media = InputMediaPhoto(media=data['photo_id'], caption=caption_text, parse_mode=ParseMode.HTML)
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=draft_message_id,
                    media=input_media,
                    reply_markup=kb_ad_submission_edit()
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=draft_message_id,
                    text=caption_text,
                    reply_markup=kb_ad_submission_edit()
                )
        except TelegramBadRequest as e:
            logging.warning(f"Failed to edit draft message {draft_message_id}: {e}. Retrying with send_... and delete.")
            await safe_delete_message(bot, chat_id, draft_message_id)

            if is_photo_in_data:
                new_draft = await message.bot.send_photo(
                    chat_id=chat_id,
                    photo=data['photo_id'],
                    caption=caption_text,
                    reply_markup=kb_ad_submission_edit()
                )
            else:
                new_draft = await message.answer(
                    caption_text,
                    reply_markup=kb_ad_submission_edit()
                )
            new_draft_message_id = new_draft.message_id

    await state.update_data(draft_message_id=new_draft_message_id)
    await state.set_state(AdSubmission.waiting_for_confirmation)

    await message.answer("✅ <b>Редактирование завершено.</b>\n\nПроверьте обновленный черновик выше.",
                         reply_markup=types.ReplyKeyboardRemove())


async def command_start(message: Message, state: FSMContext):
    """Начало диалога, сброс состояния, проверка бана и приветствие."""
    user_id = message.from_user.id

    await async_db_add_broadcast_user(user_id)

    if await async_db_is_banned(user_id):
        await message.answer(
            "🚫 <b>Доступ запрещен.</b>\n\nВы заблокированы в этом боте.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.clear()
        return

    await delete_instruction_message(message.bot, message.chat.id, state)
    await delete_user_draft(message.bot, message.chat.id, state)

    await state.clear()
    await state.set_state(AdSubmission.waiting_for_start_button)

    is_owner = user_id == SETTINGS.OWNER_ID
    current_count = await async_db_get_current_limit_count(user_id)

    if is_owner:
        limit_info = "<b>Безлимит</b> (Владелец)"
    else:
        remaining = max(0, SETTINGS.MAX_POSTS_PER_DAY - current_count)
        limit_info = f"<b>Лимит:</b> {SETTINGS.MAX_POSTS_PER_DAY} <b>постов в сутки.</b> <b>Осталось:</b> {remaining}"

    welcome_text = (
        f"<b>Здравствуйте, {escape_html(message.from_user.full_name)}!</b>\n\n"
        f"Я бот для сбора объявлений. Вы можете предложить пост для публикации в нашем канале.\n\n"
        f"💡 <b>Важно:</b>\n"
        f"• Объявления проходят модерацию\n"
        f"• {limit_info}\n"
        f"• Придерживайтесь делового стиля общения"
    )

    await message.answer(welcome_text, reply_markup=kb_start_submit())


async def callback_start_submit(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()

    current_count = await async_db_get_current_limit_count(user_id)
    if user_id != SETTINGS.OWNER_ID and current_count >= SETTINGS.MAX_POSTS_PER_DAY:
        await callback.message.edit_text(
            f"🚫 <b>Превышен лимит постов</b>\n\n"
            f"На сегодня вы уже использовали {SETTINGS.MAX_POSTS_PER_DAY} постов.\n"
            f"Попробуйте завтра!",
            reply_markup=None
        )
        await state.clear()
        return

    await state.set_state(AdSubmission.waiting_for_item_desc)

    step1_text = (
        "📝 <b>Шаг 1 из 3: Описание и фото</b>\n\n"
        "Пришлите <b>одно фото</b> (по желанию) и подробное описание вашего товара.\n\n"
        "📌 <b>Требования:</b>\n"
        "• Описание должно быть полным и понятным\n"
        "• Минимум 10 символов\n"
        "• Укажите все важные детали"
    )

    instruction_message = await callback.message.edit_text(step1_text, reply_markup=kb_ad_submission_cancel())
    await state.update_data(instruction_message_id=instruction_message.message_id)


async def callback_final_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Хендлер финальной отправки."""
    data = await state.get_data()
    user_id = callback.from_user.id

    await callback.answer("📤 Отправка на модерацию...")

    current_count = await async_db_get_current_limit_count(user_id)
    is_limited = user_id != SETTINGS.OWNER_ID and current_count >= SETTINGS.MAX_POSTS_PER_DAY
    is_banned = await async_db_is_banned(user_id)

    if is_banned or is_limited:
        error_text = "🚫 <b>Доступ запрещен.</b>\n\nЧерновик удален."

        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=error_text, reply_markup=None)
            else:
                await callback.message.edit_text(text=error_text, reply_markup=None)
        except Exception:
            pass

        await delete_user_draft(bot, callback.message.chat.id, state)
        await state.clear()
        return

    try:
        ad_text = format_ad_text(data, parse_mode=ParseMode.HTML)
        photo_id = data.get('photo_id')

        username = f"@{callback.from_user.username}" if callback.from_user.username else "Нет юзернейма"
        author_sig = f"\n\n— ID Автора: {user_id} ({escape_html(username)}) —"
        caption_for_mod = ad_text + author_sig

        message_info: Message
        if photo_id:
            message_info = await bot.send_photo(
                chat_id=SETTINGS.CHANNEL_PREDLOZHKA_ID,
                photo=photo_id,
                caption=caption_for_mod,
                reply_markup=kb_moderation_main(user_id),
                parse_mode=ParseMode.HTML
            )
        else:
            message_info = await bot.send_message(
                chat_id=SETTINGS.CHANNEL_PREDLOZHKA_ID,
                text=caption_for_mod,
                reply_markup=kb_moderation_main(user_id),
                parse_mode=ParseMode.HTML
            )

        await async_db_increment_limit(user_id)
        await async_db_record_pending_post(message_info.message_id, user_id)

        await delete_user_draft(bot, callback.message.chat.id, state)

        await bot.send_message(
            user_id,
            "✅ <b>Объявление отправлено на модерацию!</b>\n\n"
            "Ожидайте публикации. Мы уведомим вас о результате."
        )

        await send_log(bot,
                       f"Пост от {callback.from_user.full_name} ({user_id}) отправлен в предложку (Message ID: {message_info.message_id}).")

        await state.clear()

    except Exception as e:
        logging.error(f"Error sending to moderation (User: {user_id}): {e}")

        # Откатываем лимит, так как отправка не удалась
        await async_db_decrement_limit(user_id)

        await bot.send_message(
            user_id,
            f"❌ <b>Ошибка при отправке:</b>\n\n{escape_html(str(e))}\n\nПопробуйте еще раз."
        )

        await delete_user_draft(bot, callback.message.chat.id, state)

        await state.clear()


# --- ХЭНДЛЕРЫ ВЛАДЕЛЬЦА/МОДЕРАТОРА ---

async def cmd_help_owner(message: Message):
    if message.from_user.id != SETTINGS.OWNER_ID: return
    help_text = (
        "🛠️ <b>Меню Владельца</b>\n\n"
        "<code>/stats</code> - <b>Статистика</b>\n"
        "<code>/broadcast</code> - <b>Рассылка</b>\n"
        "<code>/ban</code> <code>[user_id]</code> - <b>Забанить</b>\n"
        "<code>/unban</code> <code>[user_id]</code> - <b>Разбанить</b>"
    )
    await message.answer(help_text)


async def cmd_ban(message: Message):
    if message.from_user.id != SETTINGS.OWNER_ID: return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "❌ <b>Ошибка:</b> Укажите ID пользователя.\n\n"
            "Формат: <code>/ban [user_id] [причина]</code>"
        )
        return

    user_id_to_ban = int(parts[1])
    reason = parts[2] if len(parts) == 3 else "Не указана"

    if user_id_to_ban == SETTINGS.OWNER_ID:
        await message.answer("❌ Вы не можете забанить самого себя.")
        return

    await async_db_ban_user(user_id_to_ban, message.from_user.id, reason)
    await message.answer(
        f"✅ <b>Пользователь <code>{user_id_to_ban}</code> заблокирован.</b>\n\n"
        f"📝 <b>Причина:</b> <i>{escape_html(reason)}</i>"
    )
    await send_log(message.bot, f"Пользователь `{user_id_to_ban}` заблокирован. Причина: `{reason}`")


async def cmd_unban(message: Message):
    if message.from_user.id != SETTINGS.OWNER_ID: return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "❌ <b>Ошибка:</b> Укажите ID пользователя.\n\n"
            "Формат: <code>/unban [user_id]</code>"
        )
        return

    user_id_to_unban = int(parts[1])

    if await async_db_is_banned(user_id_to_unban):
        await async_db_unban_user(user_id_to_unban)
        await message.answer(f"✅ <b>Пользователь <code>{user_id_to_unban}</code> разблокирован.</b>")
        await send_log(message.bot, f"Пользователь `{user_id_to_unban}` разблокирован.")
    else:
        await message.answer(f"ℹ️ <b>Пользователь <code>{user_id_to_unban}</code> не был забанен.</b>")


async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != SETTINGS.OWNER_ID: return
    await state.set_state(Broadcast.waiting_for_message)
    await message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Пришлите сообщение для рассылки (текст, фото и т.д.).\n\n"
        "❌ Используйте /cancel для отмены."
    )


async def process_broadcast_message(message: Message, state: FSMContext):
    if message.from_user.id != SETTINGS.OWNER_ID: return

    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id
    )
    await state.set_state(Broadcast.waiting_for_confirmation)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="bc_confirm")

    await message.answer(
        "⚠️ <b>Подтверждение рассылки</b>\n\n"
        "Отправить это сообщение всем пользователям?",
        reply_markup=builder.as_markup()
    )


async def callback_broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.from_user.id != SETTINGS.OWNER_ID: return

    await callback.answer("📤 Начинаем рассылку...")

    data = await state.get_data()
    source_chat_id = data.get('broadcast_chat_id')
    source_message_id = data.get('broadcast_message_id')

    if not source_message_id or not source_chat_id:
        await callback.message.edit_text("❌ Ошибка: Сообщение для рассылки не найдено.", reply_markup=None)
        await state.clear()
        return

    user_ids = await async_db_get_all_broadcast_users()
    success_count = 0
    fail_count = 0

    await callback.message.edit_text(
        f"📤 <b>Начало рассылки</b>\n\n"
        f"Получателей: {len(user_ids)}\n"
        f"Ожидайте завершения...",
        reply_markup=None
    )

    for user_id in user_ids:
        if user_id == callback.from_user.id:
            continue

        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except (TelegramBadRequest, TelegramAPIError) as e:
            fail_count += 1
            logging.warning(f"Failed to send broadcast to user {user_id}: {e}")
            await asyncio.sleep(0.05)

    await callback.message.answer(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📊 <b>Результаты:</b>\n"
        f"• Успешно: <b>{success_count}</b>\n"
        f"• Не доставлено: <b>{fail_count}</b>"
    )

    await send_log(bot, f"Рассылка завершена. Успешно: {success_count}, Ошибка: {fail_count}.")
    await state.clear()


# --- ХЕНДЛЕРЫ РЕДАКТИРОВАНИЯ ---

async def callback_edit_desc(callback: CallbackQuery, state: FSMContext):
    await callback.answer("✏️ Редактирование описания...")
    await delete_instruction_message(callback.bot, callback.message.chat.id, state)

    await state.set_state(AdSubmission.waiting_for_edit_desc)
    instruction_message = await callback.message.answer(
        "📝 <b>Редактирование описания</b>\n\n"
        "Введите новое описание (можно с фото).",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=instruction_message.message_id)


async def callback_edit_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer("💰 Редактирование цены...")
    await delete_instruction_message(callback.bot, callback.message.chat.id, state)

    await state.set_state(AdSubmission.waiting_for_edit_price)
    instruction_message = await callback.message.answer(
        "💰 <b>Редактирование цены</b>\n\n"
        "Введите новую цену.",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=instruction_message.message_id)


async def callback_edit_contact(callback: CallbackQuery, state: FSMContext):
    await callback.answer("📞 Редактирование контакта...")
    await delete_instruction_message(callback.bot, callback.message.chat.id, state)

    await state.set_state(AdSubmission.waiting_for_edit_contact)
    instruction_message = await callback.message.answer(
        "📞 <b>Редактирование контакта</b>\n\n"
        "Введите новый контакт для связи.",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=instruction_message.message_id)


# --- ХЕНДЛЕРЫ СТАТИСТИКИ ---

async def async_get_stats_text(period: str) -> str:
    """Формирует текст статистики (Асинхронно)."""
    pub_count, rej_count = await async_db_get_stats_counts(period)
    total = pub_count + rej_count

    if total == 0:
        pub_perc = "0.00%"
        rej_perc = "0.00%"
    else:
        pub_perc = f"{(pub_count / total) * 100:.2f}%"
        rej_perc = f"{(rej_count / total) * 100:.2f}%"

    header = "📊 <b>Статистика за Сегодня</b>" if period == 'today' else "📈 <b>Общая Статистика</b>"

    stats_text = (
        f"{header}\n\n"
        f"<b>Опубликовано:</b> {pub_count} ({pub_perc})\n"
        f"<b>Отклонено:</b> {rej_count} ({rej_perc})\n"
        f"<b>Всего обработано:</b> {total}"
    )
    return stats_text


async def cmd_stats(message: Message, state: FSMContext):
    if message.from_user.id != SETTINGS.OWNER_ID: return

    await state.set_state(Stats.initial)

    menu_text = (
        "📊 <b>Меню статистики</b>\n\n"
        "Выберите интересующий период:"
    )
    await message.answer(menu_text, reply_markup=kb_stats_options())


async def callback_stats_today(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SETTINGS.OWNER_ID: return
    await callback.answer("📊 Статистика за сегодня...")

    stats_text = await async_get_stats_text('today')
    await callback.message.edit_text(stats_text, reply_markup=kb_stats_back_only())


async def callback_stats_all(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SETTINGS.OWNER_ID: return
    await callback.answer("📈 Общая статистика...")

    stats_text = await async_get_stats_text('all')
    await callback.message.edit_text(stats_text, reply_markup=kb_stats_back_only())


async def callback_stats_show_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SETTINGS.OWNER_ID: return
    await callback.answer("🔙 Возврат в меню статистики...")

    menu_text = (
        "📊 <b>Меню статистики</b>\n\n"
        "Выберите интересующий период:"
    )
    await callback.message.edit_text(menu_text, reply_markup=kb_stats_options())


async def callback_stats_back(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SETTINGS.OWNER_ID: return
    await callback.answer("🔙 Возврат в главное меню...")

    try:
        await callback.message.edit_text("🔙 <b>Возврат в главное меню.</b>", reply_markup=None)
    except TelegramBadRequest:
        pass

    await state.clear()
    await command_start(callback.message, state)


# --- ХЕНДЛЕРЫ МОДЕРАЦИИ ---

async def callback_moderation(callback: CallbackQuery, bot: Bot):
    """
    Обработчик кнопок модерации (ОПУБЛИКОВАТЬ/ОТКЛОНИТЬ).
    """
    if callback.from_user.id != SETTINGS.OWNER_ID:
        await callback.answer("❌ У вас нет прав на модерацию.", show_alert=True)
        return

    try:
        action, user_id_str = callback.data.split(':')
        author_id = int(user_id_str)
    except ValueError:
        await callback.answer("❌ Некорректный формат данных.", show_alert=True)
        return

    message_id_in_predlozhka = callback.message.message_id
    is_published = action == "mod_pub"

    await callback.answer("⏳ Обработка...")

    post_data = await async_db_get_pending_post_data(message_id_in_predlozhka)
    if not post_data:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply("❌ Ошибка: данные поста не найдены в БД.")
        except Exception:
            pass
        return

    fetched_author_id, submitted_at = post_data
    if fetched_author_id != author_id:
        author_id = fetched_author_id

    try:
        # Убираем кнопки сразу
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        original_content = callback.message.caption if callback.message.caption else callback.message.text

        if is_published:
            # ПУБЛИКАЦИЯ
            final_content = AUTHOR_SIG_PATTERN.sub('', original_content).strip()

            # Отправка в финальный канал
            if callback.message.photo:
                await bot.send_photo(
                    chat_id=SETTINGS.CHANNEL_FINAL_ID,
                    photo=callback.message.photo[-1].file_id,
                    caption=final_content,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=SETTINGS.CHANNEL_FINAL_ID,
                    text=final_content,
                    parse_mode=ParseMode.HTML
                )

            # Обновление сообщения в предложке
            status_text = "\n\n✅ <b>ОПУБЛИКОВАНО</b>"

            # Уведомление автора и статистика
            try:
                await bot.send_message(
                    author_id,
                    "🎉 <b>Ваше объявление опубликовано!</b>\n\n"
                    "Спасибо за ваш вклад в наше сообщество!"
                )
            except Exception as e:
                logging.warning(f"Could not notify author {author_id}: {e}")

            await async_db_add_stat('published', submitted_at, message_id_in_predlozhka)
            await send_log(bot, f"Пост от {author_id} ОПУБЛИКОВАН.")

        else:
            # ОТКЛОНЕНИЕ
            status_text = "\n\n❌ <b>ОТКЛОНЕНО</b>"

            # Откат лимита и уведомление автора
            await async_db_decrement_limit(author_id)
            try:
                await bot.send_message(
                    author_id,
                    "❌ <b>Ваше объявление отклонено.</b>\n\n"
                    "Пожалуйста, ознакомьтесь с правилами и попробуйте снова."
                )
            except Exception as e:
                logging.warning(f"Could not notify author {author_id}: {e}")

            await async_db_add_stat('rejected', submitted_at, message_id_in_predlozhka)
            await send_log(bot, f"Пост от {author_id} ОТКЛОНЕН.")

        # Финальное обновление сообщения в предложке
        try:
            if callback.message.photo:
                await callback.message.edit_caption(
                    caption=original_content + status_text,
                    reply_markup=None
                )
            else:
                await callback.message.edit_text(
                    text=original_content + status_text,
                    reply_markup=None
                )
        except Exception as e:
            logging.warning(f"Could not update moderation message: {e}")

    except Exception as e:
        logging.error(f"Moderation error for post {message_id_in_predlozhka}: {e}")
        try:
            # Откатываем лимит на случай ошибки после публикации, но до статистики
            if not is_published and author_id:
                await async_db_decrement_limit(author_id)

            await callback.message.reply(f"❌ Ошибка при обработке: {e}")
        except Exception:
            pass


# --- MAIN ---

async def main():
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
        await DatabaseManager.close_connection()  # Закрываем подключение при остановке


if __name__ == "__main__":
    # Добавляем простой HTTP сервер для проверки здоровья (health check)
    from aiohttp import web
    import threading
    
    def run_health_check():
        async def health_check(request):
            return web.Response(text='OK')
        
        app = web.Application()
        app.router.add_get('/health', health_check)
        web.run_app(app, host='0.0.0.0', port=8080)
    
    # Запускаем health check в отдельном потоке
    health_thread = threading.Thread(target=run_health_check, daemon=True)
    health_thread.start()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Бот остановлен.")

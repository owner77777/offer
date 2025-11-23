import logging
import asyncio
import re
from typing import Optional, Union, Dict, List, Any
from typing import Dict, Any, Union
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

from config import SETTINGS, TIMEZONE, AUTHOR_SIG_PATTERN
from database import (
    async_db_is_banned, async_db_ban_user, async_db_unban_user,
    async_db_get_current_limit_count, async_db_increment_limit, async_db_decrement_limit,
    async_db_record_pending_post, async_db_add_broadcast_user, async_db_get_all_broadcast_users,
    async_db_get_pending_post_data, async_db_delete_pending_post, async_db_add_stat,
    async_db_get_stats_counts
)
from keyboards import (
    kb_start_submit, kb_ad_submission_cancel, kb_ad_submission_edit,
    kb_moderation_main, kb_stats_options, kb_stats_back_only
)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(SETTINGS.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# FSM состояния
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

# Вспомогательные функции
def escape_html(text: Optional[str]) -> str:
    if text is None:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_ad_text(data: Dict[str, Any], parse_mode: ParseMode = ParseMode.HTML) -> str:
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
    try:
        await bot.send_message(SETTINGS.CHANNEL_LOG_ID, f"📋 **LOG:** {message}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Failed to send log message to channel: {e}")

async def safe_delete_message(bot: Bot, chat_id: int, message_id: Optional[int]):
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass

async def delete_instruction_message(bot: Bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    message_id = data.get('instruction_message_id')
    await safe_delete_message(bot, chat_id, message_id)
    if message_id is not None:
        await state.update_data(instruction_message_id=None)

async def delete_user_draft(bot: Bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    message_id = data.get('draft_message_id')
    await safe_delete_message(bot, chat_id, message_id)
    if message_id is not None:
        await state.update_data(draft_message_id=None)

# Хэндлеры: пользователь (START/CANCEL/SUBMISSION)
async def cmd_cancel(entity: Union[Message, CallbackQuery], state: FSMContext):
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

        await async_db_decrement_limit(user_id)

        await bot.send_message(
            user_id,
            f"❌ <b>Ошибка при отправке:</b>\n\n{escape_html(str(e))}\n\nПопробуйте еще раз."
        )

        await delete_user_draft(bot, callback.message.chat.id, state)

        await state.clear()

# Хэндлеры владельца/модератора
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

# Хендлеры редактирования
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

# Хендлеры статистики
async def async_get_stats_text(period: str) -> str:
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

# Хендлеры модерации
async def callback_moderation(callback: CallbackQuery, bot: Bot):
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
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        original_content = callback.message.caption if callback.message.caption else callback.message.text

        if is_published:
            final_content = re.sub(AUTHOR_SIG_PATTERN, '', original_content, flags=re.DOTALL).strip()

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

            status_text = "\n\n✅ <b>ОПУБЛИКОВАНО</b>"

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
            status_text = "\n\n❌ <b>ОТКЛОНЕНО</b>"

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
            if not is_published and author_id:
                await async_db_decrement_limit(author_id)

            await callback.message.reply(f"❌ Ошибка при обработке: {e}")
        except Exception:
            pass

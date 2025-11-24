import logging
import asyncio
from typing import Optional, Dict, Any, Union

from aiogram import Bot, Router, types, F
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

from config import SETTINGS, AUTHOR_SIG_PATTERN
from states import AdSubmission, Broadcast, Stats
from keyboards import (
    kb_start_submit, kb_ad_submission_cancel, kb_ad_submission_edit,
    kb_moderation_main, kb_stats_options, kb_stats_back_only
)
import database as db

router = Router()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_html(text: Optional[str]) -> str:
    if text is None: return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_ad_text(data: Dict[str, Any], parse_mode: ParseMode = ParseMode.HTML) -> str:
    description = escape_html(data.get('description', 'Описание не указано'))
    price = escape_html(data.get('price', 'Цена не указана'))
    contact = escape_html(data.get('contact', 'Контакт не указан'))

    return (
        f"📝 <b>Описание:</b>\n{description}\n\n"
        f"💰 <b>Цена:</b> {price}\n"
        f"📞 <b>Контакт:</b> {contact}"
    )

async def send_log(bot: Bot, message: str):
    try:
        await bot.send_message(SETTINGS.CHANNEL_LOG_ID, f"📋 **LOG:** {message}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"Failed to send log: {e}")

async def safe_delete_message(bot: Bot, chat_id: int, message_id: Optional[int]):
    if message_id is None: return
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

# --- ХЭНДЛЕРЫ ---

@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel(message: Message, state: FSMContext):
    await _cancel_action(message, state)

@router.callback_query(F.data == "cancel_fsm", F.message.chat.type == ChatType.PRIVATE)
async def cmd_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await _cancel_action(callback, state)

async def _cancel_action(entity: Union[Message, CallbackQuery], state: FSMContext):
    is_cb = isinstance(entity, CallbackQuery)
    msg = entity.message if is_cb else entity
    chat_id = msg.chat.id
    bot = msg.bot

    await delete_instruction_message(bot, chat_id, state)
    await delete_user_draft(bot, chat_id, state)
    await state.clear()
    
    txt = "❌ <b>Действие отменено.</b>\n\nНачните с /start."
    if is_cb:
        await entity.answer("Действие отменено.")
        try:
            await msg.edit_text(txt, reply_markup=None)
        except:
            await bot.send_message(chat_id, txt)
    else:
        await msg.answer(txt, reply_markup=types.ReplyKeyboardRemove())

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def command_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await db.async_db_add_broadcast_user(user_id)

    if await db.async_db_is_banned(user_id):
        await message.answer("🚫 <b>Доступ запрещен.</b>", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    await delete_instruction_message(message.bot, message.chat.id, state)
    await delete_user_draft(message.bot, message.chat.id, state)
    await state.clear()
    await state.set_state(AdSubmission.waiting_for_start_button)

    is_owner = user_id == SETTINGS.OWNER_ID
    current_count = await db.async_db_get_current_limit_count(user_id)
    
    limit_info = "<b>Безлимит</b> (Владелец)" if is_owner else f"<b>Лимит:</b> {SETTINGS.MAX_POSTS_PER_DAY} постов. Осталось: {max(0, SETTINGS.MAX_POSTS_PER_DAY - current_count)}"
    
    welcome_text = (
        f"<b>Здравствуйте, {escape_html(message.from_user.full_name)}!</b>\n\n"
        f"Я бот для сбора объявлений.\n\n💡 <b>Важно:</b>\n"
        f"• Объявления проходят модерацию\n• {limit_info}\n"
    )
    await message.answer(welcome_text, reply_markup=kb_start_submit())

@router.callback_query(F.data == "start_submit", StateFilter(AdSubmission.waiting_for_start_button))
async def callback_start_submit(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()

    cnt = await db.async_db_get_current_limit_count(user_id)
    if user_id != SETTINGS.OWNER_ID and cnt >= SETTINGS.MAX_POSTS_PER_DAY:
        await callback.message.edit_text("🚫 <b>Превышен лимит постов</b>", reply_markup=None)
        await state.clear()
        return

    await state.set_state(AdSubmission.waiting_for_item_desc)
    msg = await callback.message.edit_text(
        "📝 <b>Шаг 1 из 3: Описание и фото</b>\n\nПришлите фото и описание (мин 10 символов).",
        reply_markup=kb_ad_submission_cancel()
    )
    await state.update_data(instruction_message_id=msg.message_id)

@router.message(StateFilter(AdSubmission.waiting_for_item_desc), F.chat.type == ChatType.PRIVATE, F.caption | F.text)
async def process_item_description(message: Message, state: FSMContext):
    bot = message.bot
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    desc = message.caption if message.caption else message.text
    photo_id = message.photo[-1].file_id if message.photo else None

    if not desc or len(desc.strip()) < 10:
        im = await message.answer("❌ Описание должно быть не менее 10 символов.", reply_markup=kb_ad_submission_cancel())
        await state.update_data(instruction_message_id=im.message_id)
        return

    await state.update_data(photo_id=photo_id, description=desc.strip())
    await state.set_state(AdSubmission.waiting_for_price)
    im = await message.answer("💰 <b>Шаг 2 из 3: Цена</b>", reply_markup=kb_ad_submission_cancel())
    await state.update_data(instruction_message_id=im.message_id)

@router.message(StateFilter(AdSubmission.waiting_for_price), F.chat.type == ChatType.PRIVATE, F.text)
async def process_price(message: Message, state: FSMContext):
    bot = message.bot
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    if not message.text.strip() or len(message.text) < 2:
        im = await message.answer("❌ Цена слишком короткая.", reply_markup=kb_ad_submission_cancel())
        await state.update_data(instruction_message_id=im.message_id)
        return

    await state.update_data(price=message.text.strip())
    await state.set_state(AdSubmission.waiting_for_contact)
    im = await message.answer("📞 <b>Шаг 3 из 3: Контакт</b>", reply_markup=kb_ad_submission_cancel())
    await state.update_data(instruction_message_id=im.message_id)

@router.message(StateFilter(AdSubmission.waiting_for_contact), F.chat.type == ChatType.PRIVATE, F.text)
async def process_contact(message: Message, state: FSMContext):
    bot = message.bot
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)

    if not message.text.strip() or len(message.text) < 3:
        im = await message.answer("❌ Контакт слишком короткий.", reply_markup=kb_ad_submission_cancel())
        await state.update_data(instruction_message_id=im.message_id)
        return

    await state.update_data(contact=message.text.strip())
    data = await state.get_data()
    ad_text = format_ad_text(data)
    
    await state.set_state(AdSubmission.waiting_for_confirmation)
    caption = f"📋 <b>ПРЕДПРОСМОТР:</b>\n\n{ad_text}\n\n✅ <b>Проверьте данные</b>"
    
    await delete_user_draft(bot, message.chat.id, state)
    
    if data.get('photo_id'):
        pm = await message.bot.send_photo(message.chat.id, data['photo_id'], caption=caption, reply_markup=kb_ad_submission_edit())
    else:
        pm = await message.answer(caption, reply_markup=kb_ad_submission_edit())
    await state.update_data(draft_message_id=pm.message_id)

@router.callback_query(F.data == "final_send", StateFilter(AdSubmission.waiting_for_confirmation))
async def callback_final_send(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    bot = callback.bot
    await callback.answer("📤 Отправка...")

    if await db.async_db_is_banned(user_id) or (user_id != SETTINGS.OWNER_ID and await db.async_db_get_current_limit_count(user_id) >= SETTINGS.MAX_POSTS_PER_DAY):
         await delete_user_draft(bot, callback.message.chat.id, state)
         await state.clear()
         return

    try:
        ad_text = format_ad_text(data)
        username = f"@{callback.from_user.username}" if callback.from_user.username else "Нет юзернейма"
        full_text = f"{ad_text}\n\n— ID Автора: {user_id} ({escape_html(username)}) —"
        
        if data.get('photo_id'):
            mi = await bot.send_photo(SETTINGS.CHANNEL_PREDLOZHKA_ID, data['photo_id'], caption=full_text, reply_markup=kb_moderation_main(user_id))
        else:
            mi = await bot.send_message(SETTINGS.CHANNEL_PREDLOZHKA_ID, full_text, reply_markup=kb_moderation_main(user_id))
            
        await db.async_db_increment_limit(user_id)
        await db.async_db_record_pending_post(mi.message_id, user_id)
        await delete_user_draft(bot, callback.message.chat.id, state)
        await bot.send_message(user_id, "✅ <b>Отправлено на модерацию!</b>")
        await send_log(bot, f"Пост от {user_id} в предложке.")
        await state.clear()
    except Exception as e:
        logging.error(e)
        await db.async_db_decrement_limit(user_id)
        await bot.send_message(user_id, "❌ Ошибка отправки.")

# --- Редактирование ---
@router.callback_query(F.data.in_({"edit_desc", "edit_price", "edit_contact"}), StateFilter(AdSubmission.waiting_for_confirmation))
async def callback_edit_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await delete_instruction_message(callback.bot, callback.message.chat.id, state)
    
    mapping = {
        "edit_desc": (AdSubmission.waiting_for_edit_desc, "Введите новое описание"),
        "edit_price": (AdSubmission.waiting_for_edit_price, "Введите новую цену"),
        "edit_contact": (AdSubmission.waiting_for_edit_contact, "Введите новый контакт")
    }
    
    new_state, text = mapping[callback.data]
    await state.set_state(new_state)
    im = await callback.message.answer(f"✏️ {text}", reply_markup=kb_ad_submission_cancel())
    await state.update_data(instruction_message_id=im.message_id)

@router.message(StateFilter(AdSubmission.waiting_for_edit_desc, AdSubmission.waiting_for_edit_price, AdSubmission.waiting_for_edit_contact), F.chat.type == ChatType.PRIVATE)
async def process_edit_value(message: Message, state: FSMContext):
    bot = message.bot
    await delete_instruction_message(bot, message.chat.id, state)
    await safe_delete_message(bot, message.chat.id, message.message_id)
    
    st = await state.get_state()
    data = await state.get_data()
    new_data = {}
    
    if st == AdSubmission.waiting_for_edit_desc:
        desc = message.caption or message.text
        if not desc or len(desc.strip()) < 10:
             im = await message.answer("❌ Минимум 10 символов.", reply_markup=kb_ad_submission_cancel())
             await state.update_data(instruction_message_id=im.message_id)
             return
        new_data['description'] = desc.strip()
        if message.photo: new_data['photo_id'] = message.photo[-1].file_id
        
    elif st == AdSubmission.waiting_for_edit_price:
        new_data['price'] = message.text.strip()
    elif st == AdSubmission.waiting_for_edit_contact:
        new_data['contact'] = message.text.strip()
        
    await state.update_data(**new_data)
    data.update(new_data)
    
    ad_text = format_ad_text(data)
    caption = f"📋 <b>ПРЕДПРОСМОТР:</b>\n\n{ad_text}\n\n✅ <b>Проверьте данные</b>"
    
    draft_id = data.get('draft_message_id')
    photo_id = data.get('photo_id')
    
    try:
        if photo_id:
            media = InputMediaPhoto(media=photo_id, caption=caption, parse_mode=ParseMode.HTML)
            await bot.edit_message_media(message.chat.id, draft_id, media=media, reply_markup=kb_ad_submission_edit())
        else:
            await bot.edit_message_text(caption, message.chat.id, draft_id, reply_markup=kb_ad_submission_edit())
    except Exception:
        # Если не вышло отредактировать, шлем новое
        await safe_delete_message(bot, message.chat.id, draft_id)
        if photo_id:
            nm = await bot.send_photo(message.chat.id, photo_id, caption=caption, reply_markup=kb_ad_submission_edit())
        else:
            nm = await message.answer(caption, reply_markup=kb_ad_submission_edit())
        await state.update_data(draft_message_id=nm.message_id)
        
    await state.set_state(AdSubmission.waiting_for_confirmation)

# --- Модерация ---
@router.callback_query(F.data.startswith("mod_"), F.from_user.id == SETTINGS.OWNER_ID)
async def callback_moderation(callback: CallbackQuery):
    action, uid_str = callback.data.split(':')
    author_id = int(uid_str)
    msg = callback.message
    is_pub = action == "mod_pub"
    
    await callback.answer("⏳ Обработка...")
    post_data = await db.async_db_get_pending_post_data(msg.message_id)
    if not post_data:
        await msg.reply("❌ Пост не найден в БД.")
        return
        
    real_author, sub_at = post_data
    
    try:
        await msg.edit_reply_markup(reply_markup=None)
        orig_content = msg.caption or msg.text
        
        if is_pub:
            final = AUTHOR_SIG_PATTERN.sub('', orig_content).strip()
            if msg.photo:
                await msg.bot.send_photo(SETTINGS.CHANNEL_FINAL_ID, msg.photo[-1].file_id, caption=final)
            else:
                await msg.bot.send_message(SETTINGS.CHANNEL_FINAL_ID, final)
                
            status = "\n\n✅ <b>ОПУБЛИКОВАНО</b>"
            await msg.bot.send_message(real_author, "🎉 Опубликовано!")
            await db.async_db_add_stat('published', sub_at, msg.message_id)
        else:
            status = "\n\n❌ <b>ОТКЛОНЕНО</b>"
            await db.async_db_decrement_limit(real_author)
            await msg.bot.send_message(real_author, "❌ Отклонено.")
            await db.async_db_add_stat('rejected', sub_at, msg.message_id)
            
        if msg.photo: await msg.edit_caption(caption=orig_content + status)
        else: await msg.edit_text(orig_content + status)
        
    except Exception as e:
        logging.error(f"Mod error: {e}")
        await msg.reply(f"Error: {e}")

# --- Админка (Owner) ---
@router.message(Command("ban"), F.from_user.id == SETTINGS.OWNER_ID)
async def cmd_ban(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2: return
    uid = int(parts[1])
    reason = parts[2] if len(parts) > 2 else "None"
    await db.async_db_ban_user(uid, message.from_user.id, reason)
    await message.answer(f"Banned {uid}")

@router.message(Command("unban"), F.from_user.id == SETTINGS.OWNER_ID)
async def cmd_unban(message: Message):
    parts = message.text.split()
    if len(parts) < 2: return
    await db.async_db_unban_user(int(parts[1]))
    await message.answer(f"Unbanned {parts[1]}")

@router.message(Command("broadcast"), F.from_user.id == SETTINGS.OWNER_ID, F.chat.type == ChatType.PRIVATE)
async def cmd_broadcast(message: Message, state: FSMContext):
    await state.set_state(Broadcast.waiting_for_message)
    await message.answer("📢 Пришлите сообщение для рассылки.")

@router.message(StateFilter(Broadcast.waiting_for_message), F.from_user.id == SETTINGS.OWNER_ID)
async def process_broadcast(message: Message, state: FSMContext):
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id)
    await state.set_state(Broadcast.waiting_for_confirmation)
    kb = InlineKeyboardBuilder().button(text="✅", callback_data="bc_confirm").as_markup()
    await message.answer("Отправить?", reply_markup=kb)

@router.callback_query(F.data == "bc_confirm", StateFilter(Broadcast.waiting_for_confirmation))
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    users = await db.async_db_get_all_broadcast_users()
    count = 0
    await callback.message.edit_text(f"Рассылка на {len(users)}...")
    for u in users:
        try:
            await callback.bot.copy_message(u, data['bc_chat'], data['bc_msg'])
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await callback.message.answer(f"Успешно: {count}")
    await state.clear()

# --- Статистика ---
@router.message(Command("stats"), F.from_user.id == SETTINGS.OWNER_ID)
async def cmd_stats(message: Message, state: FSMContext):
    await state.set_state(Stats.initial)
    await message.answer("Статистика:", reply_markup=kb_stats_options())

@router.callback_query(F.data.in_({"stats_today", "stats_all"}), F.from_user.id == SETTINGS.OWNER_ID)
async def stats_view(callback: CallbackQuery):
    mode = 'today' if 'today' in callback.data else 'all'
    pub, rej = await db.async_db_get_stats_counts(mode)
    await callback.message.edit_text(f"Pub: {pub}, Rej: {rej}", reply_markup=kb_stats_back_only())

@router.callback_query(F.data == "stats_show_menu", F.from_user.id == SETTINGS.OWNER_ID)
async def stats_back(callback: CallbackQuery):
    await callback.message.edit_text("Статистика:", reply_markup=kb_stats_options())

@router.callback_query(F.data == "stats_back", F.from_user.id == SETTINGS.OWNER_ID)
async def stats_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Меню закрыто.")

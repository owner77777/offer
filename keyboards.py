from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import types


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

"""Telegram keyboards."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings


def main_menu_kb(has_api_key: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="Открыть генератор",
            web_app=WebAppInfo(url=settings.WEBAPP_URL),
        )
    )

    if has_api_key:
        builder.button(text="Профиль", callback_data="profile")
        builder.button(text="История", callback_data="history")
        builder.button(text="Сменить ключ", callback_data="change_key")
    else:
        builder.button(text="Подключить API-ключ", callback_data="set_key")

    builder.adjust(1)
    return builder.as_markup()


def profile_kb(has_api_key: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть генератор", web_app=WebAppInfo(url=settings.WEBAPP_URL))
    builder.button(text="Сменить ключ" if has_api_key else "Подключить ключ", callback_data="change_key")
    builder.button(text="В меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()

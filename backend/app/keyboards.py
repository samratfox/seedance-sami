"""Telegram keyboards."""

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu_kb(has_api_key: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if has_api_key:
        builder.button(text="Сменить ключ", callback_data="change_key")
    else:
        builder.button(text="Подключить API-ключ", callback_data="set_key")

    builder.adjust(1)
    return builder.as_markup()


def profile_kb(has_api_key: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сменить ключ" if has_api_key else "Подключить ключ", callback_data="change_key")
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

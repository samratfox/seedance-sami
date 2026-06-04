"""Telegram bot handlers."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from app import keyboards as kb
from app.api_client import AIGateClient, AIGateError, format_balance
from app.config import settings
from app.database import db
from app.states import ProfileStates

router = Router()


async def ensure_user(from_user: User):
    user = await db.get_user(from_user.id)
    if not user:
        await db.create_user(from_user.id, from_user.username, from_user.full_name)
        user = await db.get_user(from_user.id)
    return user


async def start_text(from_user: User) -> tuple[str, bool]:
    user = await ensure_user(from_user)
    has_key = bool(user and user.get("api_key"))
    status = "ключ подключен" if has_key else "ключ ещё не подключен"
    text = (
        "<b>AIGate Video</b>\n\n"
        "Это твой интерфейс для генерации видео через AIGate. "
        "Расход идёт с баланса пользователя, чей API-ключ подключён в боте.\n\n"
        f"<b>Статус:</b> {status}\n\n"
        "<b>Как начать:</b>\n"
        f"1. Зарегистрируйся и пополни баланс: {settings.REFERRAL_URL}\n"
        "2. Получи API-ключ в кабинете AIGate.\n"
        "3. Нажми «Подключить API-ключ» или отправь /setkey.\n"
        "4. Открой генератор и создай видео."
    )
    return text, has_key


@router.message(Command("start"))
async def cmd_start(message: Message):
    text, has_key = await start_text(message.from_user)
    await message.answer(text, reply_markup=kb.main_menu_kb(has_key))


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/start - главное меню\n"
        "/setkey - подключить API-ключ AIGate\n"
        "/profile - баланс и статус ключа\n"
        "/history - последние генерации\n"
        "/generate - открыть Mini App"
    )


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    user = await ensure_user(message.from_user)
    await message.answer(
        "Генерация открывается в Mini App: там удобнее выбирать модель, формат, качество и референсы.",
        reply_markup=kb.main_menu_kb(bool(user and user.get("api_key"))),
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = await ensure_user(message.from_user)
    if not user or not user.get("api_key"):
        await message.answer("API-ключ пока не подключён.", reply_markup=kb.profile_kb(False))
        return

    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        await message.answer(
            f"<b>Профиль</b>\n\n{format_balance(balance)}",
            reply_markup=kb.profile_kb(True),
        )
    except AIGateError as exc:
        await message.answer(f"Не удалось получить баланс: {exc}", reply_markup=kb.profile_kb(True))


@router.message(Command("history"))
async def cmd_history(message: Message):
    await send_history(message, message.from_user)


async def send_history(message: Message, from_user: User):
    await ensure_user(from_user)
    generations = await db.get_user_generations(from_user.id, 10)
    if not generations:
        await message.answer("История пока пустая.", reply_markup=kb.main_menu_kb(False))
        return

    lines = ["<b>Последние генерации</b>"]
    icons = {"pending": "ожидает", "processing": "в работе", "completed": "готово", "failed": "ошибка"}
    for item in generations:
        status = icons.get(item["status"], item["status"])
        model = item.get("model") or "model"
        lines.append(
            f"#{item['id']} - {status} - {item['duration']}с, {item['resolution']}, {item['ratio']} - {model}"
        )
    await message.answer("\n".join(lines), reply_markup=kb.main_menu_kb(True))


@router.message(Command("setkey"))
async def cmd_setkey(message: Message, state: FSMContext):
    await ensure_user(message.from_user)
    await state.set_state(ProfileStates.waiting_api_key)
    await message.answer(
        "Отправь API-ключ AIGate. Я проверю его через баланс и сохраню только на backend.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(StateFilter(ProfileStates.waiting_api_key), F.text)
async def process_key(message: Message, state: FSMContext):
    key = message.text.strip()
    if key.lower() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb.remove_kb())
        await cmd_start(message)
        return

    checking = await message.answer("Проверяю ключ...")
    try:
        balance = await AIGateClient(key).get_balance()
        await db.set_user_api_key(message.from_user.id, key)
        await checking.edit_text(f"Ключ подключён.\n\n{format_balance(balance)}", reply_markup=kb.profile_kb(True))
    except AIGateError as exc:
        await checking.edit_text(f"Ключ не прошёл проверку: {exc}")
    finally:
        await state.clear()
        await message.answer("Готово.", reply_markup=kb.remove_kb())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    text, has_key = await start_text(callback.from_user)
    await callback.message.edit_text(text, reply_markup=kb.main_menu_kb(has_key))


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    user = await ensure_user(callback.from_user)
    if not user or not user.get("api_key"):
        await callback.message.edit_text("API-ключ пока не подключён.", reply_markup=kb.profile_kb(False))
        return

    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        await callback.message.edit_text(f"<b>Профиль</b>\n\n{format_balance(balance)}", reply_markup=kb.profile_kb(True))
    except AIGateError as exc:
        await callback.message.edit_text(f"Не удалось получить баланс: {exc}", reply_markup=kb.profile_kb(True))


@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    await callback.answer()
    await ensure_user(callback.from_user)
    generations = await db.get_user_generations(callback.from_user.id, 10)
    if not generations:
        await callback.message.edit_text("История пока пустая.", reply_markup=kb.main_menu_kb(True))
        return

    lines = ["<b>Последние генерации</b>"]
    for item in generations:
        lines.append(f"#{item['id']} - {item['status']} - {item['duration']}с / {item['resolution']} / {item['ratio']}")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb.main_menu_kb(True))


@router.callback_query(F.data.in_({"set_key", "change_key"}))
async def cb_setkey(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProfileStates.waiting_api_key)
    await callback.message.answer(
        "Отправь новый API-ключ AIGate.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(F.text.lower().in_({"отмена", "cancel"}))
async def universal_cancel(message: Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb.remove_kb())
        await cmd_start(message)

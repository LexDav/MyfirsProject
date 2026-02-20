import asyncio
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")

WELCOME_AND_COMMANDS = (
    "Здравствуйте! Выберите режим работы: Light (опрос) или Expert (свободный ввод).\n\n"
    "Доступные команды:\n"
    "/start — выбор режима\n"
    "/mode — переключение режима\n"
    "/classify — начать классификацию\n"
    "/check <код> — проверить код (10 цифр, 8408… или 8418…)\n"
    "/codes — справка по 8408/8418\n"
    "/history — история запросов\n"
    "/cancel — завершить диалог"
)

CODE10_RE = re.compile(r"\b(84(?:08|18)\d{6})\b")


@dataclass
class ClassificationResult:
    code: str
    title: str
    confidence: float
    explanation: str


class LightStates(StatesGroup):
    check_code = State()
    know_code = State()
    code_input = State()
    name = State()
    has_image = State()
    category = State()
    purpose = State()
    material = State()
    supply_form = State()
    packaging = State()
    additional = State()


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            username TEXT,
            registered_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            mode TEXT,
            description TEXT,
            result TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tnved (
            code TEXT PRIMARY KEY,
            title TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_user(db_path: str, chat_id: int, username: str | None) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (chat_id, username, registered_at) VALUES (?, ?, ?)",
            (chat_id, username, datetime.utcnow().isoformat()),
        )
    conn.commit()
    conn.close()


def save_query(db_path: str, chat_id: int, mode: str, description: str, result: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queries (chat_id, mode, description, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, mode, description, result, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(db_path: str, chat_id: int, limit: int = 10) -> Iterable[tuple]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT created_at, mode, description, result FROM queries WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def tnved_get_by_code(db_path: str, code: str) -> Optional[str]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT title FROM tnved WHERE code = ?", (code,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def tnved_suggest_by_text(db_path: str, text: str, limit: int = 5) -> list[tuple[str, str]]:
    tokens = [t for t in re.findall(r"[а-яa-z0-9-]+", text.lower()) if len(t) >= 3]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    found: dict[str, str] = {}
    for token in tokens[:6]:
        cur.execute(
            """
            SELECT code, title
            FROM tnved
            WHERE (code LIKE '8408%' OR code LIKE '8418%')
              AND lower(title) LIKE ?
            LIMIT ?
            """,
            (f"%{token}%", limit),
        )
        for code, title in cur.fetchall():
            found[code] = title
        if len(found) >= limit:
            break

    if not found:
        cur.execute(
            """
            SELECT code, title
            FROM tnved
            WHERE code LIKE '8408%' OR code LIKE '8418%'
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    conn.close()
    return list(found.items())[:limit]


def classify_text_with_db(db_path: str, text: str) -> list[ClassificationResult]:
    raw = (text or "").strip()

    if raw.isdigit() and len(raw) == 10 and raw.startswith(("8408", "8418")):
        title = tnved_get_by_code(db_path, raw)
        if title:
            return [ClassificationResult(raw, title, 0.92, "Код найден в БД tnved")]
        return [ClassificationResult(raw, "Код не найден в БД tnved", 0.35, "Нет записи")]

    m = CODE10_RE.search(raw)
    if m:
        code10 = m.group(1)
        title = tnved_get_by_code(db_path, code10)
        if title:
            return [ClassificationResult(code10, title, 0.9, "Код найден в тексте и подтверждён БД")]
        return [ClassificationResult(code10, "Код найден в тексте, но отсутствует в БД", 0.35, "Нет записи")]

    return [
        ClassificationResult(
            "0000000000",
            "Требуется уточнение классификации (8408/8418)",
            0.45,
            "Недостаточно признаков: нужен код из документов или подробные характеристики.",
        )
    ]


def format_results(results: list[ClassificationResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"Код {r.code} — {r.title} (вероятность {int(r.confidence * 100)}%).\nОбоснование: {r.explanation}")
    return "\n\n".join(lines)


def format_suggestions(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return "Подходящих кодов не найдено."
    lines = ["Возможные коды (предварительно):"]
    for code, title in rows:
        lines.append(f"• {code} — {title}")
    lines.append("Если видите подходящий код — отправьте его 10 цифрами, и я проверю его по БД.")
    return "\n".join(lines)


def assess_risk(confidence: float) -> str:
    if confidence >= 0.8:
        return "низкая"
    if confidence >= 0.6:
        return "средняя"
    return "высокая"


def chunk_message(text: str, chunk_size: int = 3500) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def build_description(data: dict[str, Any]) -> str:
    parts = []
    for key in ("code_input", "name", "category", "purpose", "material", "supply_form", "packaging", "additional"):
        if data.get(key):
            parts.append(str(data[key]))
    return " ".join(parts)


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Light"), KeyboardButton(text="Expert")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def yes_no_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


router = Router()
user_modes: dict[int, str] = {}


@router.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await asyncio.to_thread(
        ensure_user,
        DB_PATH,
        message.chat.id,
        message.from_user.username if message.from_user else None,
    )
    await state.clear()
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(WELCOME_AND_COMMANDS, reply_markup=mode_keyboard())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(WELCOME_AND_COMMANDS, reply_markup=mode_keyboard())


@router.message(Command("mode"))
async def mode_command(message: Message) -> None:
    await message.answer("Выберите режим: Light или Expert.", reply_markup=mode_keyboard())


async def start_light_questionnaire(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LightStates.check_code)
    await message.answer("1) Хотите проверить правильность имеющегося кода ТН ВЭД? (Да/Нет)", reply_markup=yes_no_keyboard())


@router.message(F.text.lower().in_({"light", "expert"}))
async def set_mode(message: Message, state: FSMContext) -> None:
    selected_mode = (message.text or "").lower()
    user_modes[message.chat.id] = selected_mode
    await message.answer(f"Режим установлен: {message.text}.")
    if selected_mode == "light":
        await start_light_questionnaire(message, state)
    else:
        await state.clear()
        await message.answer("Expert режим: пришлите описание товара (назначение + тех.параметры + модель/артикул).")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог завершён. Данные сброшены.")


@router.message(Command("classify"))
async def classify_command(message: Message, state: FSMContext, command: CommandObject) -> None:
    mode = user_modes.get(message.chat.id)

    if command.args:
        text = command.args.strip()
        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode or "quick", text, response)
        await message.answer(f"{response}\n\nОценка риска неверной классификации: {assess_risk(results[0].confidence)}.")
        return

    if mode == "light":
        await start_light_questionnaire(message, state)
        return

    user_modes[message.chat.id] = "expert"
    await message.answer("Expert режим: пришлите описание товара (можно сразу с кодом 8408.../8418...).")


@router.message(LightStates.check_code)
async def light_check_code(message: Message, state: FSMContext) -> None:
    answer = (message.text or "").strip().lower()
    if answer in {"да", "yes", "y"}:
        await state.set_state(LightStates.know_code)
        await message.answer("2) Знаете ли вы 10-значный код? (Да/Нет)", reply_markup=yes_no_keyboard())
        return
    if answer in {"нет", "no", "n"}:
        await state.set_state(LightStates.name)
        await message.answer("2) Укажите наименование товара (название/модель), и я предложу возможные коды.")
        return
    await message.answer("Пожалуйста, ответьте Да или Нет.", reply_markup=yes_no_keyboard())


@router.message(LightStates.know_code)
async def light_know_code(message: Message, state: FSMContext) -> None:
    answer = (message.text or "").strip().lower()
    if answer in {"да", "yes", "y"}:
        await state.set_state(LightStates.code_input)
        await message.answer("3) Введите код ТН ВЭД (10 цифр, начинается на 8408 или 8418).")
        return
    if answer in {"нет", "no", "n"}:
        await state.set_state(LightStates.name)
        await message.answer("3) Напишите наименование товара и ключевые признаки, я предложу 5 вероятных кодов.")
        return
    await message.answer("Пожалуйста, ответьте Да или Нет.", reply_markup=yes_no_keyboard())


@router.message(LightStates.code_input)
async def light_code_input(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not (code.isdigit() and len(code) == 10 and code.startswith(("8408", "8418"))):
        await message.answer("Код должен быть 10 цифр и начинаться на 8408 или 8418.")
        return

    title = await asyncio.to_thread(tnved_get_by_code, DB_PATH, code)
    if title:
        result = [ClassificationResult(code, title, 0.92, "Код найден в классификаторе (БД)")]
        response = format_results(result)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light_check", code, response)
        await message.answer(f"{response}\n\nОценка риска неверной классификации: {assess_risk(result[0].confidence)}.")
        await state.clear()
        return

    await state.update_data(code_input=code)
    await state.set_state(LightStates.name)
    await message.answer("Такого кода нет в БД. Укажите наименование товара — предложу похожие коды.")


@router.message(LightStates.name)
async def light_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    await state.update_data(name=name)

    suggestions = await asyncio.to_thread(tnved_suggest_by_text, DB_PATH, name, 5)
    await message.answer(format_suggestions(suggestions))

    await state.set_state(LightStates.has_image)
    await message.answer("4) Есть ли изображение товара? (Да/Нет)", reply_markup=yes_no_keyboard())


@router.message(LightStates.has_image)
async def light_has_image(message: Message, state: FSMContext) -> None:
    await state.update_data(has_image=message.text)
    await state.set_state(LightStates.category)
    await message.answer("5) Категория товара? (например: двигатель / холодильное оборудование)")


@router.message(LightStates.category)
async def light_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=message.text)
    await state.set_state(LightStates.purpose)
    await message.answer("6) Назначение товара (для чего используется)?")


@router.message(LightStates.purpose)
async def light_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=message.text)
    await state.set_state(LightStates.material)
    await message.answer("7) Материал (если применимо)?")


@router.message(LightStates.material)
async def light_material(message: Message, state: FSMContext) -> None:
    await state.update_data(material=message.text)
    await state.set_state(LightStates.supply_form)
    await message.answer("8) Форма поставки (изделие/запчасть/комплект)?")


@router.message(LightStates.supply_form)
async def light_supply_form(message: Message, state: FSMContext) -> None:
    await state.update_data(supply_form=message.text)
    await state.set_state(LightStates.packaging)
    await message.answer("9) Упаковка (коробка/контейнер/иное)?")


@router.message(LightStates.packaging)
async def light_packaging(message: Message, state: FSMContext) -> None:
    await state.update_data(packaging=message.text)
    await state.set_state(LightStates.additional)
    await message.answer("10) Дополнительные характеристики (мощность, объём, тип охлаждения и т.п.).")


@router.message(LightStates.additional)
async def light_additional(message: Message, state: FSMContext) -> None:
    await state.update_data(additional=message.text)
    data = await state.get_data()
    description = build_description(data)

    results = await asyncio.to_thread(classify_text_with_db, DB_PATH, description)
    response = format_results(results)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light", description, response)

    if results[0].code == "0000000000":
        suggestions = await asyncio.to_thread(tnved_suggest_by_text, DB_PATH, description, 5)
        await message.answer(
            f"{response}\n\n{format_suggestions(suggestions)}\n\n"
            "Если среди вариантов нет точного совпадения — пришлите более точные параметры (тип, мощность, объём, назначение)."
        )
    else:
        await message.answer(f"{response}\n\nОценка риска неверной классификации: {assess_risk(results[0].confidence)}.")
    await state.clear()


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например, /check 8408101100")
        return

    code = command.args.strip()
    if not (code.isdigit() and len(code) == 10 and code.startswith(("8408", "8418"))):
        await message.answer("Код должен быть 10 цифр и начинаться на 8408 или 8418.")
        return

    title = await asyncio.to_thread(tnved_get_by_code, DB_PATH, code)
    if title:
        result = [ClassificationResult(code, title, 0.92, "Код найден в БД")]
    else:
        result = [ClassificationResult(code, "Код не найден в вашей БД (tnved)", 0.35, "Нет записи")]

    response = format_results(result)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "check", code, response)
    await message.answer(response)


@router.message(Command("codes"))
async def codes_command(message: Message) -> None:
    lines = [
        "8408 — Двигатели внутреннего сгорания поршневые с воспламенением от сжатия (дизели или полудизели)",
        "Источник: https://www.consultant.ru/document/cons_doc_LAW_397176/d2e18fa6efe80e1767a0ef5ead02555926ae3f75/",
        "",
        "8418 — Холодильники, морозильники и прочее холодильное или морозильное оборудование; тепловые насосы (кроме 8415)",
        "Источник: https://www.consultant.ru/document/cons_doc_LAW_397176/0cb259f3938f1fa13e47c90bcf5839fa536c6119/",
    ]
    for chunk in chunk_message("\n".join(lines)):
        await message.answer(chunk)


@router.message(Command("history"))
async def history(message: Message) -> None:
    rows = await asyncio.to_thread(get_history, DB_PATH, message.chat.id, 10)
    if not rows:
        await message.answer("История пуста.")
        return

    lines = [f"{created_at} — {mode}: {description}\n{result}" for created_at, mode, description, result in rows]
    for chunk in chunk_message("\n\n".join(lines)):
        await message.answer(chunk)


@router.message(F.text)
async def fallback(message: Message) -> None:
    mode = user_modes.get(message.chat.id)
    text = message.text or ""

    if mode == "expert":
        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "expert", text, response)

        if results[0].code == "0000000000":
            suggestions = await asyncio.to_thread(tnved_suggest_by_text, DB_PATH, text, 5)
            await message.answer(
                f"{response}\n\n{format_suggestions(suggestions)}\n\n"
                "Недостаточно данных для уверенной классификации. Рекомендуем пройти Light-режим: /mode → Light"
            )
            return

        await message.answer(f"{response}\n\nОценка риска неверной классификации: {assess_risk(results[0].confidence)}.")
        return

    await message.answer("Не удалось распознать команду. Используйте /help.")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN через переменную окружения BOT_TOKEN")
    init_db(DB_PATH)
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

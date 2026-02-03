import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

DB_PATH = os.getenv("DB_PATH", "bot.db")


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
    conn.commit()
    conn.close()


def ensure_user(db_path: str, chat_id: int, username: str | None) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE chat_id = ?", (chat_id,))
    existing = cur.fetchone()
    if existing is None:
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


@dataclass
class ClassificationResult:
    code: str
    title: str
    confidence: float
    explanation: str


KEYWORD_MAP = [
    ("мышь", "8471602009", "Части машин и аппаратов для обработки данных"),
    ("аккумулятор", "8507208008", "Аккумуляторы свинцово-кислотные, прочие"),
    ("электромобиль", "8703800005", "Автомобили с гибридным приводом"),
    ("дерево", "4403990000", "Древесина необработанная, прочая"),
    ("бумага", "4707100000", "Бумага и картон для переработки"),
]


def classify(description: str, answers: dict[str, Any] | None = None) -> list[ClassificationResult]:
    lower = description.lower()
    candidates: list[ClassificationResult] = []
    for keyword, code, title in KEYWORD_MAP:
        if keyword in lower:
            candidates.append(
                ClassificationResult(
                    code=code,
                    title=title,
                    confidence=0.7,
                    explanation=f"Ключевое слово: {keyword}",
                )
            )

    if not candidates:
        candidates.append(
            ClassificationResult(
                code="0000000000",
                title="Требуется уточнение классификации",
                confidence=0.45,
                explanation="Недостаточно признаков для уверенной классификации.",
            )
        )
    return candidates


def format_results(results: list[ClassificationResult]) -> str:
    lines = []
    for result in results:
        confidence_pct = int(result.confidence * 100)
        lines.append(
            f"Код {result.code} — {result.title} (вероятность {confidence_pct}%).\n"
            f"Обоснование: {result.explanation}"
        )
    return "\n\n".join(lines)


def assess_risk(confidence: float) -> str:
    if confidence >= 0.8:
        return "низкая"
    if confidence >= 0.6:
        return "средняя"
    return "высокая"


class LightStates(StatesGroup):
    check_code = State()
    name = State()
    has_image = State()
    category = State()
    purpose = State()
    material = State()
    supply_form = State()
    packaging = State()
    additional = State()


router = Router()
user_modes: dict[int, str] = {}


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Light"), KeyboardButton(text="Expert")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    ensure_user(DB_PATH, message.chat.id, message.from_user.username if message.from_user else None)
    await state.clear()
    await message.answer(
        "Здравствуйте! Выберите режим работы: Light (опрос) или Expert (свободный ввод).",
        reply_markup=mode_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n"
        "/start — выбор режима\n"
        "/mode — переключение режима\n"
        "/classify — начать классификацию\n"
        "/check <код> — проверить код\n"
        "/history — история запросов\n"
        "/cancel — завершить диалог"
    )


@router.message(Command("mode"))
async def mode_command(message: Message) -> None:
    await message.answer("Выберите режим: Light или Expert.", reply_markup=mode_keyboard())


@router.message(F.text.lower().in_({"light", "expert"}))
async def set_mode(message: Message) -> None:
    user_modes[message.chat.id] = message.text.lower()
    await message.answer(f"Режим установлен: {message.text}.")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог завершён. Данные сброшены.")


@router.message(Command("classify"))
async def classify_command(message: Message, state: FSMContext) -> None:
    mode = user_modes.get(message.chat.id)
    if not mode:
        await message.answer("Сначала выберите режим: Light или Expert.", reply_markup=mode_keyboard())
        return
    if mode == "light":
        await state.set_state(LightStates.check_code)
        await message.answer("Хотите проверить правильность имеющегося кода ТН ВЭД? (Да/Нет)")
        return
    await message.answer("Введите подробное описание товара одним сообщением.")


@router.message(LightStates.check_code)
async def light_check_code(message: Message, state: FSMContext) -> None:
    await state.update_data(check_code=message.text)
    await state.set_state(LightStates.name)
    await message.answer("Укажите полное наименование товара (официальное название, артикул, модель).")


@router.message(LightStates.name)
async def light_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text)
    await state.set_state(LightStates.has_image)
    await message.answer("Есть ли изображение товара? (Да/Нет)")


@router.message(LightStates.has_image)
async def light_has_image(message: Message, state: FSMContext) -> None:
    await state.update_data(has_image=message.text)
    await state.set_state(LightStates.category)
    await message.answer("К какой категории (разделу/группе) относится товар?")


@router.message(LightStates.category)
async def light_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=message.text)
    await state.set_state(LightStates.purpose)
    await message.answer("Назначение товара?")


@router.message(LightStates.purpose)
async def light_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=message.text)
    await state.set_state(LightStates.material)
    await message.answer("Из какого материала изготовлен товар?")


@router.message(LightStates.material)
async def light_material(message: Message, state: FSMContext) -> None:
    await state.update_data(material=message.text)
    await state.set_state(LightStates.supply_form)
    await message.answer("Форма поставки (готовое изделие/полуфабрикат/запчасть/набор)?")


@router.message(LightStates.supply_form)
async def light_supply_form(message: Message, state: FSMContext) -> None:
    await state.update_data(supply_form=message.text)
    await state.set_state(LightStates.packaging)
    await message.answer("В какой упаковке поставляется товар?")


@router.message(LightStates.packaging)
async def light_packaging(message: Message, state: FSMContext) -> None:
    await state.update_data(packaging=message.text)
    await state.set_state(LightStates.additional)
    await message.answer("Дополнительная информация (сертификаты, функции, составные части).")


@router.message(LightStates.additional)
async def light_additional(message: Message, state: FSMContext) -> None:
    data = await state.update_data(additional=message.text)
    description = " ".join(str(value) for value in data.values())
    results = classify(description, data)
    response = format_results(results)
    risk = assess_risk(results[0].confidence)
    save_query(DB_PATH, message.chat.id, "light", description, response)
    await message.answer(
        f"{response}\n\nОценка риска неверной классификации: {risk}.\n"
        "Неверное декларирование может повлечь штрафы по ст. 16.2 КоАП РФ."
    )
    await state.clear()


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например: /check 8703800005")
        return
    code = command.args.strip()
    description = f"Проверка кода {code}"
    results = classify(code)
    response = format_results(results)
    save_query(DB_PATH, message.chat.id, "check", description, response)
    await message.answer(response)


@router.message(Command("history"))
async def history(message: Message) -> None:
    rows = list(get_history(DB_PATH, message.chat.id, limit=10))
    if not rows:
        await message.answer("История пуста.")
        return
    lines = [
        f"{created_at} — {mode}: {description}\n{result}" for created_at, mode, description, result in rows
    ]
    await message.answer("\n\n".join(lines))


@router.message()
async def fallback(message: Message) -> None:
    mode = user_modes.get(message.chat.id)
    if mode == "expert":
        description = message.text
        results = classify(description)
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        save_query(DB_PATH, message.chat.id, "expert", description, response)
        await message.answer(
            f"{response}\n\nОценка риска неверной классификации: {risk}."
        )
        return
    await message.answer("Не удалось распознать команду. Используйте /help.")


async def main() -> None:
    init_db(DB_PATH)
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

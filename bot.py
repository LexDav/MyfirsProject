import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")

ANALYSIS_TEXT = """1. Юридические риски
Фокус на импорте

Около 85% нормативных актов регулируют импорт или импорт/экспорт одновременно.

Отдельных экспортных регуляций практически нет.

Основные риски сосредоточены при ввозе товаров.

Экспорт можно охватить минимально, без выделения в приоритет.

Вывод для MVP: приоритет — контроль требований при импорте.

Частые изменения законодательства

Перечни и разъяснения ФТС регулярно обновляются.

Приказы корректируются ежегодно.

Классификационные нормы быстро устаревают.

Изменение кода может повлечь изменение ставок пошлин и условий ввоза.

Риск: использование устаревшей информации приводит к финансовым потерям.

Вывод:

необходим механизм постоянного обновления базы данных;

желательно внедрить уведомления об изменениях по кодам пользователя.

Финансовые последствия

В НПА редко указаны прямые финансовые ставки.

Однако изменение кода влияет на размер пошлины.

Разница может составлять 10–12% и более.

Штрафы часто ниже потенциальной экономии, что провоцирует сознательные нарушения.

Вывод:

встроить калькулятор пошлин;

отображать финансовые последствия переклассификации;

демонстрировать реальный риск (доначисление + штраф).

Регуляторы:

Основной контролирующий орган — ФТС РФ.

Практическое применение норм — на уровне таможенных органов.

ЕЭК устанавливает общие правила, но реализация осуществляется ФТС.

Вывод:

опора на официальные данные ФТС и ЕАЭС;

учет региональной практики при необходимости.

Судебная практика

Основные причины споров:

Намеренное занижение пошлин через выбор «выгодного» кода.

Сложность технической классификации (ошибки без умысла).

Статистика:

около 60% дел — в пользу таможни;

около 40% — в пользу бизнеса (при доказанной добросовестности).

2. Торгово-логистические приоритеты
Наиболее рискованные товарные группы

Приоритетные направления для MVP:

Машины и оборудование (HS 84, 85).

Автозапчасти (HS 87).

Химическая продукция (HS 28, 38, 68).

Сложный текстиль (HS 60).

Причины:

высокая сложность классификации;

значительные ставки пошлин;

частые судебные споры.

География рисков

Страны:

Большинство спорных товаров — китайского происхождения.

Китай — крупнейший поставщик оборудования, запчастей и химии.

Рекомендация: запуск MVP с фокусом на импорт из КНР.

Регионы РФ:

Споры распределены по всей стране.

Московский регион — крупнейший центр ВЭД и таможенной практики.

Рекомендация: пилотный запуск в Московском регионе."""

WELCOME_AND_COMMANDS = (
    "Здравствуйте! Выберите режим работы: Light (опрос) или Expert (свободный ввод). "
    "Доступные команды:\n"
    "/start — выбор режима\n"
    "/mode — переключение режима\n"
    "/classify — начать классификацию\n"
    "/check <код> — проверить код\n"
    "/npa — список НПА\n"
    "/npa <номер> — детали НПА\n"
    "/analysis — аналитика по рискам и логистике\n"
    "/history — история запросов\n"
    "/cancel — завершить диалог"
)

KEYWORD_MAP: list[tuple[str, str, str]] = [
    ("мышь", "8471602009", "Части машин и аппаратов для обработки данных"),
    ("аккумулятор", "8507208008", "Аккумуляторы свинцово-кислотные, прочие"),
    ("электромобиль", "8703800005", "Автомобили с гибридным приводом"),
    ("дерево", "4403990000", "Древесина необработанная, прочая"),
    ("бумага", "4707100000", "Бумага и картон для переработки"),
]
CODE_LOOKUP = {code: (title, 0.9, "Код найден в словаре") for _, code, title in KEYWORD_MAP}


@dataclass
class ClassificationResult:
    code: str
    title: str
    confidence: float
    explanation: str


class LightStates(StatesGroup):
    check_code = State()
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


def classify(description: str) -> list[ClassificationResult]:
    lower = description.lower()
    found = [
        ClassificationResult(code=code, title=title, confidence=0.7, explanation=f"Ключевое слово: {keyword}")
        for keyword, code, title in KEYWORD_MAP
        if keyword in lower
    ]
    if found:
        return found
    return [
        ClassificationResult(
            code="0000000000",
            title="Требуется уточнение классификации",
            confidence=0.45,
            explanation="Недостаточно признаков для уверенной классификации.",
        )
    ]


def format_results(results: list[ClassificationResult]) -> str:
    lines = []
    for result in results:
        lines.append(
            f"Код {result.code} — {result.title} (вероятность {int(result.confidence * 100)}%).\n"
            f"Обоснование: {result.explanation}"
        )
    return "\n\n".join(lines)


def assess_risk(confidence: float) -> str:
    if confidence >= 0.8:
        return "низкая"
    if confidence >= 0.6:
        return "средняя"
    return "высокая"


def chunk_message(text: str, chunk_size: int = 3500) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Light"), KeyboardButton(text="Expert")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


router = Router()
user_modes: dict[int, str] = {}
welcome_message_ids: dict[int, int] = {}


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

    existing_message_id = welcome_message_ids.get(message.chat.id)
    if existing_message_id:
        await message.answer("Приветственное сообщение уже показано выше. Выберите режим: Light или Expert.", reply_markup=mode_keyboard())
        return

    sent = await message.answer(WELCOME_AND_COMMANDS, reply_markup=mode_keyboard())
    welcome_message_ids[message.chat.id] = sent.message_id

    try:
        await message.bot.pin_chat_message(message.chat.id, sent.message_id, disable_notification=True)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


@router.message(F.pinned_message)
async def cleanup_pin_service_message(message: Message) -> None:
    """Try to remove service message created by pinning."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(WELCOME_AND_COMMANDS)


@router.message(Command("mode"))
async def mode_command(message: Message) -> None:
    await message.answer("Выберите режим: Light или Expert.", reply_markup=mode_keyboard())


@router.message(F.text.lower().in_({"light", "expert"}))
async def set_mode(message: Message) -> None:
    user_modes[message.chat.id] = message.text.lower()
    await message.answer(f"Режим установлен: {message.text}.")


@router.message(Command("analysis"))
async def analysis_command(message: Message) -> None:
    for chunk in chunk_message(ANALYSIS_TEXT):
        await message.answer(chunk)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог завершён. Данные сброшены.")


async def send_classification(message: Message, description: str, mode: str) -> None:
    results = classify(description)
    response = format_results(results)
    risk = assess_risk(results[0].confidence)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode, description, response)
    await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")


@router.message(Command("classify"))
async def classify_command(message: Message, state: FSMContext, command: CommandObject) -> None:
    mode = user_modes.get(message.chat.id)
    if command.args:
        await send_classification(message, command.args.strip(), mode or "quick")
        return

    if mode == "light":
        await state.set_state(LightStates.check_code)
        await message.answer("Хотите проверить правильность имеющегося кода ТН ВЭД? (Да/Нет)")
        return

    if mode is None:
        user_modes[message.chat.id] = "expert"
    await message.answer("Введите описание товара одним сообщением (например: электромобиль).")


@router.message(LightStates.check_code)
async def light_check_code(message: Message, state: FSMContext) -> None:
    answer = (message.text or "").strip().lower()
    if answer in {"да", "yes", "y"}:
        await state.set_state(LightStates.code_input)
        await message.answer("Введите код ТН ВЭД (10 цифр).")
        return
    if answer in {"нет", "no", "n"}:
        await state.set_state(LightStates.name)
        await message.answer("Укажите полное наименование товара.")
        return
    await message.answer('Пожалуйста, ответьте "Да" или "Нет".')


@router.message(LightStates.code_input)
async def light_code_input(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not (code.isdigit() and len(code) == 10):
        await message.answer("Код должен состоять из 10 цифр. Попробуйте еще раз.")
        return

    if code in CODE_LOOKUP:
        title, conf, explanation = CODE_LOOKUP[code]
        results = [ClassificationResult(code, title, conf, explanation)]
    else:
        results = classify(code)

    response = format_results(results)
    risk = assess_risk(results[0].confidence)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light_check", f"Проверка кода {code}", response)
    await message.answer(
        f"{response}\n\nОценка риска неверной классификации: {risk}.\n"
        "Неверное декларирование может повлечь штрафы по ст. 16.2 КоАП РФ."
    )
    await state.clear()


@router.message(LightStates.name)
async def light_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text)
    await state.set_state(LightStates.has_image)
    await message.answer("Есть ли изображение товара? (Да/Нет)")


@router.message(LightStates.has_image)
async def light_has_image(message: Message, state: FSMContext) -> None:
    await state.update_data(has_image=message.text)
    await state.set_state(LightStates.category)
    await message.answer("К какой категории относится товар?")


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
    await message.answer("Форма поставки?")


@router.message(LightStates.supply_form)
async def light_supply_form(message: Message, state: FSMContext) -> None:
    await state.update_data(supply_form=message.text)
    await state.set_state(LightStates.packaging)
    await message.answer("В какой упаковке поставляется товар?")


@router.message(LightStates.packaging)
async def light_packaging(message: Message, state: FSMContext) -> None:
    await state.update_data(packaging=message.text)
    await state.set_state(LightStates.additional)
    await message.answer("Дополнительная информация.")


@router.message(LightStates.additional)
async def light_additional(message: Message, state: FSMContext) -> None:
    data = await state.update_data(additional=message.text)
    description = " ".join(
        str(data.get(key, ""))
        for key in ("name", "category", "purpose", "material", "supply_form", "packaging", "additional")
        if data.get(key)
    )
    await send_classification(message, description, "light")
    await state.clear()


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например, /check 8703800005")
        return

    code = command.args.strip()
    if not (code.isdigit() and len(code) == 10):
        await message.answer("Код должен состоять из 10 цифр. Попробуйте еще раз.")
        return

    if code in CODE_LOOKUP:
        title, conf, explanation = CODE_LOOKUP[code]
        result_list = [ClassificationResult(code, title, conf, explanation)]
    else:
        result_list = classify(code)

    response = format_results(result_list)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "check", f"Проверка кода {code}", response)
    await message.answer(response)


@router.message(Command("npa"))
async def npa_command(message: Message) -> None:
    await message.answer("Список НПА доступен в полной версии прототипа.")


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
    if mode == "expert":
        await send_classification(message, message.text or "", "expert")
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

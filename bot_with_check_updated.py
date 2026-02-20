import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from dotenv import load_dotenv
 
load_dotenv()  # загружает переменные из .env, если файл существует

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# =========================
# CONFIG / CONSTANTS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")

# OpenAI (опционально)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
USE_OPENAI = bool(OPENAI_API_KEY)

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

WELCOME_AND_COMMANDS = (
    "Здравствуйте! Я помогаю подобрать код ТН ВЭД ЕАЭС для вашего товара.\n"
    "Выберите режим работы: Light (пошаговый опросник) или Expert (свободный ввод описания).\n\n"
    "Доступные команды:\n"
    "/start — выбор режима\n"
    "/mode — переключение режима\n"
    "/classify — начать классификацию\n"
    "/check <код> — проверить любой 10-значный код ТН ВЭД\n"
    "/suggest <описание> — подобрать 3–5 кодов по описанию\n"
    "/analysis — анализ правовых рисков и применимые НПА\n"
    "/history — история запросов\n"
    "/cancel — завершить диалог\n\n"
    "Нормативная база: Решение Совета ЕЭК от 14.09.2021 № 80 (ТН ВЭД ЕАЭС, ред. 26.09.2025).\n"
    "Результаты носят информационный характер. /analysis — подробнее о правовых рисках."
)

LEGAL_DISCLAIMER = (
    "\n\n⚠️ Внимание: результат носит информационный характер и не является "
    "официальным решением таможенного органа о классификации товара. "
    "Для получения обязательного предварительного решения обратитесь в ФТС России "
    "(Приказ Минфина от 01.09.2020 № 181н)."
)

ANALYSIS_TEXT = """
📋 Анализ правовых рисков при классификации товаров по ТН ВЭД

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. ПРАВОВЫЕ РИСКИ НЕВЕРНОЙ КЛАССИФИКАЦИИ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Неправильно присвоенный код ТН ВЭД влечёт:
• Доначисление таможенных пошлин, НДС, акцизов и пеней.
• Административную ответственность по ст. 16.2 КоАП РФ (недостоверное декларирование) — штраф до двукратного размера стоимости товара.
• При крупном размере — уголовную ответственность (ст. 194 УК РФ, уклонение от уплаты таможенных платежей).
• Задержку товара на таможне и его арест.

Основной НПА: Решение Совета ЕЭК от 14.09.2021 № 80 — утверждает ТН ВЭД ЕАЭС и Единый таможенный тариф ЕАЭС. Именно по этому документу определяются коды и ставки пошлин.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. ЭКСПОРТНЫЕ ОГРАНИЧЕНИЯ И ЗАПРЕТЫ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

С 2022 года действуют масштабные ограничения на вывоз товаров из РФ:

• Указ Президента РФ от 08.03.2022 № 100 — базовый документ, устанавливающий специальные экономические меры в сфере ВЭД.
• Постановление Правительства РФ от 09.03.2022 № 311 — перечень товаров, запрещённых к вывозу до 31.12.2027.
• Постановление Правительства РФ от 09.03.2022 № 313 — перечень государств, в которые запрещён вывоз отдельных товаров.
• Постановление Правительства РФ от 09.03.2022 № 312 — разрешительный порядок вывоза отдельных товаров.

Риск: если товар попадает в запретный перечень, его вывоз невозможен вне зависимости от присвоенного кода. Код ТН ВЭД — ключевой идентификатор для применения этих ограничений.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. СТАВКИ ВВОЗНЫХ ТАМОЖЕННЫХ ПОШЛИН
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Единый таможенный тариф ЕАЭС (утверждён Решением Совета ЕЭК № 80) — стандартные ставки.
• Постановление Правительства РФ от 07.12.2022 № 2240 — повышенные ставки ввозных пошлин на товары из «недружественных» государств.

Ставки варьируются в зависимости от кода ТН ВЭД: один и тот же товар при разной классификации может облагаться пошлиной 0%, 5%, 10% или более.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. ПРОЦЕДУРА ПРЕДВАРИТЕЛЬНОГО РЕШЕНИЯ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Чтобы исключить риск оспаривания классификации, участники ВЭД вправе получить официальное предварительное решение:

• Приказ Минфина России от 01.09.2020 № 181н — устанавливает порядок подачи заявления и его рассмотрения таможенными органами.
• Статьи 21–27 ТК ЕАЭС — регулируют выдачу, действие, изменение и отзыв предварительных решений.
• Срок действия предварительного решения — 3 года с даты принятия.

Предварительное решение обязательно для таможенных органов и защищает декларанта от претензий.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. ПРАВИЛА ИНТЕРПРЕТАЦИИ ТН ВЭД
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Классификация осуществляется по Основным правилам интерпретации (ОПИ) ТН ВЭД:

• Решение Комиссии Таможенного союза от 28.01.2011 № 522 — Положение о порядке применения ОПИ.
• Рекомендация Коллегии ЕЭК от 07.11.2017 № 21 — Пояснения к ТН ВЭД ЕАЭС с детальными критериями классификации товаров.
• Приказ ФТС России от 17.11.2021 № 995 (в ред. Приказа ФТС от 08.07.2025 № 635) — разъяснения о классификации отдельных видов товаров.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ ВАЖНО
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Данный бот предоставляет информационную помощь в подборе кода ТН ВЭД. Результаты классификации не являются официальным решением таможенного органа. При возникновении сомнений рекомендуется обратиться к таможенному представителю или запросить предварительное решение в ФТС России.
"""

CODE10_RE = re.compile(r"\b(\d{10})\b")

# =========================
# DATA STRUCTURES
# =========================

@dataclass
class ClassificationResult:
    code: str
    title: str
    confidence: float
    explanation: str


class LightStates(StatesGroup):
    category = State()   # шаг 1: группа товара (кнопки)
    usage = State()      # шаг 2: сфера применения (кнопки)
    params = State()     # шаг 3: технические параметры (свободный текст)


# =========================
# DB HELPERS
# =========================

def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            username TEXT,
            registered_at TEXT,
            mode TEXT
        )
        """
    )
    # Миграция для старых БД: добавляем колонку mode, если её нет
    cur.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in cur.fetchall()}
    if "mode" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN mode TEXT")
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
    # таблица с кодами
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tnved (
            code TEXT PRIMARY KEY,
            title TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_queries_chat_id_id
        ON queries(chat_id, id DESC)
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
            "INSERT INTO users (chat_id, username, registered_at, mode) VALUES (?, ?, ?, ?)",
            (chat_id, username, datetime.now(timezone.utc).isoformat(), "expert"),
        )
    elif username:
        cur.execute("UPDATE users SET username = ? WHERE chat_id = ?", (username, chat_id))
    conn.commit()
    conn.close()


def get_user_mode(db_path: str, chat_id: int) -> Optional[str]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT mode FROM users WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return row[0] or None


def set_user_mode(db_path: str, chat_id: int, mode: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (chat_id, username, registered_at, mode) VALUES (?, ?, ?, ?)",
            (chat_id, None, datetime.now(timezone.utc).isoformat(), mode),
        )
    else:
        cur.execute("UPDATE users SET mode = ? WHERE chat_id = ?", (mode, chat_id))
    conn.commit()
    conn.close()


def save_query(db_path: str, chat_id: int, mode: str, description: str, result: str) -> None:
    max_desc = 1000
    max_result = 4000
    safe_description = (description or "")[:max_desc]
    safe_result = (result or "")[:max_result]

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queries (chat_id, mode, description, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, mode, safe_description, safe_result, datetime.now(timezone.utc).isoformat()),
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


def tnved_random(db_path: str, limit: int = 5) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT code, title FROM tnved ORDER BY RANDOM() LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def _tokenize(text: str) -> list[str]:
    # примитивная токенизация: слова/цифры, длина >= 3
    raw = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", (text or "").lower())
    toks = [t for t in raw if len(t) >= 3]
    # чуть-чуть подчистим общеупотребимое
    stop = {"это", "для", "как", "или", "иное", "прочие", "прочее", "такой", "такие", "товар", "изделие"}
    return [t for t in toks if t not in stop]


# Технические слова, присутствие которых говорит об осмысленном описании
_TECH_KEYWORDS = {
    "квт", "кВт", "л.с", "мощност", "объём", "объем", "дизел", "бензин",
    "компрессор", "холодильн", "морозильн", "рефрижератор", "температур",
    "цилиндр", "топлив", "промышленн", "бытов", "транспорт", "морск",
    "сельхоз", "литр", "тип", "марка", "модел", "серийн", "артикул",
}


def assess_expert_input(text: str) -> bool:
    """
    Возвращает True, если описания достаточно для попытки классификации.

    Критерии достаточности (все три должны выполняться):
    1. Длина текста >= 20 символов.
    2. Не менее 3 содержательных токенов (без стоп-слов).
    3. Есть хотя бы один числовой фрагмент (мощность, объём, год…)
       ИЛИ хотя бы одно техническое ключевое слово.
    """
    stripped = (text or "").strip()

    # 1. Минимальная длина
    if len(stripped) < 20:
        return False

    # 2. Минимальное количество значимых слов
    tokens = _tokenize(stripped)
    if len(tokens) < 3:
        return False

    # 3. Техническая конкретика: число или тех.термин
    has_number = bool(re.search(r"\d+", stripped))
    text_lower = stripped.lower()
    has_tech = any(kw.lower() in text_lower for kw in _TECH_KEYWORDS)

    return has_number or has_tech


def tnved_search_candidates(db_path: str, query_text: str, limit: int = 12) -> list[tuple[str, str]]:
    """
    Достаём кандидатов из БД по словам пользователя.
    Это НЕ финальная классификация — только список возможных кодов.
    """
    tokens = _tokenize(query_text)
    if not tokens:
        return tnved_random(db_path, limit=min(limit, 5))

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # строим WHERE title LIKE ? OR title LIKE ? ...
    where = " OR ".join(["title LIKE ?"] * min(len(tokens), 6))  # ограничим 6 токенами
    params = [f"%{t}%" for t in tokens[:6]]

    sql = f"""
        SELECT code, title
        FROM tnved
        WHERE {where}
        LIMIT ?
    """
    cur.execute(sql, (*params, limit))
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


# =========================
# OpenAI helper (optional)
# =========================

def llm_rank_candidates(user_text: str, candidates: list[tuple[str, str]], top_k: int = 5) -> list[tuple[str, str, str]]:
    """
    Возвращает список (code, title, reason) длиной <= top_k.
    Если OpenAI не настроен — вернёт первые top_k кандидатов без причин.
    """
    if not candidates:
        return []

    if not USE_OPENAI:
        return [(c, t, "Подбор по ключевым словам (без LLM).") for c, t in candidates[:top_k]]

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("Библиотека openai не установлена. Запустите: pip install openai")
        return [(c, t, "LLM недоступен (нет библиотеки openai).") for c, t in candidates[:top_k]]

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Ужимаем список кандидатов в JSON
    cand_payload = [{"code": c, "title": t} for c, t in candidates[:20]]

    instructions = (
        "Ты помощник по классификации ТН ВЭД ЕАЭС. "
        "Нужно выбрать наиболее подходящие коды только из списка кандидатов. "
        "Верни ТОЛЬКО JSON (без текста вокруг) формата: "
        '{"items":[{"code":"...","reason":"..."}]}. '
        "items должен содержать 3-5 элементов. "
        "Причина короткая (1 строка) и основана на признаках из описания пользователя и названии позиции."
    )

    user_input = {
        "user_text": user_text,
        "candidates": cand_payload
    }

    response_schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": top_k,
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["code", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=json.dumps(user_input, ensure_ascii=False),
            instructions=instructions,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "tnved_rank_candidates",
                    "schema": response_schema,
                    "strict": True,
                }
            },
        )
    except Exception as e:
        logger.error("Ошибка OpenAI API: %s", e)
        return [(c, t, "LLM временно недоступен, показаны кандидаты по БД.") for c, t in candidates[:top_k]]

    # output_text — стандартное поле Responses API
    text = getattr(resp, "output_text", None)
    if not text:
        try:
            text = json.dumps(resp.model_dump(), ensure_ascii=False)
        except (TypeError, ValueError):
            text = ""

    # Пытаемся извлечь JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # если модель добавила лишнее — берём первый валидный JSON-блок
        j1 = text.find("{")
        j2 = text.rfind("}")
        if j1 != -1 and j2 != -1 and j2 > j1:
            try:
                data = json.loads(text[j1 : j2 + 1])
            except json.JSONDecodeError:
                logger.warning("Не удалось распарсить ответ LLM: %s", text[:200])
                return [(c, t, "LLM ответ не распознан, показаны кандидаты по БД.") for c, t in candidates[:top_k]]
        else:
            logger.warning("LLM вернул неожиданный формат: %s", text[:200])
            return [(c, t, "LLM ответ не распознан, показаны кандидаты по БД.") for c, t in candidates[:top_k]]

    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        logger.warning("LLM вернул items не-массив: %r", type(items))
        return [(c, t, "LLM ответ не распознан, показаны кандидаты по БД.") for c, t in candidates[:top_k]]

    chosen = []
    cand_map = {c: t for c, t in candidates}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code", "")).strip()
        reason = str(it.get("reason", "")).strip() or "—"
        if re.fullmatch(r"\d{10}", code) and code in cand_map:
            chosen.append((code, cand_map[code], reason))
        if len(chosen) >= top_k:
            break

    if not chosen:
        return [(c, t, "Подбор по ключевым словам (LLM не выбрал коды).") for c, t in candidates[:top_k]]

    return chosen


# =========================
# CLASSIFICATION LOGIC (MVP)
# =========================

def classify_text_with_db(db_path: str, text: str) -> list[ClassificationResult]:
    """
    Проверяет, содержит ли текст 10-значный код ТН ВЭД, и ищет его в БД.
    Работает с любым кодом ТН ВЭД (не ограничен группами 8408/8418).
    """
    raw = (text or "").strip()

    # 1) если пользователь ввёл ровно 10 цифр
    if raw.isdigit() and len(raw) == 10:
        title = tnved_get_by_code(db_path, raw)
        if title:
            return [ClassificationResult(raw, title, 0.92, "Код найден в классификаторе (БД)")]
        return [ClassificationResult(raw, "Код не найден в вашей БД (tnved).", 0.35, "Нет записи в таблице tnved")]

    # 2) если код "спрятан" в тексте
    # Дополнительная проверка: первые 2 цифры должны быть в диапазоне 01–97 (группы ТН ВЭД),
    # чтобы не ловить номера телефонов и другие 10-значные числа.
    m = CODE10_RE.search(raw)
    if m:
        code10 = m.group(1)
        group = int(code10[:2])
        if 1 <= group <= 97:
            title = tnved_get_by_code(db_path, code10)
            if title:
                return [ClassificationResult(code10, title, 0.90, "Код найден в тексте и подтверждён БД")]
            return [ClassificationResult(code10, "Код найден в тексте, но отсутствует в БД (tnved).", 0.35, "Нет записи")]

    # 3) иначе — код не обнаружен, нужен подбор
    return [
        ClassificationResult(
            "0000000000",
            "Требуется уточнение классификации",
            0.45,
            "Недостаточно признаков: нужен 10-значный код или технические параметры.",
        )
    ]


def format_results(results: list[ClassificationResult]) -> str:
    lines: list[str] = []
    for r in results:
        pct = int(r.confidence * 100 + 0.5)
        lines.append(f"Код {r.code} — {r.title} (вероятность {pct}%).\nОбоснование: {r.explanation}")
    return "\n\n".join(lines) + LEGAL_DISCLAIMER


def assess_risk(confidence: float) -> str:
    if confidence >= 0.8:
        return "низкая"
    if confidence >= 0.6:
        return "средняя"
    return "высокая"


def chunk_message(text: str, chunk_size: int = 3500) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


async def suggest_codes_flow(message: Message, user_text: str) -> None:
    """
    1) достаём кандидатов из БД
    2) (если есть OpenAI) ранжируем
    3) выдаём пользователю 3–5 вариантов
    """
    candidates = await asyncio.to_thread(tnved_search_candidates, DB_PATH, user_text, 15)
    if not candidates:
        rnd = await asyncio.to_thread(tnved_random, DB_PATH, 5)
        if not rnd:
            await message.answer("В таблице tnved нет кодов. Сначала импортируйте коды в bot.db.")
            return
        text = "Не удалось подобрать по описанию. Примеры кодов из базы:\n"
        text += "\n".join([f"{c} — {t}" for c, t in rnd])
        await message.answer(text)
        return

    ranked = await asyncio.to_thread(llm_rank_candidates, user_text, candidates, 5)

    lines = ["Возможные коды по вашему описанию (предварительно):"]
    for code, title, reason in ranked:
        lines.append(f"{code} — {title}\nПочему: {reason}")

    lines.append(
        "\nВажно: это предварительная подсказка по описанию. "
        "Для точной классификации необходимы технические параметры "
        "(тип, назначение, мощность/объём, материал и т.п.) и товаросопроводительные документы."
        + LEGAL_DISCLAIMER
    )

    await message.answer("\n\n".join(lines))


# =========================
# UI helpers
# =========================

def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Строит InlineKeyboardMarkup из списка рядов [(label, callback_data), ...]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def category_keyboard() -> InlineKeyboardMarkup:
    return _inline([
        [("Двигатель внутреннего сгорания (гр. 8408)", "cat:8408")],
        [("Холодильное / морозильное оборудование (гр. 8418)", "cat:8418")],
        [("Другой товар / не знаю", "cat:other")],
    ])


def usage_keyboard_8408() -> InlineKeyboardMarkup:
    return _inline([
        [("Промышленное оборудование", "use:industrial"), ("Морское / речное судно", "use:marine")],
        [("Транспортное средство", "use:transport"), ("Сельхозтехника", "use:agriculture")],
        [("Другое / не знаю", "use:other")],
    ])


def usage_keyboard_8418() -> InlineKeyboardMarkup:
    return _inline([
        [("Бытовой холодильник / морозильник", "use:household")],
        [("Промышленное / торговое оборудование", "use:commercial")],
        [("Транспортный рефрижератор", "use:transport")],
        [("Другое / не знаю", "use:other")],
    ])


def usage_keyboard_other() -> InlineKeyboardMarkup:
    return _inline([
        [("Промышленное", "use:industrial"), ("Бытовое", "use:household")],
        [("Другое / не знаю", "use:other")],
    ])


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Light"), KeyboardButton(text="Expert")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def switch_to_light_keyboard() -> InlineKeyboardMarkup:
    """Кнопки при недостаточном описании в Expert-режиме."""
    return _inline([
        [("Перейти в Light-режим (опросник)", "switch:light")],
        [("Дополнить описание и попробовать снова", "switch:retry")],
    ])


# =========================
# Router / state
# =========================

router = Router()


# =========================
# Handlers
# =========================

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


@router.message(F.text.lower().in_({"light", "expert"}))
async def set_mode(message: Message, state: FSMContext) -> None:
    selected_mode = (message.text or "").lower()
    await asyncio.to_thread(set_user_mode, DB_PATH, message.chat.id, selected_mode)
    await message.answer(f"Режим установлен: {message.text}.")
    if selected_mode == "light":
        await light_start(message, state)


@router.message(Command("analysis"))
async def analysis_command(message: Message) -> None:
    for chunk in chunk_message(ANALYSIS_TEXT):
        await message.answer(chunk)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог завершён. Данные сброшены.")


@router.message(Command("history"))
async def history(message: Message) -> None:
    rows = await asyncio.to_thread(get_history, DB_PATH, message.chat.id, 10)
    if not rows:
        await message.answer("История пуста.")
        return
    lines = [f"{created_at} — {mode}: {description}\n{result}" for created_at, mode, description, result in rows]
    for chunk in chunk_message("\n\n".join(lines)):
        await message.answer(chunk)


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например, /check 8408101100")
        return

    code = command.args.strip()
    if not (code.isdigit() and len(code) == 10):
        await message.answer("Код должен содержать ровно 10 цифр. Пример: /check 8408101100")
        return

    title = await asyncio.to_thread(tnved_get_by_code, DB_PATH, code)
    if title:
        results = [ClassificationResult(code, title, 0.92, "Код найден в классификаторе (БД)")]
    else:
        results = [ClassificationResult(code, "Код не найден в вашей БД (tnved).", 0.35, "Нет записи в tnved")]

    response = format_results(results)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "check", f"Проверка кода {code}", response)
    await message.answer(response)


@router.message(Command("suggest"))
async def suggest_command(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Напишите: /suggest <описание товара>. Например: /suggest двигатель для морского судна")
        return
    text = command.args.strip()
    await suggest_codes_flow(message, text)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "suggest", text, "OK")


@router.message(Command("classify"))
async def classify_command(message: Message, state: FSMContext, command: CommandObject) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id)

    if command.args:
        text = command.args.strip()

        if not assess_expert_input(text):
            await message.answer(
                "Описания недостаточно для классификации.\n\n"
                "Для точного подбора кода ТН ВЭД нужны технические детали: "
                "мощность, объём, тип применения, материал и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return

        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode or "quick", text, response)

        await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
        # если точного кода нет — подскажем варианты
        if results[0].code == "0000000000":
            await suggest_codes_flow(message, text)
        return

    if mode == "light":
        await light_start(message, state)
        return

    if mode is None:
        await asyncio.to_thread(set_user_mode, DB_PATH, message.chat.id, "expert")
    await message.answer(
        "Expert режим: пришлите описание товара (можно с тех.характеристиками). "
        "Если знаете 10-значный код ТН ВЭД — вставьте его в текст."
    )


# =========================
# Light flow
# =========================

# --- Шаг 1: категория (запускается из /classify или set_mode) ---

async def light_start(message: Message, state: FSMContext) -> None:
    """Точка входа в Light-режим: показываем выбор группы товара."""
    await state.set_state(LightStates.category)
    await message.answer(
        "Шаг 1 из 3 — Группа товара.\nВыберите, что ближе всего описывает ваш товар:",
        reply_markup=category_keyboard(),
    )


@router.callback_query(LightStates.category, F.data.startswith("cat:"))
async def light_category_cb(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.split(":", 1)[1]  # "8408" / "8418" / "other"

    labels = {"8408": "Двигатель внутреннего сгорания (гр. 8408)",
               "8418": "Холодильное/морозильное оборудование (гр. 8418)",
               "other": "Другой товар"}
    await state.update_data(category=labels.get(cat, cat))
    await callback.answer()

    await state.set_state(LightStates.usage)

    if cat == "8408":
        kb = usage_keyboard_8408()
    elif cat == "8418":
        kb = usage_keyboard_8418()
    else:
        kb = usage_keyboard_other()

    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 2 из 3 — Сфера применения.\nДля чего предназначен товар?",
        reply_markup=kb,
    )


@router.callback_query(LightStates.usage, F.data.startswith("use:"))
async def light_usage_cb(callback: CallbackQuery, state: FSMContext) -> None:
    use = callback.data.split(":", 1)[1]

    labels = {
        "industrial": "промышленное применение",
        "marine": "морское/речное судно",
        "transport": "транспортное средство",
        "agriculture": "сельскохозяйственная техника",
        "household": "бытовое использование",
        "commercial": "промышленное/торговое оборудование",
        "other": "иное применение",
    }
    await state.update_data(usage=labels.get(use, use))
    await callback.answer()

    await state.set_state(LightStates.params)
    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 3 из 3 — Технические параметры.\n"
        "Укажите характеристики, которые знаете:\n"
        "• для двигателей: мощность (кВт/л.с.), тип топлива, рабочий объём\n"
        "• для холодильного оборудования: объём камеры (л), тип (компрессорный/абсорбционный), температурный режим\n"
        "• для других товаров: любые технические детали, материал, назначение\n\n"
        "Можно написать коротко — главное, что знаете."
    )


@router.message(LightStates.params)
async def light_params(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, введите текстовое описание параметров.")
        return
    await state.update_data(params=message.text)
    data = await state.get_data()

    # Собираем профиль из всех шагов
    parts = [
        data.get("category", ""),
        data.get("usage", ""),
        data.get("params", ""),
    ]
    profile = " ".join(p for p in parts if p).strip()

    await message.answer("Анализирую данные, подбираю коды ТН ВЭД…")
    await suggest_codes_flow(message, profile)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light", profile, "suggest_codes_flow")

    await state.clear()


# =========================
# Expert: переключение в Light
# =========================

@router.callback_query(F.data.startswith("switch:"))
async def switch_mode_cb(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "light":
        await asyncio.to_thread(set_user_mode, DB_PATH, callback.message.chat.id, "light")  # type: ignore[union-attr]
        await light_start(callback.message, state)  # type: ignore[arg-type]
    else:
        await callback.message.answer(  # type: ignore[union-attr]
            "Хорошо! Добавьте к описанию технические параметры:\n"
            "мощность (кВт/л.с.), объём (л), тип применения, страна происхождения.\n"
            "Чем конкретнее — тем точнее результат."
        )


# =========================
# Expert fallback
# =========================

@router.message(F.text)
async def fallback(message: Message) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id)

    if mode == "expert":
        text = message.text or ""

        if not assess_expert_input(text):
            await message.answer(
                "Описания недостаточно для классификации.\n\n"
                "Для точного подбора кода ТН ВЭД нужны технические детали: "
                "мощность, объём, тип применения, материал и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return

        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "expert", text, response)

        await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
        if results[0].code == "0000000000":
            await suggest_codes_flow(message, text)
        return

    await message.answer("Не удалось распознать команду. Используйте /help.")


# =========================
# MAIN
# =========================

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Создайте файл .env на основе .env.example "
            "или установите переменную окружения BOT_TOKEN."
        )

    init_db(DB_PATH)
    logger.info("БД инициализирована: %s", DB_PATH)
    logger.info("OpenAI: %s", "включён, модель=" + OPENAI_MODEL if USE_OPENAI else "отключён")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен, ожидаю сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""
tg_bot_full.py -- Telegram bot for TН ВЭД classification with extended knowledge base

Этот модуль реализует телеграм‑бота, который загружает все markdown
файлы из каталога проекта в память, использует справочник ТН ВЭД (ru.84)
для поиска кодов и применяет упрощённый алгоритм классификации,
основанный на системной инструкции `system_prompt_tn_ved_v5.md`.

Основные отличия от первоначальной версии:
• При старте бот загружает все файлы `*.md` из текущего каталога
  (например, Podbor_NPA.md, TECH_AUDIT.md) в переменную `knowledge_base`.  
  Эти данные доступны для справочных команд, но напрямую не участвуют
  в поиске кодов.  
• В Light‑режиме шаг 2 переформулирован: бот спрашивает не только
  сферу применения, но и назначение товара. В ответ пользователю
  предлагаются кнопки с более внятными вариантами.  
• Результаты классификации ограничиваются тремя вариантами (высокая,
  средняя, низкая вероятность). Первый кандидат маркируется
  как «Высокая» вероятность, второй — «Средняя», третий — «Низкая».  
• Формат ответа дополнен колонкой «Вероятность».

Данный файл сохраняет большую часть оригинальной логики (работа с БД,
FSM, обработчики команд), но добавляет новую функцию
`load_markdown_files`, изменённый алгоритм подбора и обновлённые
шаблоны сообщений в Light‑режиме.

ПРИМЕЧАНИЕ: Полное следование Основным правилам интерпретации (ОПИ)
требует сложной логики. Здесь реализована упрощённая версия,
опирающаяся на ранжирование кандидатов через OpenAI (если доступен)
или поиск по ключевым словам. При необходимости этот модуль может
быть расширен.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Optional

from dotenv import load_dotenv

load_dotenv()

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
TNVED_JSON_PATH = os.getenv("TNVED_JSON_PATH", "ru.84_2022_21.09.2025.json")
LEGACY_RU84_MD_PATH = os.getenv("RU84_PATH", "ru.84_2022_21.09.2025.md")

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
    "/check <код> — проверить любой 10‑значный код ТН ВЭД\n"
    "/suggest <описание> — подобрать 3–5 кодов по описанию\n"
    "/analysis — анализ правовых рисков и применимые НПА\n"
    "/history — история запросов\n"
    "/cancel — завершить диалог\n\n"
    "/reimport — обновить коды из JSON-справочника ТН ВЭД в базе\n\n"
    "Нормативная база: Решение Совета ЕЭК от 14.09.2021 № 80 (ред. 26.09.2025).\n"
    "Результаты носят информационный характер. /analysis — подробнее о правовых рисках."
)

LEGAL_DISCLAIMER = (
    "\n\n⚠️ Внимание: результат носит информационный характер и не является "
    "официальным решением таможенного органа о классификации товара. "
    "Для получения обязательного предварительного решения обратитесь в ФТС России "
    "(Приказ Минфина от 01.09.2020 № 181н)."
)

# =========================
# DATA STRUCTURES
# =========================


@dataclass
class ClassificationResult:
    code: str
    title: str
    confidence: float
    explanation: str


@dataclass(frozen=True)
class RU84Row:
    code: str
    title: str
    unit: str
    duty_rate: str
    line_no: int


class LightStates(StatesGroup):
    category = State()  # шаг 1: группа товара
    usage = State()     # шаг 2: назначение/сфера
    params = State()    # шаг 3: технические параметры


def normalize_tnved_code(value: str | None) -> str:
    """Возвращает код ТН ВЭД только из 10 цифр, если он есть в строке."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits if len(digits) == 10 else ""


def format_tnved_code(code: str | None) -> str:
    """Форматирует 10-значный код ТН ВЭД для показа пользователю."""
    normalized = normalize_tnved_code(code)
    if not normalized:
        return code or ""
    return f"{normalized[:4]} {normalized[4:6]} {normalized[6:9]} {normalized[9:]}"


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


def _normalize_ru84_line(line: str) -> str:
    """Нормализует Markdown-строку таблицы перед разбором."""
    line = line.strip().replace("﻿", "")
    if not line or line.startswith("<!--"):
        return ""
    # Подчищаем артефакты вида |+|..., встречающиеся после OCR/ручной склейки.
    line = re.sub(r"^\|\++\|", "|", line)
    return line


def _split_ru84_parts(line: str) -> list[str]:
    """Безопасно разбивает строку таблицы на ожидаемые 4 столбца."""
    raw_parts = line.split("|")
    if raw_parts and raw_parts[0] == "":
        raw_parts = raw_parts[1:]
    if raw_parts and raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    raw_parts = [part.strip() for part in raw_parts]
    if len(raw_parts) < 4:
        raw_parts.extend([""] * (4 - len(raw_parts)))
    elif len(raw_parts) > 4:
        # Если в наименовании случайно оказался символ '|', собираем среднюю часть обратно.
        raw_parts = [raw_parts[0], " | ".join(raw_parts[1:-2]).strip(), raw_parts[-2], raw_parts[-1]]
    return raw_parts[:4]


def _extract_code10_candidates(cell: str) -> list[str]:
    """Ищет в первом столбце возможные 10-значные коды, сохраняя порядок появления."""
    tokens = re.findall(r"\d+", cell)
    candidates: list[str] = []
    for start in range(len(tokens)):
        first = tokens[start]
        if len(first) == 10:
            candidates.append(first)
            continue
        if len(first) != 4:
            continue
        combined = first
        for end in range(start + 1, min(start + 4, len(tokens))):
            combined += tokens[end]
            if len(combined) == 10:
                candidates.append(combined)
                break
            if len(combined) > 10:
                break
    return candidates


def _is_ru84_header_or_divider(parts: list[str]) -> bool:
    code_cell = (parts[0] or "").strip().lower()
    title_cell = (parts[1] or "").strip().lower()
    if code_cell in {"код тн вэд", "код"} or title_cell in {"код тн вэд", "код"}:
        return True
    return all(not cell.strip() or set(cell.strip()) <= {"-"} for cell in parts)


def _load_ru84_rows_from_json(path: str) -> list[RU84Row]:
    """Загружает канонический JSON-справочник ТН ВЭД."""
    if not os.path.exists(path):
        logger.warning("JSON-справочник не найден: %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Ошибка чтения JSON-справочника %s: %s", path, e)
        return []
    if not isinstance(payload, list):
        logger.error("JSON-справочник %s должен содержать список записей", path)
        return []

    rows: list[RU84Row] = []
    bad_rows = 0
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            bad_rows += 1
            if bad_rows <= 10:
                logger.warning("JSON-справочник: запись %s пропущена, так как она не является объектом", idx)
            continue
        code = normalize_tnved_code(str(item.get("code", "")))
        title = _merge_title_parts(str(item.get("title", "")))
        unit = str(item.get("unit", "")).strip() or "-"
        duty_rate = str(item.get("duty_rate", "")).strip() or "не указана"
        if not code or not title:
            bad_rows += 1
            if bad_rows <= 10:
                logger.warning("JSON-справочник: запись %s пропущена (code=%r, title=%r)", idx, item.get("code"), item.get("title"))
            continue
        rows.append(RU84Row(code=code, title=title, unit=unit, duty_rate=duty_rate, line_no=idx))

    logger.info("JSON-справочник: загружено %s валидных строк (%s пропусков)", len(rows), bad_rows)
    return rows


def load_reference_rows(json_path: str = TNVED_JSON_PATH, md_path: str = LEGACY_RU84_MD_PATH) -> list[RU84Row]:
    """Читает JSON как основной источник и markdown как fallback."""
    rows = _load_ru84_rows_from_json(json_path)
    if rows:
        return rows
    logger.warning("Переключаюсь на fallback-источник markdown: %s", md_path)
    return parse_ru84_rows(md_path)


def _merge_title_parts(*parts: str) -> str:
    pieces = [re.sub(r"\s+", " ", part).strip() for part in parts if part and part.strip()]
    if not pieces:
        return ""
    merged = " ".join(pieces)
    merged = re.sub(r"\s+([,.;:])", r"\1", merged)
    return merged.strip()


def parse_ru84_rows(path: str = LEGACY_RU84_MD_PATH) -> list[RU84Row]:
    """
    Нормализует ru.84 и возвращает только 10-значные строки.

    Особенности:
    - поддерживает строки-продолжения без кода в первом столбце;
    - переживает лишние символы в начале строки;
    - умеет извлекать код, даже если в первой ячейке случайно склеились два кода;
    - логирует проблемные строки вместо молчаливой потери данных.
    """
    rows: list[RU84Row] = []
    if not os.path.exists(path):
        logger.warning("Файл с тарифами не найден: %s", path)
        return rows

    current: dict[str, str | int] | None = None
    pending_prefix_title = ""
    malformed_lines = 0
    continued_lines = 0
    recovered_code_lines = 0

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        code = str(current.get("code", "")).strip()
        title = _merge_title_parts(str(current.get("title", "")))
        unit = str(current.get("unit", "")).strip() or "-"
        duty_rate = str(current.get("duty_rate", "")).strip() or "не указана"
        line_no = int(current.get("line_no", 0) or 0)
        if len(code) != 10:
            logger.warning("ru.84: пропущена строка %s: не удалось собрать 10-значный код (%r)", line_no, code)
        elif not title:
            logger.warning("ru.84: пропущена строка %s: пустое наименование для кода %s", line_no, code)
        else:
            rows.append(RU84Row(code=code, title=title, unit=unit, duty_rate=duty_rate, line_no=line_no))
        current = None

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = _normalize_ru84_line(raw_line)
                if not line.startswith("|"):
                    continue

                parts = _split_ru84_parts(line)
                if _is_ru84_header_or_divider(parts):
                    continue

                code_cell, title_cell, unit_cell, duty_cell = parts
                title_cell = _merge_title_parts(title_cell)
                unit_cell = unit_cell.strip() or "-"
                duty_cell = duty_cell.strip() or "не указана"
                code_candidates = _extract_code10_candidates(code_cell)
                code10 = code_candidates[-1] if code_candidates else ""

                if code10:
                    if len(code_candidates) > 1 or re.sub(r"\D", "", code_cell) != code10:
                        recovered_code_lines += 1
                        if recovered_code_lines <= 10:
                            logger.warning(
                                "ru.84: строка %s содержит неоднозначный код (%r); использован %s",
                                line_no,
                                code_cell,
                                code10,
                            )
                    if pending_prefix_title and (
                        not title_cell
                        or title_cell[:1].islower()
                        or title_cell.lower().startswith(("более", "менее", "не ", "не-", "или ", "для ", "из ", "с "))
                    ):
                        title_cell = _merge_title_parts(pending_prefix_title, title_cell)
                        pending_prefix_title = ""
                    elif title_cell:
                        pending_prefix_title = ""
                    flush_current()
                    current = {
                        "code": code10,
                        "title": title_cell,
                        "unit": unit_cell,
                        "duty_rate": duty_cell,
                        "line_no": line_no,
                    }
                    continue

                # Строка без кода: если это продолжение предыдущего наименования, доклеиваем её.
                if current and not code_cell and title_cell:
                    current_title = str(current.get("title", "")).strip()
                    current_unit = str(current.get("unit", "")).strip()
                    current_duty = str(current.get("duty_rate", "")).strip()
                    continuation_has_payload = unit_cell not in {"", "-"} or duty_cell not in {"", "не указана"}
                    current_looks_incomplete = (
                        current_unit in {"", "-"}
                        or current_duty in {"", "не указана"}
                        or current_title.endswith((":", "-", "(", "для", "из", "на", "от", "до", "более", "менее", "не более", "не менее", "включая", "оснащенные"))
                    )
                    if continuation_has_payload or current_looks_incomplete:
                        current["title"] = _merge_title_parts(current_title, title_cell)
                        if current_unit in {"", "-"} and unit_cell not in {"", "-"}:
                            current["unit"] = unit_cell
                        if current_duty in {"", "не указана"} and duty_cell not in {"", "не указана"}:
                            current["duty_rate"] = duty_cell
                        continued_lines += 1
                        continue

                if title_cell:
                    if code_cell:
                        pending_prefix_title = title_cell
                    else:
                        pending_prefix_title = _merge_title_parts(pending_prefix_title, title_cell)
                    continue

                malformed_lines += 1
                if malformed_lines <= 10:
                    logger.warning("ru.84: пропущена строка %s: не удалось интерпретировать %r", line_no, raw_line.rstrip())
    except OSError as e:
        logger.error("Ошибка чтения файла %s: %s", path, e)
        return []

    flush_current()

    logger.info(
        "ru.84: разобрано %s строк с 10-значными кодами (%s продолжений, %s восстановленных кодов, %s пропусков)",
        len(rows),
        continued_lines,
        recovered_code_lines,
        malformed_lines,
    )
    return rows


@lru_cache(maxsize=4)
def load_ru84_codes(path: str | None = None) -> dict[str, tuple[str, str]]:
    """Загружает словарь: 10‑значный код → (наименование, ставка)."""
    rows = load_reference_rows() if path is None else (
        _load_ru84_rows_from_json(path) if path.lower().endswith(".json") else parse_ru84_rows(path)
    )
    return {row.code: (row.title, row.duty_rate) for row in rows}


def tnved_lookup_with_rate(db_path: str, code: str) -> tuple[str, str]:
    """Возвращает (наименование, ставка) по коду: сначала ru.84, затем БД."""
    ru84 = load_ru84_codes()
    if code in ru84:
        return ru84[code]
    title = tnved_get_by_code(db_path, code) or "Код не найден"
    return title, "нет данных в ru.84"

# =========================
# IMPORT FROM MARKDOWN TO TNVED
# =========================

def tnved_count(db_path: str) -> int:
    """Возвращает количество записей в таблице tnved."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM tnved")
    count = int(cur.fetchone()[0] or 0)
    conn.close()
    return count


def import_ru84_md_to_tnved(db_path: str, md_path: str, clear_mode: str = "none") -> int:
    """Совместимый импорт из markdown‑файла в таблицу tnved."""
    rows = [(row.code, row.title) for row in parse_ru84_rows(md_path)]
    if not rows:
        logger.warning("Не найдено строк для импорта из %s", md_path)
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Очистка при необходимости
    if clear_mode == "full":
        cur.execute("DELETE FROM tnved")
    elif clear_mode == "84":
        cur.execute("DELETE FROM tnved WHERE code LIKE '84%'")
    # Вставка/обновление записей
    cur.executemany(
        "INSERT OR REPLACE INTO tnved(code, title) VALUES(?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def import_ru84_json_to_tnved(db_path: str, json_path: str, clear_mode: str = "none") -> int:
    """Импортирует коды и наименования из JSON-справочника в таблицу tnved."""
    rows = [(row.code, row.title) for row in _load_ru84_rows_from_json(json_path)]
    if not rows:
        logger.warning("Не найдено строк для импорта из JSON %s", json_path)
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if clear_mode == "full":
        cur.execute("DELETE FROM tnved")
    elif clear_mode == "84":
        cur.execute("DELETE FROM tnved WHERE code LIKE '84%'")
    cur.executemany(
        "INSERT OR REPLACE INTO tnved(code, title) VALUES(?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def import_reference_to_tnved(
    db_path: str,
    json_path: str = TNVED_JSON_PATH,
    md_path: str = LEGACY_RU84_MD_PATH,
    clear_mode: str = "none",
) -> int:
    """Импортирует справочник в tnved: сначала из JSON, затем из markdown как fallback."""
    imported = import_ru84_json_to_tnved(db_path, json_path, clear_mode=clear_mode)
    if imported:
        return imported
    logger.warning("Импорт из JSON не удался, использую markdown-файл %s", md_path)
    return import_ru84_md_to_tnved(db_path, md_path, clear_mode=clear_mode)


# =========================
# KNOWLEDGE BASE HELPERS
# =========================


def load_markdown_files(directory: str) -> dict[str, str]:
    """Сканирует каталог и загружает содержимое всех `.md` файлов.
    Возвращает словарь filename → content (строка).
    """
    knowledge: dict[str, str] = {}
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith(".md"):
                path = Path(root) / name
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        knowledge[name] = f.read()
                except Exception as e:
                    logger.warning("Не удалось прочитать %s: %s", path, e)
    return knowledge


# =========================
# SEARCH & RANKING LOGIC
# =========================

def _tokenize(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", (text or "").lower())
    toks = [t for t in raw if len(t) >= 3]
    stop = {"это", "для", "как", "или", "иное", "прочие", "прочее", "такой", "такие", "товар", "изделие"}
    return [t for t in toks if t not in stop]


TECH_KEYWORDS = {
    "квт", "кВт", "л.с", "мощност", "объём", "объем", "дизел", "бензин",
    "компрессор", "холодильн", "морозильн", "рефрижератор", "температур",
    "цилиндр", "топлив", "промышленн", "бытов", "транспорт", "морск",
    "сельхоз", "литр", "тип", "марка", "модел", "серийн", "артикул",
}


def assess_expert_input(text: str) -> bool:
    """Проверка достаточности описания в Expert‑режиме."""
    stripped = (text or "").strip()
    if len(stripped) < 20:
        return False
    tokens = _tokenize(stripped)
    if len(tokens) < 3:
        return False
    has_number = bool(re.search(r"\d+", stripped))
    text_lower = stripped.lower()
    has_tech = any(kw.lower() in text_lower for kw in TECH_KEYWORDS)
    return has_number or has_tech


def tnved_search_candidates(db_path: str, query_text: str, limit: int = 12) -> list[tuple[str, str]]:
    """Находит кандидаты в БД по ключевым словам пользователя."""
    tokens = _tokenize(query_text)
    if not tokens:
        return tnved_random(db_path, limit=min(limit, 5))
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    where = " OR ".join(["title LIKE ?"] * min(len(tokens), 6))
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


def llm_rank_candidates(user_text: str, candidates: list[tuple[str, str]], top_k: int = 3) -> list[tuple[str, str, str]]:
    """Использует OpenAI для ранжирования кандидатов по запросу пользователя.
    Возвращает список из (code, title, reason). Первый считается наиболее вероятным.
    Если OpenAI недоступен, возвращает первые три кандидата с пояснением.
    """
    if not candidates:
        return []
    if not USE_OPENAI:
        return [(c, t, "Подбор по ключевым словам (без LLM).") for c, t in candidates[:top_k]]
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("Библиотека openai не установлена. Установите пакет openai.")
        return [(c, t, "LLM недоступен (нет библиотеки openai).") for c, t in candidates[:top_k]]
    client = OpenAI(api_key=OPENAI_API_KEY)
    cand_payload = [
        {"code": c, "title": t} for c, t in candidates[:20]
    ]
    instructions = (
        "Ты помощник по классификации ТН ВЭД ЕАЭС. "
        "Нужно выбрать наиболее подходящие коды только из списка кандидатов. "
        "Верни ТОЛЬКО JSON (без текста вокруг) формата: {\"items\":[{\"code\":\"...\",\"reason\":\"...\"}]}. "
        "items должен содержать 3 элемента. "
        "Причина короткая (1 строка) и основана на признаках из описания пользователя и названии позиции."
    )
    user_input = {"user_text": user_text, "candidates": cand_payload}
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
    text = getattr(resp, "output_text", None)
    if not text:
        try:
            text = json.dumps(resp.model_dump(), ensure_ascii=False)
        except Exception:
            text = ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
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


def classify_text_with_db(db_path: str, text: str, allowed_prefixes: Optional[list[str]] = None) -> list[ClassificationResult]:
    """Упрощённая классификация: ищет 3 наиболее вероятных кода.

    Параметр allowed_prefixes (список строк) ограничивает выдачу только кодами,
    начинающимися с этих префиксов (например, ["8408", "8418"]). Если
    параметр не указан или равен None, в расчёт берутся все коды.
    """
    raw = (text or "").strip()
    # 1) Если пользователь ввёл ровно 10 цифр
    if raw.isdigit() and len(raw) == 10:
        title, duty = tnved_lookup_with_rate(db_path, raw)
        if title != "Код не найден":
            return [ClassificationResult(raw, title, 0.92, f"Код найден. Ставка: {duty}")]
        return [ClassificationResult(raw, "Код не найден в вашей БД (tnved).", 0.35, "Нет записи в таблице tnved")]
    # 2) если код спрятан в тексте
    m = re.search(r"\b(\d{10})\b", raw)
    if m:
        code10 = m.group(1)
        group = int(code10[:2])
        if 1 <= group <= 97:
            title, duty = tnved_lookup_with_rate(db_path, code10)
            if title != "Код не найден":
                return [ClassificationResult(code10, title, 0.90, f"Код найден в тексте. Ставка: {duty}")]
            return [ClassificationResult(code10, "Код найден в тексте, но отсутствует в БД (tnved).", 0.35, "Нет записи")]
    # 3) иначе — подбор трёх кандидатов
    # Находим кандидатов из БД по ключевым словам
    candidates = tnved_search_candidates(db_path, text, 15)
    # Если указаны допустимые префиксы, фильтруем кандидаты
    if allowed_prefixes:
        pref_set = {str(p) for p in allowed_prefixes}
        candidates = [(c, t) for c, t in candidates if any(c.startswith(pref) for pref in pref_set)]
    if not candidates:
        # Если есть ограничение по префиксам, пробуем взять случайные коды из этой группы
        if allowed_prefixes:
            pref_set = {str(p) for p in allowed_prefixes}
            # получаем больше случайных записей, чтобы потом отфильтровать
            rnd_all = tnved_random(db_path, 20)
            rnd_filtered = [(c, t) for c, t in rnd_all if any(c.startswith(pref) for pref in pref_set)]
            if rnd_filtered:
                return [ClassificationResult(c, t, 0.30, "Пример кода из базы (ограничено группой).") for c, t in rnd_filtered[:3]]
        # иначе возвращаем общий случай
        return [ClassificationResult("0000000000", "Не удалось подобрать кандидаты", 0.45, "База данных пуста или не содержит подходящих кодов")]
    ranked = llm_rank_candidates(text, candidates, top_k=3)
    results: list[ClassificationResult] = []
    # Присваиваем вероятности: 1‑й = высокая (0.8), 2‑й = средняя (0.65), 3‑й = низкая (0.5)
    for idx, (code, title, reason) in enumerate(ranked[:3]):
        if idx == 0:
            conf = 0.82
        elif idx == 1:
            conf = 0.68
        else:
            conf = 0.55
        results.append(ClassificationResult(code, title, conf, reason))
    return results


def format_results(results: list[ClassificationResult]) -> str:
    """Формирует таблицу с вероятностями и ставками пошлины."""
    def conf_label(conf: float) -> str:
        if conf >= 0.8:
            return "Высокая"
        if conf >= 0.6:
            return "Средняя"
        return "Низкая"
    lines: list[str] = [
        "| Код | Наименование позиции | Вероятность | Ставка пошлины |",
        "|-----|---------------------|-------------|----------------|",
    ]
    for r in results:
        title = r.title.replace("|", "/")
        duty = "—"
        if re.fullmatch(r"\d{10}", r.code) and r.code != "0000000000":
            ru_title, duty_rate = tnved_lookup_with_rate(DB_PATH, r.code)
            title = ru_title.replace("|", "/")
            duty = duty_rate
        lines.append(f"| {r.code} | {title} | {conf_label(r.confidence)} | {duty} |")
    reasons = "\n".join([f"• {r.code}: {r.explanation}" for r in results])
    return "\n".join(lines) + f"\n\nОбоснование:\n{reasons}" + LEGAL_DISCLAIMER


def assess_risk(confidence: float) -> str:
    if confidence >= 0.8:
        return "низкая"
    if confidence >= 0.6:
        return "средняя"
    return "высокая"


def chunk_message(text: str, chunk_size: int = 3500) -> list[str]:
    return [text[i: i + chunk_size] for i in range(0, len(text), chunk_size)]


async def suggest_codes_flow(message: Message, user_text: str) -> None:
    """Подбирает кандидаты и выводит первые три с указанием вероятности."""
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
    ranked = await asyncio.to_thread(llm_rank_candidates, user_text, candidates, 3)
    lines = [
        "Возможные коды по вашему описанию:",
        "| Код | Наименование позиции | Вероятность | Ставка пошлины | Причина |",
        "|-----|---------------------|-------------|----------------|---------|",
    ]
    for idx, (code, title, reason) in enumerate(ranked):
        if idx == 0:
            prob = "Высокая"
        elif idx == 1:
            prob = "Средняя"
        else:
            prob = "Низкая"
        ru_title, duty = await asyncio.to_thread(tnved_lookup_with_rate, DB_PATH, code)
        safe_title = (ru_title or title).replace("|", "/")
        safe_reason = reason.replace("|", "/")
        lines.append(f"| {code} | {safe_title} | {prob} | {duty} | {safe_reason} |")
    lines.append(
        "\nВажно: это предварительная подсказка. Для точной классификации необходимы технические параметры "
        "(тип, назначение, мощность/объём, материал и т.п.) и товаросопроводительные документы."
        + LEGAL_DISCLAIMER
    )
    await message.answer("\n\n".join(lines))


# =========================
# UI helpers
# =========================


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
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
        [("Промышленное оборудование", "use:industrial"), ("Морское/речное судно", "use:marine")],
        [("Транспортное средство", "use:transport"), ("Сельхозтехника", "use:agriculture")],
        [("Другое / не знаю", "use:other")],
    ])


def usage_keyboard_8418() -> InlineKeyboardMarkup:
    return _inline([
        [("Бытовое использование", "use:household"), ("Промышленное/коммерческое", "use:commercial")],
        [("Транспортный рефрижератор", "use:transport"), ("Части/комплектующие", "use:parts")],
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
    return _inline([
        [("Перейти в Light‑режим (опросник)", "switch:light")],
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
    # Здесь можно использовать знания из Podbor_NPA.md или других файлов.
    # Для простоты выводим заранее подготовленный текст.
    analysis_text = (
        "📋 Анализ правовых рисков при классификации товаров по ТН ВЭД\n\n"
        "1. Неправильно присвоенный код ТН ВЭД влечёт доначисление пошлин, штрафы, задержку товара.\n"
        "2. С 2022 года действуют ограничения на вывоз товаров из РФ (Указ Президента №100, ПП 311, 312, 313).\n"
        "3. Ставки ввозных пошлин зависят от кода (ЕТТ ЕАЭС, ПП 2240).\n"
        "4. Предварительное решение о классификации (Приказ Минфина №181н, ст.21-27 ТК ЕАЭС) защищает от претензий.\n"
        "5. Классификация осуществляется по Основным правилам интерпретации ТН ВЭД (КТС №522, ЕЭК №21, ФТС №995/635).\n"
        + LEGAL_DISCLAIMER
    )
    for chunk in chunk_message(analysis_text):
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


@router.message(Command("reimport"))
async def reimport_command(message: Message) -> None:
    """Команда для ручного переимпорта кодов из markdown‑файла в таблицу tnved.
    Позволяет администратору обновить данные, не перезапуская бота.
    """
    await message.answer("Импортирую коды из JSON-справочника ТН ВЭД в таблицу tnved…")
    imported = await asyncio.to_thread(import_reference_to_tnved, DB_PATH, TNVED_JSON_PATH, LEGACY_RU84_MD_PATH, "84")
    await asyncio.to_thread(load_ru84_codes.cache_clear)
    total = await asyncio.to_thread(tnved_count, DB_PATH)
    await message.answer(
        f"Импорт завершён. Обновлено/добавлено строк: {imported}. Всего записей в tnved: {total}."
    )


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например, /check 8408101100")
        return
    code = command.args.strip()
    if not (code.isdigit() and len(code) == 10):
        await message.answer("Код должен содержать ровно 10 цифр. Пример: /check 8408101100")
        return
    title, duty = await asyncio.to_thread(tnved_lookup_with_rate, DB_PATH, code)
    if title != "Код не найден":
        results = [ClassificationResult(code, title, 0.92, f"Код найден. Ставка: {duty}")]
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
            # Предлагаем пользователю уточнить описание или перейти в Light‑режим
            await message.answer(
                "Описания недостаточно для классификации.\n\n"
                "Для точного подбора кода ТН ВЭД в режиме Expert указание 2–3 технических параметров обязательно.\n"
                "Например: мощность (кВт/л.с.), рабочий объём, тип применения, материал, назначение, страна происхождения и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return
        # В Expert-режиме ограничиваем поиск кодами групп 8408 и 8418
        # allowed_prefixes задаёт, что возвращать только коды, начинающиеся с 8408 или 8418
        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text, ["8408", "8418"])
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode or "quick", text, response)
        await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
        if results[0].code == "0000000000":
            await suggest_codes_flow(message, text)
        return
    if mode == "light":
        await light_start(message, state)
        return
    if mode is None:
        await asyncio.to_thread(set_user_mode, DB_PATH, message.chat.id, "expert")
    await message.answer(
        "Expert‑режим: пришлите описание товара (можно с тех.характеристиками). "
        "Если знаете 10‑значный код ТН ВЭД — вставьте его в текст."
    )


# =========================
# Light flow
# =========================


async def light_start(message: Message, state: FSMContext) -> None:
    """Начинает Light‑режим: выбор категории."""
    await state.set_state(LightStates.category)
    await message.answer(
        "Шаг 1 из 3 — Группа товара.\nВыберите, что ближе всего описывает ваш товар:",
        reply_markup=category_keyboard(),
    )


@router.callback_query(LightStates.category, F.data.startswith("cat:"))
async def light_category_cb(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.split(":", 1)[1]
    labels = {
        "8408": "Двигатель внутреннего сгорания (гр. 8408)",
        "8418": "Холодильное/морозильное оборудование (гр. 8418)",
        "other": "Другой товар",  # для общих случаев
    }
    await state.update_data(category=labels.get(cat, cat))
    await callback.answer()
    await state.set_state(LightStates.usage)
    # Выбор назначений зависит от категории
    if cat == "8408":
        kb = usage_keyboard_8408()
    elif cat == "8418":
        kb = usage_keyboard_8418()
    else:
        kb = usage_keyboard_other()
    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 2 из 3 — Назначение товара.\nДля чего предназначен товар?",
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
        "parts": "части/комплектующие",
        "other": "иное применение",
    }
    await state.update_data(usage=labels.get(use, use))
    await callback.answer()
    await state.set_state(LightStates.params)
    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 3 из 3 — Технические параметры.\n"
        "Укажите характеристики, которые знаете:\n"
        "• для двигателей: мощность (кВт/л.с.), тип топлива, рабочий объём\n"
        "• для холодильного оборудования: объём камеры (л), тип (компрессионный/абсорбционный), температурный режим\n"
        "• для других товаров: любые технические детали, материал, назначение\n\n"
        "Можно написать коротко — главное, что известно.",
    )


@router.message(LightStates.params)
async def light_params(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, введите текстовое описание параметров.")
        return
    await state.update_data(params=message.text)
    data = await state.get_data()
    parts = [
        data.get("category", ""),
        data.get("usage", ""),
        data.get("params", ""),
    ]
    profile = " ".join(p for p in parts if p).strip()
    await message.answer("Анализирую данные, подбираю коды ТН ВЭД…")
    # Подбор трёх кандидатов с вероятностями
    results = await asyncio.to_thread(classify_text_with_db, DB_PATH, profile)
    response = format_results(results)
    risk = assess_risk(results[0].confidence)
    await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light", profile, response)
    await state.clear()


# =========================
# Expert fallback
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


@router.message(F.text)
async def fallback(message: Message) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id)
    if mode == "expert":
        text = message.text or ""
        if not assess_expert_input(text):
            await message.answer(
                "Описания недостаточно для классификации.\n\n"
                "Для Expert необходимо как минимум несколько технических характеристик: мощность (кВт/л.с.), объём (л), тип применения, материал, назначение, страна происхождения и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return
        # ограничиваем экспертный подбор только группами 8408 и 8418
        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text, ["8408", "8418"])
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
    # Инициализируем БД и при необходимости импортируем коды из JSON (с fallback на md)
    init_db(DB_PATH)
    # Попытка автоимпорта: если таблица tnved пуста, импортируем коды из JSON-справочника
    try:
        current_count = tnved_count(DB_PATH)
    except Exception:
        current_count = 0
    if current_count == 0:
        imported_rows = import_reference_to_tnved(DB_PATH, TNVED_JSON_PATH, LEGACY_RU84_MD_PATH, clear_mode="none")
        logger.info("tnved была пустой. Импортировано строк из источника JSON/markdown: %s", imported_rows)
    else:
        logger.info("tnved содержит %s записей. Автоимпорт не выполняется.", current_count)
    # Загрузка знаний из всех md-файлов (для справочных команд)
    knowledge_base = load_markdown_files(os.getcwd())
    logger.info("Загружены md-файлы: %s", list(knowledge_base.keys()))
    logger.info("БД инициализирована: %s", DB_PATH)
    logger.info("OpenAI: %s", "включён" if USE_OPENAI else "отключён")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот запущен, ожидаю сообщения…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
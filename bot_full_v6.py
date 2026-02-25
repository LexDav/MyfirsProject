# -*- coding: utf-8 -*-
"""
bot_full.py — Telegram bot for TН ВЭД classification (v6-algorithm, rule-based)

Ключевые принципы (system_prompt_tn_ved_v6.md):
- Источник кодов/наименований/ставок: только ru.84_2022_21.09.2025.md
- Алгоритм PATCH-8: полный обход ветвей и спуск по уровням 6 → 8 → 10 (GATE-6/8/10)
- Контракт вывода: таблица с 1 строкой (1 код), альтернативы отдельным блоком
- Режимы:
    Light — уточняющие вопросы обязательны, если не хватает ключевых признаков
    Expert — вопросы запрещены (только рекомендация перейти в Light)
- Вероятность: Высокая / Средняя / Низкая
- В конце ответа: маркер [DISCLAIMER] + текст дисклеймера из rules.yaml

Примечание:
Этот файл не реализует "RAG-лайт" команды по всем md (по просьбе пользователя).
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
from typing import Iterable, Optional

import yaml
from dotenv import load_dotenv

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

load_dotenv()

# =========================
# CONFIG / CONSTANTS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")
RU84_PATH = os.getenv("RU84_PATH", "ru.84_2022_21.09.2025.md")
RULES_PATH = os.getenv("RULES_PATH", "rules.yaml")

# OpenAI не используется в v6-алгоритме (детерминированный режим)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_OPENAI = False

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CODE10_RE = re.compile(r"\b(\d{10})\b")

# Загруженные правила
rules: dict = {}

# Индекс ru.84 (строится при старте)
RU84_10: dict[str, tuple[str, str]] = {}          # code10 -> (title, duty)
RU84_BY_4: dict[str, list[str]] = {}              # "8418" -> [code10...]
RU84_BY_6: dict[str, list[str]] = {}              # "841830" -> [code10...]
RU84_BY_8: dict[str, list[str]] = {}              # "84183020" -> [code10...]

WELCOME_AND_COMMANDS = (
    "Здравствуйте! Я помогаю подобрать код ТН ВЭД ЕАЭС для вашего товара.\n"
    "Выберите режим работы: Light (пошаговый опросник) или Expert (свободный ввод описания).\n\n"
    "Доступные команды:\n"
    "/start — выбор режима\n"
    "/mode — переключение режима\n"
    "/classify — начать классификацию\n"
    "/check <код> — проверить 10-значный код ТН ВЭД (по ru.84 и БД)\n"
    "/history — история запросов\n"
    "/reimport — обновить коды из ru.84.md в таблице tnved (опционально)\n"
    "/cancel — завершить диалог\n\n"
    "Нормативная база: Решение Совета ЕЭК от 14.09.2021 № 80 (ТН ВЭД ЕАЭС, ред. 26.09.2025).\n"
)

# =========================
# DATA STRUCTURES
# =========================

@dataclass
class ClassificationResultV6:
    code: str
    title: str
    probability: str  # Высокая/Средняя/Низкая
    duty: str
    explanation: str
    alternatives: list[tuple[str, str, str]]  # code, title, why


class LightStates(StatesGroup):
    category = State()   # шаг 1: группа товара
    purpose = State()    # шаг 2: назначение (важно по v6)
    params = State()     # шаг 3: параметры


# =========================
# RULES LOADER
# =========================

def load_rules(path: str = RULES_PATH) -> dict:
    if not os.path.exists(path):
        logger.warning("rules.yaml не найден: %s (будут использованы дефолты)", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("rules.yaml: ожидается словарь в корне")
            return {}
        return data
    except Exception as e:
        logger.error("Ошибка чтения rules.yaml: %s", e)
        return {}


def rule_get(path: list[str], default=None):
    cur = rules
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


# =========================
# DB HELPERS (history/users + optional tnved cache)
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
    max_desc = 1500
    max_result = 8000
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


def tnved_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM tnved")
    count = int(cur.fetchone()[0] or 0)
    conn.close()
    return count


def import_ru84_md_to_tnved(db_path: str, md_path: str, clear_mode: str = "84") -> int:
    """
    Опционально: импортирует коды и наименования из ru.84.md в таблицу tnved.
    Это не основной источник (основной — RU84_10 в памяти), но удобно как кэш.
    clear_mode:
      - "84": очистить только группу 84
      - "full": очистить всю таблицу
      - "none": не очищать
    """
    if not os.path.exists(md_path):
        logger.warning("Файл ru.84 не найден: %s", md_path)
        return 0

    rows: list[tuple[str, str]] = []
    try:
        with open(md_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) < 2:
                    continue
                if parts[0].lower() in {"код тн вэд", "код", "-"}:
                    continue
                code10 = re.sub(r"\D", "", parts[0])
                if len(code10) != 10:
                    continue
                title = parts[1].strip()
                if not title:
                    continue
                rows.append((code10, title))
    except Exception as e:
        logger.error("Ошибка чтения ru.84.md: %s", e)
        return 0

    if not rows:
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if clear_mode == "full":
        cur.execute("DELETE FROM tnved")
    elif clear_mode == "84":
        cur.execute("DELETE FROM tnved WHERE code LIKE '84%'")

    cur.executemany("INSERT OR REPLACE INTO tnved(code, title) VALUES(?, ?)", rows)
    conn.commit()
    conn.close()
    return len(rows)


# =========================
# RU84 PARSING + INDEX BUILD
# =========================

@lru_cache(maxsize=1)
def load_ru84_codes(path: str = RU84_PATH) -> dict[str, tuple[str, str]]:
    """Загружает из ru.84 словарь: 10-значный код -> (наименование, ставка)."""
    mapping: dict[str, tuple[str, str]] = {}

    if not os.path.exists(path):
        logger.warning("Файл ru.84 не найден: %s", path)
        return mapping

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue

                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) < 4:
                    continue

                # пропускаем заголовки и разделители таблиц
                if parts[0].lower() in {"код тн вэд", "код", "-"}:
                    continue

                code10 = re.sub(r"\D", "", parts[0])
                if len(code10) != 10:
                    continue

                title = parts[1].strip() or "(наименование отсутствует в ru.84)"
                duty_rate = parts[3].strip() or "не указана"
                mapping[code10] = (title, duty_rate)
    except OSError as e:
        logger.error("Ошибка чтения файла %s: %s", path, e)

    return mapping


def build_ru84_index() -> None:
    """Строит индексы по префиксам 4/6/8 из ru.84."""
    global RU84_10, RU84_BY_4, RU84_BY_6, RU84_BY_8
    RU84_10 = load_ru84_codes(RU84_PATH)

    RU84_BY_4 = {}
    RU84_BY_6 = {}
    RU84_BY_8 = {}

    for code10 in RU84_10.keys():
        p4 = code10[:4]
        p6 = code10[:6]
        p8 = code10[:8]
        RU84_BY_4.setdefault(p4, []).append(code10)
        RU84_BY_6.setdefault(p6, []).append(code10)
        RU84_BY_8.setdefault(p8, []).append(code10)

    logger.info("ru.84 loaded: %s codes (10-digit)", len(RU84_10))


def ru84_lookup(code10: str) -> tuple[str, str]:
    if code10 in RU84_10:
        return RU84_10[code10]
    # fallback: tnved cache
    title = tnved_get_by_code(DB_PATH, code10) or "н/д (код не найден)"
    return title, "н/д (нет данных в ru.84)"


# =========================
# TOKENIZE / FEATURE DETECT
# =========================

def _tokenize(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", (text or "").lower())
    toks = [t for t in raw if len(t) >= 3]
    stop = {"это", "для", "как", "или", "иное", "прочие", "прочее", "такой", "такие", "товар", "изделие"}
    return [t for t in toks if t not in stop]


def detect_feature(feature_name: str, text: str) -> bool:
    defs = rule_get(["features", "definitions"], {}) or {}
    spec = defs.get(feature_name, {}) if isinstance(defs, dict) else {}
    t = (text or "").lower()

    any_list = spec.get("detect_any")
    if isinstance(any_list, list) and any_list:
        if any(w.lower() in t for w in map(str, any_list)):
            return True

    regex_list = spec.get("detect_regex")
    if isinstance(regex_list, list) and regex_list:
        for pat in regex_list:
            try:
                if re.search(str(pat), t, flags=re.IGNORECASE):
                    return True
            except re.error:
                continue
    return False


def missing_questions_for_group(group4: str, text: str) -> list[str]:
    """
    GATE-8: проверка ключевых признаков. В Light — задаём уточнение.
    В Expert — вопросов не задаём, но используем это для понижения вероятности.
    """
    req = rule_get(["features", "per_group", group4, "level8_require"], []) or []
    if not isinstance(req, list):
        return []
    missing = [f for f in map(str, req) if not detect_feature(f, text)]

    questions = []
    defs = rule_get(["features", "definitions"], {}) or {}
    for f in missing:
        q = ""
        if isinstance(defs, dict):
            q = str((defs.get(f, {}) or {}).get("question", "")).strip()
        if q:
            questions.append(q)
    return questions


# =========================
# PATCH-8 SCORING / SELECTION
# =========================

def token_overlap_score(text: str, title: str) -> int:
    qt = set(_tokenize(text))
    tt = set(_tokenize(title))
    return sum(1 for x in qt if x in tt)


def score_code10(text: str, code10: str) -> int:
    title, _duty = ru84_lookup(code10)
    w = int(rule_get(["ranking", "token_overlap_weight"], 1))
    return w * token_overlap_score(text, title)


def select_best_prefix(text: str, prefixes: list[str], prefix_to_codes: dict[str, list[str]]) -> tuple[str, int, list[tuple[str, int]]]:
    """
    Возвращает (лучший_prefix, score, alternatives(prefix, score)).
    Score = max(score_code10) по листьям ветви.
    Полный перебор всех ветвей — обязательный (PATCH-8).
    """
    best_pref = ""
    best_score = -1
    scored: list[tuple[str, int]] = []

    for pref in prefixes:
        codes = prefix_to_codes.get(pref, [])
        if not codes:
            continue
        s = max(score_code10(text, c) for c in codes)
        scored.append((pref, s))
        if s > best_score:
            best_score = s
            best_pref = pref

    scored.sort(key=lambda x: x[1], reverse=True)
    alts = scored[1:1 + int(rule_get(["output_contract", "max_alternatives"], 2))]
    return best_pref, best_score, alts


def select_best_code10(text: str, codes: list[str]) -> tuple[str, int, list[tuple[str, int]]]:
    best = ""
    best_score = -1
    scored: list[tuple[str, int]] = []
    for c in codes:
        s = score_code10(text, c)
        scored.append((c, s))
        if s > best_score:
            best_score = s
            best = c
    scored.sort(key=lambda x: x[1], reverse=True)
    alts = scored[1:1 + int(rule_get(["output_contract", "max_alternatives"], 2))]
    return best, best_score, alts


def probability_from_score(score: int, forced_max: Optional[str] = None) -> str:
    thr = rule_get(["ranking", "probability_thresholds"], {}) or {}
    high = int((thr or {}).get("high", 6))
    medium = int((thr or {}).get("medium", 3))

    prob = "Низкая"
    if score >= high:
        prob = "Высокая"
    elif score >= medium:
        prob = "Средняя"

    if forced_max == "Средняя" and prob == "Высокая":
        return "Средняя"
    if forced_max == "Низкая" and prob in {"Высокая", "Средняя"}:
        return "Низкая"
    return prob


def classify_patch8(group4: str, user_text: str, mode: str) -> tuple[Optional[ClassificationResultV6], Optional[str]]:
    """
    Основная процедура v6 (PATCH-8).
    Возвращает (result, pending_question_block).
    Если pending_question_block не None — в Light нужно спросить уточнение и не финализировать.
    """
    group4 = (group4 or "").strip()
    if group4 not in {"8408", "8418"}:
        return None, None

    pool10 = RU84_BY_4.get(group4, [])
    if not pool10:
        return ClassificationResultV6(
            code="0000000000",
            title="н/д (нет данных в ru.84 для выбранной группы)",
            probability="Низкая",
            duty="н/д",
            explanation="В ru.84 отсутствуют коды для указанной группы.",
            alternatives=[],
        ), None

    # GATE-6: выбираем лучшую ветвь 6-значную
    prefixes6 = sorted(set(c[:6] for c in pool10))
    best6, score6, alt6 = select_best_prefix(user_text, prefixes6, RU84_BY_6)

    # GATE-8: выбираем лучшую ветвь 8-значную внутри best6
    pool10_best6 = RU84_BY_6.get(best6, [])
    prefixes8 = sorted(set(c[:8] for c in pool10_best6))
    best8, score8, alt8 = select_best_prefix(user_text, prefixes8, RU84_BY_8)

    # Проверка требований признаков (GATE-8)
    missing_q = missing_questions_for_group(group4, user_text)
    if missing_q and mode.lower() == "light":
        ask_style = str(rule_get(["light_flow", "ask_style"], "list"))
        if ask_style == "one_by_one":
            pending = "Нужно уточнить один момент:\n" + missing_q[0]
        else:
            pending = "Чтобы точно выбрать код, уточните:\n- " + "\n- ".join(missing_q)
        return None, pending

    # GATE-10: выбираем конкретный 10-значный код внутри best8
    pool10_best8 = RU84_BY_8.get(best8, [])
    best10, score10, alt10 = select_best_code10(user_text, pool10_best8)

    title, duty = ru84_lookup(best10)

    # если по какой-то причине не найдено в ru.84 — ограничиваем вероятность
    forced_max = None
    if best10 not in RU84_10:
        forced_max = "Средняя"
        duty = "н/д (нет данных в ru.84)"
        title = "н/д (код не найден в ru.84)"

    # если Expert и не хватает признаков — понижаем вероятность и добавляем рекомендацию
    expert_note = ""
    if missing_q and mode.lower() == "expert":
        forced_max = "Средняя"
        expert_note = str(rule_get(["expert_policy", "switch_to_light_text"], "")).strip()

    # итоговый score берём как score10 (наиболее конкретный)
    prob = probability_from_score(score10, forced_max=forced_max)

    # альтернативы — до 2 шт (из alt10; если их нет, из alt8/alt6)
    alternatives: list[tuple[str, str, str]] = []
    for c, s in alt10:
        t, _d = ru84_lookup(c)
        alternatives.append((c, t, "Отличается детализацией/характеристиками в позиции."))
        if len(alternatives) >= int(rule_get(["output_contract", "max_alternatives"], 2)):
            break
    if len(alternatives) < int(rule_get(["output_contract", "max_alternatives"], 2)):
        # добавим альтернативы уровня 8 как "ветки" (покажем лучший лист в ветке)
        for p8, _s8 in alt8:
            leaf = max(RU84_BY_8.get(p8, []), key=lambda c: score_code10(user_text, c), default="")
            if leaf and leaf != best10:
                t, _d = ru84_lookup(leaf)
                alternatives.append((leaf, t, "Альтернативная ветвь уровня 8 (нужны уточняющие признаки)."))
            if len(alternatives) >= int(rule_get(["output_contract", "max_alternatives"], 2)):
                break

    explanation_lines = [
        f"Алгоритм PATCH-8: полный перебор ветвей и спуск 6→8→10.",
        f"GATE-6: выбрана ветвь {best6} (score={score6}).",
        f"GATE-8: выбрана ветвь {best8} (score={score8}).",
        f"GATE-10: выбран код {best10} (score={score10}).",
        "ОПИ: применено ОПИ 1 (классификация по тексту позиций). При конкуренции ветвей учтён принцип наиболее конкретного описания (ОПИ 3(a)).",
    ]
    if expert_note:
        explanation_lines.append(expert_note)

    return ClassificationResultV6(
        code=best10,
        title=title,
        probability=prob,
        duty=duty,
        explanation="\n".join(explanation_lines),
        alternatives=alternatives,
    ), None


# =========================
# OUTPUT FORMAT (v6 contract)
# =========================

def format_v6(result: ClassificationResultV6, mode: str) -> str:
    cols = rule_get(["output_contract", "table_columns"], ["Код", "Наименование позиции", "Вероятность", "Ставка пошлины"])
    marker = str(rule_get(["disclaimer", "marker"], "[DISCLAIMER]"))
    disclaimer_text = str(rule_get(["disclaimer", "text"], "")).strip()

    # 1) обязательная шапка "Режим: ..."
    lines = [f"Режим: {mode.capitalize()}"]

    # 2) таблица с 1 строкой
    lines.append("")
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join(["---"] * len(cols)) + "|")
    lines.append(f"| {result.code} | {result.title.replace('|','/')} | {result.probability} | {result.duty} |")

    # 3) альтернативы (если есть)
    if result.alternatives:
        lines.append("")
        lines.append("2) Альтернативные коды (если нужны уточнения):")
        for i, (c, t, why) in enumerate(result.alternatives[: int(rule_get(['output_contract','max_alternatives'],2))], 1):
            lines.append(f"• {c} — {t.replace('|','/')} ({why})")

    # 4) обоснование
    lines.append("")
    lines.append("3) Обоснование:")
    lines.append(result.explanation)

    # 5) дисклеймер (маркер + текст)
    lines.append("")
    lines.append(marker)
    if disclaimer_text:
        lines.append(disclaimer_text)

    return "\n".join(lines)


def chunk_message(text: str, chunk_size: int = 3500) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


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


def purpose_keyboard_8408() -> InlineKeyboardMarkup:
    return _inline([
        [("Промышленное оборудование", "purpose:industrial"), ("Морское/речное судно", "purpose:marine")],
        [("Транспортное средство", "purpose:transport"), ("Сельхозтехника", "purpose:agriculture")],
        [("Другое/не знаю", "purpose:other")],
    ])


def purpose_keyboard_8418() -> InlineKeyboardMarkup:
    return _inline([
        [("Бытовое", "purpose:household")],
        [("Промышленное/торговое", "purpose:commercial")],
        [("Транспорт (рефрижератор/контейнер)", "purpose:transport")],
        [("Другое/не знаю", "purpose:other")],
    ])


def purpose_keyboard_other() -> InlineKeyboardMarkup:
    return _inline([
        [("Бытовое", "purpose:household"), ("Промышленное/торговое", "purpose:commercial")],
        [("Другое/не знаю", "purpose:other")],
    ])


def mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Light"), KeyboardButton(text="Expert")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


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
    await message.answer("Импортирую коды из ru.84.md в таблицу tnved…")
    imported = await asyncio.to_thread(import_ru84_md_to_tnved, DB_PATH, RU84_PATH, "84")
    total = await asyncio.to_thread(tnved_count, DB_PATH)
    # также перестроим in-memory индекс
    await asyncio.to_thread(build_ru84_index)
    await message.answer(f"Импорт завершён. Обновлено/добавлено: {imported}. Всего в tnved: {total}.")


@router.message(Command("check"))
async def check_code(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Укажите код после команды, например, /check 8408101100")
        return

    code = command.args.strip()
    if not (code.isdigit() and len(code) == 10):
        await message.answer("Код должен содержать ровно 10 цифр. Пример: /check 8408101100")
        return

    title, duty = await asyncio.to_thread(ru84_lookup, code)
    forced_max = None
    if code not in RU84_10:
        forced_max = "Средняя"

    score = token_overlap_score(code, title)  # примитивный score
    prob = probability_from_score(score, forced_max=forced_max)

    res = ClassificationResultV6(
        code=code,
        title=title,
        probability=prob,
        duty=duty,
        explanation="Проверка кода по ru.84 (и кэшу tnved при отсутствии записи в ru.84).",
        alternatives=[],
    )
    response = format_v6(res, mode="check")
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "check", f"Проверка кода {code}", response)
    for chunk in chunk_message(response):
        await message.answer(chunk)


@router.message(Command("classify"))
async def classify_command(message: Message, state: FSMContext, command: CommandObject) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id) or "expert"
    mode = mode.lower()

    if command.args:
        text = command.args.strip()

        # В Expert нельзя задавать вопросов: если описания мало — рекомендация перейти в Light
        if mode == "expert" and len(text) < 10:
            await message.answer(str(rule_get(["expert_policy", "switch_to_light_text"], "Перейдите в режим Light.")))
            return

        # Попытка определить группу (8408/8418) по наличию в тексте.
        t = text.lower()
        group4 = "8418" if any(w in t for w in ["холодиль", "морозиль", "ларь", "витрин", "рефриж"]) else \
                 "8408" if any(w in t for w in ["двигател", "дизел", "двс"]) else "other"

        if group4 == "other":
            # В ru.84 у нас только группа 84; если не можем определить — предлагаем Light.
            if mode == "expert":
                await message.answer(str(rule_get(["expert_policy", "switch_to_light_text"], "Перейдите в режим Light.")))
                return
            await light_start(message, state)
            return

        result, pending = await asyncio.to_thread(classify_patch8, group4, text, mode)
        if pending and mode == "light":
            await message.answer(pending)
            return
        if not result:
            await message.answer("Не удалось классифицировать по введённым данным. Попробуйте режим Light.")
            return

        response = format_v6(result, mode=mode)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode, text, response)
        for chunk in chunk_message(response):
            await message.answer(chunk)
        return

    # если /classify без args
    if mode == "light":
        await light_start(message, state)
        return

    await message.answer(
        "Expert режим: пришлите описание товара (можно с тех.характеристиками). "
        "Вопросы в Expert не задаются; при необходимости перейдите в Light (/mode → Light)."
    )


# =========================
# Light flow
# =========================

async def light_start(message: Message, state: FSMContext) -> None:
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
        "other": "Другой товар",
    }
    await state.update_data(category=cat, category_label=labels.get(cat, cat))
    await callback.answer()

    await state.set_state(LightStates.purpose)

    if cat == "8408":
        kb = purpose_keyboard_8408()
    elif cat == "8418":
        kb = purpose_keyboard_8418()
    else:
        kb = purpose_keyboard_other()

    step2_q = str(rule_get(["light_flow", "step2_question"], "Шаг 2 из 3 — Назначение товара.\nДля чего предназначен товар?"))
    await callback.message.answer(step2_q, reply_markup=kb)  # type: ignore[union-attr]


@router.callback_query(LightStates.purpose, F.data.startswith("purpose:"))
async def light_purpose_cb(callback: CallbackQuery, state: FSMContext) -> None:
    purpose = callback.data.split(":", 1)[1]
    labels = {
        "industrial": "промышленное оборудование",
        "marine": "морское/речное судно",
        "transport": "транспортное средство/рефрижератор",
        "agriculture": "сельскохозяйственная техника",
        "household": "бытовое назначение",
        "commercial": "промышленное/торговое назначение",
        "other": "иное/неизвестно",
    }
    await state.update_data(purpose=purpose, purpose_label=labels.get(purpose, purpose))
    await callback.answer()

    await state.set_state(LightStates.params)
    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 3 из 3 — Технические параметры.\n"
        "Укажите характеристики, которые знаете:\n"
        "• для двигателей (8408): мощность (кВт/л.с.), рабочий объём, тип топлива\n"
        "• для холодильного оборудования (8418): объём (л), тип (компрессорный/абсорбционный), температурный режим\n"
        "Можно коротко, главное — ключевые признаки."
    )


@router.message(LightStates.params)
async def light_params(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, введите текстовое описание параметров.")
        return

    await state.update_data(params=message.text)
    data = await state.get_data()

    cat = str(data.get("category", "other"))
    parts = [
        str(data.get("category_label", "")),
        str(data.get("purpose_label", "")),
        str(data.get("params", "")),
    ]
    profile = " ".join(p for p in parts if p).strip()

    # GATE-8 уточнения: в Light обязаны спросить, если ключевых признаков нет
    pending_questions = await asyncio.to_thread(missing_questions_for_group, cat, profile) if cat in {"8408","8418"} else []
    if pending_questions:
        ask_style = str(rule_get(["light_flow", "ask_style"], "list"))
        if ask_style == "one_by_one":
            await message.answer("Нужно уточнить один момент:\n" + pending_questions[0])
        else:
            await message.answer("Чтобы точно выбрать код, уточните:\n- " + "\n- ".join(pending_questions))
        return

    await message.answer("Анализирую данные и подбираю код ТН ВЭД…")

    if cat not in {"8408", "8418"}:
        await message.answer("Для точной классификации в этом прототипе поддерживаются группы 8408 и 8418. Выберите группу на шаге 1.")
        return

    result, pending = await asyncio.to_thread(classify_patch8, cat, profile, "light")
    if pending:
        await message.answer(pending)
        return
    if not result:
        await message.answer("Не удалось классифицировать. Попробуйте уточнить параметры.")
        return

    response = format_v6(result, mode="light")
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light", profile, response)
    for chunk in chunk_message(response):
        await message.answer(chunk)

    await state.clear()


# =========================
# Expert fallback
# =========================

@router.message(F.text)
async def fallback(message: Message, state: FSMContext) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id) or "expert"
    mode = mode.lower()

    if mode == "expert":
        text = message.text or ""
        # никаких вопросов в Expert: либо классифицируем, либо советуем Light
        if len(text.strip()) < 10:
            await message.answer(str(rule_get(["expert_policy", "switch_to_light_text"], "Перейдите в режим Light.")))
            return

        t = text.lower()
        group4 = "8418" if any(w in t for w in ["холодиль", "морозиль", "ларь", "витрин", "рефриж"]) else \
                 "8408" if any(w in t for w in ["двигател", "дизел", "двс"]) else "other"

        if group4 == "other":
            await message.answer(str(rule_get(["expert_policy", "switch_to_light_text"], "Перейдите в режим Light.")))
            return

        result, _pending = await asyncio.to_thread(classify_patch8, group4, text, "expert")
        if not result:
            await message.answer(str(rule_get(["expert_policy", "switch_to_light_text"], "Перейдите в режим Light.")))
            return

        response = format_v6(result, mode="expert")
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "expert", text, response)
        for chunk in chunk_message(response):
            await message.answer(chunk)
        return

    # если режим light, но пользователь пишет вне state — подскажем
    await message.answer("Используйте /classify для начала классификации или /help.")


# =========================
# MAIN
# =========================

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Создайте файл .env или установите переменную окружения BOT_TOKEN."
        )

    global rules
    rules = load_rules(RULES_PATH)

    init_db(DB_PATH)

    # строим индекс ru.84 в память (основной источник)
    build_ru84_index()

    # опциональный кэш в tnved: если таблица пуста — импортируем (без очистки) для удобства /check
    try:
        if tnved_count(DB_PATH) == 0:
            imported = import_ru84_md_to_tnved(DB_PATH, RU84_PATH, clear_mode="none")
            logger.info("tnved была пустой: импортировано из ru.84.md: %s", imported)
    except Exception as e:
        logger.warning("Не удалось импортировать в tnved: %s", e)

    logger.info("Rules loaded: %s", bool(rules))
    logger.info("Bot started…")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

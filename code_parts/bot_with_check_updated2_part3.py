# AUTO-GENERATED PART 3
# Source: bot_with_check_updated2.py
# Lines: 301-449

    cur.execute("SELECT code, title FROM tnved ORDER BY RANDOM() LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


@lru_cache(maxsize=1)
def load_ru84_codes(path: str = RU84_PATH) -> dict[str, tuple[str, str]]:
    """Загружает из ru.84 словарь: 10-значный код -> (наименование, ставка)."""
    mapping: dict[str, tuple[str, str]] = {}

    if not os.path.exists(path):
        logger.warning("Файл с тарифами не найден: %s", path)
        return mapping

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|"):
                    continue

                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) < 4:
                    continue

                # пропускаем заголовки и разделители таблиц
                if parts[0].lower() in {"код тн вэд", "-"}:
                    continue

                code_raw = parts[0]
                code10 = re.sub(r"\D", "", code_raw)
                if len(code10) != 10:
                    continue

                title = parts[1].strip() or "(наименование отсутствует в ru.84)"
                duty_rate = parts[3].strip() or "не указана"
                mapping[code10] = (title, duty_rate)
    except OSError as e:
        logger.error("Ошибка чтения файла %s: %s", path, e)

    return mapping


def tnved_lookup_with_rate(db_path: str, code: str) -> tuple[str, str]:
    """Возвращает (наименование, ставка) по коду: сначала ru.84, затем БД."""
    ru84 = load_ru84_codes()
    if code in ru84:
        return ru84[code]

    title = tnved_get_by_code(db_path, code) or "Код не найден"
    return title, "нет данных в ru.84"


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

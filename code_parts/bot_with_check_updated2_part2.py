# AUTO-GENERATED PART 2
# Source: bot_with_check_updated2.py
# Lines: 201-400

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


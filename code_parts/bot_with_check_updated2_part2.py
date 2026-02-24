# AUTO-GENERATED PART 2
# Source: bot_with_check_updated2.py
# Lines: 151-300

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

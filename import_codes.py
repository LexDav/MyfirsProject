import os
import re
import sqlite3
from pathlib import Path

import pdfplumber

PDF_PATH = Path(os.getenv("TNVED_PDF_PATH", "tnved.pdf"))
DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))

CODE_SPACED_RE = re.compile(r"\b(84(?:08|18))\s*(\d{2})\s*(\d{3})\s*(\d)\b")
CODE_SOLID_RE = re.compile(r"\b(84(?:08|18)\d{6})\b")
DASH_SPLIT_RE = re.compile(r"\s*[–-]\s*")


def init_tnved_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tnved (
            code TEXT PRIMARY KEY,
            title TEXT NOT NULL
        )
        """
    )
    conn.commit()


def upsert_codes(conn: sqlite3.Connection, codes: dict[str, str]) -> int:
    cur = conn.cursor()
    count = 0
    for code, title in codes.items():
        cur.execute(
            """
            INSERT INTO tnved(code, title)
            VALUES (?, ?)
            ON CONFLICT(code) DO UPDATE SET title = excluded.title
            """,
            (code, title),
        )
        count += 1
    conn.commit()
    return count


def _extract_title_from_line(line: str, end_pos: int) -> str:
    after = line[end_pos:].strip()
    if not after:
        return "—"
    parts = DASH_SPLIT_RE.split(after, maxsplit=1)
    title = parts[1].strip() if len(parts) > 1 else after
    return re.sub(r"\s{2,}", " ", title).strip() or "—"


def extract_codes_from_pdf(pdf_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not text.strip():
                continue
            for line in text.splitlines():
                for m in CODE_SPACED_RE.finditer(line):
                    code10 = "".join(m.groups())
                    title = _extract_title_from_line(line, m.end())
                    if code10 not in result or result[code10] == "—":
                        result[code10] = title
                for m in CODE_SOLID_RE.finditer(line):
                    code10 = m.group(1)
                    title = _extract_title_from_line(line, m.end())
                    if code10 not in result or result[code10] == "—":
                        result[code10] = title

    return {
        c: t
        for c, t in result.items()
        if len(c) == 10 and c.isdigit() and c.startswith(("8408", "8418"))
    }


def main() -> None:
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"PDF не найден: {PDF_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        init_tnved_table(conn)
        codes = extract_codes_from_pdf(PDF_PATH)
        inserted = upsert_codes(conn, codes)
        print(f"PDF: {PDF_PATH}")
        print(f"БД:  {DB_PATH}")
        print(f"Найдено кодов: {len(codes)}")
        print(f"Загружено в БД: {inserted}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

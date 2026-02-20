import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import sqlite3

from bot_with_check_updated2 import (
    assess_expert_input,
    classify_text_with_db,
    init_db,
    tnved_search_candidates,
)


def _prepare_db(path: str) -> None:
    init_db(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO tnved(code, title) VALUES(?, ?)",
        [
            ("8408101100", "Двигатель судовой дизельный"),
            ("8418102000", "Холодильник бытовой компрессорный"),
            ("8504403000", "Преобразователь статический промышленный"),
        ],
    )
    conn.commit()
    conn.close()


def test_assess_expert_input_accepts_embedded_code() -> None:
    assert assess_expert_input("код 8408101100") is True


def test_assess_expert_input_rejects_short_non_technical_text() -> None:
    assert assess_expert_input("это товар хороший") is False


def test_classify_text_with_db_finds_code_in_text(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_db(str(db_path))

    results = classify_text_with_db(str(db_path), "Прошу проверить код 8408101100 для товара")

    assert len(results) == 1
    assert results[0].code == "8408101100"
    assert "судовой" in results[0].title.lower()


def test_tnved_search_candidates_returns_matches(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    _prepare_db(str(db_path))

    rows = tnved_search_candidates(str(db_path), "промышленный преобразователь", limit=5)

    assert rows
    assert any(code == "8504403000" for code, _ in rows)

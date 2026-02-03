from dataclasses import dataclass
from typing import Any


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

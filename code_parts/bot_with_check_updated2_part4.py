# AUTO-GENERATED PART 4
# Source: bot_with_check_updated2.py
# Lines: 450-598


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
        title, duty = tnved_lookup_with_rate(db_path, raw)
        if title != "Код не найден":
            return [ClassificationResult(raw, title, 0.92, f"Код найден. Ставка: {duty}")]
        return [ClassificationResult(raw, "Код не найден в вашей БД (tnved).", 0.35, "Нет записи в таблице tnved")]

    # 2) если код "спрятан" в тексте
    # Дополнительная проверка: первые 2 цифры должны быть в диапазоне 01–97 (группы ТН ВЭД),
    # чтобы не ловить номера телефонов и другие 10-значные числа.
    m = CODE10_RE.search(raw)
    if m:
        code10 = m.group(1)
        group = int(code10[:2])
        if 1 <= group <= 97:
            title, duty = tnved_lookup_with_rate(db_path, code10)
            if title != "Код не найден":
                return [ClassificationResult(code10, title, 0.90, f"Код найден в тексте. Ставка: {duty}")]
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


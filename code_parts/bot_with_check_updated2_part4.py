# AUTO-GENERATED PART 4
# Source: bot_with_check_updated2.py
# Lines: 601-800

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
            ru_title, duty = tnved_lookup_with_rate(DB_PATH, r.code)
            title = ru_title.replace("|", "/")
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

    lines = [
        "Возможные коды по вашему описанию (предварительно):",
        "| Код | Наименование позиции | Ставка пошлины | Почему |",
        "|-----|---------------------|----------------|--------|",
    ]
    for code, title, reason in ranked:
        ru_title, duty = await asyncio.to_thread(tnved_lookup_with_rate, DB_PATH, code)
        safe_title = (ru_title or title).replace("|", "/")
        safe_reason = reason.replace("|", "/")
        lines.append(f"| {code} | {safe_title} | {duty} | {safe_reason} |")

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

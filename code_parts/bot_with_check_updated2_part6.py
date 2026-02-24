# AUTO-GENERATED PART 6
# Source: bot_with_check_updated2.py
# Lines: 748-896

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
    for chunk in chunk_message("\n\n".join(lines)):
        await message.answer(chunk)


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
            await message.answer(
                "Описания недостаточно для классификации.\n\n"
                "Для точного подбора кода ТН ВЭД нужны технические детали: "
                "мощность, объём, тип применения, материал и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return

        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, mode or "quick", text, response)

        await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
        # если точного кода нет — подскажем варианты
        if results[0].code == "0000000000":
            await suggest_codes_flow(message, text)
        return

    if mode == "light":
        await light_start(message, state)
        return

    if mode is None:
        await asyncio.to_thread(set_user_mode, DB_PATH, message.chat.id, "expert")
    await message.answer(
        "Expert режим: пришлите описание товара (можно с тех.характеристиками). "
        "Если знаете 10-значный код ТН ВЭД — вставьте его в текст."
    )


# =========================
# Light flow
# =========================

# --- Шаг 1: категория (запускается из /classify или set_mode) ---

async def light_start(message: Message, state: FSMContext) -> None:
    """Точка входа в Light-режим: показываем выбор группы товара."""
    await state.set_state(LightStates.category)
    await message.answer(
        "Шаг 1 из 3 — Группа товара.\nВыберите, что ближе всего описывает ваш товар:",
        reply_markup=category_keyboard(),
    )


@router.callback_query(LightStates.category, F.data.startswith("cat:"))
async def light_category_cb(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.split(":", 1)[1]  # "8408" / "8418" / "other"

    labels = {"8408": "Двигатель внутреннего сгорания (гр. 8408)",

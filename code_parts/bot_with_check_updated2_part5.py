# AUTO-GENERATED PART 5
# Source: bot_with_check_updated2.py
# Lines: 801-1000

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
               "8418": "Холодильное/морозильное оборудование (гр. 8418)",
               "other": "Другой товар"}
    await state.update_data(category=labels.get(cat, cat))
    await callback.answer()

    await state.set_state(LightStates.usage)

    if cat == "8408":
        kb = usage_keyboard_8408()
    elif cat == "8418":
        kb = usage_keyboard_8418()
    else:
        kb = usage_keyboard_other()

    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 2 из 3 — Сфера применения.\nДля чего предназначен товар?",
        reply_markup=kb,
    )


@router.callback_query(LightStates.usage, F.data.startswith("use:"))
async def light_usage_cb(callback: CallbackQuery, state: FSMContext) -> None:
    use = callback.data.split(":", 1)[1]

    labels = {
        "industrial": "промышленное применение",
        "marine": "морское/речное судно",
        "transport": "транспортное средство",
        "agriculture": "сельскохозяйственная техника",
        "household": "бытовое использование",
        "commercial": "промышленное/торговое оборудование",
        "other": "иное применение",
    }
    await state.update_data(usage=labels.get(use, use))
    await callback.answer()

    await state.set_state(LightStates.params)
    await callback.message.answer(  # type: ignore[union-attr]
        "Шаг 3 из 3 — Технические параметры.\n"
        "Укажите характеристики, которые знаете:\n"
        "• для двигателей: мощность (кВт/л.с.), тип топлива, рабочий объём\n"
        "• для холодильного оборудования: объём камеры (л), тип (компрессорный/абсорбционный), температурный режим\n"
        "• для других товаров: любые технические детали, материал, назначение\n\n"
        "Можно написать коротко — главное, что знаете."
    )


@router.message(LightStates.params)
async def light_params(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, введите текстовое описание параметров.")
        return
    await state.update_data(params=message.text)
    data = await state.get_data()

    # Собираем профиль из всех шагов
    parts = [
        data.get("category", ""),
        data.get("usage", ""),
        data.get("params", ""),
    ]
    profile = " ".join(p for p in parts if p).strip()

    await message.answer("Анализирую данные, подбираю коды ТН ВЭД…")
    await suggest_codes_flow(message, profile)
    await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "light", profile, "suggest_codes_flow")

    await state.clear()


# =========================
# Expert: переключение в Light
# =========================

@router.callback_query(F.data.startswith("switch:"))
async def switch_mode_cb(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "light":
        await asyncio.to_thread(set_user_mode, DB_PATH, callback.message.chat.id, "light")  # type: ignore[union-attr]
        await light_start(callback.message, state)  # type: ignore[arg-type]
    else:
        await callback.message.answer(  # type: ignore[union-attr]
            "Хорошо! Добавьте к описанию технические параметры:\n"
            "мощность (кВт/л.с.), объём (л), тип применения, страна происхождения.\n"
            "Чем конкретнее — тем точнее результат."
        )


# =========================
# Expert fallback
# =========================

@router.message(F.text)
async def fallback(message: Message) -> None:
    mode = await asyncio.to_thread(get_user_mode, DB_PATH, message.chat.id)

    if mode == "expert":
        text = message.text or ""

        if not assess_expert_input(text):
            await message.answer(
                "Описания недостаточно для классификации.\n\n"

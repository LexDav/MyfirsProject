# AUTO-GENERATED PART 7
# Source: bot_with_check_updated2.py
# Lines: 897-1045

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
                "Для точного подбора кода ТН ВЭД нужны технические детали: "
                "мощность, объём, тип применения, материал и т.п.\n\n"
                "Что хотите сделать?",
                reply_markup=switch_to_light_keyboard(),
            )
            return

        results = await asyncio.to_thread(classify_text_with_db, DB_PATH, text)
        response = format_results(results)
        risk = assess_risk(results[0].confidence)
        await asyncio.to_thread(save_query, DB_PATH, message.chat.id, "expert", text, response)

        await message.answer(f"{response}\n\nОценка риска неверной классификации: {risk}.")
        if results[0].code == "0000000000":
            await suggest_codes_flow(message, text)
        return

    await message.answer("Не удалось распознать команду. Используйте /help.")


# =========================
# MAIN
# =========================

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Создайте файл .env на основе .env.example "
            "или установите переменную окружения BOT_TOKEN."
        )

    init_db(DB_PATH)
    logger.info("БД инициализирована: %s", DB_PATH)
    logger.info("OpenAI: %s", "включён, модель=" + OPENAI_MODEL if USE_OPENAI else "отключён")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен, ожидаю сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

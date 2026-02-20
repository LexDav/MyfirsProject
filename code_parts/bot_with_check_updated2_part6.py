# AUTO-GENERATED PART 6
# Source: bot_with_check_updated2.py
# Lines: 1001-1045

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

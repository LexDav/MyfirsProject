import os


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

DB_PATH = os.getenv("DB_PATH", "bot.db")

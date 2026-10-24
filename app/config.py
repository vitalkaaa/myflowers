"""Все настройки берутся из переменных окружения (или файла .env)."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

DB_PATH = os.getenv("DB_PATH", "data/flowers.db")
SEED_PHOTOS_DIR = os.getenv("SEED_PHOTOS_DIR", "seed_photos")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")  # например http://host.docker.internal:3128
REMINDER_CRON = os.getenv("REMINDER_CRON", "0 19 * * *")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # для ссылок на карточки в Telegram

BACKUP_CRON = os.getenv("BACKUP_CRON", "30 3 * * *")  # резервная копия БД каждую ночь
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "14")))


def require(*names: str) -> None:
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

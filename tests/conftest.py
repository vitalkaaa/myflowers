"""Тесты работают с временной БД и тестовыми настройками — настоящий .env и data/ не трогаются.

Переменные задаются до импорта app: config читает окружение при импорте.
Файл .env тестам не нужен вовсе — load_dotenv отключаем, чтобы настройки из него не подмешивались.
"""
import os
import tempfile

import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False

_tmp = tempfile.mkdtemp(prefix="my-flowers-tests-")
os.environ.update(
    APP_USERNAME="tester",
    APP_PASSWORD="test-password-long",
    SECRET_KEY="test-secret-key-" + "x" * 32,
    DB_PATH=os.path.join(_tmp, "test.db"),
    SEED_PHOTOS_DIR=os.path.join(_tmp, "no-photos"),  # без фото: тесты быстрее
    TELEGRAM_BOT_TOKEN="test-token",
    TELEGRAM_CHAT_ID="42",
    TELEGRAM_PROXY="",
    BASE_URL="",
    TZ_NAME="Europe/Moscow",
)

import pytest  # noqa: E402

from app.db import PLANT_FIELDS, get_db, init_db, insert_plant  # noqa: E402

CREATED = "2026-01-10T10:00:00+03:00"


def plant_values(**overrides) -> dict:
    values = {field: "" for field in PLANT_FIELDS}
    values.update(water_summer_days=5, water_winter_days=10, water_ml=200,
                  feed_days=None, repot_years=None, spray_days=None, wipe_days=None)
    values.update(overrides)
    return values


def plant_row(**overrides) -> dict:
    """Растение как строка таблицы — для чистых функций care."""
    return {"id": 1, "created_at": CREATED, **plant_values(**overrides)}


@pytest.fixture
def db_plants():
    """Пустая БД и функция добавления растения: add(name=…, …) -> id."""
    init_db()
    with get_db() as db:
        db.execute("DELETE FROM plants")

    def add(**overrides) -> int:
        with get_db() as db:
            return insert_plant(db, plant_values(**overrides), CREATED)

    return add


def events(plant_id: int | None = None) -> list[tuple[int, str]]:
    with get_db() as db:
        rows = db.execute("SELECT plant_id, kind FROM events ORDER BY id").fetchall()
    return [(r["plant_id"], r["kind"]) for r in rows if plant_id is None or r["plant_id"] == plant_id]


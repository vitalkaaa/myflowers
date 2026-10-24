import json
import os
import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    species           TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    water_summer_days INTEGER NOT NULL,          -- интервал полива апрель–сентябрь
    water_winter_days INTEGER NOT NULL,          -- интервал полива октябрь–март
    water_ml          INTEGER,                   -- объём полива, если известен
    water_how         TEXT NOT NULL DEFAULT '',  -- особенности полива
    soil              TEXT NOT NULL DEFAULT '',  -- какой должна быть почва перед поливом
    temperature       TEXT NOT NULL DEFAULT '',
    light             TEXT NOT NULL DEFAULT '',
    air_humidity      TEXT NOT NULL DEFAULT '',
    feed_days         INTEGER,                   -- подкормка только в сезон
    repot_years       INTEGER,
    spray_days        INTEGER,                   -- опрыскивание в отопительный сезон
    wipe_days         INTEGER,                   -- протирка листьев
    toxic             TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    facts             TEXT NOT NULL DEFAULT '',  -- справочные факты о виде
    diseases          TEXT NOT NULL DEFAULT '',  -- частые болезни и вредители
    repotting         TEXT NOT NULL DEFAULT '',  -- рекомендации по пересадке
    created_at        TEXT NOT NULL,
    archived_at       TEXT,                      -- в архиве (погибло, подарено): история сохраняется
    archive_reason    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY,
    plant_id   INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    data       BLOB NOT NULL,
    thumb      BLOB NOT NULL,
    is_main    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Заметки дневника. processed_at заполнит будущая LLM-автоматизация (после правки записи — снова NULL).
CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY,
    plant_id     INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    photo_id     INTEGER REFERENCES photos(id) ON DELETE SET NULL,  -- фото к записи (оно же в галерее)
    updated_at   TEXT
);

-- Выполненный уход: water / feed / repot / spray / wipe.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    done_at  TEXT NOT NULL
);

-- «Отложить»: до дня until (ГГГГ-ММ-ДД) задача не напоминает. Снимается отметкой «сделано».
CREATE TABLE IF NOT EXISTS snoozes (
    plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    until    TEXT NOT NULL,
    PRIMARY KEY (plant_id, kind)
);

-- Настройки со страницы «Настройки» (значение — JSON)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_photos_plant ON photos(plant_id);
CREATE INDEX IF NOT EXISTS idx_comments_plant ON comments(plant_id);
CREATE INDEX IF NOT EXISTS idx_events_plant_kind ON events(plant_id, kind);
"""


# Редактируемые поля растения (имена колонок — константы, не пользовательский ввод)
PLANT_FIELDS = (
    "name", "species", "location",
    "water_summer_days", "water_winter_days", "water_ml", "water_how", "soil",
    "temperature", "light", "air_humidity",
    "feed_days", "repot_years", "spray_days", "wipe_days",
    "toxic", "notes", "facts", "diseases", "repotting",
)
INT_FIELDS = ("water_summer_days", "water_winter_days", "water_ml",
              "feed_days", "repot_years", "spray_days", "wipe_days")
LONG_TEXT_FIELDS = ("notes", "facts", "diseases", "repotting")  # до 10000 символов, остальной текст — до 2000
_PLANT_COLUMNS = ", ".join(PLANT_FIELDS)
_PLANT_PARAMS = ", ".join(f":{f}" for f in PLANT_FIELDS)
_PLANT_SET = ", ".join(f"{f} = :{f}" for f in PLANT_FIELDS)


def insert_plant(db, values: dict, created_at: str) -> int:
    cur = db.execute(
        f"INSERT INTO plants ({_PLANT_COLUMNS}, created_at) VALUES ({_PLANT_PARAMS}, :created_at)",
        {**values, "created_at": created_at},
    )
    return cur.lastrowid


def update_plant(db, plant_id: int, values: dict) -> None:
    db.execute(f"UPDATE plants SET {_PLANT_SET} WHERE id = :id", {**values, "id": plant_id})


def add_photo(db, plant_id: int, data: bytes, thumb: bytes, created_at: str) -> int:
    has_main = db.execute(
        "SELECT 1 FROM photos WHERE plant_id = ? AND is_main = 1", (plant_id,)
    ).fetchone()
    cur = db.execute(
        "INSERT INTO photos (plant_id, data, thumb, is_main, created_at) VALUES (?, ?, ?, ?, ?)",
        (plant_id, data, thumb, 0 if has_main else 1, created_at),
    )
    return cur.lastrowid


def record_done(db, plant_id: int, kind: str, done_at: str) -> int | None:
    """Отмечает выполненный уход и снимает «отложить». Возвращает id нового события.
    Повторная отметка того же действия в тот же день (двойной клик, кнопка в двух
    сообщениях) второго события не создаёт — тогда результат None."""
    db.execute("DELETE FROM snoozes WHERE plant_id = ? AND kind = ?", (plant_id, kind))
    if db.execute(
        "SELECT 1 FROM events WHERE plant_id = ? AND kind = ? AND substr(done_at, 1, 10) = ?",
        (plant_id, kind, done_at[:10]),
    ).fetchone():
        return None
    cur = db.execute("INSERT INTO events (plant_id, kind, done_at) VALUES (?, ?, ?)", (plant_id, kind, done_at))
    return cur.lastrowid


def set_snooze(db, plant_id: int, kind: str, until: str | None) -> None:
    """Отложить задачу до дня until (ГГГГ-ММ-ДД); None — снять «отложить»."""
    if until is None:
        db.execute("DELETE FROM snoozes WHERE plant_id = ? AND kind = ?", (plant_id, kind))
    else:
        db.execute("INSERT INTO snoozes (plant_id, kind, until) VALUES (?, ?, ?) "
                   "ON CONFLICT (plant_id, kind) DO UPDATE SET until = excluded.until", (plant_id, kind, until))


def get_setting(db, key: str):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_setting(db, key: str, value) -> None:
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    """Соединение с автоматическим commit/rollback и закрытием."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    folder = os.path.dirname(DB_PATH) or "."
    os.makedirs(folder, exist_ok=True)
    # SQLite в режиме WAL создаёт рядом с базой служебные файлы — нужна запись и в папку, и в сам файл
    if not os.access(folder, os.W_OK) or (os.path.exists(DB_PATH) and not os.access(DB_PATH, os.W_OK)):
        raise RuntimeError(
            f"Нет прав на запись в {folder} (процесс работает с uid {os.getuid()}). "
            f"На сервере выполните в папке проекта: sudo chown -R {os.getuid()}:{os.getgid()} data"
        )
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")  # web и напоминания работают с одной БД
        db.executescript(SCHEMA)
        # миграция для баз, созданных до появления новых колонок (имена и типы — константы MIGRATIONS)
        for table, added in MIGRATIONS.items():
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, ddl in added:
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


MIGRATIONS = {
    "plants": [
        ("facts", "TEXT NOT NULL DEFAULT ''"),
        ("diseases", "TEXT NOT NULL DEFAULT ''"),
        ("repotting", "TEXT NOT NULL DEFAULT ''"),
        ("archived_at", "TEXT"),
        ("archive_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "comments": [
        ("photo_id", "INTEGER REFERENCES photos(id) ON DELETE SET NULL"),
        ("updated_at", "TEXT"),
    ],
}

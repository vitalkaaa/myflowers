"""Резервные копии БД (вместе с фото — они хранятся в ней же).

Копия делается через VACUUM INTO: это безопасно при работающем сайте и даёт
целостный, сжатый файл. Хранятся BACKUP_KEEP последних копий в data/backups/.

    python -m app.backup          # сделать копию прямо сейчас

По расписанию BACKUP_CRON копию делает контейнер reminders.
"""
import logging
import os
from datetime import datetime
from pathlib import Path

from app import config
from app.db import connect

log = logging.getLogger("backup")
BACKUP_DIR = Path(config.DB_PATH).parent / "backups"


def make_backup() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"flowers-{datetime.now(config.TZ):%Y-%m-%d_%H%M%S}.db"
    conn = connect()
    try:
        conn.execute("VACUUM INTO ?", (str(path),))
    finally:
        conn.close()
    os.chmod(path, 0o600)  # в копии весь дневник и фото — читать только владельцу
    remove_old()
    return path


def remove_old() -> None:
    """Удаляет самые старые копии сверх BACKUP_KEEP (имена с датой — сортировка по имени = по времени)."""
    backups = sorted(BACKUP_DIR.glob("flowers-*.db"))
    for old in backups[:-config.BACKUP_KEEP]:
        old.unlink()


def job() -> None:
    try:
        path = make_backup()
        log.info("Резервная копия: %s (%d КБ)", path.name, path.stat().st_size // 1024)
    except Exception:
        log.exception("Не удалось сделать резервную копию")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    job()

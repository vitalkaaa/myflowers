"""Резервные копии и дамп для LLM."""
import sqlite3

from conftest import CREATED

from app import backup, config, dump
from app.db import get_db


def test_backup_is_a_working_copy_and_old_ones_are_removed(db_plants, monkeypatch, tmp_path):
    db_plants(name="Папоротник")
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    for i in range(3):
        (tmp_path / f"flowers-2000-01-0{i + 1}_000000.db").write_bytes(b"old")
    path = backup.make_backup()
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT name FROM plants").fetchall() == [("Папоротник",)]
    copy.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["flowers-2000-01-03_000000.db", path.name]
    assert path.stat().st_mode & 0o077 == 0


def test_dump_mark_processed(db_plants):
    plant = db_plants(name="Папоротник")
    with get_db() as db:
        db.execute("INSERT INTO comments (plant_id, text, created_at) VALUES (?, 'новый лист', ?)", (plant, CREATED))
    data = dump.build_dump(unprocessed_only=True)
    assert [c["text"] for c in data["plants"][0]["comments"]] == ["новый лист"]
    assert dump.mark_processed(data) == 1
    assert dump.build_dump(unprocessed_only=True)["plants"][0]["comments"] == []
    assert dump.build_dump()["plants"][0]["comments"][0]["processed_at"] == data["exported_at"]

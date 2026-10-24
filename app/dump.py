"""Дамп растений с комментариями (записями дневника) в JSON — например, для обработки LLM.

    python -m app.dump                       # в stdout
    python -m app.dump -o plants.json        # в файл
    python -m app.dump --unprocessed         # только комментарии без processed_at
    python -m app.dump --unprocessed --mark-processed -o new.json
                                             # выгрузить новые и пометить их обработанными

Пометка ставится только после успешной записи дампа, так что записи не теряются.

Фото в дамп не попадают (только их количество).
"""
import argparse
import json
import sys

from app import care
from app.db import get_db


def build_dump(unprocessed_only: bool = False) -> dict:
    with get_db() as db:
        plants = db.execute("SELECT * FROM plants ORDER BY name").fetchall()
        comment_sql = "SELECT id, text, created_at, processed_at FROM comments WHERE plant_id = ?"
        if unprocessed_only:
            comment_sql += " AND processed_at IS NULL"
        result = []
        for plant in plants:
            comments = db.execute(comment_sql + " ORDER BY created_at", (plant["id"],)).fetchall()
            events = db.execute(
                "SELECT kind, done_at FROM events WHERE plant_id = ? ORDER BY done_at", (plant["id"],)
            ).fetchall()
            photos = db.execute("SELECT COUNT(*) FROM photos WHERE plant_id = ?", (plant["id"],)).fetchone()[0]
            result.append({
                **dict(plant),
                "photos_count": photos,
                "comments": [dict(c) for c in comments],
                "care_events": [dict(e) for e in events],
            })
    return {"exported_at": care.now_str(), "plants": result}


def mark_processed(dump: dict) -> int:
    """Ставит processed_at комментариям из дампа, которые ещё не обработаны. Возвращает их число."""
    ids = [c["id"] for plant in dump["plants"] for c in plant["comments"] if c["processed_at"] is None]
    with get_db() as db:
        db.executemany("UPDATE comments SET processed_at = ? WHERE id = ? AND processed_at IS NULL",
                       [(dump["exported_at"], comment_id) for comment_id in ids])
    return len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="Дамп растений с комментариями в JSON")
    parser.add_argument("-o", "--out", help="файл для записи (по умолчанию stdout)")
    parser.add_argument("--unprocessed", action="store_true", help="только необработанные комментарии")
    parser.add_argument("--mark-processed", action="store_true",
                        help="после выгрузки пометить комментарии обработанными")
    args = parser.parse_args()

    dump = build_dump(args.unprocessed)
    text = json.dumps(dump, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Сохранено: {args.out}", file=sys.stderr)
    else:
        print(text)
    if args.mark_processed:
        print(f"Помечено обработанными: {mark_processed(dump)}", file=sys.stderr)


if __name__ == "__main__":
    main()

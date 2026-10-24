"""Дамп растений с комментариями (записями дневника) в JSON — например, для обработки LLM.

    python -m app.dump                       # в stdout
    python -m app.dump -o plants.json        # в файл
    python -m app.dump --unprocessed         # только комментарии без processed_at

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Дамп растений с комментариями в JSON")
    parser.add_argument("-o", "--out", help="файл для записи (по умолчанию stdout)")
    parser.add_argument("--unprocessed", action="store_true", help="только необработанные комментарии")
    args = parser.parse_args()

    text = json.dumps(build_dump(args.unprocessed), ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Сохранено: {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()

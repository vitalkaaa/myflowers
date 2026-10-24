"""Расчёт сроков ухода. Используется и сайтом, и рассылкой напоминаний."""
from datetime import date, datetime, timedelta

from app.config import TZ

TASKS = {
    "water": "💧 Полив",
    "feed": "🌱 Подкормка",
    "repot": "🪴 Пересадка",
    "spray": "💦 Опрыскивание",
    "wipe": "🧽 Протереть листья",
}
DONE_LABELS = {
    "water": "Полил(а)",
    "feed": "Подкормил(а)",
    "repot": "Пересадил(а)",
    "spray": "Опрыскал(а)",
    "wipe": "Протёр(ла)",
}

# для кнопок в Telegram: «Монстера — полито»
DONE_STATES = {
    "water": "полито",
    "feed": "подкормлено",
    "repot": "пересажено",
    "spray": "опрыскано",
    "wipe": "листья протёрты",
}

SEASON_MONTHS = range(4, 10)               # апрель–сентябрь: активный рост
HEATING_MONTHS = (10, 11, 12, 1, 2, 3, 4)  # сухой воздух от батарей
REPOT_MONTHS = (3, 4)                      # пересаживаем только весной


def now_str() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(TZ).date()


def is_season(day: date) -> bool:
    return day.month in SEASON_MONTHS


def last_done_map(db) -> dict[int, dict[str, str]]:
    """{plant_id: {kind: дата последнего выполнения}}"""
    rows = db.execute(
        "SELECT plant_id, kind, MAX(done_at) AS done_at FROM events GROUP BY plant_id, kind"
    )
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        result.setdefault(row["plant_id"], {})[row["kind"]] = row["done_at"]
    return result


def interval_for(plant, kind: str, day: date) -> tuple[int | None, bool]:
    """(интервал в днях, задача по сезону в этот день). Интервал None — задача не задана."""
    if kind == "water":
        return (plant["water_summer_days"] if is_season(day) else plant["water_winter_days"]), True
    if kind == "feed":
        return plant["feed_days"], is_season(day)
    if kind == "repot":
        return (plant["repot_years"] or 0) * 365 or None, day.month in REPOT_MONTHS
    if kind == "spray":
        return plant["spray_days"], day.month in HEATING_MONTHS
    return plant["wipe_days"], True


def next_due(plant, last_done: dict[str, str], kind: str, interval: int) -> date:
    """Дата следующего выполнения: последняя отметка + интервал.

    Если отметок ещё нет — от даты добавления растения; полив в этом случае
    назначается на следующий день: когда растение поливали до этого, неизвестно.
    """
    last = last_done.get(kind)
    if last:
        return date.fromisoformat(last[:10]) + timedelta(days=interval)
    created = date.fromisoformat(plant["created_at"][:10])
    return created + timedelta(days=1 if kind == "water" else interval)


def plant_tasks(plant, last_done: dict[str, str], day: date) -> list[dict]:
    """Задачи ухода за растением с количеством дней до следующего выполнения.

    active=False — задача сейчас не по сезону (например, подкормка зимой).
    Если задача ещё ни разу не выполнялась — см. next_due.
    """
    tasks = []
    for kind in TASKS:
        interval, active = interval_for(plant, kind, day)
        if not interval:
            continue
        last = last_done.get(kind)
        days_left = (next_due(plant, last_done, kind, interval) - day).days
        tasks.append({
            "kind": kind,
            "title": TASKS[kind],
            "done_label": DONE_LABELS[kind],
            "interval": interval,
            "last": last[:10] if last else None,
            "days_left": days_left,
            "active": active,
            "due": active and days_left <= 0,
        })
    return tasks


def planned(plant, last_done: dict[str, str], day: date, end: date) -> list[tuple[date, str, bool]]:
    """Плановые даты ухода с сегодняшнего дня по end: [(дата, kind, просрочено)].

    Просроченное ставится на сегодня. Дальше считаем, что всё делается вовремя;
    даты не по сезону (подкормка зимой и т.п.) пропускаем.
    """
    result = []
    for kind in TASKS:
        interval, _ = interval_for(plant, kind, day)
        if not interval:
            continue
        due = next_due(plant, last_done, kind, interval)
        overdue = due < day
        due = max(due, day)
        while due <= end:
            interval, active = interval_for(plant, kind, due)
            if active:
                result.append((due, kind, overdue))
            overdue = False
            due += timedelta(days=interval)
    return result


def water_volume(plant, day: date | None = None) -> int | None:
    """Примерный объём полива: зимой растения пьют примерно вдвое меньше."""
    if not plant["water_ml"]:
        return None
    return plant["water_ml"] if is_season(day or today()) else round(plant["water_ml"] / 2)


def days_left_text(days_left: int) -> str:
    if days_left < 0:
        return f"просрочено на {-days_left} дн."
    if days_left == 0:
        return "сегодня"
    if days_left == 1:
        return "завтра"
    return f"через {days_left} дн."

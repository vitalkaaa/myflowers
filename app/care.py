"""Расчёт сроков ухода. Используется и сайтом, и рассылкой напоминаний."""
import statistics
from dataclasses import dataclass, field
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

REPOT_MONTHS = (3, 4)  # пересаживаем только весной
MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
SNOOZE_MAX_DAYS = 14


@dataclass(frozen=True)
class Seasons:
    """Сезоны (используется SEASONS ниже). Месяцы включительно, переход через Новый год допустим."""
    grow_start: int = 4       # сезон роста: летний интервал полива, подкормка
    grow_end: int = 9
    heat_start: int = 10      # отопительный сезон: опрыскивание
    heat_end: int = 4
    transition_days: int = 0  # 0 — полив меняется скачком; N — плавно за N дней вокруг границы сезона

    @staticmethod
    def months(start: int, end: int) -> tuple[int, ...]:
        """(4, 9) -> 4…9; (10, 4) -> 10, 11, 12, 1…4"""
        return tuple((start - 1 + i) % 12 + 1 for i in range((end - start) % 12 + 1))

    def is_grow(self, day: date) -> bool:
        return day.month in self.months(self.grow_start, self.grow_end)

    def is_heating(self, day: date) -> bool:
        return day.month in self.months(self.heat_start, self.heat_end)

    def grow_share(self, day: date) -> float:
        """Доля «летнего» режима: 1 — сезон роста, 0 — покой, между ними — плавный переход."""
        base = 1.0 if self.is_grow(day) else 0.0
        if not self.transition_days or len(self.months(self.grow_start, self.grow_end)) == 12:
            return base
        half = self.transition_days / 2
        end_month = self.grow_end % 12 + 1  # первый месяц покоя
        for year in (day.year - 1, day.year, day.year + 1):
            for month, sign in ((self.grow_start, 1), (end_month, -1)):
                offset = (day - date(year, month, 1)).days
                if -half <= offset < half:
                    return min(1.0, max(0.0, 0.5 + sign * offset / self.transition_days))
        return base

    def label(self) -> str:
        """«апр–сен»"""
        return f"{MONTHS[self.grow_start - 1]}–{MONTHS[self.grow_end - 1]}"

    def rest_label(self) -> str:
        return f"{MONTHS[self.grow_end % 12]}–{MONTHS[(self.grow_start - 2) % 12]}"

    def heat_label(self) -> str:
        return f"{MONTHS[self.heat_start - 1]}–{MONTHS[self.heat_end - 1]}"


# Сезоны для всех растений: рост апр–сен, отопление окт–апр. Интервал полива и объём меняются
# не скачком 1 апреля / 1 октября, а плавно за месяц: с середины марта до середины апреля и т.д.
SEASONS = Seasons(transition_days=30)


@dataclass
class Snapshot:
    """Всё, что нужно для расчёта сроков: последние отметки, «отложить» и сезоны (в тестах — свои)."""
    last_done: dict[int, dict[str, str]] = field(default_factory=dict)  # plant_id -> kind -> done_at
    snoozed: dict[int, dict[str, str]] = field(default_factory=dict)    # plant_id -> kind -> ГГГГ-ММ-ДД
    seasons: Seasons = SEASONS


def snapshot(db) -> Snapshot:
    snap = Snapshot(last_done=last_done_map(db))
    for row in db.execute("SELECT plant_id, kind, until FROM snoozes"):
        snap.snoozed.setdefault(row["plant_id"], {})[row["kind"]] = row["until"]
    return snap


def now_str() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(TZ).date()


def last_done_map(db) -> dict[int, dict[str, str]]:
    """{plant_id: {kind: дата последнего выполнения}}"""
    rows = db.execute(
        "SELECT plant_id, kind, MAX(done_at) AS done_at FROM events GROUP BY plant_id, kind"
    )
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        result.setdefault(row["plant_id"], {})[row["kind"]] = row["done_at"]
    return result


def interval_for(plant, kind: str, day: date, seasons: Seasons = SEASONS) -> tuple[int | None, bool]:
    """(интервал в днях, задача по сезону в этот день). Интервал None — задача не задана."""
    if kind == "water":
        summer, winter = plant["water_summer_days"], plant["water_winter_days"]
        return round(winter + (summer - winter) * seasons.grow_share(day)), True
    if kind == "feed":
        return plant["feed_days"], seasons.is_grow(day)
    if kind == "repot":
        return (plant["repot_years"] or 0) * 365 or None, day.month in REPOT_MONTHS
    if kind == "spray":
        return plant["spray_days"], seasons.is_heating(day)
    return plant["wipe_days"], True


def next_due(plant, last_done: dict[str, str], kind: str, interval: int, snoozed: str | None = None) -> date:
    """Дата следующего выполнения: последняя отметка + интервал, но не раньше дня «отложить».

    Если отметок ещё нет — от даты добавления растения; полив в этом случае
    назначается на следующий день: когда растение поливали до этого, неизвестно.
    """
    last = last_done.get(kind)
    if last:
        due = date.fromisoformat(last[:10]) + timedelta(days=interval)
    else:
        created = date.fromisoformat(plant["created_at"][:10])
        due = created + timedelta(days=1 if kind == "water" else interval)
    return max(due, date.fromisoformat(snoozed)) if snoozed else due


def plant_tasks(plant, snap: Snapshot, day: date) -> list[dict]:
    """Задачи ухода за растением с количеством дней до следующего выполнения.

    active=False — задача сейчас не по сезону (например, подкормка зимой).
    snoozed — до какого дня задача отложена (если отложена на будущее).
    Если задача ещё ни разу не выполнялась — см. next_due.
    """
    last_done = snap.last_done.get(plant["id"], {})
    snoozed = snap.snoozed.get(plant["id"], {})
    tasks = []
    for kind in TASKS:
        interval, active = interval_for(plant, kind, day, snap.seasons)
        if not interval:
            continue
        last = last_done.get(kind)
        until = snoozed.get(kind)
        days_left = (next_due(plant, last_done, kind, interval, until) - day).days
        tasks.append({
            "kind": kind,
            "title": TASKS[kind],
            "done_label": DONE_LABELS[kind],
            "interval": interval,
            "last": last[:10] if last else None,
            "days_left": days_left,
            "active": active,
            "due": active and days_left <= 0,
            "snoozed": until if until and date.fromisoformat(until) > day else None,
        })
    return tasks


def planned(plant, snap: Snapshot, day: date, end: date) -> list[tuple[date, str, date | None]]:
    """Плановые даты ухода с сегодняшнего дня по end: [(дата, kind, с какого дня просрочено)].

    Просроченное ставится на сегодня, третий элемент — исходный срок (иначе None).
    Дальше считаем, что всё делается вовремя; даты не по сезону (подкормка зимой и т.п.) пропускаем.
    """
    last_done = snap.last_done.get(plant["id"], {})
    snoozed = snap.snoozed.get(plant["id"], {})
    result = []
    for kind in TASKS:
        interval, _ = interval_for(plant, kind, day, snap.seasons)
        if not interval:
            continue
        due = next_due(plant, last_done, kind, interval, snoozed.get(kind))
        overdue_from = due if due < day else None
        due = max(due, day)
        while due <= end:
            interval, active = interval_for(plant, kind, due, snap.seasons)
            if active:
                result.append((due, kind, overdue_from))
            overdue_from = None
            due += timedelta(days=interval)
    return result


def water_volume(plant, day: date | None = None, seasons: Seasons = SEASONS) -> int | None:
    """Примерный объём полива: в покое растения пьют примерно вдвое меньше."""
    if not plant["water_ml"]:
        return None
    return round(plant["water_ml"] * (0.5 + 0.5 * seasons.grow_share(day or today())))


SUGGEST_MIN_GAPS = 3   # подсказываем, только когда есть хотя бы столько промежутков между поливами
SUGGEST_LAST_GAPS = 8  # и смотрим только на последние


def suggest_interval(plant, water_dates: list[str], day: date, seasons: Seasons = SEASONS) -> dict | None:
    """Подсказка по истории: обычный промежуток между поливами в текущем сезоне,
    если он заметно отличается от интервала в карточке. None — подсказки нет."""
    grow = seasons.is_grow(day)
    column = "water_summer_days" if grow else "water_winter_days"
    current = plant[column]
    days = sorted({date.fromisoformat(d[:10]) for d in water_dates})
    gaps = [(b - a).days for a, b in zip(days, days[1:]) if seasons.is_grow(a) == grow == seasons.is_grow(b)]
    gaps = [g for g in gaps if g <= current * 3][-SUGGEST_LAST_GAPS:]  # отпуск и забытые поливы не в счёт
    if len(gaps) < SUGGEST_MIN_GAPS:
        return None
    typical = round(statistics.median(gaps))
    if abs(typical - current) < max(2, current * 0.25):
        return None
    return {"column": column, "current": current, "typical": typical, "gaps": len(gaps),
            "season": seasons.label() if grow else seasons.rest_label()}


def days_left_text(days_left: int) -> str:
    if days_left < 0:
        return f"просрочено на {-days_left} дн."
    if days_left == 0:
        return "сегодня"
    if days_left == 1:
        return "завтра"
    return f"через {days_left} дн."

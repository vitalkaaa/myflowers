from datetime import date

from conftest import plant_row

from app import care

CREATED_DAY = date(2026, 1, 10)


def test_water_starts_tomorrow_without_events():
    plant = plant_row()
    assert care.next_due(plant, {}, "water", 10) == date(2026, 1, 11)


def test_other_tasks_start_after_interval_without_events():
    plant = plant_row(wipe_days=14)
    assert care.next_due(plant, {}, "wipe", 14) == date(2026, 1, 24)


def test_next_due_counts_from_last_event():
    plant = plant_row()
    assert care.next_due(plant, {"water": "2026-02-01T09:00:00+03:00"}, "water", 10) == date(2026, 2, 11)


def test_intervals_depend_on_season():
    plant = plant_row(feed_days=14, spray_days=3, repot_years=2)
    winter, summer, spring = date(2026, 1, 15), date(2026, 7, 15), date(2026, 3, 15)
    assert care.interval_for(plant, "water", winter) == (10, True)
    assert care.interval_for(plant, "water", summer) == (5, True)
    assert care.interval_for(plant, "feed", winter) == (14, False)   # зимой не подкармливаем
    assert care.interval_for(plant, "feed", summer) == (14, True)
    assert care.interval_for(plant, "spray", winter) == (3, True)    # отопительный сезон
    assert care.interval_for(plant, "spray", summer) == (3, False)
    assert care.interval_for(plant, "repot", spring) == (730, True)
    assert care.interval_for(plant, "repot", winter) == (730, False)


def test_unset_task_is_skipped():
    tasks = care.plant_tasks(plant_row(), {}, CREATED_DAY)
    assert [t["kind"] for t in tasks] == ["water"]


def test_plant_tasks_due_and_days_left():
    [water] = care.plant_tasks(plant_row(), {}, date(2026, 1, 13))
    assert water["days_left"] == -2 and water["due"] and water["last"] is None


def test_planned_puts_overdue_on_today_then_repeats():
    plan = care.planned(plant_row(), {}, date(2026, 1, 13), date(2026, 2, 3))
    assert plan == [
        (date(2026, 1, 13), "water", True),
        (date(2026, 1, 23), "water", False),
        (date(2026, 2, 2), "water", False),
    ]


def test_planned_skips_off_season_tasks():
    plan = care.planned(plant_row(feed_days=30), {}, date(2026, 1, 13), date(2026, 5, 31))
    feed_days = [day for day, kind, _ in plan if kind == "feed"]
    assert feed_days and all(day.month in care.SEASON_MONTHS for day in feed_days)


def test_water_volume_halves_in_winter():
    plant = plant_row(water_ml=301)
    assert care.water_volume(plant, date(2026, 7, 1)) == 301
    assert care.water_volume(plant, date(2026, 1, 1)) == 150
    assert care.water_volume(plant_row(water_ml=None), date(2026, 7, 1)) is None


def test_days_left_text():
    assert care.days_left_text(-3) == "просрочено на 3 дн."
    assert care.days_left_text(0) == "сегодня"
    assert care.days_left_text(1) == "завтра"
    assert care.days_left_text(5) == "через 5 дн."

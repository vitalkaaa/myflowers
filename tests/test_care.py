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
    tasks = care.plant_tasks(plant_row(), care.Snapshot(), CREATED_DAY)
    assert [t["kind"] for t in tasks] == ["water"]


def test_plant_tasks_due_and_days_left():
    [water] = care.plant_tasks(plant_row(), care.Snapshot(), date(2026, 1, 13))
    assert water["days_left"] == -2 and water["due"] and water["last"] is None


def test_planned_puts_overdue_on_today_then_repeats():
    plan = care.planned(plant_row(), care.Snapshot(), date(2026, 1, 13), date(2026, 2, 3))
    assert plan == [
        (date(2026, 1, 13), "water", date(2026, 1, 11)),  # просрочено: исходный срок — 11 января
        (date(2026, 1, 23), "water", None),
        (date(2026, 2, 2), "water", None),
    ]


def test_planned_skips_off_season_tasks():
    plan = care.planned(plant_row(feed_days=30), care.Snapshot(), date(2026, 1, 13), date(2026, 5, 31))
    feed_days = [day for day, kind, _ in plan if kind == "feed"]
    assert feed_days and all(care.DEFAULT_SEASONS.is_grow(day) for day in feed_days)


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


def test_snooze_moves_due_date_until_done():
    plant = plant_row()
    snap = care.Snapshot(snoozed={1: {"water": "2026-01-15"}})
    [water] = care.plant_tasks(plant, snap, date(2026, 1, 13))
    assert water["days_left"] == 2 and not water["due"] and water["snoozed"] == "2026-01-15"
    [water] = care.plant_tasks(plant, snap, date(2026, 1, 15))
    assert water["due"] and water["snoozed"] is None
    # срок по интервалу позже дня «отложить» — «отложить» не сдвигает его назад
    assert care.next_due(plant, {"water": "2026-01-14T10:00"}, "water", 10, "2026-01-15") == date(2026, 1, 24)


def test_custom_seasons_and_wraparound():
    seasons = care.Seasons(grow_start=5, grow_end=8, heat_start=11, heat_end=3)
    assert care.Seasons.months(10, 4) == (10, 11, 12, 1, 2, 3, 4)
    assert seasons.is_grow(date(2026, 5, 1)) and not seasons.is_grow(date(2026, 9, 1))
    assert seasons.is_heating(date(2026, 1, 1)) and not seasons.is_heating(date(2026, 4, 1))
    assert care.interval_for(plant_row(), "water", date(2026, 4, 20), seasons) == (10, True)
    assert seasons.label() == "май–авг" and seasons.rest_label() == "сен–апр" and seasons.heat_label() == "ноя–мар"


def test_smooth_transition_between_seasons():
    seasons = care.Seasons(transition_days=30)
    plant = plant_row(water_summer_days=5, water_winter_days=15, water_ml=200)
    water = lambda day: care.interval_for(plant, "water", day, seasons)[0]  # noqa: E731
    assert water(date(2026, 7, 1)) == 5 and water(date(2026, 1, 1)) == 15
    assert water(date(2026, 9, 15)) == 5    # переход начинается за 15 дней до 1 октября
    assert water(date(2026, 10, 1)) == 10   # граница сезона — середина перехода
    assert water(date(2026, 10, 16)) == 15  # и заканчивается через 15 дней после
    assert 5 < water(date(2026, 9, 25)) < 10
    assert water(date(2026, 4, 1)) == 10    # весной — так же, в обратную сторону
    assert care.water_volume(plant, date(2026, 10, 1), seasons) == 150
    assert care.interval_for(plant, "water", date(2026, 10, 1)) == (15, True)  # без перехода — скачком


def test_suggest_interval_from_history():
    plant = plant_row(water_summer_days=7, water_winter_days=10)
    july = ["2026-07-01", "2026-07-06", "2026-07-11", "2026-07-16", "2026-07-21"]
    hint = care.suggest_interval(plant, july, date(2026, 7, 25))
    assert hint == {"column": "water_summer_days", "current": 7, "typical": 5, "gaps": 4, "season": "апр–сен"}
    assert care.suggest_interval(plant, july[:3], date(2026, 7, 25)) is None             # мало данных
    assert care.suggest_interval(plant_row(water_summer_days=5), july, date(2026, 7, 25)) is None  # совпадает
    assert care.suggest_interval(plant, july, date(2026, 12, 1)) is None                 # летние поливы не про зиму
    # отпуск (промежуток > 3 интервалов) не учитывается
    assert care.suggest_interval(plant, ["2026-06-01", *july], date(2026, 7, 25))["gaps"] == 4

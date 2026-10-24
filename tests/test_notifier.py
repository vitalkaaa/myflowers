import copy
from datetime import date

import pytest
from conftest import events

from app import care, notifier
from app.db import get_db

DAY = date(2026, 1, 11)  # день после добавления растений — пора поливать


@pytest.fixture
def calls(monkeypatch):
    """Вместо Telegram — список вызовов (метод, параметры)."""
    recorded = []
    monkeypatch.setattr(notifier.telegram, "call", lambda method, params, timeout=20: recorded.append((method, params)) or {})
    return recorded


def press(data: str, keyboard: list, chat_id: int = 42) -> None:
    message = {"message_id": 1, "chat": {"id": chat_id}, "reply_markup": {"inline_keyboard": keyboard}}
    notifier.handle_callback({"id": "cb", "from": {"id": 1}, "data": data, "message": message})


def last_answer(calls) -> str:
    return [p["text"] for m, p in calls if m == "answerCallbackQuery"][-1]


def test_build_messages_one_per_task_with_buttons(db_plants):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус", water_ml=None)
    [(text, keyboard)] = notifier.build_messages(DAY)
    assert "Полив" in text and "Папоротник — ≈ 100 мл" in text  # зима: половина объёма
    buttons = [b["callback_data"] for row in keyboard for b in row]
    assert buttons == [f"d:{fern}:water", f"d:{ficus}:water", "da:water", "ds:water:1", "ds:water:2"]


def test_nothing_due_no_messages(db_plants):
    db_plants(name="Папоротник")
    assert notifier.build_messages(date(2026, 1, 10)) == []


def test_press_writes_event_once(db_plants, calls):
    fern = db_plants(name="Папоротник")
    [(_, keyboard)] = notifier.build_messages(DAY)
    first = copy.deepcopy(keyboard)
    press(f"d:{fern}:water", keyboard)
    assert events() == [(fern, "water")]
    assert last_answer(calls).startswith("✅ Отмечено")

    press(f"d:{fern}:water", keyboard)       # та же кнопка уже отмечена
    assert last_answer(calls) == "Уже отмечено"
    press(f"d:{fern}:water", first)          # то же действие в тот же день из другого сообщения
    assert events() == [(fern, "water")]


def test_forged_button_is_ignored(db_plants, calls):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    keyboard = [[{"text": "x", "callback_data": f"d:{fern}:water"}]]
    press(f"d:{ficus}:water", keyboard)      # такой кнопки в сообщении нет
    assert events() == []


def test_mark_all(db_plants, calls):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(_, keyboard)] = notifier.build_messages(DAY)
    press("da:water", keyboard)
    assert sorted(events()) == sorted([(fern, "water"), (ficus, "water")])
    edited = [p for m, p in calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
    assert all(b["callback_data"] == "x" for row in edited for b in row)  # «Всё …» и «⏰» убраны


def test_other_chat_rejected(db_plants, calls):
    fern = db_plants(name="Папоротник")
    [(_, keyboard)] = notifier.build_messages(DAY)
    press(f"d:{fern}:water", keyboard, chat_id=777)
    assert events() == []
    assert last_answer(calls) == "Кнопка не из канала напоминаний"


def test_demo_buttons_write_nothing(db_plants, calls):
    db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(text, keyboard)] = notifier.build_messages(DAY, demo=True)
    assert "Пример" in text
    press(keyboard[0][0]["callback_data"], keyboard)
    press("pa:water", keyboard)
    assert events() == []
    assert "Это пример" in last_answer(calls)


def test_parse_button():
    assert notifier.parse_button("d:12:water") == (12, "water", False)
    assert notifier.parse_button("p:12:feed") == (12, "feed", True)
    for bad in ("d:12:drop", "d:abc:water", "d:1234567890:water", "da:water", "x", ""):
        assert notifier.parse_button(bad) is None


def snoozes() -> dict:
    with get_db() as db:
        return {(r["plant_id"], r["kind"]): r["until"] for r in db.execute("SELECT * FROM snoozes")}


def test_snooze_button_postpones_remaining_plants(db_plants, calls, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: DAY)
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(_, keyboard)] = notifier.build_messages(DAY)
    press(f"d:{fern}:water", keyboard)        # папоротник полит, фикус почва ещё влажная
    press("ds:water:2", keyboard)
    assert events() == [(fern, "water")]
    assert snoozes() == {(ficus, "water"): "2026-01-13"}
    assert last_answer(calls).startswith("⏰ Отложено до 13.01")
    assert notifier.build_messages(DAY) == []  # фикус отложен — больше не напоминаем
    [(_, again)] = notifier.build_messages(date(2026, 1, 13))
    assert [b["callback_data"] for b in again[0]] == [f"d:{ficus}:water"]


def test_snooze_demo_and_forged(db_plants, calls):
    db_plants(name="Папоротник")
    [(_, keyboard)] = notifier.build_messages(DAY, demo=True)
    press("ps:water:1", keyboard)
    press("ds:water:9", keyboard)             # такой кнопки нет и значение вне списка
    assert snoozes() == {} and events() == []
    assert notifier.parse_snooze("ds:water:1") == (1, False)
    assert notifier.parse_snooze("ds:water:5") is None and notifier.parse_snooze("ds:drop:1") is None


def test_repeat_reminder_only_unmarked(db_plants, calls, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: DAY)
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(_, keyboard)] = notifier.build_messages(DAY)
    press(f"d:{fern}:water", keyboard)
    calls.clear()
    notifier.repeat_job()
    [text] = [p["text"] for m, p in calls if m == "sendMessage"]
    assert "Ещё не отмечено" in text and "Фикус" in text and "Папоротник" not in text


def test_today_command(db_plants, calls, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: DAY)
    db_plants(name="Папоротник")
    notifier.handle_message({"chat": {"id": 777, "type": "private"}, "text": "/today"})
    assert calls == []                        # чужой чат — список растений не показываем
    notifier.handle_message({"chat": {"id": 42, "type": "private"}, "text": "/today@flowers_bot"})
    assert "Папоротник" in [p["text"] for m, p in calls if m == "sendMessage"][0]


def test_archived_plants_not_reminded(db_plants, calls):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(_, keyboard)] = notifier.build_messages(DAY)
    with get_db() as db:
        db.execute("UPDATE plants SET archived_at = '2026-01-11' WHERE id = ?", (ficus,))
    assert "Фикус" not in notifier.build_messages(DAY)[0][0]
    press(f"d:{ficus}:water", keyboard)       # кнопка из старого сообщения
    assert events() == []

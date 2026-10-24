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


def press(data: str, keyboard: list, chat_id: int = 42, text: str = "") -> None:
    message = {"message_id": 1, "chat": {"id": chat_id}, "text": text, "reply_markup": {"inline_keyboard": keyboard}}
    notifier.handle_callback({"id": "cb", "from": {"id": 1}, "data": data, "message": message})


def last_answer(calls) -> str:
    return [p["text"] for m, p in calls if m == "answerCallbackQuery"][-1]


def test_build_messages_one_per_task_with_buttons(db_plants):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус", water_ml=None)
    [(text, keyboard)] = notifier.build_messages(DAY)
    # текст — сводка, список растений — только в кнопках, по одной в ряд
    assert "Полив" in text and "Осталось 2 из 2 · всего ≈ 100 мл" in text and "Папоротник" not in text
    assert [[b["text"] for b in row] for row in keyboard[:2]] == [["Папоротник · 100 мл"], ["Фикус"]]  # зима: половина
    buttons = [b["callback_data"] for row in keyboard for b in row]
    assert buttons == [f"d:{fern}:water", f"d:{ficus}:water", "da:water", "ds:water:1", "ds:water:2"]


def test_plants_grouped_by_location_and_overdue_marked(db_plants):
    db_plants(name="Фикус", location="кухня"), db_plants(name="Алоэ", location="спальня")
    db_plants(name="Бегония", location="кухня")
    [(_, keyboard)] = notifier.build_messages(date(2026, 1, 13))
    names = [row[0]["text"] for row in keyboard[:3]]
    assert names == ["Бегония · 100 мл · ⚠️ +2 дн.", "Фикус · 100 мл · ⚠️ +2 дн.", "Алоэ · 100 мл · ⚠️ +2 дн."]


def last_edit(calls) -> dict:
    return [p for m, p in calls if m == "editMessageText"][-1]


def test_press_again_unmarks(db_plants, calls, monkeypatch):
    monkeypatch.setattr(notifier.care, "today", lambda: DAY)
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(text, keyboard)] = notifier.build_messages(DAY)
    message_text = text.replace("<b>", "").replace("</b>", "")  # Telegram присылает текст без разметки
    press(f"d:{fern}:water", keyboard, text=message_text)
    assert events() == [(fern, "water")]
    edit = last_edit(calls)
    assert "Осталось 1 из 2" in edit["text"] and "полито 1" in edit["text"]
    done_button = edit["reply_markup"]["inline_keyboard"][0][0]
    assert done_button["text"] == "✅ Папоротник" and done_button["callback_data"].startswith(f"du:{fern}:water:")
    press(done_button["callback_data"], edit["reply_markup"]["inline_keyboard"], text=message_text)
    assert events() == []
    assert last_answer(calls).startswith("↩️ Отметка снята")
    assert last_edit(calls)["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"d:{fern}:water"


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

    press(f"d:{fern}:water", keyboard)       # клавиатура в keyboard изменена на месте — кнопки уже нет
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
    edit = last_edit(calls)
    edited = edit["reply_markup"]["inline_keyboard"]
    assert all(b["callback_data"].startswith("du:") for row in edited for b in row)  # «Всё …» и «⏰» убраны
    assert "Готово: полито 2" in edit["text"]


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
    for bad in ("d:12:drop", "d:abc:water", "d:1234567890123:water", "da:water", "x", ""):
        assert notifier.parse_button(bad) is None
    assert notifier.parse("du:12:water:345") == notifier.Button("unmark", False, 12, "water", 345)
    assert notifier.parse("pz:12:feed") == notifier.Button("unsnooze", True, 12, "feed")
    assert notifier.parse("x").action == "legacy"  # кнопки сообщений, отправленных до обновления
    for bad in ("du:12:water", "du:12:water:x", "dq:12:water", "dd:12:water", "ds:water:3", "d:12:water:1"):
        assert notifier.parse(bad).action is None, bad


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
    # из сообщения отложенное можно вернуть повторным нажатием
    snoozed = last_edit(calls)["reply_markup"]["inline_keyboard"][1][0]
    assert snoozed["text"] == "⏰ Фикус · до 13.01"
    press(snoozed["callback_data"], last_edit(calls)["reply_markup"]["inline_keyboard"])
    assert snoozes() == {}


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
    [sent] = [p for m, p in calls if m == "sendMessage"]
    names = [row[0]["text"] for row in sent["reply_markup"]["inline_keyboard"]]
    assert "Ещё не отмечено" in sent["text"] and "Фикус · 100 мл" in names and not any("Папоротник" in n for n in names)


def test_today_command(db_plants, calls, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: DAY)
    db_plants(name="Папоротник")
    notifier.handle_message({"chat": {"id": 777, "type": "private"}, "text": "/today"})
    assert calls == []                        # чужой чат — список растений не показываем
    notifier.handle_message({"chat": {"id": 42, "type": "private"}, "text": "/today@flowers_bot"})
    [sent] = [p for m, p in calls if m == "sendMessage"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("Папоротник")


def test_archived_plants_not_reminded(db_plants, calls):
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус")
    [(_, keyboard)] = notifier.build_messages(DAY)
    with get_db() as db:
        db.execute("UPDATE plants SET archived_at = '2026-01-11' WHERE id = ?", (ficus,))
    [(_, fresh)] = notifier.build_messages(DAY)
    assert not any("Фикус" in b["text"] for row in fresh for b in row)
    press(f"d:{ficus}:water", keyboard)       # кнопка из старого сообщения
    assert events() == []

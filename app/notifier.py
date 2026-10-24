"""Напоминания в Telegram с кнопками «<растение> — полито».

Запускается отдельным контейнером: `python -m app.notifier`. Процесс:
  * по расписанию REMINDER_CRON шлёт по сообщению на каждое действие (полив, подкормка…)
    с кнопками для каждого растения, кнопкой «Всё …» и «⏰ +1 день / +2 дня» (отложить);
  * по расписанию REMINDER_REPEAT_CRON повторяет напоминание о том, что так и не отмечено;
  * постоянно опрашивает Telegram (getUpdates): записывает нажатия кнопок в events,
    на команду /today присылает дела на сегодня;
  * по расписанию BACKUP_CRON делает резервную копию БД (см. app/backup.py).

Проверка: `--test` — тестовое сообщение с кнопкой, `--now` — рассылка на сегодня,
`--preview [ГГГГ-ММ-ДД]` — пример рассылки на ближайший день с делами (кнопки в базу не пишут).
"""
import html
import logging
import sys
import time
from datetime import date, timedelta
from typing import NamedTuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup, care, config, telegram
from app.db import get_db, init_db, record_done, set_snooze

log = logging.getLogger("notifier")
PLANTS_PER_MESSAGE = 30  # в Telegram до 100 кнопок и 4096 символов в сообщении
NAME_LEN = 60            # длинные названия обрезаем в тексте и на кнопках


# ---------- рассылка ----------
#
# Сообщение — короткая сводка («осталось 21 из 23 · всего ≈ 2,6 л») и по кнопке на растение,
# по одной в ряд, чтобы названия не обрезались. Нажатие отмечает растение (✅), повторное — снимает
# отметку. При каждом нажатии сводка и кнопки «Всё …» / «⏰» пересчитываются.
#
# callback_data (первая буква: d — настоящая кнопка, p — пример, в базу не пишет):
#   d:<растение>:<действие>            отметить
#   du:<растение>:<действие>:<событие> снять отметку (✅)
#   dz:<растение>:<действие>           вернуть отложенное (⏰)
#   da:<действие>                      «✅ Всё …» — отметить все неотмеченные
#   ds:<действие>:<1|2>                «⏰ +N» — отложить все неотмеченные
#   x                                  уже отмечено (кнопки старых сообщений)

def short(name: str) -> str:
    return name if len(name) <= NAME_LEN else name[:NAME_LEN - 1] + "…"


def fmt_volume(ml: int) -> str:
    return f"{ml} мл" if ml < 1000 else f"{ml / 1000:.1f} л".replace(".", ",")


def plant_label(plant, kind: str, task: dict | None, day: date, seasons: care.Seasons) -> str:
    """Текст кнопки неотмеченного растения: «Монстера · 300 мл», просроченное — «… · ⚠️ +3 дн.».
    Название — первым: если кнопка не влезает в ширину экрана, Telegram обрезает конец."""
    label = short(plant["name"])
    volume = care.water_volume(plant, day, seasons) if kind == "water" else None
    if volume:
        label += f" · {volume} мл"
    if task and task["days_left"] < 0:
        label += f" · ⚠️ +{-task['days_left']} дн."
    return label


def header(kind: str, day: date, demo: bool = False, repeat: bool = False) -> str:
    mark = "🧪 Пример · " if demo else "🔁 Ещё не отмечено · " if repeat else "🌿 "
    return f"{mark}{care.TASKS[kind]} · {day:%d.%m.%Y}"


def build_messages(day: date | None = None, demo: bool = False, repeat: bool = False) -> list[tuple[str, list]]:
    """[(текст, inline-клавиатура)] — по сообщению на каждое действие, которое пора сделать в day.
    demo=True — пример: пометка «🧪 Пример» и кнопки, которые не пишут в базу.
    repeat=True — повторное напоминание о неотмеченном (в заголовке «🔁 Ещё не отмечено»)."""
    day = day or care.today()
    prefix = "p" if demo else "d"
    with get_db() as db:
        plants = db.execute("SELECT * FROM plants WHERE archived_at IS NULL").fetchall()
        snap = care.snapshot(db)

    due: dict[str, list[tuple]] = {}  # kind -> [(растение, задача)]
    # по комнатам, внутри — по названию: удобно идти по квартире сверху вниз по списку
    for plant in sorted(plants, key=lambda p: (p["location"].lower(), p["name"].lower())):
        for task in care.plant_tasks(plant, snap, day):
            if task["due"]:
                due.setdefault(task["kind"], []).append((plant, task))

    messages = []
    for kind in care.TASKS:
        items = due.get(kind, [])
        for start in range(0, len(items), PLANTS_PER_MESSAGE):
            buttons = [{"text": plant_label(plant, kind, task, day, snap.seasons),
                        "callback_data": f"{prefix}:{plant['id']}:{kind}"}
                       for plant, task in items[start:start + PLANTS_PER_MESSAGE]]
            text = summary(header(kind, day, demo, repeat), kind, buttons, day, snap.seasons)
            messages.append((text, assemble(buttons, kind, prefix)))
    return messages


def assemble(plant_buttons: list[dict], kind: str, prefix: str) -> list[list[dict]]:
    """Клавиатура: растения по одному в ряд, затем «Всё …» и «⏰», пока есть неотмеченные, и ссылка на сайт."""
    keyboard = [[button] for button in plant_buttons]
    left = sum(1 for b in plant_buttons if parse(b.get("callback_data", "")).action == "mark")
    if left > 1:
        keyboard.append([{"text": f"✅ Всё {care.DONE_STATES[kind]}", "callback_data": f"{prefix}a:{kind}"}])
    if left:
        keyboard.append([{"text": "⏰ +1 день", "callback_data": f"{prefix}s:{kind}:1"},
                         {"text": "⏰ +2 дня", "callback_data": f"{prefix}s:{kind}:2"}])
    if config.BASE_URL:
        keyboard.append([{"text": "🌐 Открыть сайт", "url": f"{config.BASE_URL}/"}])
    return keyboard


def summary(title: str, kind: str, plant_buttons: list[dict], day: date, seasons: care.Seasons) -> str:
    """Текст сообщения: заголовок и прогресс — «Осталось 21 из 23 · всего ≈ 2,6 л · полито 2»."""
    states = [parse(b.get("callback_data", "")) for b in plant_buttons]
    left = [s.plant_id for s in states if s.action == "mark"]
    done = sum(1 for s in states if s.action in ("unmark", "legacy"))
    snoozed = sum(1 for s in states if s.action == "unsnooze")
    progress = [f"{care.DONE_STATES[kind]} {done}"] if done else []
    if snoozed:
        progress.append(f"отложено {snoozed}")

    lines = [f"<b>{html.escape(title)}</b>"]
    if left:
        status = f"Осталось {len(left)} из {len(plant_buttons)}"
        if kind == "water":
            with get_db() as db:
                rows = db.execute(f"SELECT * FROM plants WHERE id IN ({', '.join('?' * len(left))})", left).fetchall()
            total = sum(care.water_volume(row, day, seasons) or 0 for row in rows)
            if total:
                status += f" · всего ≈ {fmt_volume(total)}"
        lines.append(" · ".join([status, *progress]))
        lines.append("<i>Нажмите на растение, когда сделаете; повторное нажатие снимает отметку</i>")
    else:
        lines.append("✅ Готово: " + ", ".join(progress or ["всё отмечено"]))
    return "\n".join(lines)


def send(text: str, keyboard: list | None = None) -> bool:
    params = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return telegram.call("sendMessage", params) is not None


def job(repeat: bool = False) -> None:
    try:
        messages = build_messages(repeat=repeat)
        if not messages:
            log.info("Неотмеченного ухода нет" if repeat else "Сегодня ухода не требуется")
            return
        sent = sum(send(text, keyboard) for text, keyboard in messages)
        log.info("Отправлено %s: %d из %d", "повторных напоминаний" if repeat else "напоминаний", sent, len(messages))
    except Exception:
        log.exception("Ошибка при отправке напоминаний")


def repeat_job() -> None:
    job(repeat=True)


def send_today() -> None:
    """Ответ на /today: дела на сегодня с кнопками или «всё сделано»."""
    messages = build_messages()
    if not messages:
        send("✨ На сегодня всё сделано")
    for text, keyboard in messages:
        send(text, keyboard)


def preview(day: date | None) -> None:
    """Пример рассылки: на указанный день или на ближайший (до 60 дней), когда есть дела."""
    days = [day] if day else [care.today() + timedelta(days=i) for i in range(61)]
    for candidate in days:
        messages = build_messages(candidate, demo=True)
        if messages:
            sent = sum(send(text, keyboard) for text, keyboard in messages)
            log.info("Пример на %s: отправлено сообщений %d из %d", candidate, sent, len(messages))
            return
    log.info("В ближайшие дни дел нет — пример отправлять не о чем")


# ---------- нажатия кнопок ----------

class Button(NamedTuple):
    action: str | None        # mark, unmark, unsnooze, all, snooze, legacy; None — не наша кнопка
    demo: bool = False
    plant_id: int = 0
    kind: str = ""
    number: int = 0           # для unmark — id события, для snooze — на сколько дней


def _id(raw: str) -> int | None:
    return int(raw) if raw.isdigit() and len(raw) <= 12 else None


def parse(data: str) -> Button:
    """Разбор callback_data (форматы — в начале раздела «рассылка»); неизвестное -> Button(None)."""
    if data == "x":
        return Button("legacy")
    parts = data.split(":")
    head, rest = parts[0], parts[1:]
    if not head or head[0] not in "dp" or len(head) > 2:
        return Button(None)
    demo, op = head[0] == "p", head[1:]
    if op in ("", "u", "z") and len(rest) >= 2 and rest[1] in care.TASKS and _id(rest[0]):
        if op == "" and len(rest) == 2:
            return Button("mark", demo, _id(rest[0]), rest[1])
        if op == "z" and len(rest) == 2:
            return Button("unsnooze", demo, _id(rest[0]), rest[1])
        if op == "u" and len(rest) == 3 and _id(rest[2]) is not None:
            return Button("unmark", demo, _id(rest[0]), rest[1], _id(rest[2]))
    if op == "a" and len(rest) == 1 and rest[0] in care.TASKS:
        return Button("all", demo, kind=rest[0])
    if op == "s" and len(rest) == 2 and rest[0] in care.TASKS and rest[1] in ("1", "2"):
        return Button("snooze", demo, kind=rest[0], number=int(rest[1]))
    return Button(None)


def parse_button(data: str) -> tuple[int, str, bool] | None:
    """'d:12:water' -> (12, 'water', False); 'p:…' — то же для примера (demo=True); иначе None."""
    button = parse(data)
    return (button.plant_id, button.kind, button.demo) if button.action == "mark" else None


def parse_snooze(data: str) -> tuple[int, bool] | None:
    """Кнопка «⏰ +N»: 'ds:water:1' -> (1, False); 'ps:…' — в примере; иначе None."""
    button = parse(data)
    return (button.number, button.demo) if button.action == "snooze" else None


PLANT_ACTIONS = ("mark", "unmark", "unsnooze", "legacy")


def apply(button: Button, until: date | None) -> tuple[str | None, dict | None]:
    """Выполняет нажатие для одного растения: (название, новая кнопка).
    Название None — растения нет или оно в архиве. В примере в базу ничего не пишется."""
    prefix = "p" if button.demo else "d"
    today = care.today()
    with get_db() as db:
        plant = db.execute("SELECT * FROM plants WHERE id = ? AND archived_at IS NULL", (button.plant_id,)).fetchone()
        if not plant:
            return None, {"text": "растение удалено или в архиве", "callback_data": "x"}
        name = short(plant["name"])
        if button.action == "mark" and until:
            if not button.demo:
                set_snooze(db, plant["id"], button.kind, until.isoformat())
            return plant["name"], {"text": f"⏰ {name} · до {until:%d.%m}",
                                   "callback_data": f"{prefix}z:{plant['id']}:{button.kind}"}
        if button.action == "mark":
            event_id = 0
            if not button.demo:
                event_id = record_done(db, plant["id"], button.kind, care.now_str()) or db.execute(
                    "SELECT id FROM events WHERE plant_id = ? AND kind = ? AND substr(done_at, 1, 10) = ? "
                    "ORDER BY id DESC LIMIT 1", (plant["id"], button.kind, today.isoformat())).fetchone()[0]
            return plant["name"], {"text": f"✅ {name}",
                                   "callback_data": f"{prefix}u:{plant['id']}:{button.kind}:{event_id}"}
        # снять отметку или «отложить» — кнопка снова становится обычной
        if not button.demo:
            if button.action == "unmark":
                db.execute("DELETE FROM events WHERE id = ? AND plant_id = ? AND kind = ?",
                           (button.number, plant["id"], button.kind))
            else:
                set_snooze(db, plant["id"], button.kind, None)
        snap = care.snapshot(db)
    task = next((t for t in care.plant_tasks(plant, snap, today) if t["kind"] == button.kind), None)
    return plant["name"], {"text": plant_label(plant, button.kind, task, today, snap.seasons),
                           "callback_data": f"{prefix}:{plant['id']}:{button.kind}"}


def is_our_chat(chat: dict) -> bool:
    target = config.TELEGRAM_CHAT_ID
    return str(chat.get("id")) == target or (bool(chat.get("username")) and f"@{chat['username']}" == target)


def answer(callback_id: str, text: str, alert: bool = False) -> None:
    telegram.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert})


def handle_callback(callback: dict) -> None:
    message = callback.get("message") or {}
    data = callback.get("data", "")

    # Нажатия принимаем только из чата TELEGRAM_CHAT_ID: в личке с ботом кнопки видит
    # только его владелец, в канале — все, кто может нажимать кнопки в этом канале.
    if not is_our_chat(message.get("chat", {})):
        answer(callback["id"], "Кнопка не из канала напоминаний")
        return
    if data == "t":
        answer(callback["id"], "✅ Кнопки работают", alert=True)
        return

    keyboard = message.get("reply_markup", {}).get("inline_keyboard", [])
    buttons = [button for row in keyboard for button in row]
    # принимаем только кнопки, которые действительно есть в этом сообщении (подделанные игнорируем)
    pressed = parse(data) if any(b.get("callback_data") == data for b in buttons) else Button(None)
    plant_buttons = [b for b in buttons if parse(b.get("callback_data", "")).action in PLANT_ACTIONS]
    if pressed.action in ("all", "snooze"):
        # «Всё полито» и «⏰ отложить» — для растений, которые в этом сообщении ещё не отмечены
        targets = [b for b in plant_buttons if parse(b["callback_data"]).action == "mark"]
    elif pressed.action in ("mark", "unmark", "unsnooze"):
        targets = [b for b in plant_buttons if b["callback_data"] == data]
    else:
        targets = []
    if not targets:
        answer(callback["id"], "Уже отмечено")
        return

    until = care.today() + timedelta(days=pressed.number) if pressed.action == "snooze" else None
    names, kind = [], pressed.kind
    for button in targets:
        name, new = apply(parse(button["callback_data"]), until)
        button.update(new)
        if name:
            names.append(name)

    prefix = "p" if pressed.demo else "d"
    title = (message.get("text") or "").split("\n")[0] or header(kind, care.today(), pressed.demo)
    telegram.call("editMessageText", {
        "chat_id": message["chat"]["id"], "message_id": message["message_id"],
        "text": summary(title, kind, plant_buttons, care.today(), care.SEASONS),
        "parse_mode": "HTML", "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": assemble(plant_buttons, kind, prefix)},
    })

    if not names:
        answer(callback["id"], "Растение не найдено или в архиве")
    elif pressed.demo:
        answer(callback["id"], "🧪 Это пример — в историю не записано: " + ", ".join(names))
    elif until:
        answer(callback["id"], f"⏰ Отложено до {until:%d.%m}: " + ", ".join(names))
    elif pressed.action == "unmark":
        answer(callback["id"], "↩️ Отметка снята: " + ", ".join(names))
    elif pressed.action == "unsnooze":
        answer(callback["id"], "↩️ Снова в списке: " + ", ".join(names))
    else:
        answer(callback["id"], "✅ Отмечено: " + ", ".join(names))


def command(message: dict) -> str:
    """'/today@my_bot extra' -> '/today'"""
    words = message.get("text", "").split()
    return words[0].split("@")[0].lower() if words else ""


def handle_message(message: dict) -> None:
    """/today — дела на сегодня; только в чате напоминаний, чтобы список растений не видели чужие.
    В личке на /start или /id бот сообщает chat ID — его нужно вписать в .env.
    Свой ID человеку и так известен, поэтому отвечаем любому; в лог ничего не пишем."""
    chat = message.get("chat", {})
    if command(message) == "/today":
        if is_our_chat(chat):
            send_today()
        return
    if chat.get("type") != "private" or command(message) not in ("/start", "/id"):
        return
    telegram.call("sendMessage", {"chat_id": chat["id"], "text": (
        "🌿 my-flowers\n\n"
        f"Чтобы напоминания приходили сюда, впишите в .env:\nTELEGRAM_CHAT_ID={chat['id']}\n\n"
        "Затем перезапустите бота: docker compose up -d --force-recreate reminders"
    )})


def poll_forever() -> None:
    """Long polling: Telegram держит запрос до 50 с и отдаёт нажатия кнопок."""
    offset = None
    while True:
        params = {"timeout": 50, "allowed_updates": ["callback_query", "message", "channel_post"]}
        if offset:
            params["offset"] = offset
        updates = telegram.call("getUpdates", params, timeout=60)
        if updates is None:
            time.sleep(10)  # нет связи — пробуем позже
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update or "channel_post" in update:  # в канале команды приходят как channel_post
                    handle_message(update.get("message") or update["channel_post"])
            except Exception:
                log.exception("Ошибка обработки сообщения от Telegram")


# ---------- запуск ----------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx логирует URL вместе с токеном
    config.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    # значение прокси не печатаем: в нём могут быть логин и пароль
    if not telegram.proxy_is_valid():
        log.error("TELEGRAM_PROXY должен начинаться с http://, https://, socks5:// или socks5h:// "
                  "(например socks5://host.docker.internal:1080); VPN-протоколы (vless, trojan, ss) не поддерживаются")
        sys.exit(1)
    init_db()

    if "--test" in sys.argv:  # проверка токена и канала: сообщение уходит, даже если дел нет
        bot = telegram.call("getMe", {})
        if bot:
            log.info("Токен принадлежит боту @%s — в личке нажмите Start именно в нём", bot.get("username"))
        ok = send("✅ my-flowers: бот подключён, напоминания будут приходить сюда.\n"
                  "Кнопка ниже проверяет, что нажатия доходят (контейнер reminders должен быть запущен).",
                  [[{"text": "Проверить кнопку", "callback_data": "t"}]])
        log.info("Тестовое сообщение отправлено" if ok else "Тестовое сообщение не отправлено")
        return
    if "--preview" in sys.argv:  # пример: как будет выглядеть рассылка в день ухода
        args = sys.argv[sys.argv.index("--preview") + 1:]
        try:
            day = date.fromisoformat(args[0]) if args else None
        except ValueError:
            log.error("Дата для --preview в формате ГГГГ-ММ-ДД, например 2026-11-02")
            sys.exit(1)
        preview(day)
        return
    if "--now" in sys.argv:  # разовая рассылка прямо сейчас
        job()
        return

    scheduler = BackgroundScheduler(timezone=config.TZ)
    scheduler.add_job(job, CronTrigger.from_crontab(config.REMINDER_CRON, timezone=config.TZ))
    if config.REMINDER_REPEAT_CRON:
        scheduler.add_job(repeat_job, CronTrigger.from_crontab(config.REMINDER_REPEAT_CRON, timezone=config.TZ))
    scheduler.add_job(backup.job, CronTrigger.from_crontab(config.BACKUP_CRON, timezone=config.TZ))
    scheduler.start()
    telegram.call("setMyCommands", {"commands": [{"command": "today", "description": "Что сделать сегодня"}]})
    log.info("Расписание напоминаний: %s, повтор: %s, резервных копий: %s (%s); жду нажатий кнопок",
             config.REMINDER_CRON, config.REMINDER_REPEAT_CRON or "выключен", config.BACKUP_CRON, config.TZ)
    poll_forever()


if __name__ == "__main__":
    main()

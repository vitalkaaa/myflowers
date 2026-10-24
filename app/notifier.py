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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup, care, config, telegram
from app.db import get_db, init_db, record_done, set_snooze

log = logging.getLogger("notifier")
PLANTS_PER_MESSAGE = 30  # в Telegram до 100 кнопок и 4096 символов в сообщении
NAME_LEN = 60            # длинные названия обрезаем в тексте и на кнопках


# ---------- рассылка ----------

def short(name: str) -> str:
    return name if len(name) <= NAME_LEN else name[:NAME_LEN - 1] + "…"


def build_messages(day: date | None = None, demo: bool = False, repeat: bool = False) -> list[tuple[str, list]]:
    """[(текст, inline-клавиатура)] — по сообщению на каждое действие, которое пора сделать в day.
    demo=True — пример: пометка «🧪 Пример» и кнопки, которые не пишут в базу.
    repeat=True — повторное напоминание о неотмеченном (в заголовке «🔁 Ещё не отмечено»)."""
    day = day or care.today()
    # тип кнопки: p — пример, d — настоящая отметка; da/pa — «Всё …», ds/ps — «⏰ отложить»
    prefix = "p" if demo else "d"
    with get_db() as db:
        plants = db.execute("SELECT * FROM plants WHERE archived_at IS NULL ORDER BY name").fetchall()
        snap = care.snapshot(db)

    due: dict[str, list[tuple]] = {}  # kind -> [(растение, задача)]
    for plant in plants:
        for task in care.plant_tasks(plant, snap, day):
            if task["due"]:
                due.setdefault(task["kind"], []).append((plant, task))

    messages = []
    for kind, title in care.TASKS.items():
        items = due.get(kind, [])
        for start in range(0, len(items), PLANTS_PER_MESSAGE):
            chunk = items[start:start + PLANTS_PER_MESSAGE]
            mark = "🧪 <b>Пример</b> · " if demo else "🔁 Ещё не отмечено · " if repeat else "🌿 "
            lines = [f"{mark}<b>{title}</b> · {day.strftime('%d.%m.%Y')}", ""]
            buttons = []
            for plant, task in chunk:
                name = html.escape(short(plant["name"]))
                if config.BASE_URL:
                    name = f'<a href="{config.BASE_URL}/plants/{plant["id"]}">{name}</a>'
                volume = care.water_volume(plant, day, snap.seasons) if kind == "water" else None
                amount = f" — ≈ {volume} мл" if volume else ""
                late = f" (просрочено {-task['days_left']} дн.)" if task["days_left"] < 0 else ""
                lines.append(f"• {name}{amount}{late}")
                buttons.append({"text": f"{short(plant['name'])} — {care.DONE_STATES[kind]}",
                                "callback_data": f"{prefix}:{plant['id']}:{kind}"})
            keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            if len(buttons) > 1:
                keyboard.append([{"text": f"✅ Всё {care.DONE_STATES[kind]}", "callback_data": f"{prefix}a:{kind}"}])
            keyboard.append([{"text": "⏰ +1 день", "callback_data": f"{prefix}s:{kind}:1"},
                             {"text": "⏰ +2 дня", "callback_data": f"{prefix}s:{kind}:2"}])
            messages.append(("\n".join(lines), keyboard))
    return messages


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

def parse_button(data: str) -> tuple[int, str, bool] | None:
    """'d:12:water' -> (12, 'water', False); 'p:…' — то же для примера (demo=True); иначе None."""
    parts = data.split(":")
    if len(parts) == 3 and parts[0] in ("d", "p") and parts[1].isdigit() and len(parts[1]) <= 9 \
            and parts[2] in care.TASKS:
        return int(parts[1]), parts[2], parts[0] == "p"
    return None


def is_all_button(data: str) -> bool:
    """Кнопка «Всё …»: 'da:water' — настоящая, 'pa:water' — в примере."""
    return data.split(":")[0] in ("da", "pa")


def parse_snooze(data: str) -> tuple[int, bool] | None:
    """Кнопка «⏰ +N»: 'ds:water:1' -> (1, False); 'ps:…' — в примере; иначе None."""
    parts = data.split(":")
    if len(parts) == 3 and parts[0] in ("ds", "ps") and parts[1] in care.TASKS and parts[2] in ("1", "2"):
        return int(parts[2]), parts[0] == "ps"
    return None


def is_group_button(data: str) -> bool:
    """Кнопки, которые действуют на всё сообщение: «Всё …» и «⏰ отложить»."""
    return is_all_button(data) or parse_snooze(data) is not None


def mark_plant(plant_id: int, kind: str, demo: bool, until: date | None = None) -> str | None:
    """Отмечает уход (или откладывает до until) и возвращает название растения.
    None — растения нет или оно в архиве. В примере ничего не пишет."""
    with get_db() as db:
        plant = db.execute("SELECT name FROM plants WHERE id = ? AND archived_at IS NULL", (plant_id,)).fetchone()
        if plant and not demo:
            if until:
                set_snooze(db, plant_id, kind, until.isoformat())
            else:
                record_done(db, plant_id, kind, care.now_str())
    return plant["name"] if plant else None


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
    snooze = parse_snooze(data) if any(b.get("callback_data") == data for b in buttons) else None
    if is_all_button(data) or snooze:
        # «Всё полито» и «⏰ отложить» — для растений, кнопки которых ещё не нажаты в этом сообщении
        targets = [b for b in buttons if parse_button(b.get("callback_data", ""))]
    else:
        targets = [b for b in buttons if b.get("callback_data") == data and parse_button(data)]
    if not targets:
        answer(callback["id"], "Уже отмечено")
        return

    until = care.today() + timedelta(days=snooze[0]) if snooze else None
    marked, demo = [], False
    for button in targets:
        plant_id, kind, demo = parse_button(button["callback_data"])
        name = mark_plant(plant_id, kind, demo, until)
        if not name:
            button["text"] = "растение удалено или в архиве"
        elif until:
            button["text"] = f"⏰ {short(name)} — до {until:%d.%m}"
        else:
            button["text"] = f"✅ {short(name)} — {care.DONE_STATES[kind]}"
        button["callback_data"] = "x"  # уже отмечено
        if name:
            marked.append(name)

    if not any(parse_button(b.get("callback_data", "")) for b in buttons):
        # всё отмечено — убираем кнопки «Всё …» и «⏰ отложить»
        keyboard = [row for row in keyboard if not is_group_button(row[0].get("callback_data", ""))]
    telegram.call("editMessageReplyMarkup", {"chat_id": message["chat"]["id"], "message_id": message["message_id"],
                                             "reply_markup": {"inline_keyboard": keyboard}})
    if not marked:
        answer(callback["id"], "Растение не найдено или в архиве")
    elif demo:
        answer(callback["id"], "🧪 Это пример — в историю не записано: " + ", ".join(marked))
    elif until:
        answer(callback["id"], f"⏰ Отложено до {until:%d.%m}: " + ", ".join(marked))
    else:
        answer(callback["id"], "✅ Отмечено: " + ", ".join(marked))


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

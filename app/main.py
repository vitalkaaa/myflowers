import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import auth, care, config, photos
from app.auth import LoginRequired, check_credentials, check_csrf, csrf_token, login_redirect, require_login
from app.catalog import CATALOG
from app.db import (INT_FIELDS, LONG_TEXT_FIELDS, PLANT_FIELDS, add_photo, get_db, init_db, insert_plant,
                    record_done, set_snooze, update_plant)
from app.seed import REFERENCE, fill_missing, reference_text, seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """При старте сервера: проверка настроек, создание БД и начальный список растений."""
    config.require("APP_USERNAME", "APP_PASSWORD", "SECRET_KEY")
    if len(config.APP_PASSWORD) < 12 or config.APP_PASSWORD.lower() in (config.APP_USERNAME.lower(), "password"):
        # сам пароль в лог не пишем
        logging.getLogger("app").warning(
            "Слабый APP_PASSWORD (короче 12 символов или совпадает с логином). "
            "Перед доступом из интернета задайте длинный случайный пароль в .env")
    init_db()
    seed_if_empty()
    fill_missing()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="my_flowers_session",
    max_age=30 * 24 * 3600,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)
app.add_exception_handler(LoginRequired, login_redirect)
APP_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


def static_url(name: str) -> str:
    """/static/app.js?v=<хэш содержимого>: после обновления сайта браузер не возьмёт старый файл из кэша."""
    path = APP_DIR / "static" / name
    key = (name, path.stat().st_mtime_ns)
    if key not in _static_versions:
        _static_versions[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={_static_versions[key]}"


_static_versions: dict[tuple[str, int], str] = {}
templates.env.globals["static_url"] = static_url
templates.env.globals["days_left_text"] = care.days_left_text
templates.env.globals["task_titles"] = care.TASKS

login_required = [Depends(require_login)]


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def render(request: Request, name: str, status_code: int = 200, **context):
    # в шаблонах формы пишут {{ csrf_field }}; токен экранируется, хотя и так состоит из [A-Za-z0-9_-]
    token = csrf_token(request)
    field = Markup('<input type="hidden" name="csrf" value="{}">').format(token)
    common = {"csrf_field": field, "csrf": token, "logged_in": bool(request.session.get("user")),
              "flash": request.session.pop("flash", None)}
    return templates.TemplateResponse(request, name, {**common, **context}, status_code=status_code)


def wants_json(request: Request) -> bool:
    """Запрос из app.js (fetch): ответ — JSON, страница не перезагружается."""
    return request.headers.get("x-requested-with") == "fetch"


def done_response(request: Request, url: str, message: str, undo: dict | None = None) -> Response:
    """Ответ на действие: для fetch — JSON с текстом всплывашки и кнопкой «Отменить»,
    без JavaScript — переход на страницу и сообщение над содержимым."""
    if wants_json(request):
        return JSONResponse({"ok": True, "message": message, "undo": undo})
    request.session["flash"] = message
    return redirect(url)


CSRF_MESSAGE = "Страница устарела — обновите её и повторите действие"


def verify_csrf(request: Request, token: str) -> None:
    if not check_csrf(request, token):
        raise HTTPException(403, CSRF_MESSAGE)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def find_or_404(db, sql: str, row_id: int, message: str = "Не найдено"):
    row = db.execute(sql, (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, message)
    return row


def get_plant(db, plant_id: int):
    return find_or_404(db, "SELECT * FROM plants WHERE id = ?", plant_id, "Растение не найдено")


def get_active_plant(db, plant_id: int):
    plant = get_plant(db, plant_id)
    if plant["archived_at"]:
        raise HTTPException(400, f"«{plant['name']}» в архиве — сначала верните растение из архива")
    return plant


def check_kind(kind: str) -> None:
    if kind not in care.TASKS:
        raise HTTPException(400, "Неизвестное действие")


# ---------- ошибки: страница в стиле сайта вместо {"detail": …} ----------

ERROR_TEXTS = {400: "Неверный запрос", 403: "Доступ запрещён", 404: "Страница не найдена",
               405: "Такое действие здесь недоступно", 413: "Слишком большой файл"}


def error_response(request: Request, status: int, message: str, headers: dict | None = None) -> Response:
    if wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status, headers=headers)
    return render(request, "error.html", status_code=status, status=status, message=message)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    # свои тексты — по-русски; служебные «Not Found» и т.п. заменяем
    message = exc.detail if isinstance(exc.detail, str) and not exc.detail.isascii() else None
    return error_response(request, exc.status_code, message or ERROR_TEXTS.get(exc.status_code, "Ошибка"),
                          getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, _exc: RequestValidationError):
    return error_response(request, 400, "Неверный адрес или параметры запроса")


# ---------- вход ----------

@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", error=None)


FAILED_LOGIN_DELAY = 1  # секунд


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    verify_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    # проверка и запись неудачи идут без await между ними — параллельные запросы лимит не обойдут
    if auth.login_blocked(ip):
        return render(request, "login.html", status_code=429,
                      error=f"Слишком много попыток входа. Попробуйте через {auth.LOGIN_WINDOW // 60} минут")
    if not check_credentials(username, password):
        auth.login_failed(ip)
        await asyncio.sleep(FAILED_LOGIN_DELAY)  # замедляем перебор паролей
        return render(request, "login.html", error="Неверный логин или пароль")
    auth.login_succeeded(ip)
    request.session.clear()  # новая сессия после входа (защита от фиксации сессии)
    request.session["user"] = username
    return redirect("/")


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    request.session.clear()
    return redirect("/login")


# ---------- главная: «Сегодня» и список растений ----------

# ключ сортировки -> функция значения строки таблицы
SORT_KEYS = {
    "name": lambda r: r["name"].lower(),
    "species": lambda r: r["species"].lower(),
    "next_water": lambda r: r["next_water"],
    "water_summer": lambda r: r["water_summer_days"],
    "water_winter": lambda r: r["water_winter_days"],
    "next_task": lambda r: r["next_task_days"],
    "light": lambda r: r["light"].lower(),
    "humidity": lambda r: r["air_humidity"].lower(),
    "location": lambda r: r["location"].lower(),
    "comments": lambda r: r["comments"],
}
VIEWS = ("cards", "table")


def parse_sort(raw: str) -> list[tuple[str, bool]]:
    """'-next_water,name' -> [('next_water', True), ('name', False)]"""
    result = []
    for part in raw.split(","):
        desc = part.startswith("-")
        key = part.lstrip("-")
        if key in SORT_KEYS and key not in [k for k, _ in result]:
            result.append((key, desc))
    return result or [("next_water", False)]


def plant_summary(plant, snap: care.Snapshot, day: date) -> dict:
    """Растение для списка: задачи, ближайшее дело и то, что пора сделать сегодня."""
    tasks = care.plant_tasks(plant, snap, day)
    water = next(t for t in tasks if t["kind"] == "water")
    active = sorted((t for t in tasks if t["active"]), key=lambda t: t["days_left"])
    return {
        **dict(plant),
        "water": water,
        "next_water": water["days_left"],
        "next_task": active[0] if active else None,
        "next_task_days": active[0]["days_left"] if active else 10**6,
        "due": [t for t in tasks if t["due"]],
        "volume": care.water_volume(plant, day, snap.seasons),
    }


def today_groups(rows: list[dict], done_today) -> list[dict]:
    """Блок «Сегодня»: по группе на действие — что пора сделать и что уже отмечено сегодня."""
    groups = []
    for kind, title in care.TASKS.items():
        items = sorted(((row, task) for row in rows for task in row["due"] if task["kind"] == kind),
                       key=lambda item: (item[1]["days_left"], item[0]["name"].lower()))
        done = [event for event in done_today if event["kind"] == kind]
        if items or done:
            groups.append({"kind": kind, "title": title, "todo": items, "done": done,
                           "done_label": care.DONE_LABELS[kind], "state": care.DONE_STATES[kind]})
    return groups


@app.get("/", dependencies=login_required)
def index(request: Request, sort: str = "", view: str = ""):
    if view in VIEWS:
        request.session["view"] = view
    view = request.session.get("view", "cards")
    day = care.today()
    with get_db() as db:
        plants = db.execute("""
            SELECT p.*,
                   (SELECT id FROM photos WHERE plant_id = p.id AND is_main = 1) AS photo_id,
                   (SELECT COUNT(*) FROM comments WHERE plant_id = p.id) AS comments
            FROM plants p WHERE p.archived_at IS NULL
        """).fetchall()
        snap = care.snapshot(db)
        done_today = db.execute("""
            SELECT e.id, e.kind, p.id AS plant_id, p.name FROM events e JOIN plants p ON p.id = e.plant_id
            WHERE p.archived_at IS NULL AND substr(e.done_at, 1, 10) = ? ORDER BY e.done_at
        """, (day.isoformat(),)).fetchall()
        archived = db.execute("SELECT COUNT(*) FROM plants WHERE archived_at IS NOT NULL").fetchone()[0]

    rows = [plant_summary(plant, snap, day) for plant in plants]
    sort_spec = parse_sort(sort)
    for key, desc in reversed(sort_spec):  # стабильная сортировка: от младшего ключа к старшему
        rows.sort(key=SORT_KEYS[key], reverse=desc)

    return render(request, "index.html", rows=rows, sort_spec=dict(sort_spec), sort_order=[k for k, _ in sort_spec],
                  today=today_groups(rows, done_today), view=view, archived=archived, seasons=snap.seasons,
                  season=snap.seasons.is_grow(day), snooze_until=day + timedelta(days=1),
                  locations=sorted({r["location"] for r in rows if r["location"]}, key=str.lower))


MAX_IDS = 500


def parse_ids(values) -> list[int]:
    return list(dict.fromkeys(int(v) for v in values if v.isdigit() and len(v) <= 9))[:MAX_IDS]


def undo_events(ids: list[int | None]) -> dict | None:
    ids = [i for i in ids if i]
    return {"url": "/events/undo", "fields": {"ids": ",".join(map(str, ids))}} if ids else None


def marked_message(names: list[str], kind: str, new: int) -> str:
    if not new:
        return "Уже отмечено"
    shown = ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
    return f"✅ {care.DONE_STATES[kind].capitalize()}: {shown}"


def mark(request: Request, plant_id: int, kind: str, day: str, back: str) -> Response:
    check_kind(kind)
    with get_db() as db:
        plant = get_active_plant(db, plant_id)
        event_id = record_done(db, plant_id, kind, done_at(day))
    return done_response(request, back_url(back, plant_id), marked_message([plant["name"]], kind, bool(event_id)),
                         undo_events([event_id]))


@app.post("/today/done", dependencies=login_required)
async def mark_group(request: Request):
    """«✅ Отметить всё» в блоке «Сегодня»: одно действие для нескольких растений."""
    form = await request.form()
    verify_csrf(request, form.get("csrf", ""))
    kind = form.get("kind", "")
    check_kind(kind)
    ids = parse_ids(form.getlist("plant"))
    if not ids:
        raise HTTPException(400, "Не выбрано ни одного растения")
    when = care.now_str()
    with get_db() as db:
        placeholders = ", ".join("?" * len(ids))  # только знаки «?» — значения передаются параметрами
        plants = db.execute(f"SELECT id, name FROM plants WHERE archived_at IS NULL AND id IN ({placeholders})",
                            ids).fetchall()
        new = [(plant["name"], record_done(db, plant["id"], kind, when)) for plant in plants]
    created = [event_id for _, event_id in new if event_id]
    return done_response(request, back_url(form.get("back", ""), 0),
                         marked_message([name for name, event_id in new if event_id], kind, len(created)),
                         undo_events(created))


# ---------- растение ----------

FIELD_LABELS = {
    "name": "Название",
    "water_summer_days": "Интервал полива в сезон роста",
    "water_winter_days": "Интервал полива в период покоя",
    "water_ml": "Объём полива",
    "feed_days": "Подкормка",
    "repot_years": "Пересадка",
    "spray_days": "Опрыскивание",
    "wipe_days": "Протирка листьев",
}


class FormError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def parse_plant_form(form) -> dict:
    values = {}
    for field in PLANT_FIELDS:
        raw = (form.get(field) or "").strip()
        if field in INT_FIELDS:
            if not raw:
                values[field] = None
                continue
            if not raw.isdigit() or not 0 < int(raw) <= 5000:
                raise FormError(f"«{FIELD_LABELS[field]}»: нужно целое число от 1 до 5000", field)
            values[field] = int(raw)
        else:
            values[field] = raw[:10000] if field in LONG_TEXT_FIELDS else raw[:2000]
    if not values["name"]:
        raise FormError("Укажите название", "name")
    for field in ("water_summer_days", "water_winter_days"):
        if not values[field]:
            raise FormError(f"Укажите «{FIELD_LABELS[field]}»", field)
    return values


def plant_form(request: Request, values: dict, plant_id: int | None, status_code: int = 200,
               error: str | None = None, error_field: str | None = None, ref: str = ""):
    return render(request, "plant_form.html", status_code=status_code, values=values, plant_id=plant_id,
                  error=error, error_field=error_field, catalog=CATALOG, ref=ref, seasons=care.SEASONS)


PlantPhoto = tuple[bytes, bytes] | None  # (фото, превью) из формы нового растения


async def submit_plant_form(request: Request, plant_id: int | None) -> tuple[dict, PlantPhoto] | Response:
    """(значения, фото) формы растения или та же форма с ошибкой над ней — введённое не теряется.
    Фото принимается только при добавлении: у существующего растения есть галерея."""
    form = await request.form()
    entered = {field: str(form.get(field) or "")[:10000] for field in PLANT_FIELDS}
    if not check_csrf(request, str(form.get("csrf", ""))):
        # Введённое показываем снова только для своей страницы: форму с чужого сайта не отражаем
        if request.headers.get("sec-fetch-site") != "same-origin":
            raise HTTPException(403, CSRF_MESSAGE)
        return plant_form(request, entered, plant_id, 403,
                          "Страница устарела — проверьте данные и нажмите «Сохранить» ещё раз")
    try:
        values = parse_plant_form(form)
    except FormError as exc:
        return plant_form(request, entered, plant_id, 400, str(exc), exc.field)
    file = form.get("photo")
    if plant_id is not None or not isinstance(file, UploadFile) or not file.filename:
        return values, None
    try:
        return values, await read_photo(file)
    except ValueError as exc:
        # файл браузер заново не подставит — просим выбрать его ещё раз
        return plant_form(request, entered, plant_id, 400, f"Фото: {exc} — выберите другое фото", "photo")


@app.get("/plants/new", dependencies=login_required)
def new_plant_page(request: Request, ref: str = ""):
    values = CATALOG[ref]["values"] if ref in CATALOG else {}
    return plant_form(request, values, None, ref=ref if ref in CATALOG else "")


@app.post("/plants/new", dependencies=login_required)
async def create_plant(request: Request):
    result = await submit_plant_form(request, None)
    if isinstance(result, Response):
        return result
    values, photo = result
    for field in REFERENCE:  # справочные тексты по виду, если их не заполнили
        values[field] = values[field] or reference_text(field, values["species"])
    now = care.now_str()
    with get_db() as db:
        plant_id = insert_plant(db, values, now)
        if photo:
            add_photo(db, plant_id, *photo, now)
    return redirect(f"/plants/{plant_id}")


@app.get("/plants/{plant_id}", dependencies=login_required)
def plant_page(request: Request, plant_id: int):
    with get_db() as db:
        plant = get_plant(db, plant_id)
        plant_photos = db.execute(
            "SELECT id, is_main, created_at FROM photos WHERE plant_id = ? ORDER BY is_main DESC, created_at DESC",
            (plant_id,),
        ).fetchall()
        comments = db.execute(
            "SELECT * FROM comments WHERE plant_id = ? ORDER BY created_at DESC", (plant_id,)
        ).fetchall()
        events = db.execute(
            "SELECT * FROM events WHERE plant_id = ? ORDER BY done_at DESC LIMIT 30", (plant_id,)
        ).fetchall()
        water_dates = [row["done_at"] for row in db.execute(
            "SELECT done_at FROM events WHERE plant_id = ? AND kind = 'water' ORDER BY done_at DESC LIMIT 40",
            (plant_id,))]
        snap = care.snapshot(db)
    today = care.today()
    tasks = care.plant_tasks(plant, snap, today)
    suggestion = None if plant["archived_at"] else care.suggest_interval(plant, water_dates, today, snap.seasons)
    growth = sorted(plant_photos, key=lambda ph: ph["created_at"])  # лента роста: от старых к новым
    return render(request, "plant.html", plant=plant, photos=plant_photos, growth=growth, comments=comments,
                  events=events, tasks=tasks, suggestion=suggestion, seasons=snap.seasons,
                  volume=care.water_volume(plant, today, snap.seasons), archive_reasons=ARCHIVE_REASONS,
                  today=today, yesterday=today - timedelta(days=1),
                  min_day=today - timedelta(days=BACKDATE_DAYS),
                  growth_start=date.fromisoformat(growth[0]["created_at"][:10]) if growth else None,
                  date=date)


@app.get("/plants/{plant_id}/edit", dependencies=login_required)
def edit_plant_page(request: Request, plant_id: int):
    with get_db() as db:
        plant = get_plant(db, plant_id)
    return plant_form(request, dict(plant), plant_id)


@app.post("/plants/{plant_id}/edit", dependencies=login_required)
async def edit_plant(request: Request, plant_id: int):
    with get_db() as db:
        get_plant(db, plant_id)
    result = await submit_plant_form(request, plant_id)
    if isinstance(result, Response):
        return result
    values, _ = result
    with get_db() as db:
        update_plant(db, plant_id, values)
    return redirect(f"/plants/{plant_id}")


WATER_INTERVAL_SQL = {  # колонка -> запрос; имя колонки не берётся из ввода
    "water_summer_days": "UPDATE plants SET water_summer_days = ? WHERE id = ?",
    "water_winter_days": "UPDATE plants SET water_winter_days = ? WHERE id = ?",
}


@app.post("/plants/{plant_id}/interval", dependencies=login_required)
def apply_interval(request: Request, plant_id: int, csrf: str = Form(""), column: str = Form(""),
                   days: int = Form(0)):
    """Подсказка «вы поливаете раз в N дн. — обновить?»: записать интервал в карточку."""
    verify_csrf(request, csrf)
    if column not in WATER_INTERVAL_SQL or not 0 < days <= 5000:
        raise HTTPException(400, "Интервал — целое число дней от 1 до 5000")
    with get_db() as db:
        get_active_plant(db, plant_id)
        db.execute(WATER_INTERVAL_SQL[column], (days, plant_id))
    request.session["flash"] = f"💧 Интервал полива обновлён: раз в {days} дн."
    return redirect(f"/plants/{plant_id}")


# ---------- архив ----------

ARCHIVE_REASONS = ("погибло", "подарено", "продано", "отдано на передержку")


@app.post("/plants/{plant_id}/archive", dependencies=login_required)
def archive_plant(request: Request, plant_id: int, csrf: str = Form(""), reason: str = Form("")):
    """Вместо удаления: растение пропадает из списка и напоминаний, фото, дневник и история остаются."""
    verify_csrf(request, csrf)
    with get_db() as db:
        plant = get_plant(db, plant_id)
        db.execute("UPDATE plants SET archived_at = ?, archive_reason = ? WHERE id = ? AND archived_at IS NULL",
                   (care.now_str(), reason.strip()[:200], plant_id))
        db.execute("DELETE FROM snoozes WHERE plant_id = ?", (plant_id,))
    request.session["flash"] = f"📦 «{plant['name']}» перенесено в архив — история сохранена"
    return redirect("/")


@app.post("/plants/{plant_id}/restore", dependencies=login_required)
def restore_plant(request: Request, plant_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        get_plant(db, plant_id)
        db.execute("UPDATE plants SET archived_at = NULL, archive_reason = '' WHERE id = ?", (plant_id,))
    request.session["flash"] = "Растение снова в списке"
    return redirect(f"/plants/{plant_id}")


@app.post("/plants/{plant_id}/delete", dependencies=login_required)
def delete_plant(request: Request, plant_id: int, csrf: str = Form("")):
    """Удаление навсегда (вместе с фото, дневником и историей) — только из архива."""
    verify_csrf(request, csrf)
    with get_db() as db:
        plant = get_plant(db, plant_id)
        if not plant["archived_at"]:
            raise HTTPException(400, "Удалить можно только растение из архива — сначала перенесите его в архив")
        db.execute("DELETE FROM plants WHERE id = ?", (plant_id,))
    request.session["flash"] = f"«{plant['name']}» удалено навсегда"
    return redirect("/archive")


@app.get("/archive", dependencies=login_required)
def archive_page(request: Request):
    with get_db() as db:
        plants = db.execute("""
            SELECT p.id, p.name, p.species, p.archived_at, p.archive_reason,
                   (SELECT id FROM photos WHERE plant_id = p.id AND is_main = 1) AS photo_id,
                   (SELECT COUNT(*) FROM events WHERE plant_id = p.id) AS events,
                   (SELECT COUNT(*) FROM comments WHERE plant_id = p.id) AS comments
            FROM plants p WHERE p.archived_at IS NOT NULL ORDER BY p.archived_at DESC
        """).fetchall()
    return render(request, "archive.html", plants=plants)


# ---------- отметки и «отложить» ----------

@app.post("/plants/{plant_id}/done/{kind}", dependencies=login_required)
def mark_done(request: Request, plant_id: int, kind: str, csrf: str = Form(""), back: str = Form(""),
              day: str = Form("")):
    verify_csrf(request, csrf)
    return mark(request, plant_id, kind, day, back)


@app.post("/events", dependencies=login_required)
def add_event(request: Request, csrf: str = Form(""), plant: int = Form(0), kind: str = Form(""),
              day: str = Form(""), back: str = Form("")):
    """Отметка за прошедший день из календаря: растение и действие выбираются в форме."""
    verify_csrf(request, csrf)
    return mark(request, plant, kind, day, back)


@app.post("/plants/{plant_id}/snooze/{kind}", dependencies=login_required)
def snooze(request: Request, plant_id: int, kind: str, csrf: str = Form(""), days: int = Form(1),
           back: str = Form("")):
    """«⏰ +1 день»: почва ещё влажная — напомнить позже. days=0 — снять «отложить»."""
    verify_csrf(request, csrf)
    check_kind(kind)
    if not 0 <= days <= care.SNOOZE_MAX_DAYS:
        raise HTTPException(400, f"Отложить можно на 1–{care.SNOOZE_MAX_DAYS} дней")
    until = care.today() + timedelta(days=days) if days else None
    with get_db() as db:
        plant = get_active_plant(db, plant_id)
        set_snooze(db, plant_id, kind, until.isoformat() if until else None)
    if not until:
        return done_response(request, back_url(back, plant_id), "Напоминание возвращено")
    return done_response(request, back_url(back, plant_id),
                         f"⏰ {plant['name']}: {care.TASKS[kind].split(' ', 1)[1].lower()} — до {until:%d.%m}",
                         {"url": f"/plants/{plant_id}/snooze/{kind}", "fields": {"days": "0"}})


@app.post("/events/undo", dependencies=login_required)
def undo(request: Request, csrf: str = Form(""), ids: str = Form(""), back: str = Form("")):
    """«Отменить» во всплывашке: удаляет только что созданные отметки."""
    verify_csrf(request, csrf)
    parsed = parse_ids(ids.split(","))
    if parsed:
        with get_db() as db:
            db.execute(f"DELETE FROM events WHERE id IN ({', '.join('?' * len(parsed))})", parsed)
    return done_response(request, back_url(back, 0), "Отметка отменена")


@app.post("/events/{event_id}/delete", dependencies=login_required)
def delete_event(request: Request, event_id: int, csrf: str = Form(""), back: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = find_or_404(db, "SELECT plant_id FROM events WHERE id = ?", event_id)
        db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return done_response(request, back_url(back, row["plant_id"]), "Отметка удалена")


BACKDATE_DAYS = 365  # насколько назад можно отметить уход


def done_at(day: str) -> str:
    """Время отметки: сейчас, а если выбран прошедший день — полдень того дня."""
    if not day or day == care.today().isoformat():
        return care.now_str()
    parsed = parse_date(day)
    if not parsed or not care.today() - timedelta(days=BACKDATE_DAYS) <= parsed < care.today():
        raise HTTPException(400, f"Дата — не в будущем и не раньше чем {BACKDATE_DAYS} дней назад")
    return datetime.combine(parsed, time(12), config.TZ).isoformat(timespec="seconds")


def back_url(back: str, plant_id: int) -> str:
    """Куда вернуться после действия — только на свои страницы, без открытого редиректа."""
    if back == "history":
        return "/history"
    if back.startswith("calendar:"):
        day = parse_date(back.removeprefix("calendar:"))
        if day:
            return f"/calendar?day={day.isoformat()}"
    if back == "index" or not plant_id:
        return "/"
    return f"/plants/{plant_id}"


def parse_date(raw: str) -> date | None:
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    return day if 2000 <= day.year <= 2100 else None


# ---------- история и календарь ----------

HISTORY_PAGE = 100
EVENT_SQL = """SELECT e.id, e.kind, e.done_at, p.id AS plant_id, p.name
               FROM events e JOIN plants p ON p.id = e.plant_id"""


@app.get("/history", dependencies=login_required)
def history_page(request: Request, plant: int = 0, kind: str = "", page: int = 1):
    page = max(1, min(page, 10000))
    where, params = [], []
    if plant:
        where.append("e.plant_id = ?")
        params.append(plant)
    if kind in care.TASKS:
        where.append("e.kind = ?")
        params.append(kind)
    sql = EVENT_SQL + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY e.done_at DESC LIMIT ? OFFSET ?"
    with get_db() as db:
        rows = db.execute(sql, (*params, HISTORY_PAGE + 1, (page - 1) * HISTORY_PAGE)).fetchall()
        plants = db.execute("SELECT id, name, archived_at FROM plants ORDER BY archived_at IS NOT NULL, name").fetchall()

    groups: dict[str, list] = {}  # дата -> события за день
    for row in rows[:HISTORY_PAGE]:
        groups.setdefault(row["done_at"][:10], []).append(row)
    return render(request, "history.html", groups=groups, plants=plants, plant=plant,
                  kind=kind if kind in care.TASKS else "", page=page,
                  has_more=len(rows) > HISTORY_PAGE)


@app.get("/calendar", dependencies=login_required)
def calendar_page(request: Request, month: str = "", day: str = ""):
    today = care.today()
    selected = parse_date(day)
    first = parse_date(month + "-01") if month else None
    first = first or (selected or today).replace(day=1)
    first = min(first, date(today.year + 3, 12, 1))  # дальше смысла считать прогноз нет
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if not selected or selected.replace(day=1) != first:
        selected = today if today.replace(day=1) == first else None

    with get_db() as db:
        plants = db.execute("SELECT * FROM plants WHERE archived_at IS NULL ORDER BY name").fetchall()
        snap = care.snapshot(db)
        done = db.execute(EVENT_SQL + " WHERE substr(e.done_at, 1, 10) BETWEEN ? AND ? ORDER BY e.done_at",
                          (first.isoformat(), last.isoformat())).fetchall()

    min_day = today - timedelta(days=BACKDATE_DAYS)
    days = {first + timedelta(days=i): {"done": [], "planned": []} for i in range(last.day)}
    for row in done:
        days[date.fromisoformat(row["done_at"][:10])]["done"].append(row)
    if last >= today:
        for plant in plants:
            for due, kind, overdue_from in care.planned(plant, snap, today, last):
                if due >= first:
                    days[due]["planned"].append({
                        "plant_id": plant["id"], "name": plant["name"], "kind": kind, "overdue": bool(overdue_from),
                        # просроченное можно отметить исходным днём — «сделал(а) вовремя, забыл(а) отметить»
                        "overdue_from": overdue_from if overdue_from and overdue_from >= min_day else None,
                        "volume": care.water_volume(plant, due, snap.seasons) if kind == "water" else None,
                    })

    # сетка месяца по неделям, с понедельника; пустые клетки — None
    cells = [None] * first.weekday() + list(days)
    cells += [None] * (-len(cells) % 7)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    prev_month = (first - timedelta(days=1)).replace(day=1)
    return render(request, "calendar.html", weeks=weeks, days=days, first=first, today=today,
                  selected=selected, prev_month=prev_month, next_month=last + timedelta(days=1),
                  done_labels=care.DONE_LABELS, plants=plants, min_day=min_day)


# ---------- фото ----------

async def read_photo(file: UploadFile) -> tuple[bytes, bytes]:
    """(фото, превью) или ValueError с понятным текстом."""
    raw = await file.read(photos.MAX_UPLOAD_BYTES + 1)
    return photos.process(raw, file.filename or "")


@app.post("/plants/{plant_id}/photos", dependencies=login_required)
async def upload_photo(request: Request, plant_id: int):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf", "")))
    file = form.get("file")
    with get_db() as db:
        get_plant(db, plant_id)
    if not isinstance(file, UploadFile) or not file.filename:
        request.session["flash"] = "⚠️ Выберите файл с фото"
        return redirect(f"/plants/{plant_id}")
    try:
        data, thumb = await read_photo(file)
    except ValueError as exc:
        request.session["flash"] = f"⚠️ Фото не добавлено: {exc}"
        return redirect(f"/plants/{plant_id}")
    with get_db() as db:
        add_photo(db, plant_id, data, thumb, care.now_str())
    return redirect(f"/plants/{plant_id}")


def photo_response(sql: str, photo_id: int) -> Response:
    with get_db() as db:
        row = find_or_404(db, sql, photo_id)
    return Response(row[0], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/photos/{photo_id}", dependencies=login_required)
def get_photo(photo_id: int):
    return photo_response("SELECT data FROM photos WHERE id = ?", photo_id)


@app.get("/photos/{photo_id}/thumb", dependencies=login_required)
def get_thumb(photo_id: int):
    return photo_response("SELECT thumb FROM photos WHERE id = ?", photo_id)


@app.post("/photos/{photo_id}/main", dependencies=login_required)
def set_main_photo(request: Request, photo_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = find_or_404(db, "SELECT plant_id FROM photos WHERE id = ?", photo_id)
        db.execute("UPDATE photos SET is_main = (id = ?) WHERE plant_id = ?", (photo_id, row["plant_id"]))
    return redirect(f"/plants/{row['plant_id']}")


@app.post("/photos/{photo_id}/delete", dependencies=login_required)
def delete_photo(request: Request, photo_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = find_or_404(db, "SELECT plant_id, is_main FROM photos WHERE id = ?", photo_id)
        db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if row["is_main"]:  # главным становится самое свежее из оставшихся
            db.execute("""UPDATE photos SET is_main = 1 WHERE id = (
                SELECT id FROM photos WHERE plant_id = ? ORDER BY created_at DESC LIMIT 1)""",
                       (row["plant_id"],))
    return redirect(f"/plants/{row['plant_id']}")


# ---------- дневник ----------

COMMENT_LEN = 5000


@app.post("/plants/{plant_id}/comments", dependencies=login_required)
async def add_comment(request: Request, plant_id: int):
    """Запись дневника, к ней можно приложить фото — оно попадает и в галерею."""
    form = await request.form()
    verify_csrf(request, str(form.get("csrf", "")))
    text = str(form.get("text") or "").strip()[:COMMENT_LEN]
    file = form.get("file")
    photo, warning = None, None
    if isinstance(file, UploadFile) and file.filename:
        try:
            photo = await read_photo(file)
        except ValueError as exc:
            warning = f"⚠️ Запись сохранена, но фото не добавлено: {exc}"
    if text or photo:
        now = care.now_str()
        with get_db() as db:
            get_plant(db, plant_id)
            photo_id = add_photo(db, plant_id, *photo, now) if photo else None
            db.execute("INSERT INTO comments (plant_id, text, created_at, photo_id) VALUES (?, ?, ?, ?)",
                       (plant_id, text, now, photo_id))
    if warning:
        request.session["flash"] = warning
    return redirect(f"/plants/{plant_id}#comments")


@app.post("/comments/{comment_id}/edit", dependencies=login_required)
def edit_comment(request: Request, comment_id: int, csrf: str = Form(""), text: str = Form("")):
    verify_csrf(request, csrf)
    text = text.strip()[:COMMENT_LEN]
    with get_db() as db:
        row = find_or_404(db, "SELECT plant_id, photo_id FROM comments WHERE id = ?", comment_id)
        if not text and not row["photo_id"]:
            raise HTTPException(400, "Запись не может быть пустой — чтобы убрать её, нажмите «удалить»")
        # после правки запись снова «необработанная» — LLM-автоматизация увидит новый текст
        db.execute("UPDATE comments SET text = ?, updated_at = ?, processed_at = NULL WHERE id = ?",
                   (text, care.now_str(), comment_id))
    return redirect(f"/plants/{row['plant_id']}#comment-{comment_id}")


@app.post("/comments/{comment_id}/delete", dependencies=login_required)
def delete_comment(request: Request, comment_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = find_or_404(db, "SELECT plant_id FROM comments WHERE id = ?", comment_id)
        db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return redirect(f"/plants/{row['plant_id']}#comments")


# ---------- PWA ----------

@app.get("/sw.js")
def service_worker():
    """Service worker должен лежать в корне сайта, чтобы его область действия была «/»."""
    return FileResponse(APP_DIR / "static" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})

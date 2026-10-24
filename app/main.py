import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import care, config, photos
from app.auth import (LoginRequired, check_credentials, check_csrf, csrf_token,
                      login_redirect, require_login)
from app.db import PLANT_FIELDS, add_event, add_photo, get_db, init_db, insert_plant, update_plant
from app.seed import fill_missing, seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
config.require("APP_USERNAME", "APP_PASSWORD", "SECRET_KEY")
init_db()
seed_if_empty()
fill_missing()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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
templates.env.globals["days_left_text"] = care.days_left_text
templates.env.globals["water_volume"] = care.water_volume

auth = [Depends(require_login)]


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def render(request: Request, name: str, **context):
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **context})


def verify_csrf(request: Request, token: str) -> None:
    if not check_csrf(request, token):
        raise HTTPException(403, "Неверный CSRF-токен, обновите страницу")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def get_plant(db, plant_id: int):
    plant = db.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
    if not plant:
        raise HTTPException(404, "Растение не найдено")
    return plant


# ---------- вход ----------

@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    verify_csrf(request, csrf)
    if not check_credentials(username, password):
        await asyncio.sleep(1)  # замедляем перебор паролей
        return render(request, "login.html", error="Неверный логин или пароль")
    request.session.clear()  # новая сессия после входа (защита от фиксации сессии)
    request.session["user"] = username
    return redirect("/")


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    request.session.clear()
    return redirect("/login")


# ---------- таблица ----------

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


def parse_sort(raw: str) -> list[tuple[str, bool]]:
    """'-next_water,name' -> [('next_water', True), ('name', False)]"""
    result = []
    for part in raw.split(","):
        desc = part.startswith("-")
        key = part.lstrip("-")
        if key in SORT_KEYS and key not in [k for k, _ in result]:
            result.append((key, desc))
    return result or [("next_water", False)]


@app.get("/", dependencies=auth)
def index(request: Request, sort: str = ""):
    day = care.today()
    with get_db() as db:
        plants = db.execute("""
            SELECT p.*,
                   (SELECT id FROM photos WHERE plant_id = p.id AND is_main = 1) AS photo_id,
                   (SELECT COUNT(*) FROM comments WHERE plant_id = p.id) AS comments
            FROM plants p
        """).fetchall()
        last_done = care.last_done_map(db)

    rows = []
    for plant in plants:
        tasks = care.plant_tasks(plant, last_done.get(plant["id"], {}), day)
        water = next(t for t in tasks if t["kind"] == "water")
        active = sorted((t for t in tasks if t["active"]), key=lambda t: t["days_left"])
        rows.append({
            **dict(plant),
            "next_water": water["days_left"],
            "next_task": active[0] if active else None,
            "next_task_days": active[0]["days_left"] if active else 10**6,
            "due": [t for t in tasks if t["due"]],
        })

    sort_spec = parse_sort(sort)
    for key, desc in reversed(sort_spec):  # стабильная сортировка: от младшего ключа к старшему
        rows.sort(key=SORT_KEYS[key], reverse=desc)

    # сводка «что сделать сегодня» для шапки
    due_counts = {kind: 0 for kind in care.TASKS}
    for row in rows:
        for task in row["due"]:
            due_counts[task["kind"]] += 1
    stats = [(care.TASKS[k], n) for k, n in due_counts.items() if n]

    return render(request, "index.html", rows=rows, sort_spec=dict(sort_spec), stats=stats,
                  sort_order=[k for k, _ in sort_spec], season=care.is_season(day))


# ---------- растение ----------

def parse_plant_form(form) -> dict:
    values = {}
    for field in PLANT_FIELDS:
        raw = (form.get(field) or "").strip()
        if field in ("water_summer_days", "water_winter_days", "water_ml",
                     "feed_days", "repot_years", "spray_days", "wipe_days"):
            if not raw:
                values[field] = None
                continue
            if not raw.isdigit() or not 0 < int(raw) <= 5000:
                raise HTTPException(400, f"Поле {field}: нужно целое число от 1 до 5000")
            values[field] = int(raw)
        else:
            values[field] = raw[:10000] if field in ("notes", "facts", "diseases", "repotting") else raw[:2000]
    if not values["name"]:
        raise HTTPException(400, "Укажите название")
    if not values["water_summer_days"] or not values["water_winter_days"]:
        raise HTTPException(400, "Укажите интервалы полива")
    return values


@app.get("/plants/new", dependencies=auth)
def new_plant_page(request: Request):
    return render(request, "plant_form.html", plant=None)


@app.post("/plants/new", dependencies=auth)
async def create_plant(request: Request):
    form = await request.form()
    verify_csrf(request, form.get("csrf", ""))
    values = parse_plant_form(form)
    with get_db() as db:
        plant_id = insert_plant(db, values, care.now_str())
    return redirect(f"/plants/{plant_id}")


@app.get("/plants/{plant_id}", dependencies=auth)
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
        last_done = care.last_done_map(db).get(plant_id, {})
    tasks = care.plant_tasks(plant, last_done, care.today())
    return render(request, "plant.html", plant=plant, photos=plant_photos, comments=comments,
                  events=events, tasks=tasks, task_titles=care.TASKS)


@app.get("/plants/{plant_id}/edit", dependencies=auth)
def edit_plant_page(request: Request, plant_id: int):
    with get_db() as db:
        plant = get_plant(db, plant_id)
    return render(request, "plant_form.html", plant=plant)


@app.post("/plants/{plant_id}/edit", dependencies=auth)
async def edit_plant(request: Request, plant_id: int):
    form = await request.form()
    verify_csrf(request, form.get("csrf", ""))
    values = parse_plant_form(form)
    with get_db() as db:
        get_plant(db, plant_id)
        update_plant(db, plant_id, values)
    return redirect(f"/plants/{plant_id}")


@app.post("/plants/{plant_id}/delete", dependencies=auth)
def delete_plant(request: Request, plant_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        db.execute("DELETE FROM plants WHERE id = ?", (plant_id,))
    return redirect("/")


@app.post("/plants/{plant_id}/done/{kind}", dependencies=auth)
def mark_done(request: Request, plant_id: int, kind: str, csrf: str = Form(""), back: str = Form("")):
    verify_csrf(request, csrf)
    if kind not in care.TASKS:
        raise HTTPException(400, "Неизвестное действие")
    with get_db() as db:
        get_plant(db, plant_id)
        add_event(db, plant_id, kind, care.now_str())
    return redirect(back_url(back, plant_id))


@app.post("/events/{event_id}/delete", dependencies=auth)
def delete_event(request: Request, event_id: int, csrf: str = Form(""), back: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = db.execute("SELECT plant_id FROM events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return redirect(back_url(back, row["plant_id"]))


def back_url(back: str, plant_id: int) -> str:
    """Куда вернуться после действия — только на свои страницы, без открытого редиректа."""
    if back == "index":
        return "/"
    if back == "history":
        return "/history"
    if back.startswith("calendar:"):
        day = parse_date(back.removeprefix("calendar:"))
        if day:
            return f"/calendar?day={day.isoformat()}"
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


@app.get("/history", dependencies=auth)
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
        plants = db.execute("SELECT id, name FROM plants ORDER BY name").fetchall()

    groups: dict[str, list] = {}  # дата -> события за день
    for row in rows[:HISTORY_PAGE]:
        groups.setdefault(row["done_at"][:10], []).append(row)
    return render(request, "history.html", groups=groups, plants=plants, plant=plant,
                  kind=kind if kind in care.TASKS else "", page=page,
                  has_more=len(rows) > HISTORY_PAGE, task_titles=care.TASKS)


@app.get("/calendar", dependencies=auth)
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
        plants = db.execute("SELECT * FROM plants").fetchall()
        last_done = care.last_done_map(db)
        done = db.execute(EVENT_SQL + " WHERE substr(e.done_at, 1, 10) BETWEEN ? AND ? ORDER BY e.done_at",
                          (first.isoformat(), last.isoformat())).fetchall()

    days = {first + timedelta(days=i): {"done": [], "planned": []} for i in range(last.day)}
    for row in done:
        days[date.fromisoformat(row["done_at"][:10])]["done"].append(row)
    if last >= today:
        for plant in plants:
            for due, kind, overdue in care.planned(plant, last_done.get(plant["id"], {}), today, last):
                if due >= first:
                    days[due]["planned"].append({
                        "plant_id": plant["id"], "name": plant["name"], "kind": kind, "overdue": overdue,
                        "volume": care.water_volume(plant, due) if kind == "water" else None,
                    })

    # сетка месяца по неделям, с понедельника; пустые клетки — None
    cells = [None] * first.weekday() + list(days)
    cells += [None] * (-len(cells) % 7)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    prev_month = (first - timedelta(days=1)).replace(day=1)
    return render(request, "calendar.html", weeks=weeks, days=days, first=first, today=today,
                  selected=selected, prev_month=prev_month, next_month=last + timedelta(days=1),
                  task_titles=care.TASKS, done_labels=care.DONE_LABELS)


# ---------- фото ----------

@app.post("/plants/{plant_id}/photos", dependencies=auth)
async def upload_photo(request: Request, plant_id: int, file: UploadFile, csrf: str = Form("")):
    verify_csrf(request, csrf)
    raw = await file.read(photos.MAX_UPLOAD_BYTES + 1)
    try:
        data, thumb = photos.process(raw, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with get_db() as db:
        get_plant(db, plant_id)
        add_photo(db, plant_id, data, thumb, care.now_str())
    return redirect(f"/plants/{plant_id}")


def photo_response(photo_id: int, column: str) -> Response:
    with get_db() as db:
        row = db.execute(f"SELECT {column} FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return Response(row[0], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/photos/{photo_id}", dependencies=auth)
def get_photo(photo_id: int):
    return photo_response(photo_id, "data")


@app.get("/photos/{photo_id}/thumb", dependencies=auth)
def get_thumb(photo_id: int):
    return photo_response(photo_id, "thumb")


@app.post("/photos/{photo_id}/main", dependencies=auth)
def set_main_photo(request: Request, photo_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = db.execute("SELECT plant_id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("UPDATE photos SET is_main = (id = ?) WHERE plant_id = ?", (photo_id, row["plant_id"]))
    return redirect(f"/plants/{row['plant_id']}")


@app.post("/photos/{photo_id}/delete", dependencies=auth)
def delete_photo(request: Request, photo_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = db.execute("SELECT plant_id, is_main FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if row["is_main"]:  # главным становится самое свежее из оставшихся
            db.execute("""UPDATE photos SET is_main = 1 WHERE id = (
                SELECT id FROM photos WHERE plant_id = ? ORDER BY created_at DESC LIMIT 1)""",
                       (row["plant_id"],))
    return redirect(f"/plants/{row['plant_id']}")


# ---------- комментарии ----------

@app.post("/plants/{plant_id}/comments", dependencies=auth)
def add_comment(request: Request, plant_id: int, text: str = Form(""), csrf: str = Form("")):
    verify_csrf(request, csrf)
    text = text.strip()[:5000]
    if text:
        with get_db() as db:
            get_plant(db, plant_id)
            db.execute("INSERT INTO comments (plant_id, text, created_at) VALUES (?, ?, ?)",
                       (plant_id, text, care.now_str()))
    return redirect(f"/plants/{plant_id}#comments")


@app.post("/comments/{comment_id}/delete", dependencies=auth)
def delete_comment(request: Request, comment_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    with get_db() as db:
        row = db.execute("SELECT plant_id FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return redirect(f"/plants/{row['plant_id']}#comments")

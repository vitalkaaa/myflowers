import re
from datetime import timedelta

import pytest
from conftest import events
from fastapi.testclient import TestClient

from app import auth, care, main
from app.db import get_db
from app.main import app, back_url


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "FAILED_LOGIN_DELAY", 0)
    auth._failures.clear()
    with TestClient(app) as c:  # with — чтобы выполнился lifespan (создание БД, начальные растения)
        yield c


def csrf_of(client, url: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


@pytest.fixture
def logged_in(client):
    token = csrf_of(client, "/login")
    resp = client.post("/login", data={"username": "tester", "password": "test-password-long", "csrf": token},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


@pytest.mark.parametrize("back, expected", [
    ("index", "/"),
    ("history", "/history"),
    ("calendar:2026-05-01", "/calendar?day=2026-05-01"),
    ("calendar:bad", "/plants/7"),
    ("calendar:1900-01-01", "/plants/7"),
    ("https://evil.example", "/plants/7"),
    ("//evil.example", "/plants/7"),
    ("", "/plants/7"),
])
def test_back_url_only_own_pages(back, expected):
    assert back_url(back, 7) == expected


def test_pages_require_login(client):
    for url in ("/", "/history", "/calendar", "/plants/1"):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_wrong_password(client):
    token = csrf_of(client, "/login")
    resp = client.post("/login", data={"username": "tester", "password": "nope", "csrf": token})
    assert "Неверный логин или пароль" in resp.text


def test_post_without_csrf_rejected(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    resp = logged_in.post(f"/plants/{plant}/done/water", data={"csrf": "wrong"})
    assert resp.status_code == 403
    assert events() == []


def test_pages_render(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    for url in ("/", "/history", "/calendar", f"/plants/{plant}", f"/plants/{plant}/edit", "/plants/new"):
        assert logged_in.get(url).status_code == 200, url


def test_mark_done_from_site(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    resp = logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token, "back": "index"},
                          follow_redirects=False)
    assert resp.headers["location"] == "/"
    assert events() == [(plant, "water")]
    logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token})  # двойной клик
    assert events() == [(plant, "water")]
    assert logged_in.post(f"/plants/{plant}/done/drop", data={"csrf": token}).status_code == 400


def test_create_plant_validation(logged_in, db_plants):
    token = csrf_of(logged_in, "/plants/new")
    bad = logged_in.post("/plants/new", data={"csrf": token, "name": "Кактус", "water_summer_days": "abc",
                                              "water_winter_days": "30"})
    assert bad.status_code == 400
    ok = logged_in.post("/plants/new", data={"csrf": token, "name": "Кактус", "water_summer_days": "10",
                                             "water_winter_days": "30", "notes": "x" * 20000},
                        follow_redirects=False)
    assert ok.status_code == 303
    page = logged_in.get(ok.headers["location"])
    assert "Кактус" in page.text


def png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (40, 30), "green").save(buf, "PNG")
    return buf.getvalue()


def test_photos_flow(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    bad = logged_in.post(f"/plants/{plant}/photos", data={"csrf": token},
                         files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400
    for _ in range(2):
        logged_in.post(f"/plants/{plant}/photos", data={"csrf": token}, files={"file": ("a.png", png_bytes(), "image/png")})
    ids = [int(i) for i in re.findall(r'/photos/(\d+)/thumb', logged_in.get(f"/plants/{plant}").text)]
    assert len(set(ids)) == 2
    assert logged_in.get(f"/photos/{ids[0]}").headers["content-type"] == "image/jpeg"
    assert logged_in.post(f"/photos/{ids[1]}/main", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert logged_in.post(f"/photos/{ids[1]}/delete", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert logged_in.get(f"/photos/{ids[1]}").status_code == 404
    assert logged_in.get(f"/photos/{ids[1]}/thumb").status_code == 404


def test_comments_and_missing_rows(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    logged_in.post(f"/plants/{plant}/comments", data={"csrf": token, "text": "Новый лист"})
    page = logged_in.get(f"/plants/{plant}").text
    assert "Новый лист" in page
    comment = int(re.search(r'/comments/(\d+)/delete', page).group(1))
    logged_in.post(f"/comments/{comment}/delete", data={"csrf": token})
    assert "Новый лист" not in logged_in.get(f"/plants/{plant}").text
    for url in (f"/comments/{comment}/delete", "/events/999999/delete", "/photos/999999/main", "/photos/999999/delete"):
        assert logged_in.post(url, data={"csrf": token}).status_code == 404, url
    assert logged_in.get("/plants/999999").status_code == 404


def login(client, password: str):
    token = csrf_of(client, "/login")
    return client.post("/login", data={"username": "tester", "password": password, "csrf": token},
                       follow_redirects=False)


def test_login_attempts_limited(client):
    for _ in range(auth.LOGIN_ATTEMPTS):
        assert login(client, "wrong").status_code == 200
    blocked = login(client, "test-password-long")  # даже верный пароль — пока действует блокировка
    assert blocked.status_code == 429 and "Слишком много попыток" in blocked.text
    auth._failures.clear()
    assert login(client, "test-password-long").status_code == 303


def test_successful_login_resets_counter(client):
    for _ in range(auth.LOGIN_ATTEMPTS - 1):
        login(client, "wrong")
    assert login(client, "test-password-long").status_code == 303
    assert login(client, "wrong").status_code == 200  # счётчик начат заново


def test_mark_done_backdated(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    today = care.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token, "day": yesterday})
    with get_db() as db:
        [done_at] = [r[0] for r in db.execute("SELECT done_at FROM events")]
    assert done_at.startswith(yesterday + "T12:00")
    for bad in ((today + timedelta(days=1)).isoformat(), (today - timedelta(days=400)).isoformat(), "abc"):
        assert logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token, "day": bad}).status_code == 400
    assert len(events()) == 1

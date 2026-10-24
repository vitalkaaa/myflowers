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
    for url in ("/", "/?view=table", "/?view=cards", "/history", "/calendar", "/archive",
                f"/plants/{plant}", f"/plants/{plant}/edit", "/plants/new"):
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
                                              "water_winter_days": "30", "notes": "мой <b>текст</b>"})
    assert bad.status_code == 400
    # ошибка — над формой в стиле сайта, введённое сохранено (и экранировано)
    assert "нужно целое число" in bad.text and '"detail"' not in bad.text
    assert 'value="Кактус"' in bad.text and "мой &lt;b&gt;текст&lt;/b&gt;" in bad.text
    assert 'id="f-water_summer_days" class="invalid"' in bad.text
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
    assert bad.status_code == 200 and "Фото не добавлено: Файл не похож на изображение" in bad.text
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


AJAX = {"X-Requested-With": "fetch"}


def test_error_page_instead_of_json(logged_in, db_plants):
    resp = logged_in.get("/plants/999999")
    assert resp.status_code == 404 and "Растение не найдено" in resp.text and "<html" in resp.text
    assert logged_in.get("/no-such-page").status_code == 404
    bad = logged_in.get("/plants/abc")
    assert bad.status_code == 400 and "<html" in bad.text
    stale = logged_in.post("/plants/1/done/water", data={"csrf": "old"})
    assert stale.status_code == 403 and "Страница устарела" in stale.text and '"detail"' not in stale.text
    assert logged_in.post("/plants/1/done/water", data={"csrf": "old"}, headers=AJAX).json()["ok"] is False


def test_stale_csrf_keeps_form_values(logged_in, db_plants):
    data = {"csrf": "old", "name": "Кактус", "water_summer_days": "10", "water_winter_days": "30"}
    same_site = logged_in.post("/plants/new", data=data, headers={"Sec-Fetch-Site": "same-origin"})
    assert same_site.status_code == 403 and 'value="Кактус"' in same_site.text
    cross_site = logged_in.post("/plants/new", data=data, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403 and "Кактус" not in cross_site.text


def test_mark_done_ajax_and_undo(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, "/")
    data = logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token}, headers=AJAX).json()
    assert data["ok"] and "Папоротник" in data["message"] and data["undo"]["url"] == "/events/undo"
    assert events() == [(plant, "water")]
    again = logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token}, headers=AJAX).json()
    assert again["message"] == "Уже отмечено" and again["undo"] is None
    logged_in.post("/events/undo", data={"csrf": token, **data["undo"]["fields"]}, headers=AJAX)
    assert events() == []


def test_today_block_and_mark_all(logged_in, db_plants, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: care.date(2026, 1, 11))
    fern, ficus = db_plants(name="Папоротник"), db_plants(name="Фикус", spray_days=3)
    page = logged_in.get("/").text
    assert 'id="today"' in page and "Всё полито" in page
    token = csrf_of(logged_in, "/")
    resp = logged_in.post("/today/done", data={"csrf": token, "kind": "water", "plant": [str(fern), str(ficus), "x"]},
                          headers=AJAX).json()
    assert sorted(events()) == sorted([(fern, "water"), (ficus, "water")])
    assert resp["undo"]["fields"]["ids"].count(",") == 1
    assert "Всё полито" not in logged_in.get("/").text  # поливать больше некого — осталась группа опрыскивания
    assert logged_in.post("/today/done", data={"csrf": token, "kind": "drop", "plant": str(fern)}).status_code == 400


def test_snooze_from_site(logged_in, db_plants, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: care.date(2026, 1, 11))
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, "/")
    data = logged_in.post(f"/plants/{plant}/snooze/water", data={"csrf": token, "days": "2"}, headers=AJAX).json()
    assert "до 13.01" in data["message"]
    assert "Всё сделано" in logged_in.get("/").text or "всё сделано" in logged_in.get("/").text
    for bad in ("15", "-1", "x"):
        assert logged_in.post(f"/plants/{plant}/snooze/water", data={"csrf": token, "days": bad}).status_code == 400
    logged_in.post(data["undo"]["url"], data={"csrf": token, **data["undo"]["fields"]})
    assert "Папоротник" in logged_in.get("/").text.split('id="plants"')[0]  # снова в блоке «Сегодня»


def test_archive_restore_delete(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token})
    assert logged_in.post(f"/plants/{plant}/delete", data={"csrf": token}).status_code == 400  # сначала архив
    logged_in.post(f"/plants/{plant}/archive", data={"csrf": token, "reason": "подарено"})
    assert "Папоротник" not in logged_in.get("/").text.split("archive-link")[0]
    assert "подарено" in logged_in.get("/archive").text
    assert events() == [(plant, "water")]  # история сохранена
    assert logged_in.post(f"/plants/{plant}/done/water", data={"csrf": token}).status_code == 400
    logged_in.post(f"/plants/{plant}/restore", data={"csrf": token})
    assert "Папоротник" in logged_in.get("/").text
    logged_in.post(f"/plants/{plant}/archive", data={"csrf": token})
    assert logged_in.post(f"/plants/{plant}/delete", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert logged_in.get(f"/plants/{plant}").status_code == 404


def test_new_plant_from_catalog(logged_in, db_plants):
    page = logged_in.get("/plants/new?ref=Monstera").text
    assert 'value="Монстера"' in page and "catalog-data" in page
    token = csrf_of(logged_in, "/plants/new")
    resp = logged_in.post("/plants/new", data={"csrf": token, "name": "Моя монстера", "species": "Monstera deliciosa",
                                               "water_summer_days": "7", "water_winter_days": "12"})
    assert "Интересные факты" in resp.text  # справочные тексты подставлены по виду
    assert logged_in.get("/plants/new?ref=<script>").status_code == 200


def test_comment_with_photo_and_edit(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    token = csrf_of(logged_in, f"/plants/{plant}")
    logged_in.post(f"/plants/{plant}/comments", data={"csrf": token, "text": "Новый лист"},
                   files={"file": ("a.png", png_bytes(), "image/png")})
    with get_db() as db:
        comment = db.execute("SELECT * FROM comments").fetchone()
        db.execute("UPDATE comments SET processed_at = 'x'")
    assert comment["photo_id"]
    logged_in.post(f"/comments/{comment['id']}/edit", data={"csrf": token, "text": "Два новых листа"})
    with get_db() as db:
        edited = db.execute("SELECT * FROM comments").fetchone()
    assert edited["text"] == "Два новых листа" and edited["updated_at"] and edited["processed_at"] is None
    logged_in.post(f"/photos/{comment['photo_id']}/delete", data={"csrf": token})
    with get_db() as db:
        assert db.execute("SELECT photo_id FROM comments").fetchone()[0] is None  # запись осталась


def test_backdate_from_calendar(logged_in, db_plants):
    plant = db_plants(name="Папоротник")
    day = (care.today() - timedelta(days=3)).isoformat()
    page = logged_in.get(f"/calendar?day={day}").text
    assert "Отметить за этот день" in page
    token = csrf_of(logged_in, "/calendar")
    logged_in.post("/events", data={"csrf": token, "plant": str(plant), "kind": "feed", "day": day,
                                    "back": f"calendar:{day}"})
    assert events() == [(plant, "feed")]


def test_interval_suggestion(logged_in, db_plants, monkeypatch):
    monkeypatch.setattr(care, "today", lambda: care.date(2026, 7, 25))
    plant = db_plants(name="Папоротник", water_summer_days=8)
    with get_db() as db:
        db.executemany("INSERT INTO events (plant_id, kind, done_at) VALUES (?, 'water', ?)",
                       [(plant, f"2026-07-{d:02d}T10:00:00+03:00") for d in (1, 5, 9, 13, 17)])
    page = logged_in.get(f"/plants/{plant}").text
    assert "Обновить на 4 дн." in page
    token = csrf_of(logged_in, f"/plants/{plant}")
    logged_in.post(f"/plants/{plant}/interval", data={"csrf": token, "column": "water_summer_days", "days": "4"})
    with get_db() as db:
        assert db.execute("SELECT water_summer_days FROM plants").fetchone()[0] == 4
    assert logged_in.post(f"/plants/{plant}/interval",
                          data={"csrf": token, "column": "name", "days": "4"}).status_code == 400


def test_pwa_files(client):
    assert client.get("/static/manifest.webmanifest").json()["start_url"] == "/"
    assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")


def test_new_plant_with_photo(logged_in, db_plants):
    token = csrf_of(logged_in, "/plants/new")
    data = {"csrf": token, "name": "Кактус", "water_summer_days": "10", "water_winter_days": "30"}
    bad = logged_in.post("/plants/new", data=data, files={"photo": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400 and "Фото: Файл не похож на изображение" in bad.text
    assert 'value="Кактус"' in bad.text  # остальное введённое сохранено
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM plants").fetchone()[0] == 0  # растение без фото не создано
    ok = logged_in.post("/plants/new", data=data, files={"photo": ("a.png", png_bytes(), "image/png")},
                        follow_redirects=False)
    page = logged_in.get(ok.headers["location"]).text
    assert re.search(r'class="main-photo" src="/photos/\d+"', page)
    empty = logged_in.post("/plants/new", data=data, files={"photo": ("", b"", "application/octet-stream")},
                           follow_redirects=False)
    assert empty.status_code == 303  # фото необязательно

"""Авторизация: логин/пароль из окружения, сессия в подписанной cookie."""
import secrets
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import APP_PASSWORD, APP_USERNAME


def check_credentials(username: str, password: str) -> bool:
    # compare_digest — сравнение за постоянное время, без утечки по таймингам
    ok_user = secrets.compare_digest(username.encode(), APP_USERNAME.encode())
    ok_pass = secrets.compare_digest(password.encode(), APP_PASSWORD.encode())
    return ok_user and ok_pass


# Ограничение перебора пароля: не больше LOGIN_ATTEMPTS неудачных попыток с одного IP за LOGIN_WINDOW.
# Счётчик в памяти процесса — сайт работает одним процессом uvicorn; после перезапуска он обнуляется.
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}  # ip -> время неудачных попыток


def _recent(ip: str) -> list[float]:
    now = time.monotonic()
    recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


def login_blocked(ip: str) -> bool:
    return len(_recent(ip)) >= LOGIN_ATTEMPTS


def login_failed(ip: str) -> None:
    if len(_failures) > 10_000:  # перебор с множества адресов — не даём словарю расти бесконечно
        for other in list(_failures):
            _recent(other)
    _failures.setdefault(ip, []).append(time.monotonic())


def login_succeeded(ip: str) -> None:
    _failures.pop(ip, None)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("csrf", "")
    return bool(expected) and secrets.compare_digest(token.encode(), expected.encode())


class LoginRequired(Exception):
    pass


def require_login(request: Request) -> None:
    if not request.session.get("user"):
        raise LoginRequired


def login_redirect(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)

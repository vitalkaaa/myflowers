"""Авторизация: логин/пароль из окружения, сессия в подписанной cookie."""
import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import APP_PASSWORD, APP_USERNAME


def check_credentials(username: str, password: str) -> bool:
    # compare_digest — сравнение за постоянное время, без утечки по таймингам
    ok_user = secrets.compare_digest(username.encode(), APP_USERNAME.encode())
    ok_pass = secrets.compare_digest(password.encode(), APP_PASSWORD.encode())
    return ok_user and ok_pass


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

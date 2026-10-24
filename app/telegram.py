"""Вызовы Telegram Bot API.

Токен бота входит в URL запроса, поэтому в лог пишем только метод, тип ошибки
и описание от Telegram — никогда URL, текст исключения или traceback httpx.
"""
import logging

import httpx

from app import config

log = logging.getLogger("telegram")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def proxy_info() -> str:
    """Для лога: через какой прокси шли запросы — только схема, хост и порт, без логина и пароля."""
    if not config.TELEGRAM_PROXY:
        return "напрямую, без прокси"
    url = httpx.URL(config.TELEGRAM_PROXY)
    return f"через прокси {url.scheme}://{url.host}:{url.port or 'порт не указан'}"


def proxy_is_valid() -> bool:
    return not config.TELEGRAM_PROXY or config.TELEGRAM_PROXY.split("://")[0] in PROXY_SCHEMES


def call(method: str, params: dict, timeout: float = 20):
    """Вызывает метод API. Возвращает result или None при любой ошибке (она уже записана в лог)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = httpx.post(url, json=params, timeout=timeout, proxy=config.TELEGRAM_PROXY or None)
    except httpx.HTTPError as exc:
        log.error("Нет связи с api.telegram.org %s (%s, %s). Проверьте сеть, VPN или TELEGRAM_PROXY",
                  proxy_info(), type(exc).__name__, method)
        return None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("ok"):
        log.error("Telegram %s ответил %s: %s", method, resp.status_code, data.get("description"))
        return None
    return data["result"]

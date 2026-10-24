"""Проверка и пережатие загружаемых фото.

Файл открывается Pillow (проверка сигнатуры), разрешены только JPEG/PNG/WebP,
затем картинка заново кодируется в JPEG — так из файла выбрасывается всё,
кроме пикселей (EXIF с геолокацией, вложенные данные и т.п.).
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}  # MPO — так Pillow видит JPEG с iPhone
FULL_SIZE = 1600
THUMB_SIZE = 240

Image.MAX_IMAGE_PIXELS = 40_000_000  # защита от «декомпрессионных бомб»


def process(raw: bytes, filename: str) -> tuple[bytes, bytes]:
    """Возвращает (фото, превью) в JPEG или бросает ValueError."""
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Разрешены только файлы .jpg, .jpeg, .png, .webp")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("Файл больше 10 МБ")
    try:
        img = Image.open(BytesIO(raw))
        if img.format not in ALLOWED_FORMATS:
            raise ValueError
        img.load()
    except Exception as exc:
        raise ValueError("Файл не похож на изображение JPEG/PNG/WebP") from exc

    img = ImageOps.exif_transpose(img).convert("RGB")
    return _to_jpeg(img, FULL_SIZE), _to_jpeg(img, THUMB_SIZE)


def _to_jpeg(img: Image.Image, size: int) -> bytes:
    copy = img.copy()
    copy.thumbnail((size, size))
    buf = BytesIO()
    copy.save(buf, "JPEG", quality=85)
    return buf.getvalue()

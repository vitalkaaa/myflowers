FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed_photos ./seed_photos

# Не запускаем от root; БД лежит в смонтированном томе /app/data.
# Код и фото — только на чтение (права файлов с хоста могут быть 600 и недоступны пользователю flowers)
RUN useradd --create-home --uid 1000 flowers && mkdir -p /app/data && chown flowers /app/data \
    && chmod -R a+rX,go-w /app/app /app/seed_photos
USER flowers

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

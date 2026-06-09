FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY main.py postgres_driver.py schema.sql ./
COPY materials/ ./materials/

EXPOSE 8001

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 main:app"]

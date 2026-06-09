FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANDING_PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY landing_driver.py index.html index1.html index2.html ./

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "landing_driver:app"]

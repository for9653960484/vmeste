import os
import re
import time

import redis
from dotenv import load_dotenv
import requests
from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

app = Flask(__name__, static_folder=".")

# Значения для CRM, соответствующие контенту лендинга.
LANDING_SOURCES = {
    "landing-1": os.getenv("LANDING_1_SOURCE", "landing_1"),
    "landing-2": os.getenv("LANDING_2_SOURCE", "landing_2"),
}
DEFAULT_INTEREST_TYPE = "consultation"
CRM_API_URL = os.getenv("CRM_API_URL", "http://127.0.0.1:8000/lead")
MIN_FORM_FILL_SECONDS = int(os.getenv("MIN_FORM_FILL_SECONDS", "3"))
RATE_LIMIT_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "redis://127.0.0.1:6379/0")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_REGEX = re.compile(r"^\+7\d{10}$")
CYRILLIC_NAME_REGEX = re.compile(r"^[А-Яа-яЁё\\-\\s]+$")


def resolve_rate_limit_storage_uri() -> str:
    if not RATE_LIMIT_STORAGE_URI.startswith("redis://"):
        return RATE_LIMIT_STORAGE_URI
    try:
        redis.from_url(RATE_LIMIT_STORAGE_URI, socket_connect_timeout=1, socket_timeout=1).ping()
        return RATE_LIMIT_STORAGE_URI
    except Exception:
        app.logger.warning("Redis недоступен, fallback на memory:// для rate-limit.")
        return "memory://"


limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=resolve_rate_limit_storage_uri(),
    default_limits=["120 per hour"],
)


def _stringify_error(data) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("details"), str):
            return data["details"]
        if isinstance(data.get("error"), str):
            return data["error"]
        return str(data)
    return str(data)


@app.get("/")
def landing_page():
    return send_from_directory(".", "index.html")


@app.get("/landing-1")
def landing_page_1():
    return send_from_directory(".", "index1.html")


@app.get("/landing-2")
def landing_page_2():
    return send_from_directory(".", "index2.html")


@app.post("/api/submit-lead")
@limiter.limit("10 per minute")
def submit_lead():
    payload = request.get_json(silent=True) or {}

    # Honeypot: боты обычно заполняют все поля, включая скрытые.
    if str(payload.get("website", "")).strip():
        return jsonify({"error": "validation_error", "details": "bot_detected"}), 400

    loaded_at_ms = payload.get("loaded_at")
    try:
        loaded_at_ms = int(loaded_at_ms)
    except (TypeError, ValueError):
        return jsonify({"error": "validation_error", "details": "missing_loaded_at"}), 400

    now_ms = int(time.time() * 1000)
    elapsed_seconds = (now_ms - loaded_at_ms) / 1000
    if elapsed_seconds < MIN_FORM_FILL_SECONDS:
        return jsonify({"error": "validation_error", "details": "form_submitted_too_fast"}), 400

    required_fields = ["last_name", "first_name", "phone", "email"]
    missing = [field for field in required_fields if not str(payload.get(field, "")).strip()]
    if missing:
        return jsonify({"error": "validation_error", "details": f"missing required fields: {', '.join(missing)}"}), 400

    first_name = str(payload.get("first_name", "")).strip()
    last_name = str(payload.get("last_name", "")).strip()
    if not CYRILLIC_NAME_REGEX.fullmatch(first_name):
        return jsonify({"error": "validation_error", "details": "invalid_first_name_cyrillic_only"}), 400
    if not CYRILLIC_NAME_REGEX.fullmatch(last_name):
        return jsonify({"error": "validation_error", "details": "invalid_last_name_cyrillic_only"}), 400

    email = str(payload.get("email", "")).strip().lower()
    phone = str(payload.get("phone", "")).strip()
    if not EMAIL_REGEX.fullmatch(email):
        return jsonify({"error": "validation_error", "details": "invalid_email_format"}), 400
    if not PHONE_REGEX.fullmatch(phone):
        return jsonify({"error": "validation_error", "details": "invalid_phone_format_use_+7XXXXXXXXXX"}), 400

    consent = bool(payload.get("consent"))
    if not consent:
        return jsonify({"error": "validation_error", "details": "consent_required"}), 400

    landing_id = str(payload.get("landing_id", "")).strip().lower()
    source = LANDING_SOURCES.get(landing_id)
    if not source:
        return jsonify({"error": "validation_error", "details": "invalid_landing_id"}), 400

    crm_payload = {
        "company_name": str(payload.get("company_name", "")).strip() or "Не указана",
        "first_name": first_name,
        "last_name": last_name,
        "position": str(payload.get("position", "")).strip() or "other",
        "email": email,
        "phone": phone,
        "source": source,
        "employees_count": payload.get("employees_count"),
        "interest_type": DEFAULT_INTEREST_TYPE,
        "marketing_consent": bool(payload.get("marketing_consent")),
    }

    try:
        response = requests.post(CRM_API_URL, json=crm_payload, timeout=15)
        data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
    except requests.RequestException as exc:
        return jsonify({"error": "crm_unreachable", "details": str(exc)}), 502

    if not response.ok:
        return (
            jsonify(
                {
                    "error": "crm_error",
                    "status_code": response.status_code,
                    "details": _stringify_error(data),
                    "crm_response": data,
                }
            ),
            response.status_code,
        )

    return jsonify(
        {
            "ok": True,
            "message": "Контакт успешно отправлен",
            "lead_id": data.get("id"),
            "source": source,
            "interest_type": DEFAULT_INTEREST_TYPE,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("LANDING_PORT", "8080")), debug=True)

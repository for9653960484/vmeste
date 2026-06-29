import logging
import os
import secrets
from functools import wraps
from io import BytesIO
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import openpyxl
import requests
import time
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from postgres_driver import PostgreSQLDriver
from pydantic import BaseModel, EmailStr, ValidationError, field_validator
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Text,
    create_engine,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


# Базовая настройка логирования для API и интеграций.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Загружаем переменные из .env при локальном запуске.
load_dotenv()


# Справочники допустимых значений для лида и воронки.
class Position(str, Enum):
    OWNER = "owner"
    CEO = "ceo"
    HRD = "hrd"
    DIRECTOR_OF_DEVELOPMENT = "director_of_development"
    HEAD_OF_CORPORATE_UNIVERSITY = "head_of_corporate_university"
    OTHER = "other"


class Source(str, Enum):
    LANDING_1 = "landing_1"
    LANDING_2 = "landing_2"
    LANDING_B2B = "landing_b2b"
    WEBINAR = "webinar"
    PARTNER_MAIL = "partner_mail"
    SOCIAL_POST = "social_post"
    OTHER = "other"


class InterestType(str, Enum):
    GUIDE = "guide"
    CHECKLIST = "checklist"
    CALCULATOR = "calculator"
    CONSULTATION = "consultation"
    PILOT_LAUNCH = "pilot_launch"


class FunnelStatus(str, Enum):
    NEW = "new"
    WARMED = "warmed"
    WEBINAR = "webinar"
    PROPOSAL = "proposal"
    CONTRACT = "contract"
    LOST = "lost"


class InteractionType(str, Enum):
    EMAIL = "email"
    CALL = "call"
    MEETING = "meeting"
    WEBINAR = "webinar"
    NOTE = "note"


# Входная схема лида из лендинга (валидация запроса).
class LeadIn(BaseModel):
    company_name: str
    first_name: str
    last_name: str
    position: Position
    email: EmailStr
    phone: Optional[str] = None
    source: Source
    employees_count: Optional[int] = None
    interest_type: InterestType
    marketing_consent: bool = False

    @field_validator("company_name", "first_name", "last_name")
    @classmethod
    def names_must_not_be_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("employees_count")
    @classmethod
    def employees_must_be_positive(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("must be >= 0")
        return value


class LeadUpdate(BaseModel):
    company_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    position: Optional[Position] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    source: Optional[Source] = None
    employees_count: Optional[int] = None
    interest_type: Optional[InterestType] = None

    @field_validator("company_name", "first_name", "last_name")
    @classmethod
    def optional_names_must_not_be_empty(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("employees_count")
    @classmethod
    def optional_employees_must_be_positive(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("must be >= 0")
        return value


class Base(DeclarativeBase):
    pass


def enum_values(enum_cls: type[Enum]) -> list[str]:
    return [item.value for item in enum_cls]


# ORM-модели таблиц CRM.
class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("employees_count IS NULL OR employees_count >= 0", name="ck_leads_employees_count_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[Position] = mapped_column(
        SqlEnum(Position, name="lead_position", values_callable=enum_values), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Source] = mapped_column(
        SqlEnum(Source, name="lead_source", values_callable=enum_values), nullable=False
    )
    employees_count: Mapped[Optional[int]] = mapped_column(nullable=True)
    interest_type: Mapped[InterestType] = mapped_column(
        SqlEnum(InterestType, name="lead_interest_type", values_callable=enum_values), nullable=False
    )
    marketing_consent: Mapped[bool] = mapped_column(nullable=False, default=False)
    mailing_sent: Mapped[bool] = mapped_column(nullable=False, default=False)
    mailing_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mailing_provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mailing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    statuses: Mapped[list["LeadStatusHistory"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class LeadStatusHistory(Base):
    __tablename__ = "lead_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[FunnelStatus] = mapped_column(
        SqlEnum(FunnelStatus, name="funnel_status", values_callable=enum_values), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="statuses")


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    interaction_type: Mapped[InteractionType] = mapped_column(
        SqlEnum(InteractionType, name="interaction_type", values_callable=enum_values), nullable=False
    )
    short_description: Mapped[str] = mapped_column(Text, nullable=False)
    interaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    lead: Mapped[Lead] = relationship(back_populates="interactions")


# Конфигурация окружения: БД и внешний email-сервис.
database_url = os.getenv("DATABASE_URL")
if not database_url:
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "crm_db")
    db_user = os.getenv("DB_USER", "crm")
    db_password = os.getenv("DB_PASSWORD", "crm")
    database_url = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

DATABASE_URL = database_url
UNISENDER_API_BASE_URL = os.getenv("UNISENDER_API_BASE_URL", "https://api.unisender.com/ru/api")
UNISENDER_API_KEY = os.getenv("UNISENDER_API_KEY", os.getenv("EMAIL_SERVICE_API_KEY", ""))
UNISENDER_LIST_IDS = os.getenv("UNISENDER_LIST_IDS", os.getenv("EMAIL_SERVICE_LIST_ID", ""))
UNISENDER_LIST_IDS_BY_SOURCE_RAW = os.getenv("UNISENDER_LIST_IDS_BY_SOURCE", "")
UNISENDER_SENDER_NAME = os.getenv("UNISENDER_SENDER_NAME", "VMESTE")
UNISENDER_SENDER_EMAIL = os.getenv("UNISENDER_SENDER_EMAIL", "noreply@example.com")
UNISENDER_MATERIAL_SUBJECT = os.getenv(
    "UNISENDER_MATERIAL_SUBJECT", "Ваш чек-лист: Карта управленческих проблем"
)
UNISENDER_MATERIAL_URL = os.getenv("UNISENDER_MATERIAL_URL", "https://example.com/materials/checklist")
UNISENDER_ALLOWED_SOURCES = {
    item.strip() for item in os.getenv("UNISENDER_ALLOWED_SOURCES", "landing_1,landing_2").split(",") if item.strip()
}


def parse_unisender_list_ids_by_source(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        source, sep, list_ids = pair.partition(":")
        source = source.strip()
        list_ids = list_ids.strip()
        if sep and source and list_ids:
            mapping[source] = list_ids
    return mapping


UNISENDER_LIST_IDS_BY_SOURCE = parse_unisender_list_ids_by_source(UNISENDER_LIST_IDS_BY_SOURCE_RAW)


def get_unisender_list_ids_for_source(source: Optional[str]) -> str:
    if source and source in UNISENDER_LIST_IDS_BY_SOURCE:
        return UNISENDER_LIST_IDS_BY_SOURCE[source]
    return UNISENDER_LIST_IDS


# Инициализация подключения к БД, сессий и пула фоновых задач.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
executor = ThreadPoolExecutor(max_workers=4)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("ADMIN_SESSION_HOURS", "8")))

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_LOGIN_MIN_FILL_SECONDS = int(os.getenv("ADMIN_LOGIN_MIN_FILL_SECONDS", "2"))
ADMIN_LOGIN_MAX_ATTEMPTS = int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5"))
ADMIN_LOGIN_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "900"))

_admin_login_attempts: dict[str, list[float]] = {}
_admin_login_lock = Lock()

db_init_lock = Lock()
db_initialized = False
driver = PostgreSQLDriver()


def is_admin_authenticated() -> bool:
    if not ADMIN_PASSWORD:
        return False
    return session.get("admin_authenticated") is True


def is_admin_login_rate_limited(client_ip: str) -> bool:
    now = time.time()
    with _admin_login_lock:
        attempts = [t for t in _admin_login_attempts.get(client_ip, []) if now - t < ADMIN_LOGIN_WINDOW_SECONDS]
        _admin_login_attempts[client_ip] = attempts
        return len(attempts) >= ADMIN_LOGIN_MAX_ATTEMPTS


def record_admin_login_failure(client_ip: str) -> None:
    now = time.time()
    with _admin_login_lock:
        attempts = _admin_login_attempts.setdefault(client_ip, [])
        attempts.append(now)
        _admin_login_attempts[client_ip] = [
            t for t in attempts if now - t < ADMIN_LOGIN_WINDOW_SECONDS
        ]


def clear_admin_login_attempts(client_ip: str) -> None:
    with _admin_login_lock:
        _admin_login_attempts.pop(client_ip, None)


def require_admin(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD:
            if request.path.startswith("/admin/leads") or request.method != "GET":
                return jsonify({"error": "admin_disabled", "details": "ADMIN_PASSWORD not configured"}), 503
            return (
                render_template_string(
                    "<h1>Админка отключена</h1><p>Задайте переменную окружения ADMIN_PASSWORD на сервере.</p>"
                ),
                503,
            )
        if is_admin_authenticated():
            return f(*args, **kwargs)
        is_api = request.path.startswith("/admin/leads") or request.method in (
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
        )
        if is_api:
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("admin_login", next=request.path))

    return wrapped


def db_init_error_response(exc: Exception):
    # Отдаем короткую причину, чтобы UI показывал, почему БД недоступна.
    short_detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return jsonify({"error": "db_init_error", "details": short_detail}), 500


def duplicate_lead_error_response(exc: Exception):
    details = str(exc)
    return (
        jsonify(
            {
                "error": "duplicate_lead",
                "details": "Контакт с такой компанией и email уже существует",
                "raw_details": details,
            }
        ),
        409,
    )


def is_duplicate_lead_error(exc: Exception) -> bool:
    details = str(exc)
    return "uq_leads_company_email" in details or "уже существует" in details.lower()


def check_driver_connection() -> None:
    # Проверка подключения через кастомный драйвер для единого способа диагностики БД.
    try:
        driver.connect()
    finally:
        driver.disconnect()

ADMIN_LOGIN_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Вход — VMESTE CRM Admin</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f7f9fc; color: #222; }
    .card { width: min(420px, calc(100vw - 32px)); background: #fff; border: 1px solid #e4e8ef; border-radius: 12px; padding: 24px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06); }
    h1 { margin: 0 0 8px; font-size: 24px; }
    .hint { color: #666; margin-bottom: 20px; font-size: 14px; }
    label { display: block; font-size: 13px; margin-bottom: 6px; color: #555; }
    input, button { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #cfd6e4; box-sizing: border-box; font-size: 15px; }
    button { margin-top: 16px; background: #1e63ff; color: white; border: none; cursor: pointer; font-weight: 600; }
    .error { margin-bottom: 14px; padding: 10px 12px; border-radius: 8px; background: #fdecec; color: #b42318; border: 1px solid #f5c2c0; font-size: 14px; }
    .hp { position: absolute; left: -10000px; top: auto; width: 1px; height: 1px; overflow: hidden; }
  </style>
</head>
<body>
  <div class="card">
    <h1>VMESTE CRM Admin</h1>
    <div class="hint">Введите пароль для доступа к базе лидов</div>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="post" action="/admin/login">
      <input type="hidden" name="next" value="{{ next_url }}" />
      <input type="hidden" name="loaded_at" value="{{ loaded_at }}" />
      <div class="hp" aria-hidden="true">
        <label for="bot_trap">Не заполняйте это поле</label>
        <input id="bot_trap" name="bot_trap" type="text" tabindex="-1" autocomplete="off" />
      </div>
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password" required autofocus />
      <button type="submit">Войти</button>
    </form>
  </div>
</body>
</html>
"""

ADMIN_PAGE_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VMESTE CRM Admin</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f7f9fc; color: #222; }
    h1 { margin-bottom: 8px; }
    .hint { color: #666; margin-bottom: 20px; }
    .card { background: #fff; border: 1px solid #e4e8ef; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; }
    label { display: block; font-size: 13px; margin-bottom: 4px; color: #555; }
    input, select, button { width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid #cfd6e4; box-sizing: border-box; }
    button { background: #1e63ff; color: white; border: none; cursor: pointer; font-weight: 600; }
    button.secondary { background: #6b7280; }
    button.export { background: #0f9d58; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
    .result { background: #0f172a; color: #d1e3ff; padding: 12px; border-radius: 8px; overflow: auto; max-height: 320px; white-space: pre-wrap; }
    .row { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }
    .notice { margin-bottom: 12px; padding: 10px 12px; border-radius: 8px; font-weight: 600; display: none; }
    .notice.success { display: block; background: #e7f8ee; color: #146c2e; border: 1px solid #b7e7c4; }
    .leads-table-wrap { overflow-x: auto; margin-top: 10px; }
    .leads-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .leads-table th, .leads-table td { border: 1px solid #e4e8ef; padding: 8px 10px; text-align: left; vertical-align: top; }
    .leads-table th { background: #f0f4fa; color: #445067; font-weight: 600; white-space: nowrap; }
    .leads-table tbody tr:nth-child(even) { background: #fafbfd; }
    .leads-empty { color: #6a778d; padding: 12px 0; }
    .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 8px; }
    .topbar h1 { margin: 0; }
    .logout-link { color: #1e63ff; text-decoration: none; font-weight: 600; white-space: nowrap; }
    .logout-link:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>VMESTE CRM Admin</h1>
    <a class="logout-link" href="/admin/logout">Выйти</a>
  </div>
  <div class="hint">Добавление, поиск, редактирование лидов и экспорт в Excel</div>
  <div id="notice" class="notice"></div>

  <div class="card">
    <h3>1) Добавить контакт вручную</h3>
    <form id="createForm">
      <div class="grid">
        <div><label>Компания</label><input name="company_name" required /></div>
        <div><label>Имя</label><input name="first_name" required /></div>
        <div><label>Фамилия</label><input name="last_name" required /></div>
        <div>
          <label>Должность</label>
          <select name="position" required>
            <option value="owner">Собственник</option><option value="ceo">Генеральный директор</option><option value="hrd">HR-директор</option>
            <option value="director_of_development">Директор по развитию</option>
            <option value="head_of_corporate_university">Руководитель корпоративного университета</option>
            <option value="other">Другое</option>
          </select>
        </div>
        <div><label>Email</label><input name="email" type="email" required /></div>
        <div><label>Телефон</label><input name="phone" /></div>
        <div>
          <label>Источник</label>
          <select name="source" required>
            <option value="landing_b2b">Лендинг B2B</option><option value="webinar">Вебинар</option>
            <option value="partner_mail">Партнерская рассылка</option><option value="social_post">Пост в соцсетях</option>
            <option value="other">Другое</option>
          </select>
        </div>
        <div><label>Сотрудников</label><input name="employees_count" type="number" min="0" /></div>
        <div>
          <label>Интерес</label>
          <select name="interest_type" required>
            <option value="guide">Гайд</option><option value="checklist">Чек-лист</option>
            <option value="calculator">Калькулятор</option><option value="consultation">Консультация</option>
            <option value="pilot_launch">Запуск пилота</option>
          </select>
        </div>
      </div>
      <div class="actions">
        <button type="submit">Сохранить контакт</button>
        <button type="button" class="export" onclick="downloadExport()">Скачать XLSX</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>2) Найти лид и внести правки</h3>
    <div class="grid">
      <div><label>ID</label><input id="searchId" type="number" min="1" /></div>
      <div><label>Фамилия</label><input id="searchLastName" /></div>
      <div><label>Компания</label><input id="searchCompany" /></div>
      <div style="align-self:end;"><button type="button" class="secondary" onclick="searchLeads()">Найти</button></div>
    </div>
    <div style="margin-top: 10px;">
      <label>Результаты поиска</label>
      <select id="searchResults" onchange="fillEditFormFromSelected()"></select>
    </div>

    <form id="editForm" style="margin-top: 12px;">
      <div class="grid">
        <div><label>ID лида (обязательно)</label><input name="id" type="number" min="1" required /></div>
        <div><label>Компания</label><input name="company_name" /></div>
        <div><label>Имя</label><input name="first_name" /></div>
        <div><label>Фамилия</label><input name="last_name" /></div>
        <div><label>Email</label><input name="email" type="email" /></div>
        <div><label>Телефон</label><input name="phone" /></div>
        <div><label>Сотрудников</label><input name="employees_count" type="number" min="0" /></div>
      </div>
      <div class="actions">
        <button type="submit">Сохранить изменения</button>
        <button type="button" class="secondary" onclick="clearEditForm()">Очистить форму</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>Текущие лиды</h3>
    <div id="leadsListStatus" class="hint">Загрузка списка...</div>
    <div class="leads-table-wrap">
      <table id="leadsTable" class="leads-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Компания</th>
            <th>ФИО</th>
            <th>Email</th>
            <th>Телефон</th>
            <th>Источник</th>
            <th>Статус</th>
            <th>Рассылка</th>
            <th>Дата</th>
          </tr>
        </thead>
        <tbody id="leadsTableBody"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>Ответ API</h3>
    <div id="result" class="result">{}</div>
  </div>

  <script>
    const resultEl = document.getElementById("result");
    const searchResultsEl = document.getElementById("searchResults");
    const noticeEl = document.getElementById("notice");
    const leadsListStatusEl = document.getElementById("leadsListStatus");
    const leadsTableBodyEl = document.getElementById("leadsTableBody");
    let noticeTimer = null;

    async function adminFetch(url, options = {}) {
      const response = await fetch(url, { credentials: "same-origin", ...options });
      if (response.status === 401) {
        window.location.href = "/admin/login?next=" + encodeURIComponent(window.location.pathname);
        throw new Error("unauthorized");
      }
      return response;
    }

    function showResult(data) {
      resultEl.textContent = JSON.stringify(data, null, 2);
    }

    function showNotice(message) {
      noticeEl.textContent = message;
      noticeEl.className = "notice success";
      if (noticeTimer) clearTimeout(noticeTimer);
      noticeTimer = setTimeout(() => {
        noticeEl.style.display = "none";
      }, 3000);
    }

    function cleanValue(v) {
      return (v ?? "").toString().trim();
    }

    async function parseApiResponse(response) {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        return await response.json();
      }
      const rawText = await response.text();
      return {
        error: "unexpected_response",
        status: response.status,
        details: rawText.slice(0, 300) || contentType,
      };
    }

    function describeApiError(data, fallback) {
      if (!data) return fallback;
      if (typeof data.details === "string" && data.details.trim()) return data.details;
      if (typeof data.error === "string" && data.error.trim()) return data.error;
      return fallback;
    }

    function formatLeadDate(value) {
      if (!value) return "—";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ru-RU");
    }

    function renderLeadsTable(items) {
      leadsTableBodyEl.innerHTML = "";
      if (!items.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 9;
        cell.className = "leads-empty";
        cell.textContent = "Лиды пока не добавлены";
        row.appendChild(cell);
        leadsTableBodyEl.appendChild(row);
        return;
      }

      for (const lead of items) {
        const row = document.createElement("tr");
        const fullName = [lead.last_name, lead.first_name].filter(Boolean).join(" ");
        const mailingStatus = lead.mailing_sent ? "отправлено" : "нет";
        const cells = [
          lead.id,
          lead.company_name || "—",
          fullName || "—",
          lead.email || "—",
          lead.phone || "—",
          lead.source || "—",
          lead.current_status || "—",
          mailingStatus,
          formatLeadDate(lead.created_at),
        ];
        for (const value of cells) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        }
        leadsTableBodyEl.appendChild(row);
      }
    }

    async function loadLeadsList() {
      leadsListStatusEl.textContent = "Загрузка списка...";
      try {
        const response = await adminFetch("/admin/leads");
        const data = await parseApiResponse(response);
        if (!response.ok) {
          const message = describeApiError(data, "ошибка сервера");
          leadsListStatusEl.textContent = "Не удалось загрузить список лидов: " + message;
          showResult(data);
          renderLeadsTable([]);
          return;
        }
        const items = data.items || [];
        leadsListStatusEl.textContent = "Всего лидов: " + items.length;
        renderLeadsTable(items);
      } catch (err) {
        leadsListStatusEl.textContent = "Ошибка загрузки списка лидов";
        renderLeadsTable([]);
        showResult({ error: "network_error", details: String(err) });
      }
    }

    document.addEventListener("DOMContentLoaded", loadLeadsList);

    document.getElementById("createForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {
        company_name: cleanValue(fd.get("company_name")),
        first_name: cleanValue(fd.get("first_name")),
        last_name: cleanValue(fd.get("last_name")),
        position: cleanValue(fd.get("position")),
        email: cleanValue(fd.get("email")),
        phone: cleanValue(fd.get("phone")) || null,
        source: cleanValue(fd.get("source")),
        employees_count: cleanValue(fd.get("employees_count")) ? Number(fd.get("employees_count")) : null,
        interest_type: cleanValue(fd.get("interest_type")),
      };

      try {
        const r = await adminFetch("/admin/leads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await parseApiResponse(r);
        showResult(data);
        if (r.ok && data.id) {
          showNotice("Новый контакт внесен в базу");
          e.target.reset();
          await loadLeadsList();
        }
      } catch (err) {
        showResult({ error: "network_error", details: String(err) });
      }
    });

    async function searchLeads() {
      const id = cleanValue(document.getElementById("searchId").value);
      const lastName = cleanValue(document.getElementById("searchLastName").value);
      const company = cleanValue(document.getElementById("searchCompany").value);
      const params = new URLSearchParams();
      if (id) params.set("id", id);
      if (lastName) params.set("last_name", lastName);
      if (company) params.set("company_name", company);

      const r = await adminFetch("/admin/leads/search?" + params.toString());
      const data = await parseApiResponse(r);
      showResult(data);

      searchResultsEl.innerHTML = "";
      const items = data.items || [];
      if (!items.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "Ничего не найдено";
        searchResultsEl.appendChild(option);
        return;
      }

      for (const item of items) {
        const option = document.createElement("option");
        option.value = JSON.stringify(item);
        option.textContent = "#" + item.id + " | " + item.last_name + " " + item.first_name + " | " + item.company_name;
        searchResultsEl.appendChild(option);
      }
      fillEditFormFromSelected();
    }

    function fillEditFormFromSelected() {
      const option = searchResultsEl.options[searchResultsEl.selectedIndex];
      if (!option || !option.value || option.textContent === "Ничего не найдено") return;

      const lead = JSON.parse(option.value);
      const form = document.getElementById("editForm");
      form.elements["id"].value = lead.id || "";
      form.elements["company_name"].value = lead.company_name || "";
      form.elements["first_name"].value = lead.first_name || "";
      form.elements["last_name"].value = lead.last_name || "";
      form.elements["email"].value = lead.email || "";
      form.elements["phone"].value = lead.phone || "";
      form.elements["employees_count"].value = lead.employees_count ?? "";
    }

    function clearEditForm() {
      document.getElementById("editForm").reset();
    }

    document.getElementById("editForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const id = cleanValue(fd.get("id"));
      if (!id) {
        showResult({ error: "id is required for update" });
        return;
      }

      const body = {};
      const company_name = cleanValue(fd.get("company_name"));
      const first_name = cleanValue(fd.get("first_name"));
      const last_name = cleanValue(fd.get("last_name"));
      const email = cleanValue(fd.get("email"));
      const phone = cleanValue(fd.get("phone"));
      const employees_count = cleanValue(fd.get("employees_count"));

      if (company_name) body.company_name = company_name;
      if (first_name) body.first_name = first_name;
      if (last_name) body.last_name = last_name;
      if (email) body.email = email;
      if (phone) body.phone = phone;
      if (employees_count) body.employees_count = Number(employees_count);

      try {
        const r = await adminFetch("/admin/leads/" + id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await parseApiResponse(r);
        showResult(data);
        if (r.ok) {
          await loadLeadsList();
        }
      } catch (err) {
        showResult({ error: "network_error", details: String(err) });
      }
    });

    async function downloadExport() {
      try {
        const response = await adminFetch("/admin/leads/export.xlsx");
        if (!response.ok) {
          const errData = await parseApiResponse(response);
          showResult(errData);
          showNotice("Не удалось скачать XLSX: " + describeApiError(errData, "ошибка сервера"));
          return;
        }

        const contentType = response.headers.get("content-type") || "";
        if (!contentType.includes("spreadsheetml") && !contentType.includes("octet-stream")) {
          const errData = await parseApiResponse(response);
          showResult(errData);
          showNotice("Не удалось скачать XLSX: " + describeApiError(errData, "сервер вернул не файл"));
          return;
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "leads_export.xlsx";
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        showResult({ ok: true, message: "Файл XLSX успешно сформирован" });
      } catch (err) {
        showResult({ error: "network_error", details: String(err) });
      }
    }
  </script>
</body>
</html>
"""


def format_row_datetime(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def row_to_lead_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "company_name": row["company_name"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "position": row["position"],
        "email": row["email"],
        "phone": row["phone"],
        "source": row["source"],
        "employees_count": row["employees_count"],
        "interest_type": row["interest_type"],
        "marketing_consent": bool(row.get("marketing_consent", False)),
        "mailing_sent": bool(row.get("mailing_sent", False)),
        "mailing_sent_at": format_row_datetime(row.get("mailing_sent_at")),
        "mailing_provider": row.get("mailing_provider"),
        "mailing_error": row.get("mailing_error"),
        "created_at": format_row_datetime(row.get("created_at")),
        "current_status": row.get("current_status"),
    }


def get_lead_by_id_with_status(lead_id: int) -> Optional[dict]:
    query = """
        SELECT
            l.id,
            l.company_name,
            l.first_name,
            l.last_name,
            l.position::text AS position,
            l.email::text AS email,
            l.phone,
            l.source::text AS source,
            l.employees_count,
            l.interest_type::text AS interest_type,
            l.marketing_consent,
            l.mailing_sent,
            l.mailing_sent_at,
            l.mailing_provider,
            l.mailing_error,
            l.created_at,
            (
                SELECT h.status::text
                FROM lead_status_history h
                WHERE h.lead_id = l.id
                ORDER BY h.changed_at DESC
                LIMIT 1
            ) AS current_status
        FROM leads l
        WHERE l.id = %s
        LIMIT 1
    """
    rows = driver.execute_query(query, (lead_id,))
    if not rows:
        return None
    return row_to_lead_payload(rows[0])


def save_lead_with_driver(payload: LeadIn) -> dict:
    lead_insert_query = """
        INSERT INTO leads (
            company_name,
            first_name,
            last_name,
            position,
            email,
            phone,
            source,
            employees_count,
            interest_type,
            marketing_consent,
            mailing_sent
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    rows = driver.execute_query(
        lead_insert_query,
        (
            payload.company_name,
            payload.first_name,
            payload.last_name,
            payload.position.value,
            str(payload.email),
            payload.phone,
            payload.source.value,
            payload.employees_count,
            payload.interest_type.value,
            payload.marketing_consent,
            False,
        ),
    )
    lead_id = rows[0]["id"]
    driver.execute_non_query(
        """
        INSERT INTO lead_status_history (lead_id, status, comment)
        VALUES (%s, %s, %s)
        """,
        (lead_id, FunnelStatus.NEW.value, "Initial status set at lead creation"),
    )
    result = get_lead_by_id_with_status(lead_id)
    assert result is not None
    return result


def ensure_db_initialized() -> None:
    # Ленивая автоинициализация: расширение citext и создание ORM-таблиц.
    global db_initialized
    if db_initialized:
        return

    with db_init_lock:
        if db_initialized:
            return
        check_driver_connection()
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))

        # Обновляем enum источников в отдельной транзакции.
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'landing_1'"))
                conn.execute(text("ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'landing_2'"))
        except Exception as exc:
            # Важно: читаем enum в новой транзакции, иначе получим InFailedSqlTransaction.
            with engine.begin() as conn:
                existing_values = {
                    row[0]
                    for row in conn.execute(
                        text(
                            """
                            SELECT e.enumlabel
                            FROM pg_enum e
                            JOIN pg_type t ON t.oid = e.enumtypid
                            WHERE t.typname = 'lead_source'
                            """
                        )
                    ).all()
                }
            required_values = {"landing_1", "landing_2"}
            missing_values = required_values - existing_values
            if missing_values:
                missing_str = ", ".join(sorted(missing_values))
                raise RuntimeError(
                    "lead_source enum missing values: "
                    f"{missing_str}. Run as DB owner/postgres: "
                    "ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'landing_1'; "
                    "ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'landing_2';"
                ) from exc
        Base.metadata.create_all(bind=engine)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE leads "
                        "ADD COLUMN IF NOT EXISTS marketing_consent BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                )
        except Exception:
            # Если прав на ALTER нет, продолжаем только когда колонка уже существует.
            with engine.begin() as conn:
                column_exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'leads'
                          AND column_name = 'marketing_consent'
                        LIMIT 1
                        """
                    )
                ).scalar_one_or_none()
            if not column_exists:
                raise
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS mailing_sent BOOLEAN NOT NULL DEFAULT FALSE"))
                conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS mailing_sent_at TIMESTAMPTZ NULL"))
                conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS mailing_provider TEXT NULL"))
                conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS mailing_error TEXT NULL"))
        except Exception as exc:
            logger.warning("Could not ensure mailing columns: %s", exc)

        try:
            with engine.begin() as conn:
                conn.execute(text("DROP INDEX IF EXISTS uq_leads_company_email"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_leads_company_email ON leads (company_name, email)")
                )
        except Exception as exc:
            logger.warning("Could not migrate leads index (duplicate signups may still be blocked): %s", exc)

        db_initialized = True


def build_leads_xlsx(leads_payload: list[dict]) -> BytesIO:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Leads"

    headers = [
        "id",
        "company_name",
        "first_name",
        "last_name",
        "position",
        "email",
        "phone",
        "source",
        "employees_count",
        "interest_type",
        "marketing_consent",
        "mailing_sent",
        "mailing_sent_at",
        "mailing_provider",
        "mailing_error",
        "created_at",
        "current_status",
    ]
    sheet.append(headers)

    for row in leads_payload:
        sheet.append([row.get(col) for col in headers])

    for column in sheet.columns:
        max_len = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max_len + 2, 50)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output

def unisender_post(url: str, payload: dict, *, retries: int = 3, timeout: int = 20) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.post(url, data=payload, timeout=timeout)
            response.raise_for_status()
            return response
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise last_exc or RuntimeError("UniSender request failed")


def is_unisender_send_email_plan_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return "free plan" in lowered or "confirmed emails only" in lowered

# Отправка контакта во внешний сервис рассылок (не блокирует API-ответ).
def send_to_email_service(data: dict) -> None:
    # Отправляем рассылку только при явном согласии пользователя.
    lead_id = data.get("lead_id")
    if not lead_id:
        logger.warning("Skipping email sync: lead_id missing for %s", data.get("email"))
        return
    source = data.get("source")
    if source not in UNISENDER_ALLOWED_SOURCES:
        logger.info("Skipping email sync (source '%s' is not allowed) for lead %s", source, lead_id)
        return
    if not data.get("marketing_consent"):
        logger.info("Skipping email sync (no marketing consent) for lead %s", lead_id)
        return
    if not UNISENDER_API_KEY:
        logger.warning("Skipping UniSender sync: UNISENDER_API_KEY is empty")
        return

    email = data["email"]
    first_name = data["first_name"]
    last_name = data["last_name"]
    list_ids = get_unisender_list_ids_for_source(source)
    primary_list_id = list_ids.split(",")[0].strip() if list_ids else ""
    if not list_ids:
        logger.warning(
            "Skipping UniSender sync: no list IDs configured for source '%s' (lead %s)",
            source,
            lead_id,
        )
        return

    try:
        # 1) Добавляем контакт в список рассылки.
        subscribe_payload = {
            "format": "json",
            "api_key": UNISENDER_API_KEY,
            "list_ids": list_ids,
            "fields[email]": email,
            "fields[Name]": first_name,
            "fields[last_name]": last_name,
            "fields[phone]": data.get("phone") or "",
            "fields[company]": data.get("company_name") or "",
            "fields[source]": source or "",
            "double_optin": 0,
            "overwrite": 2,
        }
        subscribe_response = unisender_post(
            f"{UNISENDER_API_BASE_URL}/subscribe", subscribe_payload
        )
        subscribe_json = subscribe_response.json()
        if subscribe_json.get("error"):
            raise RuntimeError(f"UniSender subscribe error: {subscribe_json}")

        # 2) Отправляем письмо с материалом (ошибка sendEmail не отменяет успешный subscribe).
        html_body = (
            "<p>Спасибо за заявку!</p>"
            "<p>Ваш запрошенный материал доступен по ссылке:</p>"
            f'<p><a href="{UNISENDER_MATERIAL_URL}">{UNISENDER_MATERIAL_URL}</a></p>'
        )
        send_payload = {
            "format": "json",
            "api_key": UNISENDER_API_KEY,
            "email": email,
            "sender_name": UNISENDER_SENDER_NAME,
            "sender_email": UNISENDER_SENDER_EMAIL,
            "subject": UNISENDER_MATERIAL_SUBJECT,
            "body": html_body,
            "list_id": primary_list_id,
        }
        send_error: str | None = None
        try:
            send_response = unisender_post(
                f"{UNISENDER_API_BASE_URL}/sendEmail", send_payload
            )
            send_json = send_response.json()
            if send_json.get("error"):
                error_text = str(send_json.get("error"))
                send_error = error_text[:500]
                if is_unisender_send_email_plan_error(error_text):
                    logger.warning("UniSender sendEmail skipped (free plan): %s", error_text)
                else:
                    logger.warning("UniSender sendEmail error: %s", error_text)
        except Exception as send_exc:
            error_text = str(send_exc)
            send_error = (error_text.splitlines()[0] if error_text else send_exc.__class__.__name__)[:500]
            if is_unisender_send_email_plan_error(error_text):
                logger.warning("UniSender sendEmail skipped (free plan): %s", error_text)
            else:
                logger.warning("UniSender sendEmail failed: %s", error_text)

        driver.execute_non_query(
            """
            UPDATE leads
            SET mailing_sent = %s,
                mailing_sent_at = NOW(),
                mailing_provider = %s,
                mailing_error = %s
            WHERE id = %s
            """,
            (True, "unisender", send_error, lead_id),
        )
        logger.info("UniSender sync completed for lead %s", lead_id)
    except Exception as exc:
        short_error = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        try:
            driver.execute_non_query(
                """
                UPDATE leads
                SET mailing_sent = %s,
                    mailing_provider = %s,
                    mailing_error = %s
                WHERE id = %s
                """,
                (False, "unisender", short_error[:500], lead_id),
            )
        except Exception:
            logger.exception("Failed to persist mailing_error for lead %s", lead_id)
        # Do not fail lead creation if email platform is unavailable.
        logger.exception("Email service sync failed for %s: %s", email, exc)


@app.post("/lead")
def create_lead():
    try:
        ensure_db_initialized()
    except Exception as exc:
        logger.exception("Database init failed: %s", exc)
        return db_init_error_response(exc)

    # Валидируем JSON от формы и возвращаем 400 при ошибке.
    try:
        payload = LeadIn.model_validate(request.get_json(force=True))
    except ValidationError as err:
        return jsonify({"error": "validation_error", "details": err.errors()}), 400
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    try:
        lead = save_lead_with_driver(payload)
    except Exception as exc:
        if is_duplicate_lead_error(exc):
            return duplicate_lead_error_response(exc)
        logger.exception("Failed to store lead: %s", exc)
        return jsonify({"error": "db_error"}), 500

    # Внешнюю интеграцию запускаем в фоне, чтобы не тормозить клиент.
    mail_payload = payload.model_dump(mode="json")
    mail_payload["lead_id"] = lead["id"]
    mailing_queued = payload.marketing_consent and payload.source.value in UNISENDER_ALLOWED_SOURCES
    if mailing_queued:
        executor.submit(send_to_email_service, mail_payload)

    return (
        jsonify(
            {
                "id": lead["id"],
                "status": FunnelStatus.NEW.value,
                "created_at": lead["created_at"],
                "mailing_status": "queued" if mailing_queued else "skipped",
            }
        ),
        201,
    )


@app.post("/admin/leads")
@require_admin
def create_manual_lead():
    # Ручное добавление лида через CRM-интерфейс/интеграции.
    try:
        ensure_db_initialized()
    except Exception as exc:
        logger.exception("Database init failed: %s", exc)
        return db_init_error_response(exc)

    try:
        payload = LeadIn.model_validate(request.get_json(force=True))
    except ValidationError as err:
        return jsonify({"error": "validation_error", "details": err.errors()}), 400
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    try:
        result = save_lead_with_driver(payload)
    except Exception as exc:
        if is_duplicate_lead_error(exc):
            return duplicate_lead_error_response(exc)
        logger.exception("Failed to store manual lead: %s", exc)
        return jsonify({"error": "db_error"}), 500

    return jsonify(result), 201


@app.get("/admin/login")
def admin_login_page():
    if is_admin_authenticated():
        next_url = request.args.get("next", "/admin")
        if not next_url.startswith("/admin"):
            next_url = "/admin"
        return redirect(next_url)
    if not ADMIN_PASSWORD:
        return (
            render_template_string(
                "<h1>Админка отключена</h1><p>Задайте переменную окружения ADMIN_PASSWORD на сервере.</p>"
            ),
            503,
        )
    next_url = request.args.get("next", "/admin")
    if not next_url.startswith("/admin"):
        next_url = "/admin"
    return render_template_string(
        ADMIN_LOGIN_HTML,
        error=request.args.get("error"),
        next_url=next_url,
        loaded_at=int(time.time()),
    )


@app.post("/admin/login")
def admin_login_submit():
    client_ip = request.remote_addr or "unknown"
    if not ADMIN_PASSWORD:
        return redirect(url_for("admin_login", error="Админка отключена"))
    if is_admin_login_rate_limited(client_ip):
        return redirect(
            url_for(
                "admin_login",
                error="Слишком много попыток входа. Подождите 15 минут.",
            )
        )

    bot_trap = (request.form.get("bot_trap") or "").strip()
    if bot_trap:
        record_admin_login_failure(client_ip)
        return redirect(url_for("admin_login", error="Неверный пароль"))

    try:
        loaded_at = int(request.form.get("loaded_at") or "0")
    except ValueError:
        loaded_at = 0
    if time.time() - loaded_at < ADMIN_LOGIN_MIN_FILL_SECONDS:
        record_admin_login_failure(client_ip)
        return redirect(url_for("admin_login", error="Неверный пароль"))

    password = request.form.get("password") or ""
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        record_admin_login_failure(client_ip)
        return redirect(url_for("admin_login", error="Неверный пароль"))

    clear_admin_login_attempts(client_ip)
    session["admin_authenticated"] = True
    session.permanent = True
    next_url = request.form.get("next", "/admin")
    if not next_url.startswith("/admin"):
        next_url = "/admin"
    return redirect(next_url)


@app.get("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.get("/admin")
@require_admin
def admin_page():
    # Простая встроенная страница для ручной операционной работы.
    return render_template_string(ADMIN_PAGE_HTML)


@app.get("/admin/")
@require_admin
def admin_page_slash():
    # Alias для URL со слэшем в конце.
    return render_template_string(ADMIN_PAGE_HTML)


@app.get("/")
def root_page():
    # Удобная точка входа: открытие корня ведет на админ-страницу.
    return redirect("/admin")


@app.get("/materials/<path:filename>")
def serve_material(filename: str):
    # Тестовая раздача материалов из папки проекта для ссылок в рассылке.
    return send_from_directory("materials", filename)


@app.get("/admin/leads")
@require_admin
def list_leads():
    # Полный список лидов для admin UI.
    return search_leads()


@app.get("/admin/leads/search")
@require_admin
def search_leads():
    # Поиск по ID, фамилии/имени контакта или названию компании.
    try:
        ensure_db_initialized()
    except Exception as exc:
        logger.exception("Database init failed: %s", exc)
        return db_init_error_response(exc)

    lead_id_raw = request.args.get("id")
    last_name_query = request.args.get("last_name")
    company_name_query = request.args.get("company_name")

    where_clauses = []
    params = []
    if lead_id_raw:
        try:
            lead_id = int(lead_id_raw)
        except ValueError:
            return jsonify({"error": "validation_error", "details": "id must be integer"}), 400
        where_clauses.append("l.id = %s")
        params.append(lead_id)

    if last_name_query:
        where_clauses.append("l.last_name ILIKE %s")
        params.append(f"%{last_name_query.strip()}%")

    if company_name_query:
        where_clauses.append("l.company_name ILIKE %s")
        params.append(f"%{company_name_query.strip()}%")

    where_sql = f"WHERE {' OR '.join(where_clauses)}" if where_clauses else ""
    query = f"""
        SELECT
            l.id,
            l.company_name,
            l.first_name,
            l.last_name,
            l.position::text AS position,
            l.email::text AS email,
            l.phone,
            l.source::text AS source,
            l.employees_count,
            l.interest_type::text AS interest_type,
            l.marketing_consent,
            l.mailing_sent,
            l.mailing_sent_at,
            l.mailing_provider,
            l.mailing_error,
            l.created_at,
            (
                SELECT h.status::text
                FROM lead_status_history h
                WHERE h.lead_id = l.id
                ORDER BY h.changed_at DESC
                LIMIT 1
            ) AS current_status
        FROM leads l
        {where_sql}
        ORDER BY l.created_at DESC
    """
    try:
        rows = driver.execute_query(query, tuple(params))
    except Exception as exc:
        logger.exception("Failed to search leads: %s", exc)
        return jsonify({"error": "db_error"}), 500
    payload = [row_to_lead_payload(row) for row in rows]

    return jsonify({"count": len(payload), "items": payload})


@app.patch("/admin/leads/<int:lead_id>")
@require_admin
def update_lead(lead_id: int):
    # Частичное редактирование найденного лида.
    try:
        ensure_db_initialized()
    except Exception as exc:
        logger.exception("Database init failed: %s", exc)
        return db_init_error_response(exc)

    try:
        payload = LeadUpdate.model_validate(request.get_json(force=True))
    except ValidationError as err:
        return jsonify({"error": "validation_error", "details": err.errors()}), 400
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    update_data = payload.model_dump(exclude_none=True)
    if not update_data:
        return jsonify({"error": "validation_error", "details": "no fields to update"}), 400

    if "email" in update_data:
        update_data["email"] = str(update_data["email"])
    if "position" in update_data:
        update_data["position"] = update_data["position"].value
    if "source" in update_data:
        update_data["source"] = update_data["source"].value
    if "interest_type" in update_data:
        update_data["interest_type"] = update_data["interest_type"].value

    set_parts = [f'{driver._quote_ident(key)} = %s' for key in update_data]
    params = list(update_data.values()) + [lead_id]
    update_query = f"UPDATE leads SET {', '.join(set_parts)} WHERE id = %s"

    try:
        rows_affected = driver.execute_non_query(update_query, tuple(params))
    except Exception as exc:
        logger.exception("Failed to update lead %s: %s", lead_id, exc)
        return jsonify({"error": "db_error"}), 500

    if rows_affected == 0:
        return jsonify({"error": "not_found"}), 404

    result = get_lead_by_id_with_status(lead_id)
    if result is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(result)


@app.get("/admin/leads/export.xlsx")
@require_admin
def export_leads_to_xlsx():
    # Экспорт всех лидов в Excel для выгрузки и ручной работы маркетинга/продаж.
    try:
        ensure_db_initialized()
        rows = driver.execute_query(
            """
            SELECT
                l.id,
                l.company_name,
                l.first_name,
                l.last_name,
                l.position::text AS position,
                l.email::text AS email,
                l.phone,
                l.source::text AS source,
                l.employees_count,
                l.interest_type::text AS interest_type,
                l.marketing_consent,
                l.mailing_sent,
                l.mailing_sent_at,
                l.mailing_provider,
                l.mailing_error,
                l.created_at,
                (
                    SELECT h.status::text
                    FROM lead_status_history h
                    WHERE h.lead_id = l.id
                    ORDER BY h.changed_at DESC
                    LIMIT 1
                ) AS current_status
            FROM leads l
            ORDER BY l.created_at DESC
            """
        )
        payload = [row_to_lead_payload(row) for row in rows]

        xlsx_stream = build_leads_xlsx(payload)
        filename = f"leads_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"

        return send_file(
            xlsx_stream,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        logger.exception("Failed to export leads: %s", exc)
        return jsonify({"error": "db_error"}), 500


@app.get("/health")
def health():
    driver_ok = True
    driver_error = None
    try:
        check_driver_connection()
    except Exception as exc:
        driver_ok = False
        driver_error = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__

    payload = {
        "status": "ok" if driver_ok else "degraded",
        "db_driver": "ok" if driver_ok else "error",
    }
    if driver_error:
        payload["db_driver_error"] = driver_error
    return jsonify(payload), (200 if driver_ok else 503)


# Локальный запуск dev-сервера.
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)

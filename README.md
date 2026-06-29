# Мини-CRM для маркетинга ВМЕСТЕ

Сбор лидов с лендингов, хранение в PostgreSQL, рассылка через UniSender и ручная работа с контактами через веб-админку.

**Репозиторий:** [github.com/for9653960484/vmeste](https://github.com/for9653960484/vmeste)

---

## Содержание

- [Быстрый старт](#быстрый-старт)
- [Состав проекта](#состав-проекта)
- [Архитектура БД](#архитектура-бд)
- [API и админка](#api-и-админка)
- [Переменные окружения](#переменные-окружения)
- [Локальный запуск](#локальный-запуск)
- [Деплой на сервер](#деплой-на-сервер)
- [Примеры SQL](#примеры-sql)
- [Структура репозитория](#структура-репозитория)

---

## Быстрый старт

```bash
# 1. PostgreSQL
docker run --name vmeste-postgres \
  -e POSTGRES_USER=crm -e POSTGRES_PASSWORD=crm -e POSTGRES_DB=crm_db \
  -p 5432:5432 -d postgres:16

# 2. Зависимости
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Настройки
copy .env.example .env
# отредактируйте .env: ADMIN_PASSWORD, UNISENDER_API_KEY и др.

# 4. Схема БД
psql "postgresql://crm:crm@localhost:5432/crm_db" -f schema.sql

# 5. Запуск
.\.venv\Scripts\python.exe main.py
```

| Сервис | URL |
|--------|-----|
| API | `http://localhost:8001` |
| Админка | `http://localhost:8001/admin` |
| Лендинги | `http://127.0.0.1:8080/` (нужен отдельный запуск + Redis) |

> Порт API задаётся переменной `PORT` (по умолчанию **8001**). Локально можно поставить `8000`, если удобнее.

---

## Состав проекта

| Компонент | Файл / папка | Назначение |
|-----------|--------------|------------|
| Backend API | `main.py` | приём лидов, UniSender, админка |
| Драйвер БД | `postgres_driver.py` | запись в PostgreSQL |
| Схема | `schema.sql` | таблицы, индексы, enum-типы |
| Лендинги | `Landing/` | две страницы + `landing_driver.py` |
| Материалы | `materials/` | PDF для ссылок в письмах |
| CI/CD | `.github/workflows/deploy.yml` | сборка Docker и деплой по push |

**Поток данных:** лендинг → `POST /lead` → PostgreSQL → фоновая отправка в UniSender (если есть согласие на рассылку).

---

## Архитектура БД

| Таблица | Назначение |
|---------|------------|
| `leads` | основные данные лида (компания, контакт, источник, рассылка) |
| `lead_status_history` | история статусов воронки: `new → warmed → webinar → proposal → contract → lost` |
| `interactions` | взаимодействия: `email`, `call`, `meeting`, `webinar`, `note` |

Связи: `lead_status_history.lead_id` и `interactions.lead_id` → `leads.id`.

Применить схему:

```bash
psql "postgresql://crm:crm@localhost:5432/crm_db" -f schema.sql
```

---

## API и админка

### Публичный API

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/lead` | создание лида с лендинга или интеграции |
| `GET` | `/materials/<file>` | раздача PDF для писем UniSender |

В формах пользователю показываются русские подписи, в API передаются коды: `ceo`, `landing_1`, `consultation` и т.д.

<details>
<summary>Пример запроса POST /lead</summary>

```bash
curl -X POST http://localhost:8001/lead \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "ООО ПромТех",
    "first_name": "Иван",
    "last_name": "Петров",
    "position": "ceo",
    "email": "ivan.petrov@promtech.ru",
    "phone": "+79991234567",
    "source": "landing_1",
    "employees_count": 220,
    "interest_type": "consultation",
    "marketing_consent": true
  }'
```

</details>

### Админка `/admin`

Защищена паролем (`ADMIN_PASSWORD` в `.env`). Без пароля доступ закрыт.

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/admin` | веб-интерфейс |
| `GET` | `/admin/login` | форма входа |
| `GET` | `/admin/leads` | список всех лидов |
| `GET` | `/admin/leads/search` | поиск по id, фамилии, компании |
| `POST` | `/admin/leads` | ручное добавление контакта |
| `PATCH` | `/admin/leads/{id}` | частичное обновление |
| `GET` | `/admin/leads/export.xlsx` | выгрузка в Excel |

На странице: таблица лидов, формы добавления и редактирования, экспорт XLSX.

**Защита входа:** honeypot-поле, минимальное время на форме, лимит попыток с IP.

### UniSender

При `marketing_consent=true` и разрешённом источнике (`UNISENDER_ALLOWED_SOURCES`):

1. подписка контакта в список (`subscribe`);
2. отправка письма с материалом (`sendEmail`).

Списки рассылки настраиваются отдельно для каждого источника:

```env
UNISENDER_LIST_IDS_BY_SOURCE=landing_1:9;landing_2:10
```

> Без пробелов между парами `source:id` — иначе парсер не распознает второй источник.

### Антибот на лендинге

- rate limit: 10 запросов/мин на IP (Redis или `memory://`);
- honeypot `bot_trap`;
- минимальное время заполнения формы (`MIN_FORM_FILL_SECONDS`, по умолчанию 3 с);
- автоподстановка `source` по номеру лендинга (`LANDING_1_SOURCE`, `LANDING_2_SOURCE`).

---

## Переменные окружения

Скопируйте `.env.example` → `.env`. Файл `.env` в git не попадает.

| Группа | Ключевые переменные |
|--------|---------------------|
| PostgreSQL | `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` |
| Backend | `PORT` (8001) |
| Админка | `ADMIN_PASSWORD`, `FLASK_SECRET_KEY`, `ADMIN_SESSION_HOURS` |
| Лендинг | `CRM_API_URL`, `LANDING_PORT`, `MIN_FORM_FILL_SECONDS`, `LANDING_1_SOURCE`, `LANDING_2_SOURCE` |
| Rate limit | `RATE_LIMIT_STORAGE_URI` |
| UniSender | `UNISENDER_API_KEY`, `UNISENDER_LIST_IDS`, `UNISENDER_LIST_IDS_BY_SOURCE`, `UNISENDER_ALLOWED_SOURCES`, `UNISENDER_MATERIAL_URL` |

Полный список с комментариями — в [`.env.example`](.env.example).

---

## Локальный запуск

### 1. PostgreSQL

```bash
docker start vmeste-postgres   # если контейнер уже есть
# или первый запуск — см. «Быстрый старт»
```

### 2. Backend

```bash
.\.venv\Scripts\python.exe main.py
```

- API: `http://localhost:8001`
- Админка: `http://localhost:8001/admin` (войти с `ADMIN_PASSWORD`)

### 3. Redis (для лендинга)

```bash
docker run --name vmeste-redis -p 6379:6379 -d redis:7
# или: docker start vmeste-redis
```

### 4. Лендинги

```bash
.\.venv\Scripts\python.exe Landing\landing_driver.py
```

| Страница | URL |
|----------|-----|
| Витрина | `http://127.0.0.1:8080/` |
| Лендинг 1 | `http://127.0.0.1:8080/landing-1` |
| Лендинг 2 | `http://127.0.0.1:8080/landing-2` |

Backend должен быть запущен — лендинг шлёт данные на `CRM_API_URL` (`http://127.0.0.1:8001/lead`).

### 5. Файл материалов

Положите PDF в `materials/checklist.pdf`. Ссылка в письме — из `UNISENDER_MATERIAL_URL`:

`http://127.0.0.1:8001/materials/checklist.pdf`

---

## Деплой на сервер

Автоматический деплой через GitHub Actions (`.github/workflows/deploy.yml`):

- образ API → контейнер `vmeste-api` (порт **8001**);
- образ лендинга → контейнер `vmeste-landing` (порт **8081**);
- переменные окружения на сервере: `/opt/vmeste/.env`.

После деплоя обязательно задайте на сервере:

```env
ADMIN_PASSWORD=...
FLASK_SECRET_KEY=...
UNISENDER_API_KEY=...
UNISENDER_LIST_IDS_BY_SOURCE=landing_1:9;landing_2:10
```

---

## Примеры SQL

<details>
<summary>Новые лиды за 7 дней из landing_b2b</summary>

```sql
SELECT l.id, l.company_name, l.first_name, l.last_name, l.email, l.created_at
FROM leads l
WHERE l.source = 'landing_b2b'
  AND l.created_at >= NOW() - INTERVAL '7 days'
ORDER BY l.created_at DESC;
```

</details>

<details>
<summary>Лиды со статусом proposal, без contract в истории</summary>

```sql
WITH latest_status AS (
    SELECT DISTINCT ON (lead_id) lead_id, status, changed_at
    FROM lead_status_history
    ORDER BY lead_id, changed_at DESC
)
SELECT l.id, l.company_name, l.first_name, l.last_name, ls.status, ls.changed_at
FROM leads l
JOIN latest_status ls ON ls.lead_id = l.id
WHERE ls.status = 'proposal'
  AND NOT EXISTS (
      SELECT 1 FROM lead_status_history h
      WHERE h.lead_id = l.id AND h.status = 'contract'
  )
ORDER BY ls.changed_at DESC;
```

</details>

<details>
<summary>Участники вебинара (через interactions)</summary>

```sql
SELECT DISTINCT l.id, l.company_name, l.first_name, l.last_name, l.email
FROM leads l
JOIN interactions i ON i.lead_id = l.id
WHERE i.interaction_type = 'webinar'
ORDER BY l.id DESC;
```

</details>

<details>
<summary>Количество лидов по источникам за период</summary>

```sql
SELECT l.source, COUNT(*) AS leads_count
FROM leads l
WHERE l.created_at >= TIMESTAMPTZ '2026-03-01 00:00:00+00'
  AND l.created_at <  TIMESTAMPTZ '2026-04-01 00:00:00+00'
GROUP BY l.source
ORDER BY leads_count DESC, l.source;
```

</details>

---

## Структура репозитория

```
VMESTE/
├── main.py                 # Flask API + admin UI
├── postgres_driver.py      # драйвер PostgreSQL
├── schema.sql              # DDL схемы CRM
├── requirements.txt
├── Dockerfile              # образ API
├── .env.example            # шаблон переменных окружения
├── .github/workflows/      # CI/CD
├── Landing/                # лендинги и landing_driver.py
└── materials/              # PDF для рассылки (добавляются локально)
```

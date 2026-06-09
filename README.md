# Мини-CRM для маркетинга ВМЕСТЕ

## 1) Архитектура (кратко)

- `leads`: основное хранилище входящих B2B-лидов с лендинга/вебинаров/партнеров/соцсетей.
- `lead_status_history`: неизменяемая история статусов воронки (`new -> warmed -> webinar -> proposal -> contract -> lost`).
- `interactions`: лента взаимодействий (`email`, `call`, `meeting`, `webinar`, `note`) по каждому лиду.
- Основные связи:
  - `lead_status_history.lead_id -> leads.id`
  - `interactions.lead_id -> leads.id`
- Схема легко расширяется для:
  - привязки вебинаров (сейчас можно хранить `webinar_id` в `interactions.metadata`, позже вынести в отдельную таблицу),
  - пилотных проектов (новая таблица с FK `lead_id`),
  - быстрых выборок по источникам/статусам/датам через индексы и историю статусов.

## 2) Полный DDL

Используйте готовый SQL-файл:

- `schema.sql`

Применение:

```bash
psql "postgresql://crm:crm@localhost:5432/crm_db" -f schema.sql
```

## 3) Backend (Flask)

Реализовано в:

- `main.py`

Включает:

- валидацию входящего лида через Pydantic (`LeadIn`),
- эндпоинт `POST /lead`,
- запись в БД через `postgres_driver.py` (с сохранением ORM-моделей для структуры и инициализации),
- автоматическую запись стартового статуса в `lead_status_history`,
- асинхронную интеграцию с UniSender (`subscribe` + `sendEmail`) для лидов с `marketing_consent=true` и источником лендинга.

### 3.1 Блок работы с базой (расширяемый)

Добавлены отдельные `admin`-эндпоинты для ручной операционной работы:

- `GET /admin/leads/export.xlsx` — выгрузить все контакты в Excel (`.xlsx`) со всеми полями лида + текущий статус;
- `POST /admin/leads` — добавить новый контакт вручную;
- `GET /admin/leads/search?id=...&last_name=...&company_name=...` — найти лида по `id`, фамилии контакта или названию компании;
- `PATCH /admin/leads/{id}` — внести правки в существующего лида (частичное обновление полей).

Почему это расширяемо:

- ручные операции вынесены в отдельный namespace `/admin`;
- поиск строится через динамические фильтры (легко добавлять новые критерии);
- обновление реализовано через `LeadUpdate` (можно безопасно расширять список изменяемых полей).

### 3.2 Простая веб-страница для ручной работы

Доступно по адресу:

- `GET /admin`

На странице есть:

- форма ручного добавления контакта;
- поиск лидов по `id`, фамилии контакта, названию компании;
- форма внесения правок в найденного лида;
- кнопка выгрузки всех контактов в `XLSX`.

Дополнительно в лиде хранится статус рассылки:

- `mailing_sent`, `mailing_sent_at`, `mailing_provider`, `mailing_error`.

## 4) Примеры SQL-запросов

### 4.1 Новые лиды за последние 7 дней из `landing_b2b`

```sql
SELECT
    l.id,
    l.company_name,
    l.first_name,
    l.last_name,
    l.email,
    l.created_at
FROM leads l
WHERE l.source = 'landing_b2b'
  AND l.created_at >= NOW() - INTERVAL '7 days'
ORDER BY l.created_at DESC;
```

### 4.2 Лиды с текущим статусом `proposal` и без `contract` в истории

```sql
WITH latest_status AS (
    SELECT DISTINCT ON (lead_id)
        lead_id,
        status,
        changed_at
    FROM lead_status_history
    ORDER BY lead_id, changed_at DESC
)
SELECT
    l.id,
    l.company_name,
    l.first_name,
    l.last_name,
    ls.status,
    ls.changed_at
FROM leads l
JOIN latest_status ls ON ls.lead_id = l.id
WHERE ls.status = 'proposal'
  AND NOT EXISTS (
      SELECT 1
      FROM lead_status_history h
      WHERE h.lead_id = l.id
        AND h.status = 'contract'
  )
ORDER BY ls.changed_at DESC;
```

### 4.3 Лиды, участвовавшие в вебинаре (через `interactions`)

```sql
SELECT DISTINCT
    l.id,
    l.company_name,
    l.first_name,
    l.last_name,
    l.email
FROM leads l
JOIN interactions i ON i.lead_id = l.id
WHERE i.interaction_type = 'webinar'
ORDER BY l.id DESC;
```

### 4.4 Агрегат: количество лидов по источникам за выбранный период

```sql
SELECT
    l.source,
    COUNT(*) AS leads_count
FROM leads l
WHERE l.created_at >= TIMESTAMPTZ '2026-03-01 00:00:00+00'
  AND l.created_at <  TIMESTAMPTZ '2026-04-01 00:00:00+00'
GROUP BY l.source
ORDER BY leads_count DESC, l.source;
```

## 5) Инструкции по запуску

### 5.1 Запуск PostgreSQL локально через Docker

```bash
# Если контейнер уже создан ранее:
docker start vmeste-postgres

# Если контейнера еще нет (первый запуск):
docker run --name vmeste-postgres -e POSTGRES_USER=crm -e POSTGRES_PASSWORD=crm -e POSTGRES_DB=crm_db -p 5432:5432 -d postgres:16
```

### 5.2 Установка Python-зависимостей

```bash
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 5.3 Настройка подключения и переменных окружения

Скопируйте шаблон и заполните своими значениями:

```bash
copy .env.example .env
```

Файл `.env` не попадает в git (см. `.gitignore`). Полный список переменных — в `.env.example`.

### 5.4 Применение схемы БД

```bash
psql "postgresql://crm:crm@localhost:5432/crm_db" -f schema.sql
```

### 5.5 Запуск backend (API + admin-страница)

```bash
.\.venv\Scripts\python.exe main.py
```

Backend запускается на:

- `http://localhost:8000`
- admin UI: `http://localhost:8000/admin`

### 5.6 Запуск тестового лендинга

Перед запуском лендинга поднимите Redis для rate-limit:

```bash
docker run --name vmeste-redis -p 6379:6379 -d redis:7
```

Если контейнер уже создан:

```bash
docker start vmeste-redis
```

```bash
.\.venv\Scripts\python.exe Landing\landing_driver.py
```

Тестовый лендинг доступен на:

- `http://127.0.0.1:8080/landing-1` (лендинг 1)
- `http://127.0.0.1:8080/landing-2` (лендинг 2)
- `http://127.0.0.1:8080/` (витрина лендингов со ссылками на текущие/будущие страницы)

Требование: backend должен быть запущен (`http://127.0.0.1:8000`), так как лендинг отправляет лиды в `POST /lead`.

В лендинге включена базовая антибот-защита:

- ограничение частоты отправки (`10` запросов в минуту на IP);
- honeypot-поле (`website`) для отсечения автозаполнения ботами;
- минимальное время заполнения формы (по умолчанию `3` секунды, настраивается через `MIN_FORM_FILL_SECONDS`).
- хранение rate-limit счетчиков в Redis (`RATE_LIMIT_STORAGE_URI`), с fallback на `memory://`, если Redis недоступен.
- авто-проставление источника в БД по лендингу (`landing_id -> source`) через `LANDING_1_SOURCE`/`LANDING_2_SOURCE`.

### 5.7 Размещение файла материалов

Письмо UniSender отправляет ссылку из `UNISENDER_MATERIAL_URL`.  
Для локального теста положите файл в папку `materials`, например:

- `materials/checklist.pdf`

Файл будет доступен через backend:

- `http://127.0.0.1:8000/materials/checklist.pdf`

### 5.8 Пример запроса к `POST /lead`

Важно: в формах пользователю показываются русские названия вариантов, но в API передаются технические коды (например, `ceo`, `landing_b2b`, `consultation`).

```bash
curl -X POST http://localhost:8000/lead \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "ООО ПромТех",
    "first_name": "Иван",
    "last_name": "Петров",
    "position": "ceo",
    "email": "ivan.petrov@promtech.ru",
    "phone": "+79991234567",
    "source": "landing-1",
    "employees_count": 220,
    "interest_type": "consultation",
    "marketing_consent": true
  }'
```

## 6) Структура репозитория

```
VMESTE/
├── main.py              # Flask API + admin UI
├── postgres_driver.py   # низкоуровневый драйвер PostgreSQL
├── schema.sql           # DDL схемы CRM
├── requirements.txt
├── .env.example         # шаблон переменных окружения
├── Landing/             # тестовые лендинги и landing_driver.py
└── materials/           # PDF для рассылки (файлы добавляются локально)
```

## 7) Публикация на GitHub

1. Создайте пустой репозиторий на GitHub (без README, `.gitignore` и лицензии — они уже в проекте).
2. В корне проекта выполните:

```bash
git init
git add .
git status
git commit -m "Initial commit: VMESTE mini-CRM"
git branch -M main
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

Перед первым коммитом убедитесь, что в индекс не попали `.env`, `.venv/` и PDF из `materials/`. Команда `git status` должна показывать только исходный код и документацию.

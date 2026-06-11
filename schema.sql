-- Схема PostgreSQL для mini-CRM (VMESTE)
-- Фокус: прием лидов, история статусов воронки, таймлайн взаимодействий.

-- Опционально: регистронезависимый тип текста для уникальности email.
CREATE EXTENSION IF NOT EXISTS citext;

-- Переиспользуемые enum-типы сохраняют строгий набор значений и стабильную аналитику.
CREATE TYPE lead_position AS ENUM (
    'owner',
    'ceo',
    'hrd',
    'director_of_development',
    'head_of_corporate_university',
    'other'
);

CREATE TYPE lead_source AS ENUM (
    'landing_1',
    'landing_2',
    'landing_b2b',
    'webinar',
    'partner_mail',
    'social_post',
    'other'
);

CREATE TYPE lead_interest_type AS ENUM (
    'guide',
    'checklist',
    'calculator',
    'consultation',
    'pilot_launch'
);

CREATE TYPE funnel_status AS ENUM (
    'new',
    'warmed',
    'webinar',
    'proposal',
    'contract',
    'lost'
);

CREATE TYPE interaction_type AS ENUM (
    'email',
    'call',
    'meeting',
    'webinar',
    'note'
);

-- Основная сущность с контактными данными и информацией о компании.
CREATE TABLE leads (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    position lead_position NOT NULL DEFAULT 'other',
    email CITEXT NOT NULL,
    phone TEXT,
    source lead_source NOT NULL,
    employees_count INTEGER CHECK (employees_count IS NULL OR employees_count >= 0),
    interest_type lead_interest_type NOT NULL,
    marketing_consent BOOLEAN NOT NULL DEFAULT FALSE,
    mailing_sent BOOLEAN NOT NULL DEFAULT FALSE,
    mailing_sent_at TIMESTAMPTZ,
    mailing_provider TEXT,
    mailing_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Повторные регистрации с той же компанией и email допускаются.
CREATE INDEX idx_leads_company_email ON leads (company_name, email);
CREATE INDEX idx_leads_source_created_at ON leads (source, created_at DESC);
CREATE INDEX idx_leads_created_at ON leads (created_at DESC);

COMMENT ON TABLE leads IS 'Основной реестр лидов из лендинга и других маркетинговых каналов.';
COMMENT ON COLUMN leads.id IS 'Суррогатный ключ для связей между таблицами и API-ответов.';
COMMENT ON COLUMN leads.company_name IS 'Юридическое/торговое название компании для B2B-контекста.';
COMMENT ON COLUMN leads.first_name IS 'Имя контактного лица, заполнившего форму лида.';
COMMENT ON COLUMN leads.last_name IS 'Фамилия контактного лица, заполнившего форму лида.';
COMMENT ON COLUMN leads.position IS 'Роль контакта в процессе принятия решения о покупке.';
COMMENT ON COLUMN leads.email IS 'Основной email, регистронезависимый для дедупликации.';
COMMENT ON COLUMN leads.phone IS 'Телефон контакта в свободном формате; необязателен при первичном захвате.';
COMMENT ON COLUMN leads.source IS 'Источник привлечения для атрибуции и анализа ROI.';
COMMENT ON COLUMN leads.employees_count IS 'Оценка размера компании (абсолютное число сотрудников).';
COMMENT ON COLUMN leads.interest_type IS 'Тип запрошенного лид-магнита или намерения.';
COMMENT ON COLUMN leads.marketing_consent IS 'Согласие на получение рассылки материалов.';
COMMENT ON COLUMN leads.mailing_sent IS 'Факт успешной отправки материалов по контакту.';
COMMENT ON COLUMN leads.mailing_sent_at IS 'Время успешной отправки материалов.';
COMMENT ON COLUMN leads.mailing_provider IS 'Провайдер почтовой рассылки (например, unisender).';
COMMENT ON COLUMN leads.mailing_error IS 'Последняя ошибка отправки рассылки, если была.';
COMMENT ON COLUMN leads.created_at IS 'Время создания лида в UTC.';

-- Полный журнал изменений статусов воронки.
CREATE TABLE lead_status_history (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status funnel_status NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment TEXT
);

CREATE INDEX idx_status_history_lead_changed ON lead_status_history (lead_id, changed_at DESC);
CREATE INDEX idx_status_history_status_changed ON lead_status_history (status, changed_at DESC);

COMMENT ON TABLE lead_status_history IS 'Журнал переходов статусов воронки в формате append-only.';
COMMENT ON COLUMN lead_status_history.lead_id IS 'Лид, к которому относится событие изменения статуса.';
COMMENT ON COLUMN lead_status_history.status IS 'Этап воронки в момент фиксации события.';
COMMENT ON COLUMN lead_status_history.changed_at IS 'Время изменения статуса (UTC).';
COMMENT ON COLUMN lead_status_history.comment IS 'Необязательная причина или детали изменения.';

-- Таймлайн активностей: звонки, письма, участие в вебинаре, ручные заметки.
CREATE TABLE interactions (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    interaction_type interaction_type NOT NULL,
    short_description TEXT NOT NULL,
    interaction_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_interactions_lead_date ON interactions (lead_id, interaction_date DESC);
CREATE INDEX idx_interactions_type_date ON interactions (interaction_type, interaction_date DESC);
CREATE INDEX idx_interactions_metadata_gin ON interactions USING GIN (metadata);

COMMENT ON TABLE interactions IS 'Таймлайн взаимодействий с лидом (email, звонки, вебинары, заметки).';
COMMENT ON COLUMN interactions.lead_id IS 'Лид, к которому относится событие взаимодействия.';
COMMENT ON COLUMN interactions.interaction_type IS 'Тип взаимодействия для таймлайна и аналитики.';
COMMENT ON COLUMN interactions.short_description IS 'Короткое человекочитаемое описание события.';
COMMENT ON COLUMN interactions.interaction_date IS 'Дата и время, когда произошло взаимодействие.';
COMMENT ON COLUMN interactions.metadata IS 'Расширяемые атрибуты: webinar_id, менеджер, теги и т.д.';

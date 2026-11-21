-- Transient query schema for app-state.json. Keep columns and constraints aligned with schema.sql.
PRAGMA foreign_keys = ON;

CREATE TABLE channel_connection (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'DISCONNECTED',
    target TEXT NOT NULL DEFAULT 'REAL',
    biz_id TEXT,
    biz_name TEXT,
    public_id TEXT,
    profiles_json TEXT,
    connected_at TEXT,
    verified_at TEXT,
    control_action TEXT,
    control_token TEXT,
    control_status TEXT,
    control_message TEXT,
    control_requested_at TEXT,
    control_updated_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE ai_connection (
    provider TEXT PRIMARY KEY NOT NULL,
    mode TEXT NOT NULL DEFAULT 'NONE',
    status TEXT NOT NULL DEFAULT 'UNKNOWN',
    detail TEXT,
    checked_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE prompt_template (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    body TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 0,
    active_kind TEXT GENERATED ALWAYS AS (CASE WHEN is_active = 1 THEN kind END) STORED UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (kind, version_no)
);

CREATE TABLE import_batch (
    id TEXT PRIMARY KEY NOT NULL,
    file_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_batch_created ON import_batch (created_at);

CREATE TABLE product (
    id TEXT PRIMARY KEY NOT NULL,
    product_code TEXT COLLATE NOCASE NOT NULL UNIQUE,
    batch_id TEXT NOT NULL REFERENCES import_batch (id),
    row_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_krw INTEGER,
    info TEXT,
    post_title TEXT,
    script TEXT,
    image_names TEXT,
    data_issues_json TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    reg_status TEXT NOT NULL DEFAULT 'NONE',
    last_job_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_product_batch ON product (batch_id, row_no);

CREATE TABLE product_image (
    id TEXT PRIMARY KEY NOT NULL,
    product_id TEXT NOT NULL REFERENCES product (id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    original_name TEXT,
    sha256 TEXT NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (product_id, source_type, sort_order)
);

CREATE TABLE product_registration (
    product_id TEXT NOT NULL REFERENCES product (id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    biz_id TEXT NOT NULL,
    status TEXT NOT NULL,
    job_id TEXT NOT NULL,
    fingerprint TEXT,
    registered_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (product_id, target, biz_id)
);

CREATE TABLE task_job (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL,
    product_id TEXT NOT NULL REFERENCES product (id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    current_step TEXT,
    steps_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    run_token TEXT,
    heartbeat_at TEXT,
    error_code TEXT,
    error_message TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    active_product_guard TEXT GENERATED ALWAYS AS (
        CASE WHEN status IN ('QUEUED','RUNNING','NEEDS_CHECK') THEN product_id END
    ) STORED UNIQUE
);
CREATE INDEX ix_job_queue ON task_job (status, created_at);
CREATE INDEX ix_job_run ON task_job (run_id, created_at);

CREATE TABLE job_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES task_job (id) ON DELETE CASCADE,
    step TEXT,
    level TEXT NOT NULL DEFAULT 'INFO',
    outcome TEXT,
    message TEXT NOT NULL,
    shot_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_event_job ON job_event (job_id, id);
CREATE INDEX ix_event_outcome ON job_event (outcome, id);

-- 퓨처마케터 당근 자동 등록 — MySQL 8.4 스키마
-- 9개 테이블: 연결 2, 프롬프트 1, 불러오기 1, 상품 3, 작업 2
-- 표 구조를 바꾸면 channel_connection 의 COMMENT(schema:N)와 db_init.SCHEMA_VERSION 을 함께 올린다.
-- 시각은 모두 UTC(DATETIME(6)), 문자는 utf8mb4.

CREATE TABLE channel_connection (
  id                   TINYINT UNSIGNED NOT NULL DEFAULT 1,
  status               ENUM('DISCONNECTED','CONNECTING','CONNECTED','EXPIRED') NOT NULL DEFAULT 'DISCONNECTED',
  target               ENUM('REAL','PRACTICE') NOT NULL DEFAULT 'REAL',
  biz_id               VARCHAR(40)  NULL COMMENT '로그인 뒤 고른 비즈프로필 번호',
  biz_name             VARCHAR(100) NULL,
  public_id            VARCHAR(40)  NULL COMMENT '손님이 보는 당근 업체 프로필 번호(동네 업체 번호)',
  connected_at         DATETIME(6)  NULL,
  verified_at          DATETIME(6)  NULL,
  -- 화면이 남기는 브라우저 제어 요청. 브라우저는 워커만 연다.
  control_action       ENUM('CONNECT','DISCONNECT') NULL,
  control_token        CHAR(36)     NULL,
  control_status       ENUM('REQUESTED','RUNNING','DONE','FAILED') NULL,
  control_message      VARCHAR(500) NULL,
  control_requested_at DATETIME(6)  NULL,
  control_updated_at   DATETIME(6)  NULL,
  updated_at           DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT ck_connection_single CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='schema:5';

-- AI 연결 방식만 저장한다. API 키는 DB 에 넣지 않는다(프로젝트 밖 비밀 파일).
CREATE TABLE ai_connection (
  provider   ENUM('CLAUDE','GPT') NOT NULL,
  mode       ENUM('NONE','API_KEY','SUBSCRIPTION') NOT NULL DEFAULT 'NONE',
  status     ENUM('UNKNOWN','OK','ERROR') NOT NULL DEFAULT 'UNKNOWN',
  detail     VARCHAR(300) NULL,
  checked_at DATETIME(6)  NULL,
  updated_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE prompt_template (
  id          CHAR(36)     NOT NULL,
  kind        ENUM('TEXT','IMAGE') NOT NULL,
  version_no  INT UNSIGNED NOT NULL,
  body        TEXT         NOT NULL,
  is_default  TINYINT(1)   NOT NULL DEFAULT 0,
  is_active   TINYINT(1)   NOT NULL DEFAULT 0,
  -- 종류별 활성 지침은 하나만: 활성일 때만 값이 생기는 생성열에 UNIQUE
  active_kind VARCHAR(10) AS (CASE WHEN is_active = 1 THEN kind END) STORED,
  created_at  DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_prompt_version (kind, version_no),
  UNIQUE KEY uq_prompt_active (active_kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE import_batch (
  id            CHAR(36)     NOT NULL,
  file_name     VARCHAR(255) NOT NULL,
  file_sha256   CHAR(64)     NOT NULL,
  source        ENUM('DEFAULT','UPLOAD') NOT NULL,
  row_count     INT UNSIGNED NOT NULL DEFAULT 0,
  blocked_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '파일 내용 문제로 등록할 수 없는 행 수',
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_batch_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE product (
  id                 CHAR(36)     NOT NULL,
  product_code       VARCHAR(40)  NOT NULL,
  batch_id           CHAR(36)     NOT NULL COMMENT '마지막으로 이 상품을 담고 있던 불러오기',
  row_no             INT UNSIGNED NOT NULL,
  name               VARCHAR(100) NOT NULL,
  price_krw          INT UNSIGNED NULL,
  info               TEXT         NULL COMMENT '상품 정보(AI 가 글을 쓸 때의 근거)',
  post_title         VARCHAR(100) NULL COMMENT '소식 제목(비우면 상품명 또는 AI 제목)',
  script             MEDIUMTEXT   NULL COMMENT '대본(있으면 그대로 소식 본문)',
  image_names        VARCHAR(1000) NULL COMMENT '엑셀에 적힌 이미지 파일 이름',
  data_issues_json   JSON         NOT NULL COMMENT '파일 내용 문제(있으면 등록 불가)',
  source_fingerprint CHAR(64)     NOT NULL,
  reg_status         ENUM('NONE','QUEUED','RUNNING','DONE','FAILED','NEEDS_CHECK') NOT NULL DEFAULT 'NONE'
                     COMMENT '가장 최근 작업의 상태. 어디에 올렸는지는 product_registration 이 기억한다',
  last_job_id        CHAR(36)     NULL,
  created_at         DATETIME(6)  NOT NULL,
  updated_at         DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_product_code (product_code),
  KEY ix_product_batch (batch_id, row_no),
  CONSTRAINT fk_product_batch FOREIGN KEY (batch_id) REFERENCES import_batch (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE product_image (
  id            CHAR(36)     NOT NULL,
  product_id    CHAR(36)     NOT NULL,
  source_type   ENUM('LOCAL','GENERATED') NOT NULL COMMENT 'LOCAL=내 사진, GENERATED=AI 가 만든 그림',
  storage_key   VARCHAR(300) NOT NULL COMMENT '앱 데이터 폴더 아래 상대 경로',
  original_name VARCHAR(255) NULL,
  sha256        CHAR(64)     NOT NULL,
  mime          VARCHAR(40)  NOT NULL,
  width         INT UNSIGNED NOT NULL,
  height        INT UNSIGNED NOT NULL,
  sort_order    INT UNSIGNED NOT NULL,
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_image_order (product_id, source_type, sort_order),
  CONSTRAINT fk_image_product FOREIGN KEY (product_id) REFERENCES product (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 어디에(대상·비즈프로필) 올렸는지의 기록. 연습용에 올린 것과 실제 당근에 올린 것을 따로 기억한다.
CREATE TABLE product_registration (
  product_id    CHAR(36)     NOT NULL,
  target        ENUM('REAL','PRACTICE') NOT NULL,
  biz_id        VARCHAR(40)  NOT NULL,
  status        ENUM('DONE','FAILED') NOT NULL,
  job_id        CHAR(36)     NOT NULL COMMENT '이 결과를 낸 작업(실패면 이어서 할 작업)',
  fingerprint   CHAR(64)     NULL COMMENT '그때의 상품 내용 지문(이후 엑셀이 바뀌었는지 가린다)',
  registered_at DATETIME(6)  NULL,
  updated_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (product_id, target, biz_id),
  CONSTRAINT fk_registration_product FOREIGN KEY (product_id) REFERENCES product (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE task_job (
  id              CHAR(36)     NOT NULL,
  run_id          CHAR(36)     NOT NULL COMMENT '한 번의 「선택한 상품 등록」으로 묶인 작업들',
  product_id      CHAR(36)     NOT NULL,
  status          ENUM('QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_CHECK','CANCELED') NOT NULL DEFAULT 'QUEUED',
  current_step    ENUM('SCRIPT','IMAGE','PRICE','NEWS') NULL,
  steps_json      JSON         NOT NULL COMMENT '단계별 상태와 결과. 끝난 단계는 다시 하지 않는다',
  request_json    JSON         NOT NULL COMMENT '접수 시점의 상품 값·지침 사본',
  result_json     JSON         NULL,
  attempt_count   INT UNSIGNED NOT NULL DEFAULT 0,
  run_token       CHAR(36)     NULL COMMENT '점유한 워커만 결과를 쓸 수 있게 하는 표식',
  heartbeat_at    DATETIME(6)  NULL,
  error_code      VARCHAR(40)  NULL,
  error_message   VARCHAR(500) NULL,
  idempotency_key VARCHAR(120) NOT NULL COMMENT '같은 클릭이 두 번 접수되지 않게',
  created_at      DATETIME(6)  NOT NULL,
  started_at      DATETIME(6)  NULL,
  finished_at     DATETIME(6)  NULL,
  -- 상품당 진행 중 작업은 하나만: 진행 상태일 때만 값이 생기는 생성열에 UNIQUE
  active_product_guard CHAR(36) AS (CASE WHEN status IN ('QUEUED','RUNNING','NEEDS_CHECK') THEN product_id END) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uq_job_idempotency (idempotency_key),
  UNIQUE KEY uq_job_active (active_product_guard),
  KEY ix_job_queue (status, created_at),
  KEY ix_job_run (run_id, created_at),
  CONSTRAINT fk_job_product FOREIGN KEY (product_id) REFERENCES product (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 화면의 진행 기록. 워커가 한 일을 한 줄씩 남기고, 화면은 읽기만 한다.
CREATE TABLE job_event (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id     CHAR(36)     NOT NULL,
  step       ENUM('SCRIPT','IMAGE','PRICE','NEWS') NULL,
  level      ENUM('INFO','DONE','WARN','ERROR') NOT NULL DEFAULT 'INFO',
  outcome    ENUM('SUCCEEDED','FAILED','NEEDS_CHECK') NULL COMMENT '등록 시도 한 번이 끝난 줄이면 그 결과(대시보드 작업 기록)',
  message    VARCHAR(500) NOT NULL,
  shot_key   VARCHAR(300) NULL COMMENT '그 순간의 브라우저 화면 캡처',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_event_job (job_id, id),
  KEY ix_event_outcome (outcome, id),
  CONSTRAINT fk_event_job FOREIGN KEY (job_id) REFERENCES task_job (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

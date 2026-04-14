-- RAG 데이터베이스
DROP TABLE IF EXISTS embeddings_server_logs CASCADE;

CREATE TABLE embeddings_server_logs (
    -- 기본 식별 정보 (svr_system_main 연동)
    uid TEXT NOT NULL,
    hostid BIGINT NOT NULL,
    hostname VARCHAR(50) NOT NULL,
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(20),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    -- ✅ [추가] 장비 유형: 서버(svr_system_main) | 네트워크 장비(nw_main1) | 미분류
    device_type VARCHAR(50) DEFAULT '미분류',

    -- 로그 상세 정보 (svr_log_messages 연동)
    itemid BIGINT,
    log_files VARCHAR(100),
    log_level VARCHAR(50),   -- FAIL, WARNING, ERROR 등
    log_source VARCHAR(50),  -- 파싱된 프로세스 명 (예: containerd)
    logs TEXT,               -- 원본 로그
    log_template TEXT,       -- 가변 데이터 마스킹된 뼈대

    -- 그룹화(Deduplication) 정보
    occurrence_count INTEGER DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,

    -- 로그 컨텍스트(Context) 정보
    context_before TEXT,     -- 전 1줄
    context_after TEXT,      -- 후 1줄

    -- RAG용 요약 및 벡터
    log_summary TEXT,
    embedding vector(768),

    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (uid, first_seen_at)
);

CREATE INDEX idx_server_logs_lookup     ON embeddings_server_logs (hostid, log_template, last_seen_at);
-- ✅ [추가] 장비 유형 필터 검색 최적화
CREATE INDEX idx_server_logs_device_type ON embeddings_server_logs (device_type);

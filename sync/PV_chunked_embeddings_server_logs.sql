DROP TABLE IF EXISTS embeddings_server_logs_chunked CASCADE;

CREATE TABLE IF NOT EXISTS embeddings_server_logs_chunked (
    chunk_id   SERIAL PRIMARY KEY,
    hostid     BIGINT NOT NULL,
    time_bucket TIMESTAMPTZ NOT NULL,
    -- ✅ [추가] 장비 유형: 서버 | 네트워크 장비 | 미분류  (embeddings_server_logs.device_type 최빈값 기준)
    device_type VARCHAR(50) DEFAULT '미분류',
    perf_stats    JSONB,
    process_stats JSONB,
    event_stats   JSONB,
    log_stats     JSONB,
    integrated_report TEXT,
    embedding vector(768),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (hostid, time_bucket)
);

CREATE INDEX idx_server_chunk_lookup      ON embeddings_server_logs_chunked (hostid, time_bucket);
-- ✅ [추가] 장비 유형 필터 검색 최적화
CREATE INDEX idx_server_chunk_device_type ON embeddings_server_logs_chunked (device_type);

-- ============================================================
-- Phase B2: AIX ERRPT 로그 통합 청킹 테이블 (보정 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_errpt_chunked CASCADE;

CREATE TABLE embeddings_server_errpt_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset VARCHAR(50),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(100),
    group_name VARCHAR(255),
    
    -- 활성 여부 (Master: embeddings_hosts와 동기화)
    is_active SMALLINT DEFAULT 1, 
    
    -- ========== 시간 정보 ==========
    time_bucket TIMESTAMPTZ NOT NULL,
    date_partition DATE NOT NULL,
    hour_partition SMALLINT NOT NULL,
    
    -- ========== 집계 메트릭 및 샘플 ==========
    errpt_metrics JSONB,   -- {"total": 5, "critical": 2, "warning": 3, "by_resource": {...}}
    errpt_samples JSONB,   -- 대표 로그 샘플 리스트
    
    -- RAG 데이터
    errpt_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- 메타데이터
    record_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uk_errpt_chunked_host_time UNIQUE(hostid, time_bucket)
);

-- 인덱스 설정
CREATE INDEX idx_errpt_chunked_active ON embeddings_server_errpt_chunked(is_active);
CREATE INDEX idx_errpt_chunked_host_time ON embeddings_server_errpt_chunked(hostid, time_bucket DESC);
CREATE INDEX idx_errpt_chunked_embedding ON embeddings_server_errpt_chunked USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

COMMENT ON TABLE embeddings_server_errpt_chunked IS 'AIX ERRPT 로그 1시간 단위 통합 청킹 테이블';
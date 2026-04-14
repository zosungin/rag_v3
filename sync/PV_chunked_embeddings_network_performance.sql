-- ============================================================
-- Phase B2: 네트워크 Performance 통합 청킹 테이블 (최종 보정)
-- ============================================================

DROP TABLE IF EXISTS embeddings_network_performance_chunked CASCADE;

CREATE TABLE embeddings_network_performance_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 네트워크 장비 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    asset_name VARCHAR(255),
    device_name VARCHAR(255),
    ip VARCHAR(50),
    group_name VARCHAR(255),
    
    -- 활성 여부 (Master: embeddings_network_devices와 동기화)
    is_active SMALLINT DEFAULT 1, 
    
    -- ========== 시간 정보 ==========
    time_bucket TIMESTAMPTZ NOT NULL,
    date_partition DATE NOT NULL,
    hour_partition SMALLINT NOT NULL,
    
    -- ========== 통합 데이터 (JSONB) ==========
    device_info JSONB,       -- 장비 가동시간, 에이전트 상태 등
    interface_summary JSONB, -- 전체 포트 수, UP/DOWN 수, 총 트래픽 합계
    
    -- RAG 데이터
    network_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- 메타데이터
    record_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uk_network_perf_chunked_host_time UNIQUE(hostid, time_bucket)
);

-- 인덱스 설정
CREATE INDEX idx_nw_perf_chunked_active ON embeddings_network_performance_chunked(is_active);
CREATE INDEX idx_nw_perf_chunked_host_time ON embeddings_network_performance_chunked(hostid, time_bucket DESC);
CREATE INDEX idx_nw_perf_chunked_embedding ON embeddings_network_performance_chunked USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

COMMENT ON TABLE embeddings_network_performance_chunked IS '네트워크 장비 및 인터페이스 통합 시간 단위 청킹 테이블';
-- ============================================================
-- Phase B2: 서버 Performance 통합 청킹 테이블 (최종 표준화)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_performance_chunked CASCADE;

CREATE TABLE embeddings_server_performance_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 활성 여부 (Master: embeddings_hosts와 동기화됨)
    is_active SMALLINT DEFAULT 1, 
    
    -- ========== 시간 정보 ==========
    time_bucket TIMESTAMPTZ NOT NULL,  -- 1시간 단위 시작 시각 (DATE_TRUNC('hour', ...))
    
    -- ========== 통합 메트릭 데이터 (JSONB) ==========
    cpu_metrics JSONB,    -- CPU 사용률, 코어수, 변동성 등
    mem_metrics JSONB,    -- 전체/사용/가용 메모리, % 등
    swap_metrics JSONB,   -- 스왑 사용량 및 % 등
    fs_metrics JSONB,     -- 파일시스템별 사용률 리스트
    net_metrics JSONB,    -- 인터페이스별 트래픽/상태 요약
    top_processes JSONB,  -- 리소스 과점 프로세스 리스트
    
    -- RAG 데이터
    performance_summary TEXT NOT NULL,
    embedding vector(768),
    
    record_count INTEGER DEFAULT 0, -- 통합된 원본 레코드 수
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uk_server_perf_chunked_host_time UNIQUE(hostid, time_bucket)
);

-- 인덱스: 상태 필터링 및 시간순 조회 최적화
CREATE INDEX idx_perf_chunked_active ON embeddings_server_performance_chunked(is_active);
CREATE INDEX idx_perf_chunked_host_time ON embeddings_server_performance_chunked(hostid, time_bucket DESC);
CREATE INDEX idx_perf_chunked_embedding ON embeddings_server_performance_chunked USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

COMMENT ON TABLE embeddings_server_performance_chunked IS '서버 6대 리소스 통합 시간 단위 집계 테이블';
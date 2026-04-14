-- ============================================================
-- Phase B2: 서버 프로세스 트렌드(Top-N) 청킹 테이블 (개선 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_process_topn_chunked CASCADE;

CREATE TABLE embeddings_server_process_topn_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset_name VARCHAR(255),
    ip VARCHAR(50),
    
    -- ========== 시간 및 메트릭 데이터 ==========
    time_bucket TIMESTAMPTZ NOT NULL,  -- 1시간 단위 (예: 2026-03-08 18:00:00)
    top_cpu_processes JSONB,         -- 합산된 상세 데이터 (분석용)
    top_mem_processes JSONB,         -- 합산된 상세 데이터 (분석용)
    
    -- ========== RAG 지식 데이터 ==========
    process_summary TEXT NOT NULL,   -- AI가 읽을 압축 요약문
    summary_hash VARCHAR(64),        -- 💡 변경 감지용 SHA-256 해시
    embedding vector(768),           -- nomic-embed-text-v2-moe
    
    -- ========== 메타데이터 ==========
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    -- 호스트별 시간대별 유니크 제약
    CONSTRAINT uk_process_topn_host_time UNIQUE(hostid, time_bucket)
);

CREATE INDEX idx_proc_chunk_time ON embeddings_server_process_topn_chunked(time_bucket DESC);
CREATE INDEX idx_proc_chunk_host ON embeddings_server_process_topn_chunked(hostid);
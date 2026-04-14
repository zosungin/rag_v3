DROP TABLE IF EXISTS embeddings_server_listen_ports_chunked CASCADE;

CREATE TABLE embeddings_server_listen_ports_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    server_ip VARCHAR(50),
    asset_name VARCHAR(255),
    system_name VARCHAR(255),
    
    -- 기준 시간 (변경 발생 시점)
    time_bucket TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    -- 활성 여부
    is_active SMALLINT DEFAULT 1, 
    
    -- ========== 포트 집계 데이터 (JSONB) ==========
    port_metrics JSONB,    -- {"tcp": 13, "udp": 0}
    port_details JSONB,    -- ["22/tcp(SSH)", "5432/tcp(PostgreSQL)", ...]
    
    -- RAG 데이터
    port_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- 메타데이터
    record_count INTEGER DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    -- 동일 서버의 동일 시간에 중복 데이터가 들어오는 것 방지
    CONSTRAINT uk_listen_ports_chunked_timeline UNIQUE(hostid, time_bucket)
);

-- 검색 성능 향상을 위한 인덱스
CREATE INDEX idx_listen_ports_chunked_host_time ON embeddings_server_listen_ports_chunked (hostid, time_bucket DESC);
DROP TABLE IF EXISTS embeddings_server_connections_chunked CASCADE;

CREATE TABLE embeddings_server_connections_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT,
    asset VARCHAR(200) NOT NULL,
    server_ip VARCHAR(50),
    asset_name VARCHAR(255),
    
    -- 💡 변경 감지 및 시계열 분석을 위한 기준 시간
    time_bucket TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    is_active SMALLINT DEFAULT 1, 
    
    -- ========== 지능형 통계 데이터 (JSONB) ==========
    connection_metrics JSONB,    -- {"total": 304, "inbound": 300, "top_ips": [...]}
    connection_details JSONB,    -- 주요 연결 상세 리스트 (Top IP 등)
    
    -- RAG 데이터
    connection_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- 메타데이터
    record_count INTEGER DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- 자산(Asset)과 시간(Bucket)의 조합으로 유니크 설정 (이력 적재 가능)
    CONSTRAINT uk_conn_chunked_timeline UNIQUE(asset, time_bucket)
);

-- 검색 성능을 위한 인덱스
CREATE INDEX idx_conn_chunked_host_time ON embeddings_server_connections_chunked (hostid, time_bucket DESC);
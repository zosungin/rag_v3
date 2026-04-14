DROP TABLE IF EXISTS embeddings_hosts CASCADE;

CREATE TABLE embeddings_hosts (
    id SERIAL PRIMARY KEY,
    hostid BIGINT UNIQUE NOT NULL,
    
    -- 기본 정보
    host_name VARCHAR(255),
    ip VARCHAR(50),
    asset VARCHAR(255),
    asset_name VARCHAR(255),
    os VARCHAR(100),         -- OS 명칭 확장
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 시스템 상태 및 활성 여부
    is_active SMALLINT DEFAULT 1, -- 1=활성, 0=비활성(폐기)
    zabbix_agent VARCHAR(50),
    system_description TEXT,
    system_uptime VARCHAR(100),
    system_local_time VARCHAR(250),
    
    -- 시스템 통계 (프로세스/사용자)
    number_of_processes NUMERIC,
    logined_user NUMERIC,
    
    -- RAG 데이터
    host_summary TEXT,
    summary_hash VARCHAR(64),  -- 💡 [추가됨] 해시 기반 변경 감지용 컬럼
    embedding vector(768),
    
    -- 메타데이터
    comp VARCHAR(255),
    noncomp VARCHAR(255),
    source_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_hosts_is_active ON embeddings_hosts(is_active);
CREATE INDEX idx_hosts_hostid ON embeddings_hosts(hostid);
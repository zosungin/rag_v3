-- Phase B1 Step 2.1: CPU 사용률 벡터 테이블 생성
-- pgvector DB (rag_db)에서 실행
-- 연결 정보: host=172.202.0.20, port=5432, user=rag_user, password=rag_password, database=rag_db

-- 1) 기존 테이블 삭제
DROP TABLE IF EXISTS embeddings_server_cpu CASCADE;

-- 2) 테이블 생성
CREATE TABLE embeddings_server_cpu (
    -- 기본 정보
    hostid BIGINT , -- 누적 방지를 위해 hostid를 단독 PK로 설정
    asset VARCHAR(255),
    
    -- 호스트 정보
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

	-- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,
    
    -- CPU 코어 정보
    number_of_cpus SMALLINT,
    cpu_graph_id BIGINT,
    
    -- CPU 사용률 지표
    cpu_utilization NUMERIC(6,2),
    cpu_idle_time NUMERIC(6,2),
    cpu_system_time NUMERIC(6,2),
    cpu_user_time NUMERIC(6,2),
    
    -- 표준편차 및 안정성
    cpu_utilization_std NUMERIC(6,2),
    stability_level VARCHAR(20),
    priority INTEGER,
    
    -- 검색용 텍스트 및 임베딩
    cpu_summary TEXT,
    embedding vector(768),
    
    -- 메타 정보
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, source_updated_at)
);

-- 3) 인덱스 생성
CREATE INDEX idx_cpu_hostid ON embeddings_server_cpu(hostid);
CREATE INDEX idx_cpu_asset ON embeddings_server_cpu(asset);
CREATE INDEX idx_cpu_ip ON embeddings_server_cpu(ip);
CREATE INDEX idx_cpu_os ON embeddings_server_cpu(os);
CREATE INDEX idx_cpu_priority ON embeddings_server_cpu(priority);
CREATE INDEX idx_cpu_stability ON embeddings_server_cpu(stability_level);
CREATE INDEX idx_cpu_priority_std ON embeddings_server_cpu(priority, cpu_utilization_std);
CREATE INDEX idx_cpu_hostid_priority ON embeddings_server_cpu(hostid, priority);
CREATE INDEX idx_cpu_active ON embeddings_server_cpu(is_active);
COMMENT ON COLUMN embeddings_server_cpu.is_active IS '장비 활성 여부 (1: 활성, 0: 비활성)';

-- 벡터 검색 인덱스
CREATE INDEX idx_cpu_embedding ON embeddings_server_cpu USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);



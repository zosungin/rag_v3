-- ============================================================
-- Phase B1 Step 4.1: 서버 메모리 정보 벡터화 테이블 생성 (수정 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_mem_std (Zabbix DB)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_memory CASCADE;

CREATE TABLE embeddings_server_memory (
    hostid BIGINT , -- 누적 방지를 위해 hostid를 PK로 설정
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    is_active SMALLINT DEFAULT 1, 
    
    total_memory BIGINT,
    available_memory BIGINT,
    memory_utilization BIGINT, -- (Total - Available) 계산값
    available_memory_in NUMERIC(10,2), -- 여유율(%)
    available_memory_std NUMERIC(10,2),
    stability_level VARCHAR(20),
    priority INTEGER,  
    memory_summary TEXT,
    embedding vector(768),
    
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, source_updated_at)
);

-- 3. 인덱스 생성
CREATE INDEX idx_svr_mem_hostid ON embeddings_server_memory(hostid);
CREATE INDEX idx_svr_mem_asset ON embeddings_server_memory(asset);
CREATE INDEX idx_svr_mem_ip ON embeddings_server_memory(ip);
CREATE INDEX idx_svr_mem_os ON embeddings_server_memory(os);
CREATE INDEX idx_svr_mem_available_memory_in ON embeddings_server_memory(available_memory_in);
CREATE INDEX idx_svr_mem_source_updated_at ON embeddings_server_memory(source_updated_at);

-- 복합 인덱스 (빈번한 조회 패턴)
CREATE INDEX idx_svr_mem_hostid_time ON embeddings_server_memory(hostid, source_updated_at);
CREATE INDEX idx_svr_mem_memory_usage ON embeddings_server_memory(available_memory_in, available_memory_std);

-- 4. 벡터 인덱스 생성 (임베딩 검색 성능 향상)
CREATE INDEX idx_svr_mem_embedding 
ON embeddings_server_memory 
USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 10);

-- 5. 테이블 및 컬럼 코멘트
COMMENT ON TABLE embeddings_server_memory IS '서버 메모리 정보 벡터 테이블 (시계열 누적)';
COMMENT ON COLUMN embeddings_server_memory.hostid IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_memory.asset IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_memory.ip IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_memory.asset_name IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_memory.os IS '운영체제';
COMMENT ON COLUMN embeddings_server_memory.system_name IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_memory.group_name IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_memory.total_memory IS '전체 메모리 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.available_memory IS '가용 메모리 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.memory_utilization IS '메모리 사용량 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.available_memory_in IS '여유 메모리율 (%)';
COMMENT ON COLUMN embeddings_server_memory.available_memory_std IS '여유 메모리율 표준편차 (%)';
COMMENT ON COLUMN embeddings_server_memory.memory_summary IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_memory.embedding IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_memory.source_updated_at IS '원본 데이터 업데이트 시각';
COMMENT ON COLUMN embeddings_server_memory.created_at IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_memory.updated_at IS '레코드 수정 시각';

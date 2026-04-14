-- ============================================================
-- Phase B1 Step 5.1: 서버 Swap 정보 벡터화 테이블 생성 (수정 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_swap_std (Zabbix DB)
-- ============================================================

-- embeddings_server_swap 테이블 수정 버전
DROP TABLE IF EXISTS embeddings_server_swap CASCADE;

CREATE TABLE embeddings_server_swap (
    -- 기본 정보 (누적 방지를 위해 hostid를 PK로 설정)
    hostid BIGINT , 
    asset VARCHAR(255),
    
    -- 호스트 정보
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 활성 여부 추가 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1, 
    
    -- Swap 통계 및 사용률
    total_swap_space BIGINT,
    free_swap_space BIGINT,
    free_swap_space_pct NUMERIC(10,2),
    used_swap_percent NUMERIC(10,2),
    used_swap_percent_std NUMERIC(10,2),
    stability_level VARCHAR(20),
    priority INTEGER,     
    -- 검색용 텍스트 요약 및 임베딩
    swap_summary TEXT,
    embedding vector(768),
    
    -- 메타데이터
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, source_updated_at)
);

-- 3. 인덱스 생성
CREATE INDEX idx_svr_swap_hostid ON embeddings_server_swap(hostid);
CREATE INDEX idx_svr_swap_asset ON embeddings_server_swap(asset);
CREATE INDEX idx_svr_swap_ip ON embeddings_server_swap(ip);
CREATE INDEX idx_svr_swap_os ON embeddings_server_swap(os);
CREATE INDEX idx_svr_swap_used_swap_percent ON embeddings_server_swap(used_swap_percent);
CREATE INDEX idx_svr_swap_source_updated_at ON embeddings_server_swap(source_updated_at);
CREATE INDEX idx_svr_swap_active ON embeddings_server_swap(is_active);
COMMENT ON COLUMN embeddings_server_swap.is_active IS '장비 활성 여부 (1: 활성, 0: 비활성)';

-- 복합 인덱스 (빈번한 조회 패턴)
CREATE INDEX idx_svr_swap_hostid_time ON embeddings_server_swap(hostid, source_updated_at);
CREATE INDEX idx_svr_swap_usage ON embeddings_server_swap(used_swap_percent, used_swap_percent_std);

-- 4. 벡터 인덱스 생성 (임베딩 검색 성능 향상)
CREATE INDEX idx_svr_swap_embedding ON embeddings_server_swap USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- 5. 테이블 및 컬럼 코멘트
COMMENT ON TABLE embeddings_server_swap IS '서버 Swap 정보 벡터 테이블 (시계열 누적)';
COMMENT ON COLUMN embeddings_server_swap.hostid IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_swap.asset IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_swap.ip IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_swap.asset_name IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_swap.os IS '운영체제';
COMMENT ON COLUMN embeddings_server_swap.system_name IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_swap.group_name IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_swap.total_swap_space IS '전체 Swap 공간 (bytes)';
COMMENT ON COLUMN embeddings_server_swap.free_swap_space IS '여유 Swap 공간 (bytes)';
COMMENT ON COLUMN embeddings_server_swap.free_swap_space_pct IS '여유 Swap 비율 (%)';
COMMENT ON COLUMN embeddings_server_swap.used_swap_percent IS 'Swap 사용률 (%)';
COMMENT ON COLUMN embeddings_server_swap.used_swap_percent_std IS 'Swap 사용률 표준편차 (%)';
COMMENT ON COLUMN embeddings_server_swap.swap_summary IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_swap.embedding IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_swap.source_updated_at IS '원본 데이터 업데이트 시각';
COMMENT ON COLUMN embeddings_server_swap.created_at IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_swap.updated_at IS '레코드 수정 시각';

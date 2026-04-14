-- ============================================================
-- Phase B1 Step 6.1: 서버 인터페이스 정보 벡터화 테이블 생성 (수정 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_if_std (Zabbix DB)
-- ============================================================

-- Phase B1 Step 6.1: 서버 인터페이스 정보 벡터화 테이블 생성 (수정 버전)
DROP TABLE IF EXISTS embeddings_server_interfaces CASCADE;

CREATE TABLE embeddings_server_interfaces (
    -- 기본 정보 (hostid와 interface_name 조합을 PK로 하여 최신성 유지)
    hostid BIGINT NOT NULL,
    interface_name VARCHAR(255) NOT NULL,
    
    asset VARCHAR(255),
    
    -- 호스트 정보
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 인터페이스 상세 정보
    operation_status VARCHAR(50), -- 1: Up, 0: Down 등
    speed BIGINT,
    bits_sent BIGINT,
    bits_received BIGINT,
    bits_sent_std NUMERIC(15,2),
    bits_received_std NUMERIC(15,2),
	stability_level VARCHAR(20),
    priority INTEGER, 
    
    -- 검색용 텍스트 및 임베딩
    interface_summary TEXT,
    embedding vector(768),
    
    -- 메타데이터
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, interface_name, source_updated_at)
);

-- 3. 인덱스 생성
CREATE INDEX idx_svr_if_hostid ON embeddings_server_interfaces(hostid);
CREATE INDEX idx_svr_if_asset ON embeddings_server_interfaces(asset);
CREATE INDEX idx_svr_if_ip ON embeddings_server_interfaces(ip);
CREATE INDEX idx_svr_if_os ON embeddings_server_interfaces(os);
CREATE INDEX idx_svr_if_interface_name ON embeddings_server_interfaces(interface_name);
CREATE INDEX idx_svr_if_operation_status ON embeddings_server_interfaces(operation_status);
CREATE INDEX idx_svr_if_source_updated_at ON embeddings_server_interfaces(source_updated_at);
CREATE INDEX idx_if_active ON embeddings_server_interfaces(is_active);
CREATE INDEX idx_if_embedding ON embeddings_server_interfaces USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);


-- 복합 인덱스 (빈번한 조회 패턴)
CREATE INDEX idx_svr_if_hostid_interface ON embeddings_server_interfaces(hostid, interface_name);
CREATE INDEX idx_svr_if_hostid_status ON embeddings_server_interfaces(hostid, operation_status);

-- 4. 벡터 인덱스 생성 (임베딩 검색 성능 향상)
CREATE INDEX idx_svr_if_embedding 
ON embeddings_server_interfaces 
USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 10);

-- 5. 테이블 및 컬럼 코멘트
COMMENT ON TABLE embeddings_server_interfaces IS '서버 인터페이스 정보 벡터 테이블 (시계열 누적)';
COMMENT ON COLUMN embeddings_server_interfaces.hostid IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_interfaces.asset IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_interfaces.ip IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_interfaces.asset_name IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_interfaces.os IS '운영체제';
COMMENT ON COLUMN embeddings_server_interfaces.system_name IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_interfaces.group_name IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_interfaces.interface_name IS '인터페이스명 (eth0, ens33 등)';
COMMENT ON COLUMN embeddings_server_interfaces.operation_status IS '인터페이스 상태 (1=활성, 0=비활성)';
COMMENT ON COLUMN embeddings_server_interfaces.speed IS '인터페이스 속도 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_sent IS '트래픽 송신 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_received IS '트래픽 수신 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_sent_std IS '트래픽 송신 표준편차 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_received_std IS '트래픽 수신 표준편차 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.interface_summary IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_interfaces.embedding IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_interfaces.source_updated_at IS '원본 데이터 업데이트 시각';
COMMENT ON COLUMN embeddings_server_interfaces.created_at IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_interfaces.updated_at IS '레코드 수정 시각';

-- 6. 테이블 정보 확인
SELECT 
    column_name, 
    data_type, 
    character_maximum_length,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'embeddings_server_interfaces'
ORDER BY ordinal_position;

-- 7. 인덱스 확인
SELECT 
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'embeddings_server_interfaces';

-- 8. PRIMARY KEY 확인
SELECT
    conname AS constraint_name,
    contype AS constraint_type,
    pg_get_constraintdef(oid) AS constraint_definition
FROM pg_constraint
WHERE conrelid = 'embeddings_server_interfaces'::regclass
  AND contype = 'p';

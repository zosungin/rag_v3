-- ============================================================
-- Phase B1 Step 6.1: 서버 인터페이스 정보 벡터화 테이블 생성 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_if_std (Zabbix DB)
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
--   - [정리] 중복 벡터 인덱스 제거 (idx_if_embedding / idx_svr_if_embedding 통일)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_interfaces CASCADE;

CREATE TABLE embeddings_server_interfaces (
    -- PK: hostid + 인터페이스명 + 수집시각 → 3일 누적 보존
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
    operation_status VARCHAR(50),               -- 인터페이스 상태 (1=활성, 0=비활성, Up/Down 텍스트 등)
    speed BIGINT,                               -- 인터페이스 속도 (bps)
    bits_sent BIGINT,                           -- 트래픽 송신량 (bps)
    bits_received BIGINT,                       -- 트래픽 수신량 (bps)
    bits_sent_std NUMERIC(15,2),                -- 송신 표준편차 (bps)
    bits_received_std NUMERIC(15,2),            -- 수신 표준편차 (bps)

    -- 안정성 및 우선순위
    stability_level VARCHAR(20),                -- 안정성 수준 (정상 / 주의 / 경고 / 위험)
    priority INTEGER,                           -- 우선순위 (0=정상, 값이 클수록 긴급)

    -- 검색용 텍스트 및 임베딩
    interface_summary TEXT,
    embedding vector(768),

    -- 메타데이터
    source_updated_at TIMESTAMP NOT NULL,       -- 원본 수집 시각 (PK 구성요소)
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    PRIMARY KEY (hostid, interface_name, source_updated_at)
);

-- 단일 컬럼 인덱스
CREATE INDEX idx_svr_if_hostid           ON embeddings_server_interfaces(hostid);
CREATE INDEX idx_svr_if_asset            ON embeddings_server_interfaces(asset);
CREATE INDEX idx_svr_if_ip               ON embeddings_server_interfaces(ip);
CREATE INDEX idx_svr_if_os               ON embeddings_server_interfaces(os);
CREATE INDEX idx_svr_if_interface_name   ON embeddings_server_interfaces(interface_name);
CREATE INDEX idx_svr_if_operation_status ON embeddings_server_interfaces(operation_status);
CREATE INDEX idx_svr_if_source_updated_at ON embeddings_server_interfaces(source_updated_at DESC);
CREATE INDEX idx_svr_if_active           ON embeddings_server_interfaces(is_active);
CREATE INDEX idx_svr_if_priority         ON embeddings_server_interfaces(priority);
CREATE INDEX idx_svr_if_stability        ON embeddings_server_interfaces(stability_level);

-- 복합 인덱스
CREATE INDEX idx_svr_if_hostid_interface ON embeddings_server_interfaces(hostid, interface_name);
CREATE INDEX idx_svr_if_hostid_status    ON embeddings_server_interfaces(hostid, operation_status);
CREATE INDEX idx_svr_if_hostid_time      ON embeddings_server_interfaces(hostid, source_updated_at DESC);
CREATE INDEX idx_svr_if_hostid_priority  ON embeddings_server_interfaces(hostid, priority);

-- 벡터 인덱스
CREATE INDEX idx_svr_if_embedding
    ON embeddings_server_interfaces
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_interfaces                     IS '서버 인터페이스 정보 벡터 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_server_interfaces.hostid              IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_interfaces.interface_name      IS '인터페이스명 (eth0, ens33 등)';
COMMENT ON COLUMN embeddings_server_interfaces.asset               IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_interfaces.ip                  IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_interfaces.asset_name          IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_interfaces.os                  IS '운영체제';
COMMENT ON COLUMN embeddings_server_interfaces.system_name         IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_interfaces.group_name          IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_interfaces.operation_status    IS '인터페이스 상태 (1=활성, 0=비활성)';
COMMENT ON COLUMN embeddings_server_interfaces.speed               IS '인터페이스 속도 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_sent           IS '트래픽 송신량 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_received       IS '트래픽 수신량 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_sent_std       IS '트래픽 송신 표준편차 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.bits_received_std   IS '트래픽 수신 표준편차 (bps)';
COMMENT ON COLUMN embeddings_server_interfaces.stability_level     IS '안정성 수준 (정상 / 주의 / 경고 / 위험)';
COMMENT ON COLUMN embeddings_server_interfaces.priority            IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_server_interfaces.interface_summary   IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_interfaces.embedding           IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_interfaces.source_updated_at   IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_server_interfaces.created_at          IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_interfaces.updated_at          IS '레코드 수정 시각';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + interface_name + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_server_interfaces (
--     hostid, interface_name, asset, ip, asset_name, os, system_name, group_name,
--     is_active, operation_status, speed, bits_sent, bits_received,
--     bits_sent_std, bits_received_std,
--     stability_level, priority,
--     interface_summary, embedding, source_updated_at, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(interface_name)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
--     %(is_active)s, %(operation_status)s, %(speed)s, %(bits_sent)s, %(bits_received)s,
--     %(bits_sent_std)s, %(bits_received_std)s,
--     %(stability_level)s, %(priority)s,
--     %(interface_summary)s, %(embedding)s, %(source_updated_at)s, now()
-- )
-- ON CONFLICT (hostid, interface_name, source_updated_at) DO UPDATE SET
--     asset               = EXCLUDED.asset,
--     ip                  = EXCLUDED.ip,
--     asset_name          = EXCLUDED.asset_name,
--     os                  = EXCLUDED.os,
--     system_name         = EXCLUDED.system_name,
--     group_name          = EXCLUDED.group_name,
--     is_active           = EXCLUDED.is_active,
--     operation_status    = EXCLUDED.operation_status,
--     speed               = EXCLUDED.speed,
--     bits_sent           = EXCLUDED.bits_sent,
--     bits_received       = EXCLUDED.bits_received,
--     bits_sent_std       = EXCLUDED.bits_sent_std,
--     bits_received_std   = EXCLUDED.bits_received_std,
--     stability_level     = EXCLUDED.stability_level,
--     priority            = EXCLUDED.priority,
--     interface_summary   = EXCLUDED.interface_summary,
--     embedding           = EXCLUDED.embedding,
--     updated_at          = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_server_interfaces
-- WHERE source_updated_at < now() - INTERVAL '3 days';

-- ============================================================
-- 테이블 구조 확인 쿼리 (필요시 실행)
-- ============================================================
-- SELECT column_name, data_type, character_maximum_length, is_nullable
-- FROM information_schema.columns
-- WHERE table_name = 'embeddings_server_interfaces'
-- ORDER BY ordinal_position;
--
-- SELECT indexname, indexdef FROM pg_indexes
-- WHERE tablename = 'embeddings_server_interfaces';
--
-- SELECT conname, contype, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid = 'embeddings_server_interfaces'::regclass AND contype = 'p';

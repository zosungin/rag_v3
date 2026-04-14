-- ============================================================
-- Phase B1 Step 4.1: 서버 메모리 정보 벡터화 테이블 생성 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_mem_std (Zabbix DB)
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
--   - [추가] priority / stability_level 인덱스 추가
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_memory CASCADE;

CREATE TABLE embeddings_server_memory (
    -- PK: hostid + 수집시각 → 3일 누적 보존
    hostid BIGINT,
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 메모리 통계
    total_memory BIGINT,                        -- 전체 메모리 (bytes)
    available_memory BIGINT,                    -- 가용 메모리 (bytes)
    memory_utilization BIGINT,                  -- 사용 메모리 = Total - Available (bytes)
    available_memory_in NUMERIC(10,2),          -- 여유율 (%)
    available_memory_std NUMERIC(10,2),         -- 여유율 표준편차 (%)

    -- 안정성 및 우선순위
    stability_level VARCHAR(20),                -- 안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))
    priority INTEGER,                           -- 우선순위 (0=정상, 값이 클수록 긴급)

    -- 검색용 텍스트 및 임베딩
    memory_summary TEXT,
    embedding vector(768),

    -- 메타 정보
    source_updated_at TIMESTAMP NOT NULL,       -- 원본 수집 시각 (PK 구성요소)
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    PRIMARY KEY (hostid, source_updated_at)
);

-- 단일 컬럼 인덱스
CREATE INDEX idx_svr_mem_hostid            ON embeddings_server_memory(hostid);
CREATE INDEX idx_svr_mem_asset             ON embeddings_server_memory(asset);
CREATE INDEX idx_svr_mem_ip                ON embeddings_server_memory(ip);
CREATE INDEX idx_svr_mem_os                ON embeddings_server_memory(os);
CREATE INDEX idx_svr_mem_available_memory_in ON embeddings_server_memory(available_memory_in);
CREATE INDEX idx_svr_mem_source_updated_at ON embeddings_server_memory(source_updated_at DESC);
CREATE INDEX idx_svr_mem_priority          ON embeddings_server_memory(priority);
CREATE INDEX idx_svr_mem_stability         ON embeddings_server_memory(stability_level);
CREATE INDEX idx_svr_mem_active            ON embeddings_server_memory(is_active);

-- 복합 인덱스
CREATE INDEX idx_svr_mem_hostid_time       ON embeddings_server_memory(hostid, source_updated_at DESC);
CREATE INDEX idx_svr_mem_memory_usage      ON embeddings_server_memory(available_memory_in, available_memory_std);
CREATE INDEX idx_svr_mem_hostid_priority   ON embeddings_server_memory(hostid, priority);

-- 벡터 인덱스
CREATE INDEX idx_svr_mem_embedding
    ON embeddings_server_memory
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_memory                    IS '서버 메모리 정보 벡터 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_server_memory.hostid             IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_memory.asset              IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_memory.ip                 IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_memory.asset_name         IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_memory.os                 IS '운영체제';
COMMENT ON COLUMN embeddings_server_memory.system_name        IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_memory.group_name         IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_memory.total_memory       IS '전체 메모리 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.available_memory   IS '가용 메모리 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.memory_utilization IS '메모리 사용량 (bytes)';
COMMENT ON COLUMN embeddings_server_memory.available_memory_in IS '여유 메모리율 (%)';
COMMENT ON COLUMN embeddings_server_memory.available_memory_std IS '여유 메모리율 표준편차 (%)';
COMMENT ON COLUMN embeddings_server_memory.stability_level    IS '안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))';
COMMENT ON COLUMN embeddings_server_memory.priority           IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_server_memory.memory_summary     IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_memory.embedding          IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_memory.source_updated_at  IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_server_memory.created_at         IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_memory.updated_at         IS '레코드 수정 시각';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_server_memory (
--     hostid, asset, ip, asset_name, os, system_name, group_name,
--     is_active, total_memory, available_memory, memory_utilization,
--     available_memory_in, available_memory_std,
--     stability_level, priority,
--     memory_summary, embedding, source_updated_at, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
--     %(is_active)s, %(total_memory)s, %(available_memory)s, %(memory_utilization)s,
--     %(available_memory_in)s, %(available_memory_std)s,
--     %(stability_level)s, %(priority)s,
--     %(memory_summary)s, %(embedding)s, %(source_updated_at)s, now()
-- )
-- ON CONFLICT (hostid, source_updated_at) DO UPDATE SET
--     asset                = EXCLUDED.asset,
--     ip                   = EXCLUDED.ip,
--     asset_name           = EXCLUDED.asset_name,
--     os                   = EXCLUDED.os,
--     system_name          = EXCLUDED.system_name,
--     group_name           = EXCLUDED.group_name,
--     is_active            = EXCLUDED.is_active,
--     total_memory         = EXCLUDED.total_memory,
--     available_memory     = EXCLUDED.available_memory,
--     memory_utilization   = EXCLUDED.memory_utilization,
--     available_memory_in  = EXCLUDED.available_memory_in,
--     available_memory_std = EXCLUDED.available_memory_std,
--     stability_level      = EXCLUDED.stability_level,
--     priority             = EXCLUDED.priority,
--     memory_summary       = EXCLUDED.memory_summary,
--     embedding            = EXCLUDED.embedding,
--     updated_at           = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_server_memory
-- WHERE source_updated_at < now() - INTERVAL '3 days';

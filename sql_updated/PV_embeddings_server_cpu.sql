-- ============================================================
-- Phase B1 Step 2.1: CPU 사용률 벡터 테이블 생성 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
-- ============================================================

-- 1) 기존 테이블 삭제
DROP TABLE IF EXISTS embeddings_server_cpu CASCADE;

-- 2) 테이블 생성
CREATE TABLE embeddings_server_cpu (
    -- 기본 정보
    hostid BIGINT,                  -- 호스트 ID
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
    stability_level VARCHAR(20),    -- 안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험) 등)
    priority INTEGER,               -- 우선순위 (0=정상, 높을수록 긴급)

    -- 검색용 텍스트 및 임베딩
    cpu_summary TEXT,
    embedding vector(768),

    -- 메타 정보
    source_updated_at TIMESTAMP NOT NULL,       -- 원본 수집 시각 (PK 구성요소)
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    -- PK: hostid + 수집시각 → 3일 누적 보존
    PRIMARY KEY (hostid, source_updated_at)
);

-- 3) 단일 컬럼 인덱스
CREATE INDEX idx_cpu_hostid            ON embeddings_server_cpu(hostid);
CREATE INDEX idx_cpu_asset             ON embeddings_server_cpu(asset);
CREATE INDEX idx_cpu_ip                ON embeddings_server_cpu(ip);
CREATE INDEX idx_cpu_os                ON embeddings_server_cpu(os);
CREATE INDEX idx_cpu_priority          ON embeddings_server_cpu(priority);
CREATE INDEX idx_cpu_stability         ON embeddings_server_cpu(stability_level);
CREATE INDEX idx_cpu_source_updated_at ON embeddings_server_cpu(source_updated_at DESC);
CREATE INDEX idx_cpu_active            ON embeddings_server_cpu(is_active);

-- 4) 복합 인덱스
CREATE INDEX idx_cpu_priority_std      ON embeddings_server_cpu(priority, cpu_utilization_std);
CREATE INDEX idx_cpu_hostid_priority   ON embeddings_server_cpu(hostid, priority);
CREATE INDEX idx_cpu_hostid_time       ON embeddings_server_cpu(hostid, source_updated_at DESC);

-- 5) 벡터 검색 인덱스
CREATE INDEX idx_cpu_embedding
    ON embeddings_server_cpu
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 6) 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_cpu                  IS 'CPU 사용률 벡터 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_server_cpu.hostid           IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_cpu.stability_level  IS '안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))';
COMMENT ON COLUMN embeddings_server_cpu.priority         IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_server_cpu.is_active        IS '장비 활성 여부 (1: 활성, 0: 비활성)';
COMMENT ON COLUMN embeddings_server_cpu.source_updated_at IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_server_cpu.embedding        IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_server_cpu (
--     hostid, asset, ip, asset_name, os, system_name, group_name,
--     is_active, number_of_cpus, cpu_graph_id,
--     cpu_utilization, cpu_idle_time, cpu_system_time, cpu_user_time,
--     cpu_utilization_std, stability_level, priority,
--     cpu_summary, embedding, source_updated_at, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
--     %(is_active)s, %(number_of_cpus)s, %(cpu_graph_id)s,
--     %(cpu_utilization)s, %(cpu_idle_time)s, %(cpu_system_time)s, %(cpu_user_time)s,
--     %(cpu_utilization_std)s, %(stability_level)s, %(priority)s,
--     %(cpu_summary)s, %(embedding)s, %(source_updated_at)s, now()
-- )
-- ON CONFLICT (hostid, source_updated_at) DO UPDATE SET
--     asset              = EXCLUDED.asset,
--     ip                 = EXCLUDED.ip,
--     asset_name         = EXCLUDED.asset_name,
--     os                 = EXCLUDED.os,
--     system_name        = EXCLUDED.system_name,
--     group_name         = EXCLUDED.group_name,
--     is_active          = EXCLUDED.is_active,
--     number_of_cpus     = EXCLUDED.number_of_cpus,
--     cpu_graph_id       = EXCLUDED.cpu_graph_id,
--     cpu_utilization    = EXCLUDED.cpu_utilization,
--     cpu_idle_time      = EXCLUDED.cpu_idle_time,
--     cpu_system_time    = EXCLUDED.cpu_system_time,
--     cpu_user_time      = EXCLUDED.cpu_user_time,
--     cpu_utilization_std= EXCLUDED.cpu_utilization_std,
--     stability_level    = EXCLUDED.stability_level,
--     priority           = EXCLUDED.priority,
--     cpu_summary        = EXCLUDED.cpu_summary,
--     embedding          = EXCLUDED.embedding,
--     updated_at         = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_server_cpu
-- WHERE source_updated_at < now() - INTERVAL '3 days';

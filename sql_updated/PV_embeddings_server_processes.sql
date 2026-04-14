-- ============================================================
-- Phase B1: 서버별 프로세스 자원 정보 원본 임베딩 테이블 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
--   - [추가] priority / stability_level / cpu / mem 인덱스 보강
-- ============================================================

DROP TABLE IF EXISTS embeddings_process_resources CASCADE;

CREATE TABLE embeddings_process_resources (
    -- PK: 호스트ID + 프로세스ID + 수집시각 (이력 보존 및 고유성 확보)
    hostid BIGINT NOT NULL,
    pid INTEGER NOT NULL,
    source_updated_at TIMESTAMP NOT NULL,       -- Zabbix clock 기반 수집 시각

    -- 호스트 식별 정보
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(100),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 프로세스 상세 정보 및 자원 지표
    user_name VARCHAR(100),                     -- 프로세스 실행 사용자
    command TEXT,                               -- 실행 명령줄 (전체 커맨드)
    cpu NUMERIC(6,2),                           -- CPU 사용률 (%)
    cpu_time BIGINT,                            -- CPU 점유 시간
    mem NUMERIC(6,2),                           -- 메모리 점유율 (%)
    mem_used BIGINT,                            -- 실제 사용 메모리량 (Byte/KB 등)
    resource_std NUMERIC(6,2),                  -- 리소스 변동 표준편차

    -- 안정성 및 우선순위
    stability_level VARCHAR(20),                -- 안정성 수준 (기동 중 / 중지됨 / 과부하 등)
    priority INTEGER,                           -- 우선순위 (0=정상, 값이 클수록 긴급)

    -- RAG 데이터
    process_summary TEXT NOT NULL,              -- "사용자 root가 실행한 java 프로세스 (CPU: 15%)" 등
    embedding vector(768),                      -- 768차원 벡터 데이터

    -- 메타데이터
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    PRIMARY KEY (hostid, pid, source_updated_at)
);

-- 단일 컬럼 인덱스
CREATE INDEX idx_proc_res_hostid            ON embeddings_process_resources(hostid);
CREATE INDEX idx_proc_res_asset             ON embeddings_process_resources(asset);
CREATE INDEX idx_proc_res_ip                ON embeddings_process_resources(ip);
CREATE INDEX idx_proc_res_time              ON embeddings_process_resources(source_updated_at DESC);
CREATE INDEX idx_proc_res_cpu               ON embeddings_process_resources(cpu DESC);
CREATE INDEX idx_proc_res_mem               ON embeddings_process_resources(mem DESC);
CREATE INDEX idx_proc_res_user_name         ON embeddings_process_resources(user_name);
CREATE INDEX idx_proc_res_active            ON embeddings_process_resources(is_active);
CREATE INDEX idx_proc_res_priority          ON embeddings_process_resources(priority);
CREATE INDEX idx_proc_res_stability         ON embeddings_process_resources(stability_level);

-- 복합 인덱스
CREATE INDEX idx_proc_res_hostid_time       ON embeddings_process_resources(hostid, source_updated_at DESC);
CREATE INDEX idx_proc_res_hostid_priority   ON embeddings_process_resources(hostid, priority);
CREATE INDEX idx_proc_res_hostid_cpu        ON embeddings_process_resources(hostid, cpu DESC);
CREATE INDEX idx_proc_res_hostid_mem        ON embeddings_process_resources(hostid, mem DESC);

-- 벡터 인덱스
CREATE INDEX idx_proc_res_embedding
    ON embeddings_process_resources
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_process_resources                    IS '서버별 프로세스 자원 임베딩 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_process_resources.hostid             IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_process_resources.pid                IS '프로세스 ID (PID)';
COMMENT ON COLUMN embeddings_process_resources.asset              IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_process_resources.ip                 IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_process_resources.asset_name         IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_process_resources.os                 IS '운영체제';
COMMENT ON COLUMN embeddings_process_resources.system_name        IS '서버 hostname';
COMMENT ON COLUMN embeddings_process_resources.group_name         IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_process_resources.user_name          IS '프로세스 실행 사용자';
COMMENT ON COLUMN embeddings_process_resources.command            IS '실행 명령줄 (전체 커맨드)';
COMMENT ON COLUMN embeddings_process_resources.cpu                IS 'CPU 사용률 (%)';
COMMENT ON COLUMN embeddings_process_resources.cpu_time           IS 'CPU 점유 시간';
COMMENT ON COLUMN embeddings_process_resources.mem                IS '메모리 점유율 (%)';
COMMENT ON COLUMN embeddings_process_resources.mem_used           IS '실제 사용 메모리량 (bytes)';
COMMENT ON COLUMN embeddings_process_resources.resource_std       IS '리소스 변동 표준편차';
COMMENT ON COLUMN embeddings_process_resources.stability_level    IS '안정성 수준 (기동 중 / 중지됨 / 과부하 등)';
COMMENT ON COLUMN embeddings_process_resources.priority           IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_process_resources.process_summary    IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_process_resources.embedding          IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_process_resources.source_updated_at  IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_process_resources.created_at         IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_process_resources.updated_at         IS '레코드 수정 시각';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + pid + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_process_resources (
--     hostid, pid, source_updated_at,
--     asset, ip, asset_name, os, system_name, group_name, is_active,
--     user_name, command, cpu, cpu_time, mem, mem_used, resource_std,
--     stability_level, priority,
--     process_summary, embedding, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(pid)s, %(source_updated_at)s,
--     %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
--     %(user_name)s, %(command)s, %(cpu)s, %(cpu_time)s, %(mem)s, %(mem_used)s, %(resource_std)s,
--     %(stability_level)s, %(priority)s,
--     %(process_summary)s, %(embedding)s, now()
-- )
-- ON CONFLICT (hostid, pid, source_updated_at) DO UPDATE SET
--     asset            = EXCLUDED.asset,
--     ip               = EXCLUDED.ip,
--     asset_name       = EXCLUDED.asset_name,
--     os               = EXCLUDED.os,
--     system_name      = EXCLUDED.system_name,
--     group_name       = EXCLUDED.group_name,
--     is_active        = EXCLUDED.is_active,
--     user_name        = EXCLUDED.user_name,
--     command          = EXCLUDED.command,
--     cpu              = EXCLUDED.cpu,
--     cpu_time         = EXCLUDED.cpu_time,
--     mem              = EXCLUDED.mem,
--     mem_used         = EXCLUDED.mem_used,
--     resource_std     = EXCLUDED.resource_std,
--     stability_level  = EXCLUDED.stability_level,
--     priority         = EXCLUDED.priority,
--     process_summary  = EXCLUDED.process_summary,
--     embedding        = EXCLUDED.embedding,
--     updated_at       = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_process_resources
-- WHERE source_updated_at < now() - INTERVAL '3 days';

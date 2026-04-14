-- ============================================================
-- Phase B1 Step 3.1: 서버 파일시스템 리소스 벡터화 테이블 생성 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
--   - [추가] priority / stability_level / hostid / ip 등 인덱스 보강
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_filesystems CASCADE;

CREATE TABLE embeddings_server_filesystems (
    -- PK: hostid + 파일시스템명 + 수집시각 → 3일 누적 보존
    hostid BIGINT NOT NULL,
    fs_name VARCHAR(255) NOT NULL,

    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 파일시스템 상세 정보
    total_size NUMERIC(12,1),                   -- 전체 용량 (MB)
    used_size NUMERIC(12,1),                    -- 사용 용량 (MB)
    used_percent NUMERIC(5,1),                  -- 사용률 (%)
    used_percent_std NUMERIC(5,2),              -- 사용률 표준편차 (%)

    -- 안정성 및 우선순위
    stability_level VARCHAR(20),                -- 안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))
    priority INTEGER,                           -- 우선순위 (0=정상, 값이 클수록 긴급)

    -- RAG 데이터
    fs_summary TEXT NOT NULL,
    embedding vector(768),

    -- 메타데이터
    source_updated_at TIMESTAMP NOT NULL,       -- 원본 수집 시각 (PK 구성요소)
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    PRIMARY KEY (hostid, fs_name, source_updated_at)
);

-- 단일 컬럼 인덱스
CREATE INDEX idx_fs_hostid            ON embeddings_server_filesystems(hostid);
CREATE INDEX idx_fs_asset             ON embeddings_server_filesystems(asset);
CREATE INDEX idx_fs_ip                ON embeddings_server_filesystems(ip);
CREATE INDEX idx_fs_os                ON embeddings_server_filesystems(os);
CREATE INDEX idx_fs_fs_name           ON embeddings_server_filesystems(fs_name);
CREATE INDEX idx_fs_used_percent      ON embeddings_server_filesystems(used_percent);
CREATE INDEX idx_fs_source_updated_at ON embeddings_server_filesystems(source_updated_at DESC);
CREATE INDEX idx_fs_active            ON embeddings_server_filesystems(is_active);
CREATE INDEX idx_fs_priority          ON embeddings_server_filesystems(priority);
CREATE INDEX idx_fs_stability         ON embeddings_server_filesystems(stability_level);

-- 복합 인덱스
CREATE INDEX idx_fs_hostid_time       ON embeddings_server_filesystems(hostid, source_updated_at DESC);
CREATE INDEX idx_fs_hostid_fs         ON embeddings_server_filesystems(hostid, fs_name);
CREATE INDEX idx_fs_hostid_priority   ON embeddings_server_filesystems(hostid, priority);

-- 벡터 인덱스
CREATE INDEX idx_fs_embedding
    ON embeddings_server_filesystems
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_filesystems                  IS '서버 파일시스템 리소스 상태 및 점유율 임베딩 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_server_filesystems.hostid           IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_filesystems.fs_name          IS '파일시스템 경로명 (/, /data 등)';
COMMENT ON COLUMN embeddings_server_filesystems.asset            IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_filesystems.ip               IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_filesystems.asset_name       IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_filesystems.os               IS '운영체제';
COMMENT ON COLUMN embeddings_server_filesystems.system_name      IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_filesystems.group_name       IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_filesystems.total_size       IS '전체 용량 (MB)';
COMMENT ON COLUMN embeddings_server_filesystems.used_size        IS '사용 용량 (MB)';
COMMENT ON COLUMN embeddings_server_filesystems.used_percent     IS '사용률 (%)';
COMMENT ON COLUMN embeddings_server_filesystems.used_percent_std IS '사용률 표준편차 (%)';
COMMENT ON COLUMN embeddings_server_filesystems.stability_level  IS '안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))';
COMMENT ON COLUMN embeddings_server_filesystems.priority         IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_server_filesystems.fs_summary       IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_filesystems.embedding        IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_filesystems.source_updated_at IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_server_filesystems.created_at       IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_filesystems.updated_at       IS '레코드 수정 시각';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + fs_name + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_server_filesystems (
--     hostid, fs_name, asset, ip, asset_name, os, system_name, group_name,
--     is_active, total_size, used_size, used_percent, used_percent_std,
--     stability_level, priority,
--     fs_summary, embedding, source_updated_at, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(fs_name)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
--     %(is_active)s, %(total_size)s, %(used_size)s, %(used_percent)s, %(used_percent_std)s,
--     %(stability_level)s, %(priority)s,
--     %(fs_summary)s, %(embedding)s, %(source_updated_at)s, now()
-- )
-- ON CONFLICT (hostid, fs_name, source_updated_at) DO UPDATE SET
--     asset             = EXCLUDED.asset,
--     ip                = EXCLUDED.ip,
--     asset_name        = EXCLUDED.asset_name,
--     os                = EXCLUDED.os,
--     system_name       = EXCLUDED.system_name,
--     group_name        = EXCLUDED.group_name,
--     is_active         = EXCLUDED.is_active,
--     total_size        = EXCLUDED.total_size,
--     used_size         = EXCLUDED.used_size,
--     used_percent      = EXCLUDED.used_percent,
--     used_percent_std  = EXCLUDED.used_percent_std,
--     stability_level   = EXCLUDED.stability_level,
--     priority          = EXCLUDED.priority,
--     fs_summary        = EXCLUDED.fs_summary,
--     embedding         = EXCLUDED.embedding,
--     updated_at        = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_server_filesystems
-- WHERE source_updated_at < now() - INTERVAL '3 days';

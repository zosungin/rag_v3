-- ============================================================
-- Phase B1 Step 5.1: 서버 Swap 정보 벡터화 테이블 생성 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 원본 테이블: svr_swap_std (Zabbix DB)
-- 변경 이력:
--   - [신규] stability_level, priority 컬럼 확인 (이미 포함됨)
--   - [변경] 저장방식: UPDATE → UPSERT (최신 1건 → 3일 누적)
--   - [추가] 3일 초과 데이터 자동 삭제 정책 (주석 포함)
--   - [추가] priority / stability_level 인덱스 추가
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_swap CASCADE;

CREATE TABLE embeddings_server_swap (
    -- PK: hostid + 수집시각 → 3일 누적 보존
    hostid BIGINT,
    asset VARCHAR(255),

    -- 호스트 정보
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- Swap 통계 및 사용률
    total_swap_space BIGINT,                    -- 전체 Swap 공간 (bytes)
    free_swap_space BIGINT,                     -- 여유 Swap 공간 (bytes)
    free_swap_space_pct NUMERIC(10,2),          -- 여유 Swap 비율 (%)
    used_swap_percent NUMERIC(10,2),            -- Swap 사용률 (%)
    used_swap_percent_std NUMERIC(10,2),        -- Swap 사용률 표준편차 (%)

    -- 안정성 및 우선순위
    stability_level VARCHAR(20),                -- 안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))
    priority INTEGER,                           -- 우선순위 (0=정상, 값이 클수록 긴급)

    -- 검색용 텍스트 요약 및 임베딩
    swap_summary TEXT,
    embedding vector(768),

    -- 메타데이터
    source_updated_at TIMESTAMP NOT NULL,       -- 원본 수집 시각 (PK 구성요소)
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    PRIMARY KEY (hostid, source_updated_at)
);

-- 단일 컬럼 인덱스
CREATE INDEX idx_svr_swap_hostid            ON embeddings_server_swap(hostid);
CREATE INDEX idx_svr_swap_asset             ON embeddings_server_swap(asset);
CREATE INDEX idx_svr_swap_ip                ON embeddings_server_swap(ip);
CREATE INDEX idx_svr_swap_os                ON embeddings_server_swap(os);
CREATE INDEX idx_svr_swap_used_swap_percent ON embeddings_server_swap(used_swap_percent);
CREATE INDEX idx_svr_swap_source_updated_at ON embeddings_server_swap(source_updated_at DESC);
CREATE INDEX idx_svr_swap_active            ON embeddings_server_swap(is_active);
CREATE INDEX idx_svr_swap_priority          ON embeddings_server_swap(priority);
CREATE INDEX idx_svr_swap_stability         ON embeddings_server_swap(stability_level);

-- 복합 인덱스
CREATE INDEX idx_svr_swap_hostid_time       ON embeddings_server_swap(hostid, source_updated_at DESC);
CREATE INDEX idx_svr_swap_usage             ON embeddings_server_swap(used_swap_percent, used_swap_percent_std);
CREATE INDEX idx_svr_swap_hostid_priority   ON embeddings_server_swap(hostid, priority);

-- 벡터 인덱스
CREATE INDEX idx_svr_swap_embedding
    ON embeddings_server_swap
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_swap                       IS '서버 Swap 정보 벡터 테이블 (3일 시계열 누적)';
COMMENT ON COLUMN embeddings_server_swap.hostid                IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_swap.asset                 IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_swap.ip                    IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_swap.asset_name            IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_swap.os                    IS '운영체제';
COMMENT ON COLUMN embeddings_server_swap.system_name           IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_swap.group_name            IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_swap.total_swap_space      IS '전체 Swap 공간 (bytes)';
COMMENT ON COLUMN embeddings_server_swap.free_swap_space       IS '여유 Swap 공간 (bytes)';
COMMENT ON COLUMN embeddings_server_swap.free_swap_space_pct   IS '여유 Swap 비율 (%)';
COMMENT ON COLUMN embeddings_server_swap.used_swap_percent     IS 'Swap 사용률 (%)';
COMMENT ON COLUMN embeddings_server_swap.used_swap_percent_std IS 'Swap 사용률 표준편차 (%)';
COMMENT ON COLUMN embeddings_server_swap.stability_level       IS '안정성 수준 (안정 / 주의 / 경고 / 임계치 초과(위험))';
COMMENT ON COLUMN embeddings_server_swap.priority              IS '우선순위 (0=정상, 값이 클수록 긴급)';
COMMENT ON COLUMN embeddings_server_swap.swap_summary          IS '검색용 텍스트 요약';
COMMENT ON COLUMN embeddings_server_swap.embedding             IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_swap.source_updated_at     IS '원본 데이터 수집 시각 (PK 구성)';
COMMENT ON COLUMN embeddings_server_swap.created_at            IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_swap.updated_at            IS '레코드 수정 시각';

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + source_updated_at 가 존재하면 최신값으로 갱신,
-- 없으면 신규 삽입 → 3일치 누적 이력 유지
--
-- INSERT INTO embeddings_server_swap (
--     hostid, asset, ip, asset_name, os, system_name, group_name,
--     is_active, total_swap_space, free_swap_space, free_swap_space_pct,
--     used_swap_percent, used_swap_percent_std,
--     stability_level, priority,
--     swap_summary, embedding, source_updated_at, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
--     %(is_active)s, %(total_swap_space)s, %(free_swap_space)s, %(free_swap_space_pct)s,
--     %(used_swap_percent)s, %(used_swap_percent_std)s,
--     %(stability_level)s, %(priority)s,
--     %(swap_summary)s, %(embedding)s, %(source_updated_at)s, now()
-- )
-- ON CONFLICT (hostid, source_updated_at) DO UPDATE SET
--     asset                  = EXCLUDED.asset,
--     ip                     = EXCLUDED.ip,
--     asset_name             = EXCLUDED.asset_name,
--     os                     = EXCLUDED.os,
--     system_name            = EXCLUDED.system_name,
--     group_name             = EXCLUDED.group_name,
--     is_active              = EXCLUDED.is_active,
--     total_swap_space       = EXCLUDED.total_swap_space,
--     free_swap_space        = EXCLUDED.free_swap_space,
--     free_swap_space_pct    = EXCLUDED.free_swap_space_pct,
--     used_swap_percent      = EXCLUDED.used_swap_percent,
--     used_swap_percent_std  = EXCLUDED.used_swap_percent_std,
--     stability_level        = EXCLUDED.stability_level,
--     priority               = EXCLUDED.priority,
--     swap_summary           = EXCLUDED.swap_summary,
--     embedding              = EXCLUDED.embedding,
--     updated_at             = now();

-- ============================================================
-- [3일 초과 데이터 정리] 수집기 또는 배치 작업에서 주기적으로 실행
-- ============================================================
-- DELETE FROM embeddings_server_swap
-- WHERE source_updated_at < now() - INTERVAL '3 days';

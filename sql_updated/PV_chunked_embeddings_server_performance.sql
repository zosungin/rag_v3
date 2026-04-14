-- ============================================================
-- Phase B2: 서버 Performance 통합 청킹 테이블 (변경 버전)
-- ============================================================
-- 실행 대상: pgvector DB (rag_db)
-- 접속 정보: host=172.202.0.20, port=5432, database=rag_db
--            user=rag_user, password=rag_password
-- 변경 이력:
--   - [변경] time_bucket 집계 단위: 1시간 → 30분 간격
--            DATE_TRUNC('hour', ...) → DATE_TRUNC('minute', ...) + INTERVAL 계산
--            또는 Python 측에서 30분 단위로 반올림하여 삽입
--   - [추가] UPSERT ON CONFLICT 가이드 (기존 UNIQUE 제약 재활용)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_performance_chunked CASCADE;

CREATE TABLE embeddings_server_performance_chunked (
    id BIGSERIAL PRIMARY KEY,

    -- ========== 호스트 식별 정보 ==========
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),

    -- 활성 여부
    is_active SMALLINT DEFAULT 1,

    -- ========== 시간 정보 ==========
    -- [변경] 30분 단위 집계 시작 시각
    -- Python 변환 예시:
    --   import pandas as pd
    --   time_bucket = pd.Timestamp(source_ts).floor('30min')
    -- SQL 변환 예시:
    --   DATE_TRUNC('hour', ts) + FLOOR(EXTRACT(MINUTE FROM ts) / 30) * INTERVAL '30 minutes'
    time_bucket TIMESTAMP NOT NULL,

    -- ========== 통합 메트릭 데이터 (JSONB) ==========
    cpu_metrics JSONB,      -- CPU 사용률, 코어수, 변동성 등
    mem_metrics JSONB,      -- 전체/사용/가용 메모리, % 등
    swap_metrics JSONB,     -- 스왑 사용량 및 % 등
    fs_metrics JSONB,       -- 파일시스템별 사용률 리스트
    net_metrics JSONB,      -- 인터페이스별 트래픽/상태 요약
    top_processes JSONB,    -- 리소스 과점 프로세스 리스트

    -- RAG 데이터
    performance_summary TEXT NOT NULL,
    embedding vector(768),

    record_count INTEGER DEFAULT 0,             -- 통합된 원본 레코드 수
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    -- UNIQUE: hostid + 30분 단위 time_bucket 조합 → UPSERT 충돌 키
    CONSTRAINT uk_server_perf_chunked_host_time UNIQUE(hostid, time_bucket)
);

-- 인덱스: 상태 필터링 및 시간순 조회 최적화
CREATE INDEX idx_perf_chunked_active     ON embeddings_server_performance_chunked(is_active);
CREATE INDEX idx_perf_chunked_host_time  ON embeddings_server_performance_chunked(hostid, time_bucket DESC);
CREATE INDEX idx_perf_chunked_time       ON embeddings_server_performance_chunked(time_bucket DESC);

-- 벡터 인덱스
CREATE INDEX idx_perf_chunked_embedding
    ON embeddings_server_performance_chunked
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- 테이블 및 컬럼 코멘트
COMMENT ON TABLE  embeddings_server_performance_chunked              IS '서버 6대 리소스 통합 30분 단위 집계 테이블';
COMMENT ON COLUMN embeddings_server_performance_chunked.hostid       IS 'Zabbix 호스트 ID';
COMMENT ON COLUMN embeddings_server_performance_chunked.asset        IS 'Zabbix 등록 자산명';
COMMENT ON COLUMN embeddings_server_performance_chunked.ip           IS '서버 IP 주소';
COMMENT ON COLUMN embeddings_server_performance_chunked.asset_name   IS 'Zabbix 표시 이름';
COMMENT ON COLUMN embeddings_server_performance_chunked.os           IS '운영체제';
COMMENT ON COLUMN embeddings_server_performance_chunked.system_name  IS '서버 hostname';
COMMENT ON COLUMN embeddings_server_performance_chunked.group_name   IS 'Zabbix 등록 그룹명';
COMMENT ON COLUMN embeddings_server_performance_chunked.is_active    IS '장비 활성 여부 (1: 활성, 0: 비활성)';
COMMENT ON COLUMN embeddings_server_performance_chunked.time_bucket  IS '30분 단위 집계 시작 시각 (변경: 1시간 → 30분)';
COMMENT ON COLUMN embeddings_server_performance_chunked.cpu_metrics  IS 'CPU 통합 지표 JSON (사용률, 코어수, 변동성 등)';
COMMENT ON COLUMN embeddings_server_performance_chunked.mem_metrics  IS '메모리 통합 지표 JSON';
COMMENT ON COLUMN embeddings_server_performance_chunked.swap_metrics IS 'Swap 통합 지표 JSON';
COMMENT ON COLUMN embeddings_server_performance_chunked.fs_metrics   IS '파일시스템 통합 지표 JSON (마운트별 리스트)';
COMMENT ON COLUMN embeddings_server_performance_chunked.net_metrics  IS '네트워크 인터페이스 통합 지표 JSON';
COMMENT ON COLUMN embeddings_server_performance_chunked.top_processes IS '리소스 과점 프로세스 Top-N JSON';
COMMENT ON COLUMN embeddings_server_performance_chunked.performance_summary IS '검색용 텍스트 요약 (RAG 입력)';
COMMENT ON COLUMN embeddings_server_performance_chunked.embedding    IS '768차원 임베딩 벡터 (nomic-embed-text-v2-moe)';
COMMENT ON COLUMN embeddings_server_performance_chunked.record_count IS '30분 구간 내 통합된 원본 레코드 수';
COMMENT ON COLUMN embeddings_server_performance_chunked.created_at   IS '레코드 생성 시각';
COMMENT ON COLUMN embeddings_server_performance_chunked.updated_at   IS '레코드 수정 시각';

-- ============================================================
-- [time_bucket 30분 단위 변환 예시]
-- ============================================================
-- [SQL 변환]
--   SELECT
--     DATE_TRUNC('hour', source_updated_at)
--     + FLOOR(EXTRACT(MINUTE FROM source_updated_at) / 30)::INT
--       * INTERVAL '30 minutes' AS time_bucket
--   FROM embeddings_server_cpu
--   WHERE hostid = 10084;
--
-- [Python 변환]
--   import pandas as pd
--   time_bucket = pd.Timestamp(source_updated_at).floor('30min')
--
-- [예시 결과]
--   source_updated_at = 2026-03-05 16:47:00  → time_bucket = 2026-03-05 16:30:00
--   source_updated_at = 2026-03-05 16:03:00  → time_bucket = 2026-03-05 16:00:00

-- ============================================================
-- [UPSERT 가이드] Python 수집기에서 사용할 INSERT ... ON CONFLICT
-- ============================================================
-- 동일 hostid + time_bucket(30분) 이 존재하면 집계값 갱신,
-- 없으면 신규 삽입
--
-- INSERT INTO embeddings_server_performance_chunked (
--     hostid, asset, ip, asset_name, os, system_name, group_name, is_active,
--     time_bucket,
--     cpu_metrics, mem_metrics, swap_metrics, fs_metrics, net_metrics, top_processes,
--     performance_summary, embedding, record_count, updated_at
-- )
-- VALUES (
--     %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
--     %(time_bucket)s,
--     %(cpu_metrics)s::jsonb, %(mem_metrics)s::jsonb, %(swap_metrics)s::jsonb,
--     %(fs_metrics)s::jsonb, %(net_metrics)s::jsonb, %(top_processes)s::jsonb,
--     %(performance_summary)s, %(embedding)s, %(record_count)s, now()
-- )
-- ON CONFLICT (hostid, time_bucket) DO UPDATE SET
--     asset               = EXCLUDED.asset,
--     ip                  = EXCLUDED.ip,
--     asset_name          = EXCLUDED.asset_name,
--     os                  = EXCLUDED.os,
--     system_name         = EXCLUDED.system_name,
--     group_name          = EXCLUDED.group_name,
--     is_active           = EXCLUDED.is_active,
--     cpu_metrics         = EXCLUDED.cpu_metrics,
--     mem_metrics         = EXCLUDED.mem_metrics,
--     swap_metrics        = EXCLUDED.swap_metrics,
--     fs_metrics          = EXCLUDED.fs_metrics,
--     net_metrics         = EXCLUDED.net_metrics,
--     top_processes       = EXCLUDED.top_processes,
--     performance_summary = EXCLUDED.performance_summary,
--     embedding           = EXCLUDED.embedding,
--     record_count        = EXCLUDED.record_count,
--     updated_at          = now();

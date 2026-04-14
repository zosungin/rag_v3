-- ============================================================
-- Phase B1: AIX ERRPT 로그 임베딩 테이블
-- ============================================================
-- 목적: svr_log_errpt의 ERRPT 정보를 개별 행 단위로 임베딩
-- 소스: svr_log_errpt (Zabbix DB)
-- 생성일: 2026-02-19
-- DB: rag_db (172.202.0.20:5432)
--
-- 보존 정책: flag='n' & chunked_at IS NOT NULL → 삭제
-- 패턴: svr_log_messages와 동일
-- ============================================================
-- Phase B1: AIX ERRPT 로그 임베딩 테이블 (최종 표준화 버전)
-- ============================================================
-- Phase B1: AIX ERRPT 로그 정보 벡터화 테이블 생성 (표준화 버전)
-- ============================================================

-- ============================================================
-- Phase B1: AIX ERRPT 로그 정보 벡터화 테이블 생성 (표준화 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_errpt CASCADE;

CREATE TABLE embeddings_server_errpt (
    -- 기본 정보 및 PK
    uid TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, -- Zabbix clock 기반
    PRIMARY KEY (uid, source_updated_at),
    
    -- 호스트 정보
    hostid BIGINT NOT NULL,
    asset VARCHAR(50),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(100),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 장비 활성 여부
    is_active SMALLINT DEFAULT 1, 
    
    -- ERRPT 상세 정보 (원본 svr_log_errpt 매핑)
    itemid BIGINT,
    identifier VARCHAR(200),
    errpt_timestamp VARCHAR(50), -- 원본의 timestamp 컬럼 데이터 저장
    type VARCHAR(50),
    class VARCHAR(50),
    resource_name VARCHAR(200),
    description TEXT,
    flag VARCHAR(10),
    
    -- 자동 분류 및 RAG 데이터
    severity_level VARCHAR(20),
    error_category VARCHAR(20),
    errpt_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- ========== 메타데이터 (표준 통일) ==========
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_errpt_active ON embeddings_server_errpt(is_active);
CREATE INDEX idx_errpt_embedding ON embeddings_server_errpt USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

COMMENT ON TABLE embeddings_server_errpt IS 'AIX ERRPT 로그 임베딩 테이블 (표준화 버전)';

DO $$
BEGIN
    RAISE NOTICE '✅ embeddings_server_errpt 테이블 생성 완료';
END $$;
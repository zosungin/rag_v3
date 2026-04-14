-- ============================================================
-- Phase B1: 서버 연결 상태 임베딩 테이블
-- ============================================================
-- 목적: svr_connections의 연결 정보를 개별 행 단위로 임베딩
-- 소스: svr_connections (Zabbix DB)
-- 생성일: 2026-02-19
-- DB: rag_db (172.202.0.20:5432)
--
-- 보존 정책:
--   - flag='y' (활성 연결): 보존
--   - flag='n' (종료된 연결): 청킹 완료 후 삭제 (용량 관리)
-- ============================================================

-- ============================================================
-- Phase B1: 서버 연결 상태 임베딩 테이블 (표준화 버전)
-- ============================================================

-- ============================================================
-- Phase B1: 서버 연결 상태 임베딩 테이블 (표준화 버전)
-- ============================================================

-- 1. 기존 테이블 삭제
-- ============================================================
-- Phase B1: 서버 연결 상태 임베딩 테이블 (최종 표준화 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_connections CASCADE;

CREATE TABLE embeddings_server_connections (
    -- 🔑 PK: uid (원본과 동일)
    uid VARCHAR(200) PRIMARY KEY,
    
    -- 호스트 정보
    hostid BIGINT,
    asset VARCHAR(200),
    asset_ip VARCHAR(50),
    os VARCHAR(100),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 연결 상세 정보 (svr_connections 매핑)
    protocol VARCHAR(10),
    local_ip VARCHAR(50),
    local_port INTEGER,
    foreign_ip VARCHAR(50),
    foreign_port INTEGER,
    source_ip VARCHAR(50),
    source_port INTEGER,
    target_ip VARCHAR(50),
    target_port INTEGER,
    service_name VARCHAR(200),
    direction VARCHAR(20),       -- INBOUND / OUTBOUND
    flag VARCHAR(10),            -- y: 활성, n: 종료
    
    -- RAG용 요약 및 벡터
    connection_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- ========== 메타데이터 (표준 통일) ==========
    source_updated_at TIMESTAMPTZ NOT NULL, -- 원본 clock 기반
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 인덱스 및 코멘트 설정
CREATE INDEX idx_conn_active ON embeddings_server_connections(is_active);
CREATE INDEX idx_conn_embedding ON embeddings_server_connections USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);
COMMENT ON TABLE embeddings_server_connections IS '서버 연결 상태 임베딩 테이블 (표준화 버전)';

DO $$
BEGIN
    RAISE NOTICE '✅ embeddings_server_connections 테이블 생성 완료';
    RAISE NOTICE 'PK: uid';
END $$;
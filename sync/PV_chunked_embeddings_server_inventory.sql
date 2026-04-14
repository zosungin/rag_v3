-- ============================================================
-- Phase B2: 서버/자산 통합 인벤토리 청킹 테이블 (해시 감지 최적화 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_infra_inventory_chunked CASCADE;

CREATE TABLE embeddings_infra_inventory_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- 🔑 호스트 식별 정보 (호스트당 1개의 최신 종합 진단 레코드만 유지)
    hostid BIGINT UNIQUE NOT NULL,
    asset_name VARCHAR(255),
    ip VARCHAR(50),
    is_active SMALLINT DEFAULT 1, -- 1=활성, 0=비활성
    
    -- 📊 분야별 요약 데이터 (NULL 방지를 위해 분리 보관)
    specs TEXT,             -- CPU 코어, OS 정보, 메모리 용량 등 사양 요약
    storage_summary TEXT,   -- 파일시스템별 사용률 요약 (중복 제거됨)
    port_summary TEXT,      -- 오픈된 리슨 포트 목록 요약 (핵심만 추출)
    recent_alerts TEXT,     -- 최근 7일간 발생한 주요 장애 명칭 및 횟수
    
    -- 🤖 RAG 전용 통합 텍스트 및 벡터
    -- 포맷: "[서버 종합 진단] 자산명: [명칭] | 상태: 활성 | OS: [OS] | 사양: [사양] | 스토리지: [스토리지] | 오픈 포트: [포트] | 최근 7일 장애: [장애이력]"
    inventory_summary TEXT NOT NULL,
    summary_hash VARCHAR(64),     -- 💡 [핵심 추가] 텍스트 변경 감지용 SHA-256 해시
    embedding vector(768),        -- nomic-embed-text-v2-moe 규격
    
    -- 📅 메타데이터
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 검색 및 필터링 최적화를 위한 인덱스
CREATE INDEX idx_infra_inventory_active ON embeddings_infra_inventory_chunked(is_active);
CREATE INDEX idx_infra_inventory_updated ON embeddings_infra_inventory_chunked(updated_at DESC);
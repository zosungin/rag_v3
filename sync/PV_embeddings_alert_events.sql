-- 1. 기존 테이블 및 관련 객체 삭제 (초기화)
DROP TABLE IF EXISTS embeddings_alert_events CASCADE;

-- 2. RAG 상태 갱신(Upsert)용 벡터 테이블 생성
CREATE TABLE embeddings_alert_events (
    
    -- 🔑 PK: triggerid 단독 
    -- (동일 장애 이벤트는 1건만 존재하며, 상태 변경 시 이 Row를 Update)
    triggerid BIGINT PRIMARY KEY,
    
    -- 기본 정보
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    name TEXT,
    
    -- 호스트 정보
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 장애 상태 (장애 해결 시 Update 됨)
    value INTEGER,       -- 0=Resolved, 1=Problem
    flag VARCHAR(10),    -- 'y'=발생중, 'n'=해결됨
    priority INTEGER,    -- 0~5
	tags TEXT, 
	item_key TEXT,
    
    -- 이벤트 유형
    event_type VARCHAR(20) DEFAULT 'unknown',
    
    -- 시간 정보 (장애 해결 시 resolved_clock, duration_minutes Update 됨)
    event_clock INTEGER NOT NULL,
    resolved_clock INTEGER,
    duration_minutes INTEGER,
    
    -- 검색 요약 및 벡터 데이터 (장애 내용 변경 시 Update 됨)
    alert_summary TEXT,
    embedding vector(768),
    
    -- ✅ 메타데이터 (통일됨)
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 3. B-Tree 일반 인덱스 (하이브리드 검색 시 WHERE 절 필터링에 핵심적인 것만 유지)
CREATE INDEX idx_alert_hostid ON embeddings_alert_events(hostid);
CREATE INDEX idx_alert_event_clock ON embeddings_alert_events(event_clock DESC);
CREATE INDEX idx_alert_value_flag ON embeddings_alert_events(value, flag);
CREATE INDEX idx_alert_event_type ON embeddings_alert_events(event_type);
CREATE INDEX idx_alert_priority ON embeddings_alert_events(priority);
CREATE INDEX idx_alert_group_name ON embeddings_alert_events(group_name);
CREATE INDEX idx_alert_source_updated_at ON embeddings_alert_events(source_updated_at DESC); -- 추가됨
CREATE INDEX idx_alert_updated_at ON embeddings_alert_events(updated_at DESC);

-- 4. 벡터 인덱스 
-- (pgvector 0.5.0 이상 기준, ivfflat보다 검색 품질과 속도가 뛰어난 hnsw 적용)
CREATE INDEX idx_alert_embedding 
ON embeddings_alert_events 
USING hnsw (embedding vector_cosine_ops);
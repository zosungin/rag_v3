DROP TABLE IF EXISTS embeddings_alerts_chunked CASCADE;

CREATE TABLE embeddings_alerts_chunked (
    id BIGSERIAL PRIMARY KEY,
    
    -- 호스트 정보
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 💡 요약 기준 일자 (이 복합키로 하루 1건만 유지)
    target_date DATE NOT NULL,
    
    -- 통계 지표 (JSONB)
    total_alerts INTEGER DEFAULT 0,
    alert_metrics JSONB,  -- 예: {"log_alerts": 3, "system_alerts": 1, "unresolved": 0}
    
    -- RAG 데이터 (일간 브리핑 텍스트)
    alert_summary TEXT NOT NULL,
    embedding vector(768),
    
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    
    -- 호스트별 하루 1개의 브리핑만 유지 (Upsert용)
    CONSTRAINT uk_alerts_daily_summary UNIQUE(hostid, target_date)
);

CREATE INDEX idx_alerts_chunked_date ON embeddings_alerts_chunked(target_date DESC);
CREATE INDEX idx_alerts_chunked_host ON embeddings_alerts_chunked(hostid);
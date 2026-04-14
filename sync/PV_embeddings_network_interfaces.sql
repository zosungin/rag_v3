-- ============================================================
-- Phase B1 Step 2.1: 네트워크 인터페이스 정보 벡터화 테이블 생성 (표준화)
-- ============================================================

DROP TABLE IF EXISTS embeddings_network_interfaces CASCADE;

CREATE TABLE embeddings_network_interfaces (
    -- 🔑 PK: 인터페이스 고유 ID (asset + itemid 기반 uid)
    uid VARCHAR(100) ,
    
    hostid BIGINT NOT NULL,
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(255),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 인터페이스 상세 정보
    interface VARCHAR(255),
    port_alias VARCHAR(255),
    port_status VARCHAR(50), -- Link Up / Link Down
    link SMALLINT,           -- 1=Up, 0=Down
    type VARCHAR(200),
    port_speed BIGINT,
    
    -- 트래픽 및 사용률 지표
    received_bits BIGINT,
    sent_bits BIGINT,
    received_rate NUMERIC(15,2),
    sent_rate NUMERIC(15,2),
    received_rate_std NUMERIC(10,2),
    sent_rate_std NUMERIC(10,2),
    stability_level VARCHAR(20),
    priority INTEGER,
	
    -- 장비 및 포트 활성 상태 (1=Active, 0=Inactive)
    is_active SMALLINT DEFAULT 1,

    -- RAG 데이터
    interface_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- ========== 메타데이터 (표준 통일) ==========
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (uid, source_updated_at)
);

CREATE INDEX idx_nw_if_hostid ON embeddings_network_interfaces(hostid);
CREATE INDEX idx_nw_if_active ON embeddings_network_interfaces(is_active);
CREATE INDEX idx_nw_if_embedding ON embeddings_network_interfaces USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);
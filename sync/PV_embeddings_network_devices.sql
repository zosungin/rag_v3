-- ============================================================
-- Phase B1 Step 3.1: 네트워크 장비 정보 벡터화 테이블 (해시 감지 고도화 버전)
-- ============================================================

DROP TABLE IF EXISTS embeddings_network_devices CASCADE;

CREATE TABLE embeddings_network_devices (
    -- 🔑 PK: hostid (Zabbix 고유 ID, 최신 상태 유지용)
    hostid BIGINT PRIMARY KEY,
    
    -- ========== 장비 기본 정보 ==========
    group_name VARCHAR(255),
    asset VARCHAR(255),
    asset_name VARCHAR(255),
    device_name VARCHAR(255),
    ip VARCHAR(50),

    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,
    
    -- ========== 상태 및 연결 정보 ==========
    agent_status VARCHAR(50),
    snmp_agent VARCHAR(50),
    
    -- ========== 시스템 상세 정보 ==========
    communication_name VARCHAR(255),
    template_name VARCHAR(255),
    ios VARCHAR(255),
    system_description TEXT,
    uptime VARCHAR(100),
    icmp_ping VARCHAR(50),
    
    -- ========== RAG 지식 데이터 ==========
    device_summary TEXT NOT NULL,
    summary_hash VARCHAR(64),     -- 💡 [핵심 추가] 텍스트 변경 감지용 SHA-256 해시
    embedding vector(768),
    
    -- ========== 메타데이터 ==========
    source_updated_at TIMESTAMPTZ NOT NULL, -- 원본 Zabbix 업데이트 시각
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 검색 및 필터링 최적화를 위한 B-Tree 인덱스
CREATE INDEX idx_network_is_active ON embeddings_network_devices(is_active);
CREATE INDEX idx_network_group_name ON embeddings_network_devices(group_name);
CREATE INDEX idx_network_updated_at ON embeddings_network_devices(updated_at DESC);
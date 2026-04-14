-- ============================================================
-- Phase B1 Step 3.1: 서버 파일시스템 리소스 벡터화 테이블 생성 (최종 표준화)
-- ============================================================

DROP TABLE IF EXISTS embeddings_server_filesystems CASCADE;

CREATE TABLE embeddings_server_filesystems (
    -- 🔑 PK: hostid + 파일시스템명 (최신 상태 유지)
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

    -- 파일시스템 상세 정보 (svr_filesystem_std 매핑)
    total_size NUMERIC(12,1),
    used_size NUMERIC(12,1),
    used_percent NUMERIC(5,1),
    used_percent_std NUMERIC(5,2),
    stability_level VARCHAR(20),
    priority INTEGER,  
    -- RAG 데이터
    fs_summary TEXT NOT NULL,
    embedding vector(768),
    
    -- ========== 메타데이터 (표준 통일) ==========
    source_updated_at TIMESTAMPTZ NOT NULL, -- 원본 updated_at 기반
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, fs_name, source_updated_at)
);

-- 인덱스 생성
CREATE INDEX idx_fs_active ON embeddings_server_filesystems(is_active);
CREATE INDEX idx_fs_embedding ON embeddings_server_filesystems USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

COMMENT ON TABLE embeddings_server_filesystems IS '서버 파일시스템 리소스 상태 및 점유율 임베딩 테이블';


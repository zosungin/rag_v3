-- 1. 기존 테이블 삭제
DROP TABLE IF EXISTS embeddings_server_listen_ports;

-- 2. 테이블 생성 (이미지 순서 + 소스 제약조건 반영)
CREATE TABLE embeddings_server_listen_ports (
    -- 기본 정보 (이미지 상단 순서)
    asset             character varying(200) NOT NULL,
    protocol          character varying(10) NOT NULL CHECK (protocol IN ('tcp', 'udp')),
    listen_port       integer NOT NULL CHECK (listen_port BETWEEN 1 AND 65535),
    bind_ip           inet NOT NULL, -- 이미지의 0.0.0.0/32 형태 반영을 위해 inet 권장
    hostid            bigint,
    server_ip         inet,
    os                character varying(100), -- LINUX, WINDOWS 등
    system_name       character varying,
    group_name        character varying,
    bind_type         character varying(20) CHECK (bind_type IN ('all', 'localhost', 'specific')),
    ip_version        character varying(10) CHECK (ip_version IN ('ipv4', 'ipv6')),
    status            character varying(20) CHECK (status IN ('active', 'inactive')),
    
    -- 상태 및 시간 (이미지 하단 순서)
    is_active         smallint DEFAULT 1, -- 이미지의 123(숫자) 표시 반영
    first_seen_at     TIMESTAMPTZ without time zone,
    last_seen_at      TIMESTAMPTZ without time zone,
    
    -- 텍스트 요약 및 임베딩
    port_summary      text NOT NULL,
    embedding         vector(768), -- pgvector 확장 필요
    
    -- 메타데이터
    source_updated_at TIMESTAMPTZ NOT NULL, 
    created_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- 중복 방지 제약 조건 (소스 테이블 기준)
    UNIQUE (asset, protocol, listen_port, bind_ip)
) WITH (fillfactor = 85);

---

### 3. 필수 인덱스 설정

-- 자산 및 호스트 식별을 위한 인덱스
CREATE INDEX idx_eslp_asset_host ON embeddings_server_listen_ports (asset, hostid);

-- 특정 포트 및 프로토콜 서비스 조회용
CREATE INDEX idx_eslp_port_service ON embeddings_server_listen_ports (listen_port, protocol);

-- 업데이트 시점 기준 정렬 및 관리용
CREATE INDEX idx_eslp_source_upd ON embeddings_server_listen_ports (source_updated_at DESC);

-- 벡터 검색 성능 최적화 (Cosine Similarity 기준)
-- 인덱스 생성 전: CREATE EXTENSION IF NOT EXISTS vector;
CREATE INDEX idx_eslp_embedding_cosine ON embeddings_server_listen_ports 
USING hnsw (embedding vector_cosine_ops);
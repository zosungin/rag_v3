-- ============================================================
-- Phase B1: 서버별 프로세스 자원 정보 원본 임베딩 테이블
-- ============================================================

DROP TABLE IF EXISTS embeddings_process_resources CASCADE;

CREATE TABLE embeddings_process_resources (
    -- 🔑 PK 설정: 호스트ID + 프로세스ID + 수집시각 조합 (이력 보존 및 고유성 확보)
    hostid BIGINT NOT NULL,
    pid INTEGER NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL, -- Zabbix clock 기반 수집 시각

    -- 호스트 식별 정보
    asset VARCHAR(255),
    ip VARCHAR(50),
    asset_name VARCHAR(255),
    os VARCHAR(100),
    system_name VARCHAR(255),
    group_name VARCHAR(255),
    
    -- 장비 활성 여부 (1=active, 0=inactive)
    is_active SMALLINT DEFAULT 1,

    -- 프로세스 상세 정보 및 자원 지표
    user_name VARCHAR(100),     -- 프로세스 실행 사용자
    command TEXT,               -- 실행 명령줄 (전체 커맨드)
    cpu NUMERIC(6,2),           -- CPU 사용률 (%)
    cpu_time BIGINT,            -- CPU 점유 시간
    mem NUMERIC(6,2),           -- 메모리 점유율 (%)
    mem_used BIGINT,            -- 실제 사용 메모리량 (Byte/KB 등)
    resource_std NUMERIC(6,2),  -- 리소스 변동 표준편차 (필요시)
    stability_level VARCHAR(20),
    priority INTEGER, 
    -- RAG 데이터
    process_summary TEXT NOT NULL, -- "사용자 root가 실행한 java 프로세스 (CPU: 15%)" 등
    embedding vector(768),         -- 768차원 벡터 데이터
    
    -- 메타데이터
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
	
	-- 핵심 수정 사항: 고유키를 서버 ID + 수집 시각으로 설정하여 누적 가능하게 함
    PRIMARY KEY (hostid, pid, source_updated_at)
);

-- 검색 성능 향상을 위한 인덱스
CREATE INDEX idx_proc_res_hostid ON embeddings_process_resources(hostid);
CREATE INDEX idx_proc_res_time ON embeddings_process_resources(source_updated_at DESC);
CREATE INDEX idx_proc_res_cpu ON embeddings_process_resources(cpu DESC);
CREATE INDEX idx_proc_res_mem ON embeddings_process_resources(mem DESC);
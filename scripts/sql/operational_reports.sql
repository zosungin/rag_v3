-- RAG DB: 통합 운영 레포트 전용 (chat_sessions / chat_messages 와 무관한 별도 테이블)
-- 앱 startup 시에도 동일 DDL이 적용됩니다. 수동 실행용.
CREATE TABLE IF NOT EXISTS operational_reports (
    id            BIGSERIAL PRIMARY KEY,
    title         VARCHAR(300) NOT NULL,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_operational_reports_created
ON operational_reports(created_at DESC);

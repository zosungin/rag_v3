# /app/app/main.py
"""
Zabbix RAG-LLM FastAPI Server
메인 진입점 - 라우터 등록 및 앱 설정
v6.0: Agent 전용 warmup (Ollama 모델만 사전 로드)
"""

import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# 라우터 import
from app.routers import chat, search, stats, reports
from app.routers.health import router as health_router          # ← 추가
from app.routers.data_scope import router as data_scope_router
from app.config import LLM_MODEL, AVAILABLE_MODELS, OLLAMA_API_URL, APP_ROOT
from app.db.connection import get_pool, close_pool
from app.services.tool_executor import get_zabbix_pool, close_zabbix_pool

# ── 로그 레벨 강제 INFO 설정 (uvicorn 기본값 WARNING 우회) ──
logging.basicConfig(level=logging.INFO)
for name in ("app.routers.chat", "app.services.agent", "app.routers.search"):
    logging.getLogger(name).setLevel(logging.INFO)

app = FastAPI(title="Zabbix RAG-LLM API")

# 템플릿·정적 경로: APP_ROOT 또는 패키지 상위 디렉터리 (배포: /app/rag/fastapi)
_BASE = Path(APP_ROOT).resolve() if Path(APP_ROOT).is_dir() else Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

# 라우터 등록
app.include_router(chat.router)
app.include_router(search.router)
app.include_router(stats.router)
app.include_router(health_router)                               # ← 추가
app.include_router(data_scope_router)
app.include_router(reports.router)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """메인 대시보드 페이지"""
    return templates.TemplateResponse("index.html", {"request": request})


# ============================================================
# Agent 전용 Warmup — Ollama 모델만 사전 로드 (Fix 8)
# ============================================================
async def warmup_agent():
    """
    Ollama 모델이 기본 LLM으로 설정된 경우 사전 로드.
    외부 API(Groq/Gemini) 모델은 warmup 불필요 — 스킵.
    """
    import httpx
    import logging
    logger = logging.getLogger(__name__)

    backend = AVAILABLE_MODELS.get(LLM_MODEL, {}).get("backend", "")
    if backend != "ollama":
        logger.info(f"[Warmup] 기본 모델 {LLM_MODEL} (backend={backend}) — warmup 스킵")
        return

    try:
        logger.info(f"[Warmup] Ollama 모델 사전 로드 시작: {LLM_MODEL}")
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{OLLAMA_API_URL}/api/generate",
                json={
                    "model": LLM_MODEL,
                    "prompt": "hi",
                    "stream": False,
                    "options": {"num_predict": 1, "num_ctx": 512},
                }
            )
            if resp.status_code == 200:
                logger.info(f"[Warmup] Ollama 모델 로드 완료: {LLM_MODEL}")
            else:
                logger.warning(f"[Warmup] Ollama 응답 이상 (status={resp.status_code})")
    except Exception as e:
        logger.warning(f"[Warmup] Ollama 사전 로드 실패 (서비스에 영향 없음): {e}")


@app.on_event("startup")
async def startup_event():
    print("=" * 60)
    print("  Zabbix RAG 시스템 시작 (v6.0 - Agentic RAG)")
    print("=" * 60)
    print(f"  LLM 모델   : {LLM_MODEL}")
    print(f"  백엔드     : {AVAILABLE_MODELS.get(LLM_MODEL, {}).get('backend', 'unknown')}")
    print("  API 문서   : http://0.0.0.0:8000/docs")
    print("  대시보드   : http://0.0.0.0:8000/")
    print("=" * 60)

    # ── 대화 이력 테이블 자동 생성 ──
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id  VARCHAR(64) PRIMARY KEY,
                    title       VARCHAR(200),
                    created_at  TIMESTAMP DEFAULT NOW(),
                    updated_at  TIMESTAMP DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id          SERIAL PRIMARY KEY,
                    session_id  VARCHAR(64) REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
                    query       TEXT NOT NULL,
                    result_data JSONB,
                    sources     JSONB,
                    model_info  JSONB,
                    created_at  TIMESTAMP DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                ON chat_messages(session_id, created_at);
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
                ON chat_sessions(updated_at DESC);
            """)
            # 운영 레포트 전용 — chat_sessions / chat_messages 와 별도 테이블 (대화 이력과 무관)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS operational_reports (
                    id            BIGSERIAL PRIMARY KEY,
                    title         VARCHAR(300) NOT NULL,
                    period_start  TIMESTAMPTZ NOT NULL,
                    period_end    TIMESTAMPTZ NOT NULL,
                    payload       JSONB NOT NULL,
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_operational_reports_created
                ON operational_reports(created_at DESC);
            """)
        print("  [DB] chat_sessions / chat_messages / operational_reports 테이블 확인 완료")
    except Exception as e:
        print(f"  [DB] 테이블 생성 실패 (수동 생성 필요): {e}")

    # ── Zabbix DB 커넥션 풀 사전 초기화 ──
    try:
        await get_zabbix_pool()
        print("  [ZabbixDB] 커넥션 풀 초기화 완료")
    except Exception as e:
        print(f"  [ZabbixDB] 커넥션 풀 초기화 실패 (서비스 시작 후 재시도): {e}")

    # Fix 8: Agent 전용 warmup (Ollama 모델만)
    await warmup_agent()


@app.on_event("shutdown")
async def shutdown_event():
    """앱 종료 시 커넥션 풀 정리"""
    await close_pool()
    await close_zabbix_pool()
    print("  [DB] 커넥션 풀 종료 완료")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

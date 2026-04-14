# /app/app/routers/health.py
"""
Health Status API
- RAG Engine  : agent.py 모듈 import 가능 여부 + AGENT_ENABLED 플래그
- Vector DB   : RAG_DB 커넥션 체크 (SELECT 1)
- Agent Mesh  : 외부 API 키 존재 여부 (OpenAI)
"""

import logging
import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import (
    RAG_DB,
    AGENT_ENABLED,
    OPENAI_API_KEY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"])


# ── 1. RAG Engine 체크 ──────────────────────────────────────
def _check_rag_engine() -> dict:
    """
    agent.py 의 run_agent 함수가 정상적으로 import 가능하고
    AGENT_ENABLED 플래그가 True 인지 확인.
    """
    try:
        from app.services.agent import run_agent  # noqa: F401
        if not AGENT_ENABLED:
            return {"status": "degraded", "label": "Disabled", "detail": "AGENT_ENABLED=False"}
        return {"status": "healthy", "label": "Healthy", "detail": "agent.run_agent 로드 정상"}
    except ImportError as e:
        return {"status": "error", "label": "Error", "detail": f"Import 실패: {e}"}
    except Exception as e:
        return {"status": "error", "label": "Error", "detail": str(e)}


# ── 2. Vector DB 체크 ───────────────────────────────────────
async def _check_vector_db() -> dict:
    """
    RAG_DB 설정으로 asyncpg 커넥션을 열고 SELECT 1 실행.
    3초 타임아웃 적용.
    """
    try:
        import asyncpg
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=RAG_DB["host"],
                port=RAG_DB["port"],
                database=RAG_DB["database"],
                user=RAG_DB["user"],
                password=RAG_DB["password"],
            ),
            timeout=3.0,
        )
        await conn.fetchval("SELECT 1")
        await conn.close()
        return {"status": "optimized", "label": "Optimized", "detail": "PostgreSQL 연결 정상"}
    except asyncio.TimeoutError:
        return {"status": "degraded", "label": "Timeout", "detail": "DB 연결 타임아웃 (3s)"}
    except Exception as e:
        return {"status": "error", "label": "Error", "detail": str(e)}


# ── 3. Agent Mesh 체크 ──────────────────────────────────────
def _check_agent_mesh() -> dict:
    """
    config.py 에 정의된 외부 API 키 존재 여부 확인.
    키가 비어있지 않으면 유효한 것으로 간주.
    """
    apis = {
        "OpenAI": OPENAI_API_KEY,
    }
    active = [name for name, key in apis.items() if key and len(key.strip()) > 10]
    inactive = [name for name in apis if name not in active]

    if active:
        return {
            "status": "active",
            "label": "Active",
            "detail": "외부 LLM API 키가 환경 변수에서 로드됨 (" + ", ".join(active) + ")",
        }
    else:
        return {
            "status": "error",
            "label": "No Keys",
            "detail": (
                "OPENAI_API_KEY가 비어 있습니다. "
                "서버에서 /app/rag/fastapi/.env 에 OPENAI_API_KEY=sk-... 를 넣은 뒤 "
                "rag-fastapi 컨테이너를 재시작하세요 (docker restart rag-fastapi)."
            ),
        }


# ── 엔드포인트 ───────────────────────────────────────────────
@router.get("/detail")
async def health_check():
    """
    세 항목을 병렬 체크하여 반환.
    {
      "rag_engine":  {"status": "healthy",   "label": "Healthy",   "detail": "..."},
      "vector_db":   {"status": "optimized", "label": "Optimized", "detail": "..."},
      "agent_mesh":  {"status": "active",    "label": "Active",    "detail": "..."}
    }

    status 값 →  healthy / optimized / active / degraded / error
    """
    rag_result, db_result = await asyncio.gather(
        asyncio.to_thread(_check_rag_engine),
        _check_vector_db(),
    )
    mesh_result = _check_agent_mesh()

    result = {
        "rag_engine": rag_result,
        "vector_db":  db_result,
        "agent_mesh": mesh_result,
    }
    logger.info(f"[Health] {result}")
    return JSONResponse(content=result)

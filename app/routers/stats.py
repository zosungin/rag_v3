# /app/app/routers/stats.py
"""
통계 및 헬스체크 API 엔드포인트
v2.0: get_sync_connection(psycopg2) → async get_pool().acquire() 전환
      이벤트 루프 블로킹 제거
"""

from fastapi import APIRouter, HTTPException
import httpx
import logging
from datetime import datetime

from app.db.connection import get_pool
from app.db.tables import VECTOR_TABLES
from app.config import OLLAMA_TAGS_URL

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/stats")
async def get_statistics():
    """벡터 DB 통계 정보"""
    stats = {}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            for table_key, table_info in VECTOR_TABLES.items():
                try:
                    count = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {table_info['name']}"
                    )
                    stats[table_key] = {
                        "table_name": table_info['name'],
                        "count": count,
                        "weight": table_info['weight']
                    }
                except Exception as te:
                    logger.warning(f"[Stats] 테이블 {table_info['name']} 조회 실패: {te}")
                    stats[table_key] = {
                        "table_name": table_info['name'],
                        "count": -1,
                        "weight": table_info['weight']
                    }
    except Exception as e:
        logger.error(f"[Stats] DB 연결 실패: {e}")
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {str(e)}")

    return {"tables": stats, "timestamp": datetime.now().isoformat()}


@router.get("/api/health")
async def health_check():
    """헬스 체크"""
    db_status = "disconnected"
    ollama_status = "disconnected"

    # DB 연결 확인 (async Pool)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception as e:
        logger.warning(f"[Health] DB 연결 실패: {e}")

    # Ollama 연결 확인 (httpx async)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(OLLAMA_TAGS_URL)
            ollama_status = "connected" if resp.status_code == 200 else "disconnected"
    except Exception:
        pass

    if db_status == "disconnected":
        raise HTTPException(
            status_code=503,
            detail=f"Service unhealthy: DB={db_status}, Ollama={ollama_status}"
        )

    return {
        "status": "healthy",
        "database": db_status,
        "ollama": ollama_status,
        "timestamp": datetime.now().isoformat()
    }

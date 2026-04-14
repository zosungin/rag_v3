# /app/app/db/connection.py
"""
데이터베이스 커넥션 관리
- asyncpg 커넥션 풀
- psycopg2 동기 연결
"""

import asyncpg
import psycopg2
import logging
from app.config import RAG_DB, RAG_DB_CONFIG

logger = logging.getLogger(__name__)

# asyncpg 커넥션 풀 (전역)
_pool = None


async def get_pool() -> asyncpg.Pool:
    """asyncpg 커넥션 풀 반환 (싱글톤)"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=RAG_DB['host'],
            port=RAG_DB['port'],
            database=RAG_DB['database'],
            user=RAG_DB['user'],
            password=RAG_DB['password'],
            min_size=3,
            max_size=20,      # ISS-09: 고부하 대비 상향 (2→3 min, 10→20 max)
            max_inactive_connection_lifetime=300,  # 5분 미사용 연결 해제
        )
        logger.info("asyncpg 커넥션 풀 생성 완료 (min=3, max=20)")
    return _pool


async def close_pool():
    """커넥션 풀 종료"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("asyncpg 커넥션 풀 종료")


async def get_async_connection() -> asyncpg.Connection:
    """단일 async 커넥션 반환"""
    return await asyncpg.connect(**RAG_DB)


def get_sync_connection():
    """psycopg2 동기 커넥션 반환"""
    return psycopg2.connect(**RAG_DB_CONFIG)

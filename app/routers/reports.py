# /app/app/routers/reports.py
"""통합 운영 레포트 API — 생성·목록·조회·삭제.

저장소: RAG DB의 `operational_reports` 테이블만 사용합니다.
대화 이력(`chat_sessions` / `chat_messages`)과 테이블·API가 완전히 분리되어 있습니다.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.connection import get_pool
from app.services.operational_report import build_operational_report_payload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])


def _parse_iso(dt: str) -> datetime:
    s = dt.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


class GenerateReportBody(BaseModel):
    period_start: str = Field(..., description="ISO-8601 시작 시각")
    period_end: str = Field(..., description="ISO-8601 종료 시각")


def _auto_title(t0: datetime, t1: datetime) -> str:
    return (
        f"운영 레포트 {t0.strftime('%Y-%m-%d %H:%M')} ~ {t1.strftime('%Y-%m-%d %H:%M')} (UTC)"
    )


@router.get("/api/reports")
async def list_reports(limit: int = 50) -> Dict[str, Any]:
    limit = max(1, min(limit, 200))
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, period_start, period_end, created_at
                FROM operational_reports
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "period_start": r["period_start"].isoformat() if r["period_start"] else None,
                    "period_end": r["period_end"].isoformat() if r["period_end"] else None,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
            )
        return {"reports": items}
    except Exception as e:
        logger.exception("[Reports] list failed")
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/api/reports/{report_id}")
async def get_report(report_id: int) -> Dict[str, Any]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, title, period_start, period_end, payload, created_at
                FROM operational_reports WHERE id = $1
                """,
                report_id,
            )
        if not row:
            raise HTTPException(status_code=404, detail="Report not found")
        return {
            "id": row["id"],
            "title": row["title"],
            "period_start": row["period_start"].isoformat() if row["period_start"] else None,
            "period_end": row["period_end"].isoformat() if row["period_end"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "payload": row["payload"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Reports] get failed")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/reports/generate")
async def generate_report(body: GenerateReportBody) -> Dict[str, Any]:
    try:
        t0 = _parse_iso(body.period_start)
        t1 = _parse_iso(body.period_end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {e}")

    payload, _warnings = await build_operational_report_payload(t0, t1)
    title = _auto_title(t0, t1)

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rid = await conn.fetchval(
                """
                INSERT INTO operational_reports (title, period_start, period_end, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id
                """,
                title,
                t0,
                t1,
                json.dumps(payload, ensure_ascii=False),
            )
    except Exception as e:
        logger.exception("[Reports] insert failed")
        raise HTTPException(status_code=503, detail=str(e))

    return {
        "id": rid,
        "title": title,
        "period_start": t0.isoformat(),
        "period_end": t1.isoformat(),
        "payload": payload,
    }


@router.delete("/api/reports/{report_id}")
async def delete_report(report_id: int) -> Dict[str, str]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM operational_reports WHERE id = $1",
                report_id,
            )
        if res == "DELETE 0":
            raise HTTPException(status_code=404, detail="Report not found")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Reports] delete failed")
        raise HTTPException(status_code=503, detail=str(e))

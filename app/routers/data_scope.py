# /app/app/routers/data_scope.py
"""
조회 가능 데이터 범위 — UI 좌측 패널용 GET /api/data-scope
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import RAG_DB, RAG_CHUNK_SYNC_INTERVAL_HOURS, RAG_EMBED_SYNC_INTERVAL_HOURS
from app.db.tables import EMBEDDING_TABLES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["data-scope"])

# RAG DB 집계 샘플 (가벼운 COUNT + 최근 시각)
_STATS_TABLES = [
    "embeddings_hosts",
    "embeddings_server_cpu",
    "embeddings_alert_events",
    "embeddings_network_devices",
    "embeddings_vmware",
]


def _build_static_scope() -> Dict[str, Any]:
    """설정·메타데이터 기반 정적 범위 설명."""
    cats: Dict[str, List[Dict[str, str]]] = {}
    for _key, meta in EMBEDDING_TABLES.items():
        cat = meta.get("category") or "server"
        raw_cats = meta.get("categories") or [cat]
        desc = (meta.get("description") or "")[:120]
        tbl = meta.get("table") or ""
        entry = {"table": tbl, "description": desc}
        for c in raw_cats:
            c = str(c).lower()
            if c not in cats:
                cats[c] = []
            # 중복 테이블명 방지
            if not any(x["table"] == tbl for x in cats[c]):
                cats[c].append(entry)

    category_labels = {
        "server": "서버 (CPU/메모리/디스크/NIC/프로세스/로그 등)",
        "network": "네트워크 장비 (장비 요약·포트 트래픽 이력)",
        "alert": "장애·이벤트",
        "infra": "자산·인벤토리 통합 요약",
        "log": "로그·ERRPT (server와 겹치는 청크 포함)",
        "vmware": "VMware 이상징후·청크 (별도 동기화 필요할 수 있음)",
    }

    vector_categories = []
    for c in sorted(cats.keys()):
        vector_categories.append({
            "id": c,
            "label_kr": category_labels.get(c, c),
            "tables": cats[c][:8],  # UI 과부하 방지
            "table_count": len(cats[c]),
        })

    realtime = {
        "title_kr": "실시간·스냅샷 (Zabbix DB, Agent 도구)",
        "items": [
            {"tool_domain": "server", "summary_kr": "서버 상태·CPU·메모리·디스크·NIC·Listen·연결·프로세스·로그·ERRPT"},
            {"tool_domain": "network", "summary_kr": "네트워크 장비 요약·인터페이스 트래픽·알림"},
            {"tool_domain": "vmware", "summary_kr": "VMware 하이퍼바이저/VM 현재 지표(get_vmware realtime)"},
            {"tool_domain": "alerts", "summary_kr": "활성/최근 장애 목록(get_alerts)"},
            {"tool_domain": "other", "summary_kr": "자산 목록·이벤트 상관(correlate_events) 등"},
        ],
    }

    limitations = [
        "벡터 검색(search_historical_data)은 유사도 상위 K개 청크만 반환하며, 전 기간·전 호스트 완전 집계가 아닙니다.",
        f"임베딩 동기화 주기(설정): 약 {RAG_EMBED_SYNC_INTERVAL_HOURS}시간마다, 청킹: 약 {RAG_CHUNK_SYNC_INTERVAL_HOURS}시간마다 — 실제 시각은 운영 스케줄러와 동일하게 맞추세요.",
        "VMware ‘이력·이상’은 embeddings_vmware 계열에 의존합니다. sync 파이프라인에 VMware 전용 gen 스크립트가 없으면 이력이 비어 있을 수 있으며, 이 경우 실시간 VM 지표와 혼동하지 않도록 답변에서 구분해야 합니다.",
    ]

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sync": {
            "embeddings_interval_hours": RAG_EMBED_SYNC_INTERVAL_HOURS,
            "chunking_interval_hours": RAG_CHUNK_SYNC_INTERVAL_HOURS,
        },
        "realtime": realtime,
        "vector_search": {
            "title_kr": "벡터 DB (RAG, search_historical_data)",
            "categories": vector_categories,
            "tool_hint_kr": "Agent가 categories 파라미터로 server / network / alert / infra / log / vmware 를 선택합니다.",
        },
        "limitations": limitations,
    }


async def _fetch_rag_sample_stats() -> Optional[List[Dict[str, Any]]]:
    """대표 테이블만 COUNT 및 최근 타임스탬프 (실패 시 None)."""
    try:
        import asyncpg
    except ImportError:
        return None

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=RAG_DB["host"],
                port=RAG_DB["port"],
                database=RAG_DB["database"],
                user=RAG_DB["user"],
                password=RAG_DB["password"],
            ),
            timeout=2.5,
        )
    except Exception as e:
        logger.debug("[data-scope] RAG connect skip: %s", e)
        return None

    rows_out: List[Dict[str, Any]] = []
    try:
        for tbl in _STATS_TABLES:
            try:
                cnt = await conn.fetchval(f"SELECT COUNT(*)::bigint FROM {tbl}")
                last_ts = None
                for col in ("updated_at", "source_updated_at"):
                    try:
                        last_ts = await conn.fetchval(f"SELECT MAX({col}) FROM {tbl}")
                        if last_ts is not None:
                            break
                    except Exception:
                        continue
                iso = last_ts.isoformat() if hasattr(last_ts, "isoformat") else (str(last_ts) if last_ts else None)
                rows_out.append({
                    "table": tbl,
                    "row_count": int(cnt or 0),
                    "last_changed_at": iso,
                })
            except Exception as e:
                logger.debug("[data-scope] stats skip %s: %s", tbl, e)
    finally:
        await conn.close()

    return rows_out or None


@router.get("/data-scope")
async def get_data_scope():
    """
    UI 좌측 패널 — 조회 가능 데이터 범위·동기 주기·한계.
    RAG 샘플 통계는 best-effort (타임아웃·오류 시 생략).
    """
    payload = _build_static_scope()
    stats = await _fetch_rag_sample_stats()
    if stats:
        payload["rag_sample_stats"] = stats
        payload["rag_sample_stats_note_kr"] = (
            "아래는 대표 테이블 기준 행 수·최근 갱신 시각입니다. 전체 23개 임베딩 테이블을 모두 포함하지 않습니다."
        )
    else:
        payload["rag_sample_stats"] = None
        payload["rag_sample_stats_note_kr"] = "RAG DB 집계를 가져오지 못했습니다. 연결·권한·타임아웃을 확인하세요."

    return JSONResponse(content=payload)

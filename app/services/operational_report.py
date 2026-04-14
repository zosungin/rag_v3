# /app/app/services/operational_report.py
"""통합 운영 레포트 집계 (Zabbix DB + RAG 벡터 테이블 기간) — v5 LLM 인사이트 포함."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.db.connection import get_pool
from app.db.tables import EMBEDDING_TABLES
from app.services.tool_executor import get_zabbix_pool, _NW_MONITORING_ACTIVE_SQL, _ts
from app.config import OPENAI_API_KEY, OPENAI_API_URL, AGENT_MODEL

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 5

# 벡터 테이블 기간 집계 시 우선하는 타임스탬프 컬럼 (청킹 → 원천 갱신 시각 순)
_TIME_COL_PRIORITY: Tuple[str, ...] = ("time_bucket", "source_updated_at", "updated_at")

PRIORITY_LABELS = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}


def _iso_ts(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


async def _detect_time_column(conn: Any, table: str) -> Optional[str]:
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table,
    )
    names = {r["column_name"] for r in rows}
    for c in _TIME_COL_PRIORITY:
        if c in names:
            return c
    return None


async def collect_embedding_period_stats(
    t0: datetime,
    t1: datetime,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """RAG DB: 모든 EMBEDDING_TABLES에 대해 MIN/MAX 시각 및 선택 기간 내 행 수."""
    warnings: List[str] = []
    out: List[Dict[str, Any]] = []
    allowed_tables = {m["table"] for m in EMBEDDING_TABLES.values()}

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            for ek, meta in sorted(
                EMBEDDING_TABLES.items(),
                key=lambda kv: kv[1]["table"],
            ):
                tbl = meta["table"]
                if tbl not in allowed_tables:
                    continue
                try:
                    col = await _detect_time_column(conn, tbl)
                    if not col:
                        out.append(
                            {
                                "key": ek,
                                "table": tbl,
                                "category": meta.get("category", ""),
                                "description": (meta.get("description") or "")[:200],
                                "time_column": None,
                                "error": "타임스탬프 컬럼 없음",
                            }
                        )
                        continue
                    row = await conn.fetchrow(
                        f"""
                        SELECT
                            COUNT(*)::bigint AS total_rows,
                            COUNT(*) FILTER (
                                WHERE {col} >= $1 AND {col} <= $2
                            )::bigint AS rows_in_period,
                            MIN({col}) AS t_min,
                            MAX({col}) AS t_max
                        FROM {tbl}
                        """,
                        t0,
                        t1,
                    )
                    if not row:
                        continue
                    out.append(
                        {
                            "key": ek,
                            "table": tbl,
                            "category": meta.get("category", ""),
                            "description": (meta.get("description") or "")[:200],
                            "time_column": col,
                            "data_min": _iso_ts(row["t_min"]),
                            "data_max": _iso_ts(row["t_max"]),
                            "row_count_total": int(row["total_rows"] or 0),
                            "row_count_in_period": int(row["rows_in_period"] or 0),
                        }
                    )
                except Exception as e:
                    logger.warning("[OpReport] vector table %s: %s", tbl, e)
                    out.append(
                        {
                            "key": ek,
                            "table": tbl,
                            "category": meta.get("category", ""),
                            "description": (meta.get("description") or "")[:120],
                            "time_column": None,
                            "error": str(e)[:200],
                        }
                    )
    except Exception as e:
        warnings.append(f"RAG DB 벡터 테이블 집계 실패: {e}")
        logger.exception("[OpReport] RAG pool error")

    return out, warnings


def _compute_vector_kpis(vector_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """선택 기간에 매칭된 벡터 행·테이블 요약."""
    ok = [r for r in vector_rows if not r.get("error") and r.get("time_column")]
    total = sum(int(r.get("row_count_in_period") or 0) for r in ok)
    with_data = [r for r in ok if int(r.get("row_count_in_period") or 0) > 0]
    top = sorted(
        with_data,
        key=lambda r: int(r.get("row_count_in_period") or 0),
        reverse=True,
    )[:8]
    return {
        "rows_in_period_total": total,
        "tables_with_data_count": len(with_data),
        "tables_tracked_count": len(vector_rows),
        "tables_ok_count": len(ok),
        "top_by_in_period": [
            {
                "table": r.get("table", ""),
                "category": r.get("category", ""),
                "rows": int(r.get("row_count_in_period") or 0),
            }
            for r in top
        ],
    }


_CAT_LABELS_KR: Dict[str, str] = {
    "server": "서버·성능·로그",
    "network": "네트워크",
    "alert": "장애·이벤트",
    "infra": "자산·인벤토리",
    "vmware": "VMware",
}


def _aggregate_vector_by_category(
    vector_rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """도메인(category)별 기간 내 행 수·대표 테이블."""
    cats: Dict[str, Dict[str, Any]] = {}
    for r in vector_rows:
        if r.get("error") or not r.get("time_column"):
            continue
        cat = (r.get("category") or "other").strip().lower() or "other"
        n = int(r.get("row_count_in_period") or 0)
        tbl = str(r.get("table") or "")
        if cat not in cats:
            cats[cat] = {
                "rows_in_period": 0,
                "tables": 0,
                "tables_with_data": 0,
                "best_table": "",
                "best_rows": 0,
            }
        c = cats[cat]
        c["rows_in_period"] += n
        c["tables"] += 1
        if n > 0:
            c["tables_with_data"] += 1
        if n > int(c["best_rows"]):
            c["best_rows"] = n
            c["best_table"] = tbl
    return cats


def _build_comprehensive_report_kr(
    t0: datetime,
    t1: datetime,
    kpis: Dict[str, Any],
    charts: Dict[str, Any],
    tables: Dict[str, Any],
    warnings: List[str],
    vector_stats: Optional[List[Dict[str, Any]]] = None,
    vector_kpis: Optional[Dict[str, Any]] = None,
) -> str:
    """벡터 집계 결과 기반 종합 분석 서술 + Zabbix 참고."""
    paras: List[str] = []
    vk = vector_kpis or {}
    vs = vector_stats or []

    paras.append(
        "【벡터 데이터 기반 종합 분석】\n"
        f"분석 구간(UTC): {t0.strftime('%Y-%m-%d %H:%M')} ~ {t1.strftime('%Y-%m-%d %H:%M')}. "
        "해석은 RAG 임베딩·청킹 테이블을 시간 컬럼으로 필터한 집계에 근거합니다."
    )
    ri = int(vk.get("rows_in_period_total") or 0)
    td = int(vk.get("tables_with_data_count") or 0)
    tt = int(vk.get("tables_tracked_count") or len(vs) or 0)
    tok = int(vk.get("tables_ok_count") or 0)
    paras.append(
        f"선택 기간에 매칭된 벡터 데이터는 총 {ri}행입니다. "
        f"감시 테이블 {tt}개 중 기간 컬럼이 확인된 {tok}개 안에서, "
        f"실제로 기간 내 행이 존재하는 테이블은 {td}개입니다."
    )

    if ri == 0 and tt > 0:
        paras.append(
            "이번 구간에서는 벡터 DB에 시간 조건이 맞는 행이 없습니다. "
            "기간을 넓히거나 임베딩·청킹 동기화 일정을 확인하는 것이 좋습니다."
        )

    by_cat = _aggregate_vector_by_category(vs)
    ordered = sorted(
        by_cat.items(),
        key=lambda x: int(x[1]["rows_in_period"]),
        reverse=True,
    )
    for cat, data in ordered:
        rp = int(data["rows_in_period"])
        if rp <= 0:
            continue
        label = _CAT_LABELS_KR.get(cat, cat)
        bt = data.get("best_table") or "—"
        br = int(data.get("best_rows") or 0)
        tw = int(data.get("tables_with_data") or 0)
        paras.append(
            f"· {label}: 기간 내 합계 {rp}행, 데이터가 있는 하위 테이블 {tw}개. "
            f"가장 많이 적재된 소스는 `{bt}`({br}행)로 추정됩니다."
        )

    empty_cats = [
        cat
        for cat, data in by_cat.items()
        if int(data.get("rows_in_period") or 0) == 0 and int(data.get("tables") or 0) > 0
    ]
    if empty_cats:
        labels = [_CAT_LABELS_KR.get(c, c) for c in empty_cats[:10]]
        paras.append(
            "다음 도메인은 테이블은 있으나 선택 기간에 매칭된 행이 없습니다: "
            + ", ".join(labels)
            + ". (동기화 지연·보관 주기·기간 설정을 점검하세요.)"
        )

    top_list = vk.get("top_by_in_period") or []
    if top_list:
        parts = [
            f"{x.get('table', '?')}({int(x.get('rows') or 0)}행)"
            for x in top_list[:5]
        ]
        paras.append(
            "데이터 적재는 일부 테이블에 상대적으로 집중되어 있습니다. "
            "상위: " + ", ".join(parts) + "."
        )

    paras.append("【Zabbix 참고 · 실시간 자산 스냅샷·동일 기간 알림】")
    paras.append(
        "관리 대상 자산 규모(현재 시점 스냅샷) — "
        f"서버 {kpis.get('servers', 0)}대, 네트워크 {kpis.get('network', 0)}대, "
        f"VMware 하이퍼바이저 {kpis.get('vmware_hypervisors', 0)}대, "
        f"가상머신 {kpis.get('vmware_vms', 0)}대."
    )
    n_alert = int(kpis.get("alerts_in_period") or 0)
    if n_alert == 0:
        paras.append("동일 기간 Zabbix 알림(alert_trigger) 집계: 없음.")
    else:
        paras.append(f"동일 기간 Zabbix 알림 집계: 총 {n_alert}건.")

    top = tables.get("top_alert_assets") or []
    if top:
        parts = [f"{r.get('asset', '?')}({int(r.get('count', 0))}건)" for r in top[:5]]
        paras.append("알림 발생 상위 자산: " + ", ".join(parts) + ".")

    pri = charts.get("alerts_by_priority") or {}
    plabels = pri.get("labels") or []
    pvals = pri.get("values") or []
    if plabels and pvals and n_alert > 0:
        pairs = []
        for i in range(min(len(plabels), len(pvals))):
            pairs.append(f"{plabels[i]} {int(pvals[i])}건")
        paras.append("심각도별 알림 분포: " + "; ".join(pairs) + ".")

    day = charts.get("alerts_by_day") or {}
    dlabels = day.get("labels") or []
    dvals = day.get("values") or []
    if dlabels and dvals:
        peak_i = max(range(len(dvals)), key=lambda i: int(dvals[i]))
        paras.append(
            f"일별 알림은 {len(dlabels)}일 구간에서 집계되었으며, "
            f"가장 많았던 날은 {dlabels[peak_i]} ({int(dvals[peak_i])}건)입니다."
        )

    if warnings:
        paras.append(
            "집계 참고: " + " ".join(str(w) for w in warnings[:5])
            + (" …" if len(warnings) > 5 else "")
        )

    return "\n\n".join(paras)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ============================================================
# LLM 인사이트 생성 (GPT-5.4)
# ============================================================
async def _generate_llm_insight(
    kpis: Dict[str, Any],
    charts: Dict[str, Any],
    tables: Dict[str, Any],
    vector_kpis: Dict[str, Any],
    t0: datetime,
    t1: datetime,
) -> Dict[str, Any]:
    """
    수집된 운영 통계를 GPT-5.4에 전달하여 구조화된 인사이트를 생성한다.
    반환: {status, status_reason, key_findings, anomalies, recommendations, executive_summary}
    """
    if not OPENAI_API_KEY:
        logger.warning("[OpReport/LLM] OPENAI_API_KEY 없음 — 인사이트 스킵")
        return {}

    ap = charts.get("alerts_by_priority") or {}
    ad = charts.get("alerts_by_day") or {}
    top_assets = tables.get("top_alert_assets") or []
    recent = tables.get("recent_alerts_sample") or []

    stats_summary = {
        "period": f"{t0.strftime('%Y-%m-%d %H:%M')} ~ {t1.strftime('%Y-%m-%d %H:%M')} (UTC)",
        "assets": {
            "servers": kpis.get("servers", 0),
            "network_devices": kpis.get("network", 0),
            "vmware_hypervisors": kpis.get("vmware_hypervisors", 0),
            "vmware_vms": kpis.get("vmware_vms", 0),
        },
        "alerts": {
            "total_in_period": kpis.get("alerts_in_period", 0),
            "by_priority": {
                lbl: val
                for lbl, val in zip(
                    ap.get("labels", []), ap.get("values", [])
                )
            },
            "by_day_peak": (
                {
                    "date": ad["labels"][ad["values"].index(max(ad["values"]))],
                    "count": max(ad["values"]),
                }
                if ad.get("values")
                else None
            ),
            "top_alert_assets": [
                {"asset": r.get("asset"), "count": r.get("count")}
                for r in top_assets[:5]
            ],
            "recent_sample": [
                {
                    "asset": r.get("asset"),
                    "priority": r.get("priority_text"),
                    "name": (r.get("name") or "")[:80],
                    "status": r.get("status"),
                }
                for r in recent[:5]
            ],
        },
        "vector_db": {
            "rows_in_period": vector_kpis.get("rows_in_period_total", 0),
            "tables_with_data": vector_kpis.get("tables_with_data_count", 0),
            "tables_tracked": vector_kpis.get("tables_tracked_count", 0),
        },
    }

    prompt = f"""당신은 엔터프라이즈 IT 인프라 운영 전문가입니다.
아래 Zabbix 모니터링 통계와 RAG 벡터 DB 집계 결과를 분석하여 운영 인사이트 보고서를 작성하세요.

[운영 통계 데이터]
{json.dumps(stats_summary, ensure_ascii=False, indent=2)}

다음 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
{{
  "status": "양호|주의|위험",
  "status_reason": "운영 상태 판정 근거 (1~2문장)",
  "key_findings": [
    "핵심 발견사항 1 (구체적인 수치 포함)",
    "핵심 발견사항 2",
    "핵심 발견사항 3"
  ],
  "anomalies": [
    "이상 징후 1 (없으면 빈 배열)"
  ],
  "recommendations": [
    "권장 조치 1 (구체적)",
    "권장 조치 2"
  ],
  "executive_summary": "경영진/담당자 보고용 2~3문장 요약. 전체 운영 상태와 주요 이슈를 포함."
}}"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": AGENT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_completion_tokens": 1024,
                },
            )
            if resp.status_code != 200:
                logger.warning("[OpReport/LLM] API 오류 status=%d", resp.status_code)
                return {}
            body = resp.json()
            content = body["choices"][0]["message"]["content"].strip()
            # ```json 블록 제거
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            insight = json.loads(content)
            logger.info("[OpReport/LLM] 인사이트 생성 완료 status=%s", insight.get("status"))
            return insight
    except Exception as e:
        logger.warning("[OpReport/LLM] 인사이트 생성 실패: %s", e)
        return {}


async def build_operational_report_payload(
    period_start: datetime,
    period_end: datetime,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    1) RAG 벡터 테이블 집계(내부) → 종합 분석 서술·KPI
    2) Zabbix: 동일 기간 알림 + 실시간 자산 스냅샷(참고)
    Returns: (payload, warnings)
    """
    warnings: List[str] = []
    t0 = _ensure_utc(period_start)
    t1 = _ensure_utc(period_end)
    if t1 < t0:
        t0, t1 = t1, t0

    vector_rows, vec_warnings = await collect_embedding_period_stats(t0, t1)
    warnings.extend(vec_warnings)
    vector_kpis = _compute_vector_kpis(vector_rows)

    epoch_start = int(t0.timestamp())
    epoch_end = int(t1.timestamp())

    kpis: Dict[str, Any] = {
        "servers": 0,
        "network": 0,
        "vmware_hypervisors": 0,
        "vmware_vms": 0,
        "alerts_in_period": 0,
    }

    charts: Dict[str, Any] = {
        "asset_mix": {"labels": [], "values": []},
        "alerts_by_priority": {"labels": [], "values": []},
        "alerts_by_day": {"labels": [], "values": []},
    }

    tables: Dict[str, Any] = {
        "top_alert_assets": [],
        "recent_alerts_sample": [],
    }

    try:
        async with (await get_zabbix_pool()).acquire() as conn:
            try:
                r = await conn.fetchrow("SELECT COUNT(*) AS c FROM svr_system_main")
                kpis["servers"] = int(r["c"]) if r else 0
            except Exception as e:
                warnings.append(f"서버 수 집계 실패: {e}")
                logger.warning("[OpReport] servers: %s", e)

            try:
                r = await conn.fetchrow(
                    f"SELECT COUNT(*) AS c FROM nw_main1 WHERE ({_NW_MONITORING_ACTIVE_SQL})"
                )
                kpis["network"] = int(r["c"]) if r else 0
            except Exception as e:
                warnings.append(f"네트워크 장비 수 집계 실패: {e}")
                logger.warning("[OpReport] network: %s", e)

            try:
                r = await conn.fetchrow("SELECT COUNT(*) AS c FROM vmware_hypervisor_info")
                kpis["vmware_hypervisors"] = int(r["c"]) if r else 0
            except Exception as e:
                warnings.append(f"VMware 하이퍼바이저 수 집계 실패: {e}")
                logger.warning("[OpReport] vmware hv: %s", e)

            try:
                r = await conn.fetchrow("SELECT COUNT(*) AS c FROM vmware_info")
                kpis["vmware_vms"] = int(r["c"]) if r else 0
            except Exception as e:
                warnings.append(f"VMware VM 수 집계 실패: {e}")
                logger.warning("[OpReport] vmware vm: %s", e)

            total_assets = (
                kpis["servers"]
                + kpis["network"]
                + kpis["vmware_hypervisors"]
                + kpis["vmware_vms"]
            )
            charts["asset_mix"] = {
                "labels": ["서버", "네트워크", "VMware HV", "VMware VM"],
                "values": [
                    kpis["servers"],
                    kpis["network"],
                    kpis["vmware_hypervisors"],
                    kpis["vmware_vms"],
                ],
            }

            try:
                pr_rows = await conn.fetch(
                    """
                    SELECT priority, COUNT(*)::bigint AS cnt
                    FROM alert_trigger
                    WHERE clock >= $1 AND clock <= $2
                    GROUP BY priority
                    ORDER BY priority
                    """,
                    epoch_start,
                    epoch_end,
                )
                plabels: List[str] = []
                pvals: List[int] = []
                alert_total = 0
                for row in pr_rows:
                    p = int(row["priority"])
                    c = int(row["cnt"])
                    alert_total += c
                    plabels.append(f"P{p} {PRIORITY_LABELS.get(p, str(p))}")
                    pvals.append(c)
                kpis["alerts_in_period"] = alert_total
                charts["alerts_by_priority"] = {"labels": plabels, "values": pvals}
            except Exception as e:
                warnings.append(f"알림 심각도 집계 실패: {e}")
                logger.warning("[OpReport] alert priority: %s", e)

            try:
                day_rows = await conn.fetch(
                    """
                    SELECT
                      (to_timestamp(clock) AT TIME ZONE 'Asia/Seoul')::date AS d,
                      COUNT(*)::bigint AS cnt
                    FROM alert_trigger
                    WHERE clock >= $1 AND clock <= $2
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    epoch_start,
                    epoch_end,
                )
                charts["alerts_by_day"] = {
                    "labels": [str(r["d"]) for r in day_rows],
                    "values": [int(r["cnt"]) for r in day_rows],
                }
            except Exception as e:
                warnings.append(f"일별 알림 집계 실패: {e}")
                logger.warning("[OpReport] alerts by day: %s", e)

            try:
                top_rows = await conn.fetch(
                    """
                    SELECT COALESCE(asset, '(unknown)') AS asset, COUNT(*)::bigint AS cnt
                    FROM alert_trigger
                    WHERE clock >= $1 AND clock <= $2
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 15
                    """,
                    epoch_start,
                    epoch_end,
                )
                tables["top_alert_assets"] = [
                    {"asset": r["asset"], "count": int(r["cnt"])} for r in top_rows
                ]
            except Exception as e:
                warnings.append(f"알림 상위 자산 집계 실패: {e}")
                logger.warning("[OpReport] top assets: %s", e)

            try:
                samp = await conn.fetch(
                    """
                    SELECT asset, hostid, priority, name, clock, value, flag
                    FROM alert_trigger
                    WHERE clock >= $1 AND clock <= $2
                    ORDER BY clock DESC
                    LIMIT 25
                    """,
                    epoch_start,
                    epoch_end,
                )
                tables["recent_alerts_sample"] = [
                    {
                        "asset": r["asset"],
                        "hostid": r["hostid"],
                        "priority": int(r["priority"]),
                        "priority_text": PRIORITY_LABELS.get(int(r["priority"]), ""),
                        "name": (r["name"] or "")[:200],
                        "time": _ts(r["clock"]),
                        "status": "Active"
                        if r["value"] == 1 and (r["flag"] or "") == "y"
                        else "Resolved",
                    }
                    for r in samp
                ]
            except Exception as e:
                warnings.append(f"최근 알림 샘플 조회 실패: {e}")
                logger.warning("[OpReport] recent sample: %s", e)

    except Exception as e:
        warnings.append(f"Zabbix 연결 또는 집계 오류: {e}")
        logger.exception("[OpReport] pool error")

    executive_summary = _build_comprehensive_report_kr(
        t0,
        t1,
        kpis,
        charts,
        tables,
        warnings,
        vector_stats=vector_rows,
        vector_kpis=vector_kpis,
    )

    # ── LLM 인사이트 생성 (GPT-5.4) ──
    llm_insight: Dict[str, Any] = {}
    try:
        llm_insight = await _generate_llm_insight(
            kpis, charts, tables, vector_kpis, t0, t1
        )
    except Exception as e:
        warnings.append(f"LLM 인사이트 생성 오류: {e}")
        logger.warning("[OpReport] LLM insight error: %s", e)

    # ── 벡터 카테고리별 시계열 차트 데이터 추가 ──
    by_cat = _aggregate_vector_by_category(vector_rows)
    charts["vector_by_category"] = {
        "labels": [_CAT_LABELS_KR.get(c, c) for c in by_cat.keys()],
        "values": [int(d.get("rows_in_period", 0)) for d in by_cat.values()],
    }

    # 벡터 테이블별 데이터 분포 (상위 10개)
    sorted_tables = sorted(
        [r for r in vector_rows if int(r.get("rows_in_period", 0)) > 0],
        key=lambda x: int(x.get("rows_in_period", 0)),
        reverse=True,
    )[:10]
    charts["vector_top_tables"] = {
        "labels": [r.get("table_name", "?").replace("embeddings_", "") for r in sorted_tables],
        "values": [int(r.get("rows_in_period", 0)) for r in sorted_tables],
    }

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {
            "start": t0.isoformat(),
            "end": t1.isoformat(),
        },
        "executive_summary": executive_summary,
        "llm_insight": llm_insight,
        "vector_period_kpis": vector_kpis,
        "warnings": warnings,
        "kpis": kpis,
        "charts": charts,
        "tables": tables,
    }
    return payload, warnings

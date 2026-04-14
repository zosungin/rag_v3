# /app/app/services/tool_executor.py
"""
Agentic RAG — Tool Executor (Phase 1)
======================================
Tool 이름 + 파라미터 → 실제 DB 조회 → Observation(순수 데이터) 반환.

변경 이력:
  [v1.1] get_server_cpu: priority, stability_level, cpu_utilization_std 반환 추가
         get_processes:  priority, stability_level, stat(프로세스 상태) 반환 추가
         get_server_filesystem: DISTINCT ON (fs_name) 최신 레코드만 반환
         search_historical_data: priority, stability_level, source_updated_at 결과 포함

핵심 함수:
  resolve_host()  — LLM이 추출한 host_query → 실제 DB 호스트 매칭
  execute_tool()  — Tool 디스패치

설계 원칙:
  - 모든 Tool은 dict를 반환 (Observation)
  - 매칭 실패 시 { "found": false, "available_hosts": [...] } 반환
  - 수치는 DB 원본 그대로 전달 (포맷팅/분석은 LLM에게 위임)
"""

import asyncpg
import re
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

from app.config import ZABBIX_DB
from app.db.connection import get_pool as get_rag_pool

logger = logging.getLogger(__name__)

# nw_main1.agent_status: 동기화 스크립트 기준 '0'=에이전트/모니터링 활성, 그 외=비활성·제외 장비
_NW_MONITORING_ACTIVE_SQL = "COALESCE(agent_status::text, '') = '0'"

# KST 타임존 정의 (UTC+9)
_KST = timezone(timedelta(hours=9))

# ============================================================
# Zabbix DB Connection Pool (싱글톤)
# — 매 도구 호출마다 신규 connect() 생성하던 방식을 Pool로 전환
# — RAG DB(connection.py)와 동일한 패턴 적용
# ============================================================
_zabbix_pool: Optional[asyncpg.Pool] = None


async def get_zabbix_pool() -> asyncpg.Pool:
    """Zabbix DB 커넥션 풀 반환 (싱글톤)"""
    global _zabbix_pool
    if _zabbix_pool is None:
        _zabbix_pool = await asyncpg.create_pool(
            host=ZABBIX_DB['host'],
            port=ZABBIX_DB['port'],
            database=ZABBIX_DB['database'],
            user=ZABBIX_DB['user'],
            password=ZABBIX_DB['password'],
            min_size=3,
            max_size=20,       # ISS-09: 고부하 대비 상향 (2→3 min, 10→20 max)
            max_inactive_connection_lifetime=300,  # 5분 미사용 연결 해제
        )
        logger.info("[ZabbixPool] 커넥션 풀 생성 완료 (min=3, max=20)")
    return _zabbix_pool


async def close_zabbix_pool():
    """Zabbix DB 커넥션 풀 종료 (shutdown_event에서 호출)"""
    global _zabbix_pool
    if _zabbix_pool:
        await _zabbix_pool.close()
        _zabbix_pool = None
        logger.info("[ZabbixPool] 커넥션 풀 종료")


# ============================================================
# DB 연결 헬퍼 (하위 호환 — 내부 전용)
# ============================================================
async def _get_zabbix_conn():
    """[Deprecated] Pool에서 connection 획득. 기존 코드 호환용."""
    pool = await get_zabbix_pool()
    return await pool.acquire()


async def _release_conn(conn):
    """Pool로 connection 반납."""
    pool = await get_zabbix_pool()
    await pool.release(conn)


# ============================================================
# ★ 핵심: 호스트 매칭 엔진
# ============================================================
def _is_os_category_host_query(host_query: str) -> bool:
    """
    LLM/연계 질의가 'AIX 서버', 'linux' 등 OS 범주를 host_query로 넘긴 경우.
    실제 자산명이 아니므로 전체 조회로 처리한다.
    """
    q = re.sub(r"\s+", " ", (host_query or "").strip().lower())
    if not q:
        return False
    os_tokens = (
        "aix", "linux", "solaris", "sunos", "freebsd", "openbsd", "netbsd",
        "hp-ux", "hpux", "unix", "windows",
    )
    for t in os_tokens:
        if q == t or q == f"{t} 서버":
            return True
    return False


async def resolve_host(
    host_query: str,
    source: str = "server"
) -> Dict:
    """
    LLM이 추출한 host_query를 실제 DB 레코드에 매칭.

    매칭 순서:
      1. 'all' / '전체' → 전체 조회
      2. IP 정확 매칭
      3. 자산명 정확 매칭 (ILIKE)
      4. 자산명 부분 매칭 (ILIKE %query%)
      5. system_name / group_name 매칭
      6. 매칭 실패 → available_hosts 반환

    Returns:
        {
            "match_type": "exact_ip" | "exact_name" | "partial" | "ambiguous" | "not_found" | "all",
            "host_key": str | None,       # ~* 검색용 키
            "hostid": int | None,         # 단일 매칭 시
            "hosts": [...],               # 복수 매칭 시
            "available_hosts": [...],     # not_found 시 전체 목록
            "message": str                # 사람이 읽을 수 있는 설명
        }
    """
    if not host_query or host_query.strip() == "":
        return {
            "match_type": "all",
            "host_key": None,
            "message": "host_query가 비어있어 전체 조회합니다."
        }

    query_clean = host_query.strip()
    query_lower = query_clean.lower()

    # 1) 전체 조회 키워드
    if query_lower in ('all', '전체', '모든', '모두', '전부'):
        return {
            "match_type": "all",
            "host_key": None,
            "message": "전체 조회"
        }

    if _is_os_category_host_query(query_clean):
        return {
            "match_type": "all",
            "host_key": None,
            "message": "OS/범주 표현으로 인식되어 전체 호스트 기준으로 조회합니다.",
        }

    async with (await get_zabbix_pool()).acquire() as conn:
        # 소스별 테이블 결정
        if source == "network":
            tables = [("nw_main1", "asset", "asset_name", "ip")]
        elif source == "both":
            tables = [
                ("svr_system_main", "asset", "asset_name", "ip"),
                ("nw_main1", "asset", "asset_name", "ip"),
            ]
        else:
            tables = [("svr_system_main", "asset", "asset_name", "ip")]

        # 2) IP 패턴 체크
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', query_clean):
            for tbl, asset_col, aname_col, ip_col in tables:
                nw = f" AND ({_NW_MONITORING_ACTIVE_SQL})" if tbl == "nw_main1" else ""
                rows = await conn.fetch(
                    f"SELECT hostid, {asset_col} as asset, {aname_col} as asset_name, {ip_col} as ip "
                    f"FROM {tbl} WHERE {ip_col} = $1{nw}",
                    query_clean
                )
                if rows:
                    if len(rows) == 1:
                        return {
                            "match_type": "exact_ip",
                            "host_key": query_clean,
                            "hostid": rows[0]['hostid'],
                            "asset": rows[0]['asset'],
                            "asset_name": rows[0]['asset_name'],
                            "ip": rows[0]['ip'],
                            "message": f"IP {query_clean}로 매칭: {rows[0]['asset']}"
                        }
                    else:
                        return {
                            "match_type": "ambiguous",
                            "host_key": query_clean,
                            "candidates": [
                                {"asset": r['asset'], "asset_name": r['asset_name'], "ip": r['ip']}
                                for r in rows
                            ],
                            "message": f"IP {query_clean}에 {len(rows)}개 호스트가 매칭됩니다."
                        }

        # 3) 자산명 정확 매칭 (대소문자 무시)
        for tbl, asset_col, aname_col, ip_col in tables:
            nw = f" AND ({_NW_MONITORING_ACTIVE_SQL})" if tbl == "nw_main1" else ""
            rows = await conn.fetch(
                f"SELECT hostid, {asset_col} as asset, {aname_col} as asset_name, {ip_col} as ip "
                f"FROM {tbl} WHERE ({asset_col} ILIKE $1 OR {aname_col} ILIKE $1){nw}",
                query_clean
            )
            if rows:
                if len(rows) == 1:
                    return {
                        "match_type": "exact_name",
                        "host_key": rows[0]['asset'],
                        "hostid": rows[0]['hostid'],
                        "asset": rows[0]['asset'],
                        "asset_name": rows[0]['asset_name'],
                        "ip": rows[0]['ip'],
                        "message": f"자산명 정확 매칭: {rows[0]['asset']} ({rows[0]['ip']})"
                    }
                else:
                    return {
                        "match_type": "ambiguous",
                        "host_key": query_clean,
                        "candidates": [
                            {"asset": r['asset'], "asset_name": r['asset_name'], "ip": r['ip']}
                            for r in rows
                        ],
                        "message": f"'{query_clean}'에 {len(rows)}개 호스트가 매칭됩니다."
                    }

        # 4) 부분 매칭
        for tbl, asset_col, aname_col, ip_col in tables:
            nw = f" AND ({_NW_MONITORING_ACTIVE_SQL})" if tbl == "nw_main1" else ""
            rows = await conn.fetch(
                f"SELECT hostid, {asset_col} as asset, {aname_col} as asset_name, {ip_col} as ip "
                f"FROM {tbl} WHERE ({asset_col} ILIKE $1 OR {aname_col} ILIKE $1){nw}",
                f"%{query_clean}%"
            )
            if rows:
                if len(rows) == 1:
                    return {
                        "match_type": "partial",
                        "host_key": rows[0]['asset'],
                        "hostid": rows[0]['hostid'],
                        "asset": rows[0]['asset'],
                        "asset_name": rows[0]['asset_name'],
                        "ip": rows[0]['ip'],
                        "message": f"부분 매칭: '{query_clean}' → {rows[0]['asset']} ({rows[0]['ip']})"
                    }
                else:
                    return {
                        "match_type": "ambiguous",
                        "host_key": query_clean,
                        "candidates": [
                            {"asset": r['asset'], "asset_name": r['asset_name'], "ip": r['ip']}
                            for r in rows
                        ],
                        "message": f"'{query_clean}'에 {len(rows)}개 호스트가 부분 매칭됩니다. 명확한 이름이나 IP를 사용해 주세요."
                    }

        # 5-N) device_name 매칭 (네트워크 장비 전용 — VSX-Secondary 등 모델명 질의 대응)
        if source in ("network", "both"):
            rows = await conn.fetch(
                "SELECT hostid, asset, asset_name, ip FROM nw_main1 "
                f"WHERE device_name ILIKE $1 AND ({_NW_MONITORING_ACTIVE_SQL})",
                f"%{query_clean}%"
            )
            if rows:
                if len(rows) == 1:
                    return {
                        "match_type": "partial",
                        "host_key": rows[0]['asset'],
                        "hostid": rows[0]['hostid'],
                        "asset": rows[0]['asset'],
                        "asset_name": rows[0]['asset_name'],
                        "ip": rows[0]['ip'],
                        "message": f"장비명 매칭: '{query_clean}' → {rows[0]['asset_name']} ({rows[0]['ip']})"
                    }
                else:
                    return {
                        "match_type": "ambiguous",
                        "host_key": query_clean,
                        "candidates": [
                            {"asset": r['asset'], "asset_name": r['asset_name'], "ip": r['ip']}
                            for r in rows
                        ],
                        "message": f"'{query_clean}'에 {len(rows)}개 장비가 매칭됩니다."
                    }

        # 5) system_name / group_name 매칭 (서버 전용)
        if source in ("server", "both"):
            rows = await conn.fetch(
                "SELECT hostid, asset, asset_name, ip FROM svr_system_main "
                "WHERE system_name ILIKE $1 OR group_name ILIKE $1",
                f"%{query_clean}%"
            )
            if rows:
                if len(rows) == 1:
                    return {
                        "match_type": "partial",
                        "host_key": rows[0]['asset'],
                        "hostid": rows[0]['hostid'],
                        "asset": rows[0]['asset'],
                        "asset_name": rows[0]['asset_name'],
                        "ip": rows[0]['ip'],
                        "message": f"시스템/그룹명 매칭: '{query_clean}' → {rows[0]['asset']} ({rows[0]['ip']})"
                    }
                else:
                    return {
                        "match_type": "ambiguous",
                        "host_key": query_clean,
                        "candidates": [
                            {"asset": r['asset'], "asset_name": r['asset_name'], "ip": r['ip']}
                            for r in rows
                        ],
                        "message": f"'{query_clean}'에 {len(rows)}개 호스트가 매칭됩니다."
                    }

        # 6) 매칭 실패 → 등록된 전체 장비 목록 제공
        all_hosts = []
        for tbl, asset_col, aname_col, ip_col in tables:
            nw = f" WHERE ({_NW_MONITORING_ACTIVE_SQL})" if tbl == "nw_main1" else ""
            rows = await conn.fetch(
                f"SELECT {asset_col} as asset, {aname_col} as asset_name, {ip_col} as ip "
                f"FROM {tbl}{nw} ORDER BY {asset_col}"
            )
            for r in rows:
                all_hosts.append({
                    "asset": r['asset'],
                    "asset_name": r['asset_name'],
                    "ip": r['ip'],
                    "type": "network" if tbl == "nw_main1" else "server"
                })

        return {
            "match_type": "not_found",
            "host_key": None,
            "query": query_clean,
            "message": f"'{query_clean}'에 해당하는 장비를 찾을 수 없습니다.",
            "available_hosts": all_hosts
        }



# ============================================================
# 공통 유틸
# ============================================================
def _ts(val) -> str:
    """타임스탬프/datetime → KST 문자열 변환"""
    if not val:
        return ''
    # datetime 객체: timezone-aware면 KST 변환, naive면 UTC로 간주 후 KST 변환
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(_KST).strftime('%Y-%m-%d %H:%M:%S')
        else:
            # naive datetime → UTC로 간주하고 KST 변환
            return val.replace(tzinfo=timezone.utc).astimezone(_KST).strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(val, str):
        return val
    # Unix Timestamp (int/float) → KST 변환
    try:
        return datetime.fromtimestamp(val, tz=_KST).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(val)


def _build_not_found_observation(resolve_result: Dict) -> Dict:
    """resolve_host()가 not_found인 경우의 표준 Observation"""
    return {
        "found": False,
        "count": 0,
        "data": [],
        "message": resolve_result.get("message", "해당 장비를 찾을 수 없습니다."),
        "available_hosts": resolve_result.get("available_hosts", [])
    }


def _build_ambiguous_observation(resolve_result: Dict) -> Dict:
    """resolve_host()가 ambiguous인 경우의 표준 Observation"""
    return {
        "found": False,
        "count": 0,
        "data": [],
        "ambiguous": True,
        "message": resolve_result.get("message", ""),
        "candidates": resolve_result.get("candidates", [])
    }


def _where_from_resolve(resolve_result: Dict, alias: str = "") -> Tuple[str, list]:
    """
    resolve_host() 결과 → SQL WHERE 절 + params 생성.
    alias: 테이블 별칭 (예: "s." → "s.asset ~* $1")
    """
    match_type = resolve_result.get("match_type")
    host_key = resolve_result.get("host_key")

    if match_type == "all" or not host_key:
        return "", []

    prefix = f"{alias}." if alias else ""
    where = f"WHERE {prefix}asset ~* $1 OR {prefix}ip ~* $1"
    return where, [host_key]


# ============================================================
# Tool 실행 함수들
# ============================================================

# ─── 1. get_server_status ───
async def tool_get_server_status(host_query: str = "all") -> Dict:
    """서버 종합 상태 조회 (ISS-11: baseline_avg 7일 평균 포함)"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

    async with (await get_zabbix_pool()).acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT hostid, group_name, ip, asset, asset_name, os, system_name,
                   number_of_processes, system_uptime, logined_user, zabbix_agent,
                   cpu_utilization, memory_utilization, swap_utilization,
                   max_fs, avg_fs, max_fs_name, updated_at
            FROM svr_system_main {where}
            ORDER BY asset
        """, *params)

        # ISS-11: 7일 baseline_avg — svr_cpu_main / svr_memory_main 이력 조회
        # hostid 목록 추출
        host_ids = [r["hostid"] for r in rows if r["hostid"]]

        # 7일 CPU 평균
        cpu_baseline_map: dict = {}
        mem_baseline_map: dict = {}
        if host_ids:
            try:
                cpu_hist = await conn.fetch("""
                    SELECT hostid, AVG(cpu_utilization) AS avg_cpu
                    FROM svr_cpu_main
                    WHERE hostid = ANY($1) AND updated_at >= $2
                    GROUP BY hostid
                """, host_ids, cutoff_7d)
                cpu_baseline_map = {r["hostid"]: round(float(r["avg_cpu"] or 0), 2) for r in cpu_hist}

                mem_hist = await conn.fetch("""
                    SELECT hostid, AVG(memory_utilization) AS avg_mem
                    FROM svr_system_main
                    WHERE hostid = ANY($1) AND updated_at >= $2
                    GROUP BY hostid
                """, host_ids, cutoff_7d)
                mem_baseline_map = {r["hostid"]: round(float(r["avg_mem"] or 0) / (1024**3), 2) for r in mem_hist}
            except Exception as e:
                logger.warning(f"[tool_get_server_status] baseline_avg 조회 실패 (무시): {e}")

        data = []
        for r in rows:
            mem_gb = round(float(r['memory_utilization'] or 0) / (1024**3), 2) if r['memory_utilization'] else 0
            hid = r['hostid']
            cpu_now  = round(float(r['cpu_utilization'] or 0), 2)
            cpu_base = cpu_baseline_map.get(hid)
            mem_base = mem_baseline_map.get(hid)

            entry = {
                'hostid': hid,
                'asset': r['asset'],
                'asset_name': r['asset_name'],
                'ip': r['ip'],
                'os': r['os'],
                'group_name': r['group_name'],
                'system_name': r['system_name'],
                'cpu_utilization': cpu_now,
                'memory_gb': mem_gb,
                'swap_utilization': round(float(r['swap_utilization'] or 0), 2),
                'max_fs': round(float(r['max_fs'] or 0), 2),
                'max_fs_name': r['max_fs_name'] or '',
                'avg_fs': round(float(r['avg_fs'] or 0), 2),
                'uptime': r['system_uptime'] or '',
                'processes': int(r['number_of_processes'] or 0),
                'logined_user': int(r['logined_user'] or 0),
                'updated_at': _ts(r['updated_at']),
            }
            # ISS-11: baseline 필드 추가
            if cpu_base is not None:
                entry['cpu_baseline_7d'] = cpu_base
                entry['cpu_diff'] = round(cpu_now - cpu_base, 2)
            if mem_base is not None:
                entry['memory_baseline_7d_gb'] = mem_base
            data.append(entry)

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 2. get_server_cpu ───
async def tool_get_server_cpu(
    host_query: str = "all",
    include_top_processes: bool = False
) -> Dict:
    """서버 CPU 상세 조회"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, number_of_cpus,
                   cpu_utilization, cpu_idle_time, cpu_system_time, cpu_user_time,
                   updated_at
            FROM svr_cpu_main {where}
        """, *params)

        cpu_data = []
        for r in rows:
            cpu_data.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'], 'os': r['os'],
                'cores': r['number_of_cpus'],
                'cpu_utilization': round(float(r['cpu_utilization'] or 0), 2),
                'cpu_idle': round(float(r['cpu_idle_time'] or 0), 2),
                'cpu_system': round(float(r['cpu_system_time'] or 0), 2),
                'cpu_user': round(float(r['cpu_user_time'] or 0), 2),
                'updated_at': _ts(r['updated_at']),
            })

        result = {
            "found": len(cpu_data) > 0,
            "count": len(cpu_data),
            "data": cpu_data,
            "match_info": resolve.get("message", ""),
        }

        # 상위 프로세스 포함
        if include_top_processes and cpu_data:
            host_key = resolve.get("host_key")
            proc_where = "WHERE flag = 'y'"
            proc_params = []
            if host_key:
                proc_where += " AND (asset ~* $1 OR hostid::text = $1)"
                proc_params = [host_key]

            proc_rows = await conn.fetch(f"""
                SELECT hostid, asset, pid, user_name, command, cpu, mem, cpu_time, stat
                FROM proc_stat {proc_where}
                ORDER BY cpu DESC LIMIT 5
            """, *proc_params)

            result["top_processes"] = [
                {
                    'pid': r['pid'], 'user': r['user_name'],
                    'command': (r['command'] or '')[:80],
                    'cpu': round(float(r['cpu'] or 0), 2),
                    'mem': round(float(r['mem'] or 0), 2),
                    'stat': r['stat'] or '',
                }
                for r in proc_rows
            ]

        return result


# ─── 3. get_server_memory ───
async def tool_get_server_memory(host_query: str = "all") -> Dict:
    """서버 메모리 상세 조회"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, total_memory, available_memory,
                   memory_utilization, available_memory_in, updated_at
            FROM svr_mem_main {where}
        """, *params)

        data = []
        for r in rows:
            total_gb = round(float(r['total_memory'] or 0) / (1024**3), 2)
            avail_gb = round(float(r['available_memory'] or 0) / (1024**3), 2)
            used_gb = round(float(r['memory_utilization'] or 0) / (1024**3), 2)
            avail_pct = float(r['available_memory_in'] or 0)
            data.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'], 'os': r['os'],
                'total_gb': total_gb, 'available_gb': avail_gb, 'used_gb': used_gb,
                'available_pct': round(avail_pct, 2),
                'used_pct': round(100 - avail_pct, 2),
                'updated_at': _ts(r['updated_at']),
            })

        # Swap도 함께 조회
        swap_rows = await conn.fetch(f"""
            SELECT hostid, asset, total_swap_space, free_swap_space,
                   free_swap_space_pct, used_swap_percent, updated_at
            FROM svr_swap_main {where}
        """, *params)

        swap_data = []
        for r in swap_rows:
            swap_data.append({
                'hostid': r['hostid'], 'asset': r['asset'],
                'total_gb': round(float(r['total_swap_space'] or 0) / (1024**3), 2),
                'free_gb': round(float(r['free_swap_space'] or 0) / (1024**3), 2),
                'used_pct': round(float(r['used_swap_percent'] or 0), 2),
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "swap": swap_data,
            "match_info": resolve.get("message", ""),
        }


# ─── 4. get_server_filesystem ───
async def tool_get_server_filesystem(host_query: str = "all") -> Dict:
    """서버 파일시스템 조회"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        # [v1.1] DISTINCT ON (hostid, fs_name): 마운트포인트별 최신 레코드만 반환
        # — 3일 누적 UPSERT 테이블이므로 동일 fs_name에 여러 행이 있을 수 있음
        rows = await conn.fetch(f"""
            SELECT DISTINCT ON (t.hostid, t.fs_name)
                   t.hostid, t.ip, t.asset, t.os, t.fs_name,
                   t.total_space, t.used_space, t.space_utilization, t.updated_at
            FROM svr_filesystem_main t
            {where}
            ORDER BY t.hostid, t.fs_name, t.updated_at DESC
        """, *params)

        data = []
        for r in rows:
            data.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'],
                'fs_name': r['fs_name'],
                'total_mb': round(float(r['total_space'] or 0), 1),
                'used_mb': round(float(r['used_space'] or 0), 1),
                'utilization': round(float(r['space_utilization'] or 0), 2),
                'updated_at': _ts(r['updated_at']),
            })

        # 사용률 내림차순 정렬
        data.sort(key=lambda x: x['utilization'], reverse=True)

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 5. get_network_device ───
async def tool_get_network_device(host_query: str = "all") -> Dict:
    """네트워크 장비 상태 조회"""
    resolve = await resolve_host(host_query, source="network")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)
    if where:
        nw_where = f"{where} AND ({_NW_MONITORING_ACTIVE_SQL})"
    else:
        nw_where = f"WHERE ({_NW_MONITORING_ACTIVE_SQL})"

    async with (await get_zabbix_pool()).acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT hostid, group_name, asset, asset_name, device_name, ip, ios,
                   snmp_agent, uptime, total_interface, up_interface,
                   icmp_ping, icmp_response_time,
                   device_cpu_usage, device_mem_usage,
                   device_temperature_1, system_description, update_clock
            FROM nw_main1 {nw_where}
        """, *params)

        data = []
        for r in rows:
            uptime_days = int(r['uptime'] or 0) // 86400
            data.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'asset_name': r['asset_name'],
                'device_name': r['device_name'], 'ip': r['ip'], 'ios': r['ios'],
                'group_name': r['group_name'],
                'icmp_ping': 'up' if r['icmp_ping'] == 1 else 'down',
                'icmp_ms': round(float(r['icmp_response_time'] or 0), 2),
                'uptime_days': uptime_days,
                'total_ports': r['total_interface'] or 0,
                'up_ports': r['up_interface'] or 0,
                'down_ports': (r['total_interface'] or 0) - (r['up_interface'] or 0),
                'cpu_usage': round(float(r['device_cpu_usage'] or 0), 2),
                'mem_usage': round(float(r['device_mem_usage'] or 0), 2),
                'temperature': round(float(r['device_temperature_1'] or 0), 1),
                'description': r['system_description'] or '',
                'update_clock': _ts(r['update_clock']),
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 6. get_network_interfaces ───
async def tool_get_network_interfaces(
    host_query: str = "all",
    link_filter: str = "all",
    sort_by: str = "name",
    top_n: int = 0
) -> Dict:
    """네트워크 인터페이스/포트 조회"""
    resolve = await resolve_host(host_query, source="network")

    if resolve["match_type"] == "not_found":
        # ── 서버 폴백 감지: nw_main1에 없지만 svr_system_main에는 존재하는 경우 ──
        # zabbix 등 서버 호스트를 get_network_interfaces로 잘못 호출했을 때 명확한 안내 반환
        if host_query and host_query.strip().lower() not in ("all", "전체", "모든", "모두", "전부"):
            server_resolve = await resolve_host(host_query, source="server")
            if server_resolve["match_type"] not in ("not_found", "ambiguous"):
                logger.warning(
                    f"[tool_get_network_interfaces] '{host_query}'는 네트워크 장비가 아닌 "
                    f"서버 자산({server_resolve.get('asset','')})입니다. "
                    f"get_server_interfaces 도구를 사용해야 합니다."
                )
                return {
                    "found": False,
                    "count": 0,
                    "data": [],
                    "is_server_asset": True,
                    "correct_tool": "get_server_interfaces",
                    "message": (
                        f"'{host_query}'는 네트워크 장비가 아닌 서버 자산입니다 "
                        f"(자산명: {server_resolve.get('asset_name', host_query)}, "
                        f"IP: {server_resolve.get('ip', '')}).\n"
                        "서버의 NIC/인터페이스 트래픽 조회는 get_server_interfaces 도구를 사용하세요."
                    ),
                    "summary": {
                        "total": 0,
                        "up": 0,
                        "down": 0,
                        "error_ports": 0,
                        "link_filter_applied": link_filter,
                        "sort_by": sort_by,
                        "top_n": top_n if top_n > 0 else "all",
                    },
                    "match_info": server_resolve.get("message", ""),
                }
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = []
        params = []
        idx = 1

        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(n.asset ~* ${idx} OR n.hostid::text = ${idx})")
            params.append(host_key)
            idx += 1

        if link_filter == 'up':
            clauses.append("n.link = 1")
        elif link_filter == 'down':
            clauses.append("n.link = 0")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        # 정렬
        if sort_by == 'traffic':
            order = "ORDER BY (COALESCE(n.received,0) + COALESCE(n.sent,0)) DESC"
        elif sort_by == 'errors':
            order = "ORDER BY (COALESCE(n.in_error_value,0) + COALESCE(n.out_error_value,0)) DESC"
        else:
            order = "ORDER BY n.interface"

        limit = f"LIMIT {top_n}" if top_n > 0 else ""

        rows = await conn.fetch(f"""
            SELECT n.hostid, n.asset, n.interface, n.port_alias_name, n.link,
                   n.received, n.sent, n.port_speed,
                   n.in_error_value, n.out_error_value, n.update_clock,
                   m.ip, m.asset_name
            FROM nw_if n
            INNER JOIN nw_main1 m ON n.hostid = m.hostid AND ({_NW_MONITORING_ACTIVE_SQL})
            {where} {order} {limit}
        """, *params)

        data = []
        for r in rows:
            recv = int(r['received'] or 0)
            sent = int(r['sent'] or 0)
            speed = int(r['port_speed'] or 0)
            data.append({
                'hostid': r['hostid'],
                'asset': r['asset'],
                'asset_name': r.get('asset_name') or r['asset'],
                'ip': r.get('ip') or '',
                'interface': r['interface'] or '',
                'alias': r['port_alias_name'] or '',
                'link': 'up' if r['link'] == 1 else 'down',
                'received_bps': recv,
                'received_fmt': _format_bps(recv),
                'sent_bps': sent,
                'sent_fmt': _format_bps(sent),
                'speed_fmt': _format_bps(speed),
                'in_errors': int(r['in_error_value'] or 0),
                'out_errors': int(r['out_error_value'] or 0),
                'update_clock': _ts(r['update_clock']),
            })

        # 통계 요약
        total_up = sum(1 for d in data if d['link'] == 'up')
        total_down = sum(1 for d in data if d['link'] == 'down')
        error_ports = sum(1 for d in data if d['in_errors'] > 0 or d['out_errors'] > 0)

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "summary": {
                "total": len(data),
                "up": total_up,
                "down": total_down,
                "error_ports": error_ports,
                "link_filter_applied": link_filter,
                "sort_by": sort_by,
                "top_n": top_n if top_n > 0 else "all",
            },
            "match_info": resolve.get("message", ""),
        }


def _format_bps(bps: int) -> str:
    """bps → 읽기 쉬운 단위"""
    if bps <= 0:
        return "0 bps"
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.2f} Kbps"
    return f"{bps} bps"


# ─── 7. get_alerts ───
async def tool_get_alerts(
    host_query: str = "all",
    status_filter: str = "all",
    limit: int = 20
) -> Dict:
    """장애 이벤트 조회"""
    resolve = await resolve_host(host_query, source="both")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = []
        params = []
        idx = 1

        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(asset ~* ${idx} OR hostid::text = ${idx})")
            params.append(host_key)
            idx += 1

        if status_filter == "active":
            clauses.append("value = 1 AND flag = 'y'")
        elif status_filter == "resolved":
            clauses.append("(value = 0 OR flag = 'n')")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = await conn.fetch(f"""
            SELECT triggerid, asset, hostid, value, priority, name, clock, flag
            FROM alert_trigger {where}
            ORDER BY priority DESC, clock DESC
            LIMIT {limit}
        """, *params)

        priority_map = {0: 'Not classified', 1: 'Information', 2: 'Warning',
                        3: 'Average', 4: 'High', 5: 'Disaster'}

        data = []
        active_count = 0
        resolved_count = 0
        for r in rows:
            is_active = r['value'] == 1 and r['flag'] == 'y'
            if is_active:
                active_count += 1
            else:
                resolved_count += 1
            data.append({
                'triggerid': r['triggerid'], 'asset': r['asset'], 'hostid': r['hostid'],
                'name': r['name'],
                'priority': r['priority'],
                'priority_text': priority_map.get(r['priority'], ''),
                'status': 'Active' if is_active else 'Resolved',
                'time': _ts(r['clock']),
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "active_count": active_count,
            "resolved_count": resolved_count,
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 8. get_processes ───
async def tool_get_processes(host_query: str = "all", top_n: int = 10) -> Dict:
    """프로세스 목록 조회 (CPU 상위)"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = ["flag = 'y'"]
        params = []
        host_key = resolve.get("host_key")
        if host_key:
            clauses.append("(asset ~* $1 OR hostid::text = $1)")
            params.append(host_key)

        where = f"WHERE {' AND '.join(clauses)}"

        rows = await conn.fetch(f"""
            SELECT hostid, asset, pid, user_name, command, cpu, mem, cpu_time, stat, clock
            FROM proc_stat {where}
            ORDER BY cpu DESC
            LIMIT {top_n}
        """, *params)

        data = []
        for r in rows:
            data.append({
                'hostid': r['hostid'], 'asset': r['asset'],
                'pid': r['pid'], 'user': r['user_name'],
                'command': (r['command'] or '')[:80],
                'cpu': round(float(r['cpu'] or 0), 2),
                'mem': round(float(r['mem'] or 0), 2),
                'cpu_time': r['cpu_time'] or '',
                'stat': r['stat'] or '',
                'measured_at': _ts(r['clock']) if r.get('clock') else '',
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 9. get_all_assets ───
async def tool_get_all_assets(
    asset_type: str = "all",
    cpu_threshold: float = 0
) -> Dict:
    """전체 자산 목록 (서버+네트워크+VMware)"""
    async with (await get_zabbix_pool()).acquire() as conn:
        servers = []
        network_devices = []
        vmware_hypervisors = []
        vmware_vms = []

        if asset_type in ("all", "server"):
            rows = await conn.fetch("""
                SELECT hostid, asset, asset_name, ip, os, group_name,
                       cpu_utilization, swap_utilization, max_fs, max_fs_name, updated_at
                FROM svr_system_main ORDER BY asset
            """)
            for r in rows:
                cpu = round(float(r['cpu_utilization'] or 0), 2)
                servers.append({
                    'type': 'server',
                    'hostid': r['hostid'], 'asset': r['asset'], 'asset_name': r['asset_name'],
                    'ip': r['ip'], 'os': r['os'], 'group_name': r['group_name'],
                    'cpu_utilization': cpu,
                    'swap_utilization': round(float(r['swap_utilization'] or 0), 2),
                    'max_fs': round(float(r['max_fs'] or 0), 2),
                    'max_fs_name': r['max_fs_name'] or '',
                    'updated_at': _ts(r['updated_at']),
                })

        if asset_type in ("all", "network"):
            rows = await conn.fetch(f"""
                SELECT hostid, asset, asset_name, ip, ios, group_name,
                       icmp_ping, device_cpu_usage, device_mem_usage,
                       total_interface, up_interface, update_clock
                FROM nw_main1 WHERE ({_NW_MONITORING_ACTIVE_SQL}) ORDER BY asset
            """)
            for r in rows:
                cpu = round(float(r['device_cpu_usage'] or 0), 2)
                network_devices.append({
                    'type': 'network',
                    'hostid': r['hostid'], 'asset': r['asset'], 'asset_name': r['asset_name'],
                    'ip': r['ip'], 'os': r['ios'], 'group_name': r['group_name'],
                    'icmp_ping': 'up' if r['icmp_ping'] == 1 else 'down',
                    'cpu_utilization': cpu,
                    'mem_usage': round(float(r['device_mem_usage'] or 0), 2),
                    'total_ports': r['total_interface'] or 0,
                    'up_ports': r['up_interface'] or 0,
                    'update_clock': _ts(r['update_clock']),
                })

        if asset_type in ("all", "vmware"):
            # VMware 하이퍼바이저(물리장비)
            hv_rows = await conn.fetch("""
                SELECT hi.hostid, hi.asset, hi.cluster_name, hi.datacenter_name,
                       hi.overall_status, hi.num_guest_vms, hi.version, hi.uptime,
                       ci.cpu_usage_pct, mi.total_memory, mi.used_memory, mi.ballooned_memory,
                       di.free_space_pct AS ds_free_pct, hi.update_clock
                FROM vmware_hypervisor_info hi
                LEFT JOIN vmware_cpu_info    ci ON hi.hostid = ci.hostid
                LEFT JOIN vmware_memory_info mi ON hi.hostid = mi.hostid
                LEFT JOIN vmware_datastore_info di ON hi.hostid = di.hostid
                ORDER BY hi.asset
            """)
            for r in hv_rows:
                used_gb  = round(float(r['used_memory']  or 0) / (1024**3), 1)
                total_gb = round(float(r['total_memory'] or 0) / (1024**3), 1)
                bal_gb   = round(float(r['ballooned_memory'] or 0) / (1024**3), 1)
                vmware_hypervisors.append({
                    'type': 'VMware 하이퍼바이저(물리장비)',
                    'hostid': r['hostid'],
                    'asset_name': r['asset'] or '',
                    'ip': '',
                    'cluster_name': r['cluster_name'] or 'N/A',
                    'datacenter_name': r['datacenter_name'] or 'N/A',
                    'overall_status': r['overall_status'] or 'N/A',
                    'num_guest_vms': r['num_guest_vms'] or 0,
                    'esxi_version': r['version'] or 'N/A',
                    'uptime': r['uptime'] or 'N/A',
                    'cpu_usage_pct': round(float(r['cpu_usage_pct'] or 0), 2),
                    'used_memory_gb': used_gb,
                    'total_memory_gb': total_gb,
                    'ballooned_memory_gb': bal_gb,
                    'ds_free_pct': round(float(r['ds_free_pct'] or 0), 2) if r['ds_free_pct'] is not None else None,
                    'update_clock': _ts(r['update_clock']),
                })

            # VMware VM(가상머신)
            vm_rows = await conn.fetch("""
                SELECT vi.hostid, vi.asset_name, vi.ip, vi.hypervisor_name,
                       vi.power_state, vi.num_vcpu, vi.vmem, vi.cluster_name,
                       vi.snapshot_count, vi.vmware_tools_status,
                       vc.cpu_usage_pct,
                       vm.host_memory_usage_pct,
                       fs.min_free_pct, fs.min_fs_name,
                       vi.update_clock
                FROM vmware_info vi
                LEFT JOIN vmware_cpu vc ON vi.hostid = vc.hostid
                LEFT JOIN vmware_mem vm ON vi.hostid = vm.hostid
                LEFT JOIN (
                    SELECT DISTINCT ON (hostid)
                        hostid,
                        free_disk_pct AS min_free_pct,
                        filesystem    AS min_fs_name
                    FROM vmware_fs
                    WHERE flag = 'y' AND total_disk > 0
                    ORDER BY hostid, free_disk_pct ASC NULLS LAST
                ) fs ON vi.hostid = fs.hostid
                ORDER BY vi.asset_name
            """)
            for r in vm_rows:
                vmem_gb = round(float(r['vmem'] or 0) / (1024**3), 1)
                vmware_vms.append({
                    'type': 'VMware VM(가상머신)',
                    'hostid': r['hostid'],
                    'asset_name': r['asset_name'] or '',
                    'ip': r['ip'] or '',
                    'hypervisor_name': r['hypervisor_name'] or 'N/A',
                    'cluster_name': r['cluster_name'] or 'N/A',
                    'power_state': r['power_state'] or 'N/A',
                    'num_vcpu': r['num_vcpu'] or 0,
                    'vmem_gb': vmem_gb,
                    'cpu_usage_pct': round(float(r['cpu_usage_pct'] or 0), 2),
                    'host_mem_usage_pct': round(float(r['host_memory_usage_pct'] or 0), 2),
                    'snapshot_count': r['snapshot_count'] or 0,
                    'vmware_tools_status': r['vmware_tools_status'] or 'N/A',
                    'min_fs_free_pct': round(float(r['min_free_pct'] or 0), 2) if r['min_free_pct'] is not None else None,
                    'min_fs_name': r['min_fs_name'] or '',
                    'update_clock': _ts(r['update_clock']),
                })

        # CPU 임계치 필터링
        all_assets = servers + network_devices + vmware_hypervisors + vmware_vms
        if cpu_threshold > 0:
            matched = [a for a in all_assets if a.get('cpu_utilization', 0) >= cpu_threshold
                       or a.get('cpu_usage_pct', 0) >= cpu_threshold]
            return {
                "found": True,
                "count": len(all_assets),
                "matched_count": len(matched),
                "threshold": cpu_threshold,
                "matched": matched,
                "message": (
                    f"CPU {cpu_threshold}% 이상 장비: {len(matched)}건 / 전체 {len(all_assets)}건. "
                    + ("해당 조건을 충족하는 장비가 없습니다." if len(matched) == 0 else "")
                ),
                "all_assets_summary": [
                    {"asset": a.get('asset') or a.get('asset_name'), "ip": a.get('ip'),
                     "cpu": a.get('cpu_utilization') or a.get('cpu_usage_pct'), "type": a['type']}
                    for a in all_assets
                ],
            }

        return {
            "found": len(all_assets) > 0,
            "count": len(all_assets),
            "server_count": len(servers),
            "network_count": len(network_devices),
            "vmware_hypervisor_count": len(vmware_hypervisors),
            "vmware_vm_count": len(vmware_vms),
            "data": all_assets,
        }


# ─── 11. get_server_interfaces ───
async def tool_get_server_interfaces(
    host_query: str = "all",
    status_filter: str = "all"
) -> Dict:
    """서버 NIC 트래픽 조회 (svr_if_main)"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    where, params = _where_from_resolve(resolve)

    # status_filter 추가
    status_clause = ""
    if status_filter == "active":
        status_clause = "AND operation_status = 1" if where else "WHERE operation_status = 1"
    elif status_filter == "inactive":
        status_clause = "AND operation_status != 1" if where else "WHERE operation_status != 1"

    async with (await get_zabbix_pool()).acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT hostid, ip, asset, os, interface_name,
                   bits_sent, bits_received, operation_status, speed, updated_at
            FROM svr_if_main {where} {status_clause}
            ORDER BY asset, interface_name
        """, *params)

        data = []
        for r in rows:
            sent_bps = int(r['bits_sent'] or 0)
            recv_bps = int(r['bits_received'] or 0)
            data.append({
                'hostid': r['hostid'],
                'asset': r['asset'],
                'ip': r['ip'],
                'os': r['os'],
                'interface': r['interface_name'],
                'status': 'active' if r['operation_status'] == 1 else 'inactive',
                'sent_bps': sent_bps,
                'sent_fmt': _format_bps(sent_bps),
                'received_bps': recv_bps,
                'received_fmt': _format_bps(recv_bps),
                'speed_gbps': round(float(r['speed'] or 0) / 1e9, 2),
                'updated_at': _ts(r['updated_at']),
            })

        active_count = sum(1 for d in data if d['status'] == 'active')
        return {
            "found": len(data) > 0,
            "count": len(data),
            "active_count": active_count,
            "inactive_count": len(data) - active_count,
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 12. get_listen_ports ───
async def tool_get_listen_ports(
    host_query: str = "all",
    port_number: int = None,
    protocol: str = "all"
) -> Dict:
    """서버 Listen 포트 목록 조회 (svr_listen_ports)"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = ["status = 'active'"]
        params = []
        idx = 1

        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(asset ~* ${idx} OR hostid::text = ${idx})")
            params.append(host_key)
            idx += 1

        if port_number:
            clauses.append(f"listen_port = ${idx}")
            params.append(port_number)
            idx += 1

        if protocol in ("tcp", "udp"):
            clauses.append(f"protocol ILIKE ${idx}")
            params.append(protocol)
            idx += 1

        where = f"WHERE {' AND '.join(clauses)}"

        rows = await conn.fetch(f"""
            SELECT asset, hostid, server_ip, protocol, listen_port,
                   bind_ip, bind_type, os, updated_at
            FROM svr_listen_ports {where}
            ORDER BY asset, listen_port
        """, *params)

        data = []
        for r in rows:
            data.append({
                'asset': r['asset'],
                'hostid': r['hostid'],
                'ip': str(r['server_ip']),
                'protocol': r['protocol'],
                'port': r['listen_port'],
                'bind_ip': str(r['bind_ip']),
                'bind_type': r['bind_type'],
                'os': r['os'],
                'updated_at': _ts(r['updated_at']),
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 13. get_connections ───
async def tool_get_connections(
    host_query: str = "all",
    direction: str = "all",
    limit: int = 50
) -> Dict:
    """서버 외부 연결 세션 조회 (svr_connections)"""
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = ["flag = 'y'"]
        params = []
        idx = 1

        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(asset ~* ${idx})")
            params.append(host_key)
            idx += 1

        if direction in ("in", "out"):
            clauses.append(f"direction ILIKE ${idx}")
            params.append(direction)
            idx += 1

        where = f"WHERE {' AND '.join(clauses)}"

        rows = await conn.fetch(f"""
            SELECT asset, protocol, source_ip, source_port,
                   target_ip, target_port, service_name, direction, clock
            FROM svr_connections {where}
            ORDER BY clock DESC
            LIMIT {limit}
        """, *params)

        data = []
        for r in rows:
            data.append({
                'asset': r['asset'],
                'protocol': r['protocol'],
                'source': f"{r['source_ip']}:{r['source_port']}",
                'target': f"{r['target_ip']}:{r['target_port']}",
                'service': r['service_name'] or '',
                'direction': r['direction'],
                'time': _ts(r['clock']),
            })

        in_count = sum(1 for d in data if d['direction'].lower() == 'in')
        out_count = sum(1 for d in data if d['direction'].lower() == 'out')

        return {
            "found": len(data) > 0,
            "count": len(data),
            "in_count": in_count,
            "out_count": out_count,
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 14. get_server_logs ───
async def tool_get_server_logs(
    host_query: str = "all",
    keyword: str = None,
    log_file: str = None,
    severity: str = "all",
    limit: int = 20
) -> Dict:
    """시스템/애플리케이션 로그 원문 조회 (svr_log_messages)
    - log_file: 특정 로그 파일 경로 필터 (부분 일치). 예: '/tmp/a', '/var/log/messages'
    """
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = []
        params = []
        idx = 1

        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(hostname ~* ${idx} OR hostid::text = ${idx})")
            params.append(host_key)
            idx += 1

        if keyword:
            clauses.append(f"logs ~* ${idx}")
            params.append(keyword)
            idx += 1

        # v23.6: 특정 로그 파일 경로 필터 (AIX 앱 로그 등)
        if log_file:
            clauses.append(f"log_files ~* ${idx}")
            params.append(log_file)
            idx += 1

        if severity in ("error", "warning", "info"):
            clauses.append(f"severity ILIKE ${idx}")
            params.append(severity)
            idx += 1

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = await conn.fetch(f"""
            SELECT hostid, hostname, log_files, logs, severity, flag, clock, updated_at
            FROM svr_log_messages {where}
            ORDER BY clock DESC
            LIMIT {limit}
        """, *params)

        data = []
        for r in rows:
            data.append({
                'hostid': r['hostid'],
                'hostname': r['hostname'],
                'log_file': r['log_files'] or '',
                'log': r['logs'] or '',   # 로그 원문 전체 반환 (잘림 없음)
                'severity': r['severity'] or '',
                'flag': r['flag'] or '',
                'time': _ts(r['clock']),
            })

        return {
            "found": len(data) > 0,
            "count": len(data),
            "data": data,
            "match_info": resolve.get("message", ""),
        }


# ─── 14-2. get_errpt ───
async def tool_get_errpt(
    host_query: str = "all",
    keyword: str = None,
    severity: str = "all",
    limit: int = 20,
    days_ago: int = None,
    hours_ago: int = None,
) -> Dict:
    """AIX ERRPT 로그 원문 조회 (svr_log_errpt)
    - host_query: 호스트명, asset, IP, 'all'
    - keyword   : identifier / resource_name / description 키워드 필터 (부분 일치)
    - severity  : 'all' | 'critical'(P) | 'warning'(T,U) | 'info'(I,D,R)
    - limit     : 최대 반환 건수 (기본 20)
    - days_ago  : 최근 N일 이내 (clock 기준)
    - hours_ago : 최근 N시간 이내 (days_ago 보다 우선)
    """
    from datetime import datetime, timedelta

    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    # severity → AIX type 코드 매핑
    # P=Permanent(critical), T=Temporary/U=Undetermined(warning), I/D/R(info)
    SEVERITY_TYPES = {
        "critical": ["P"],
        "warning":  ["T", "U"],
        "info":     ["I", "D", "R"],
    }

    async with (await get_zabbix_pool()).acquire() as conn:
        clauses = []
        params = []
        idx = 1

        # 호스트 필터
        host_key = resolve.get("host_key")
        if host_key:
            clauses.append(f"(e.hostid::text = ${idx} OR EXISTS ("
                           f"SELECT 1 FROM svr_system_main s "
                           f"WHERE s.hostid = e.hostid AND (s.asset ~* ${idx} OR s.asset_name ~* ${idx} OR s.ip = ${idx})"
                           f"))")
            params.append(host_key)
            idx += 1

        # 키워드 필터 (identifier / resource_name / description)
        if keyword:
            clauses.append(
                f"(e.identifier ~* ${idx} OR e.resource_name ~* ${idx} OR e.description ~* ${idx})"
            )
            params.append(keyword)
            idx += 1

        # severity → type 코드 변환
        if severity in SEVERITY_TYPES:
            type_codes = SEVERITY_TYPES[severity]
            placeholders = ", ".join(f"${idx + i}" for i in range(len(type_codes)))
            clauses.append(f"e.type IN ({placeholders})")
            params.extend(type_codes)
            idx += len(type_codes)

        # 시간 필터 (clock 기준, hours_ago 우선)
        if hours_ago and isinstance(hours_ago, int) and hours_ago > 0:
            cutoff = int((datetime.now() - timedelta(hours=hours_ago)).timestamp())
            clauses.append(f"e.clock >= ${idx}")
            params.append(cutoff)
            idx += 1
        elif days_ago and isinstance(days_ago, int) and days_ago > 0:
            cutoff = int((datetime.now() - timedelta(days=days_ago)).timestamp())
            clauses.append(f"e.clock >= ${idx}")
            params.append(cutoff)
            idx += 1

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = await conn.fetch(f"""
            SELECT
                e.uid,
                e.hostid,
                COALESCE(s.asset_name, s.asset, e.asset, e.hostid::text) AS hostname,
                COALESCE(s.ip, '')            AS ip,
                COALESCE(s.os, '')            AS os,
                e.identifier,
                e.timestamp                   AS errpt_time,
                e.type,
                e.class,
                e.resource_name,
                e.description,
                e.log_files,
                e.clock,
                e.flag
            FROM svr_log_errpt e
            LEFT JOIN svr_system_main s ON s.hostid = e.hostid
            {where}
            ORDER BY e.clock DESC
            LIMIT {limit}
        """, *params)

        # type / class 코드 → 한글 레이블
        TYPE_LABEL  = {'P': 'Permanent(critical)', 'T': 'Temporary(warning)',
                       'I': 'Informational(info)', 'U': 'Undetermined(warning)',
                       'D': 'Diagnosed(info)',      'R': 'Recovered(info)'}
        CLASS_LABEL = {'H': 'Hardware', 'S': 'Software', 'O': 'Informational',
                       'U': 'Undetermined', 'P': 'Performance', 'E': 'Environmental'}

        data = []
        for r in rows:
            data.append({
                'uid':           r['uid'],
                'hostid':        r['hostid'],
                'hostname':      r['hostname'],
                'ip':            r['ip'],
                'os':            r['os'],
                'identifier':    r['identifier'] or '',
                'errpt_time':    r['errpt_time'] or '',
                'type':          TYPE_LABEL.get(r['type'] or '', r['type'] or ''),
                'class':         CLASS_LABEL.get(r['class'] or '', r['class'] or ''),
                'resource_name': r['resource_name'] or '',
                'description':   r['description'] or '',   # 로그 원문 전체 반환 (잘림 없음)
                'log_files':     r['log_files'] or 'ERRPT',
                'clock':         _ts(r['clock']),
                'flag':          r['flag'] or '',
            })

        return {
            "found":      len(data) > 0,
            "count":      len(data),
            "data":       data,
            "match_info": resolve.get("message", ""),
            "source_table": "svr_log_errpt",
        }


# ─── 11. get_log_stats (embeddings_server_logs 직접 집계 + chunked integrated_report 병행) ───
async def tool_get_log_stats(
    host_query: str = "all",
    hours_ago: int = 24,
    top_n: int = 10,
) -> Dict:
    """
    embeddings_server_logs 테이블을 직접 집계하여 정확한 로그 통계를 반환합니다.
    - count(*): 기간 내 고유 로그 엔트리 수 (사용자가 SELECT count(*)로 확인한 값과 일치)
    - SUM(occurrence_count): 실제 로그 발생 총 횟수
    - log_level 별 분포, log_source 별 발생 빈도 (Top N)
    - 시간대별 분포 (1시간 단위)
    - embeddings_server_logs_chunked의 integrated_report 샘플 (원인 분석용)
    hours_ago: 최근 N시간 이내 (기본 24시간)
    top_n: 상위 N개 발생원/시간대 반환 (기본 10개)
    """
    resolve = await resolve_host(host_query, source="server")

    if resolve["match_type"] == "not_found":
        return _build_not_found_observation(resolve)
    if resolve["match_type"] == "ambiguous":
        return _build_ambiguous_observation(resolve)

    # embeddings_server_logs / embeddings_server_logs_chunked 는 RAG DB에 있음
    async with (await get_rag_pool()).acquire() as conn:
        host_key = resolve.get("host_key")
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours_ago)

        # ── 호스트 필터 조건 생성 ──
        host_clause = ""
        host_params: list = []
        if host_key and host_key != "all":
            host_clause = " AND (asset_name ~* $2 OR hostname ~* $2)"
            host_params = [host_key]

        # ── [1] embeddings_server_logs 직접 집계 ─────────────────────────────
        # count(*)        = 기간 내 로그 엔트리 수 (SELECT count(*)와 동일)
        # SUM(occurrence_count) = 실제 발생 건수 합계
        # log_level별, log_source별, 시간대별 분포
        summary_row = await conn.fetchrow(
            f"""
            SELECT
                count(*)                          AS entry_count,
                COALESCE(SUM(occurrence_count), 0) AS total_occurrences
            FROM embeddings_server_logs
            WHERE source_updated_at >= $1{host_clause}
            """,
            since, *host_params,
        )

        # log_level 분포
        level_rows = await conn.fetch(
            f"""
            SELECT log_level,
                   count(*)                           AS entries,
                   COALESCE(SUM(occurrence_count), 0) AS occurrences
            FROM embeddings_server_logs
            WHERE source_updated_at >= $1{host_clause}
            GROUP BY log_level
            ORDER BY occurrences DESC
            """,
            since, *host_params,
        )

        # log_source 분포 (Top N)
        source_rows = await conn.fetch(
            f"""
            SELECT log_source,
                   count(*)                           AS entries,
                   COALESCE(SUM(occurrence_count), 0) AS occurrences
            FROM embeddings_server_logs
            WHERE source_updated_at >= $1{host_clause}
            GROUP BY log_source
            ORDER BY occurrences DESC
            LIMIT ${ 2 + len(host_params) }
            """,
            since, *host_params, top_n,
        )

        # 시간대별 분포 (1시간 버킷, KST+9)
        hourly_rows = await conn.fetch(
            f"""
            SELECT
                to_char(
                    (source_updated_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Seoul'),
                    'YYYY-MM-DD HH24:00'
                ) AS hour_kst,
                count(*)                           AS entries,
                COALESCE(SUM(occurrence_count), 0) AS occurrences
            FROM embeddings_server_logs
            WHERE source_updated_at >= $1{host_clause}
            GROUP BY hour_kst
            ORDER BY occurrences DESC
            LIMIT ${ 2 + len(host_params) }
            """,
            since, *host_params, top_n,
        )

        # 대표 로그 Top N (occurrence_count 상위)
        top_log_rows = await conn.fetch(
            f"""
            SELECT log_level, log_source, logs, occurrence_count
            FROM embeddings_server_logs
            WHERE source_updated_at >= $1{host_clause}
            ORDER BY occurrence_count DESC
            LIMIT 5
            """,
            since, *host_params,
        )

        entry_count       = int(summary_row["entry_count"] or 0)
        total_occurrences = int(summary_row["total_occurrences"] or 0)

        # ── [2] embeddings_server_logs_chunked integrated_report 샘플 ─────────
        # (원인 분석용 텍스트 — 정확한 건수는 위 집계 사용)
        chunk_params: list = [since]
        chunk_host_filter = ""
        if host_key and host_key != "all":
            hid_rows = await conn.fetch(
                "SELECT DISTINCT hostid FROM embeddings_server_logs "
                "WHERE asset_name ~* $1 OR hostname ~* $1 LIMIT 50",
                host_key,
            )
            if hid_rows:
                hostids = [str(r["hostid"]) for r in hid_rows]
                phs = ", ".join(f"${i}" for i in range(2, 2 + len(hostids)))
                chunk_host_filter = f" AND hostid::text IN ({phs})"
                chunk_params.extend(hostids)

        chunk_rows = await conn.fetch(
            f"""
            SELECT time_bucket, integrated_report
            FROM embeddings_server_logs_chunked
            WHERE time_bucket >= $1{chunk_host_filter}
            ORDER BY time_bucket DESC
            LIMIT 6
            """,
            *chunk_params,
        )

        chunk_summaries = []
        for cr in chunk_rows:
            tb = cr["time_bucket"]
            try:
                if hasattr(tb, 'astimezone'):
                    tb_kst = tb.astimezone(timezone(timedelta(hours=9)))
                    tb_str = tb_kst.strftime("%Y-%m-%d %H:%M")
                else:
                    tb_str = str(tb)[:16] if tb else "unknown"
            except Exception:
                tb_str = str(tb)[:16] if tb else "unknown"
            report = cr["integrated_report"]
            if report:
                chunk_summaries.append({"time_bucket": tb_str, "summary": str(report)[:500]})

        return {
            "found": entry_count > 0,
            "count": entry_count,
            "data": {
                "period_hours": hours_ago,
                "host": host_query,
                # 사용자가 SELECT count(*)로 확인한 값과 동일한 기준
                "log_entry_count": entry_count,
                # 실제 발생 건수 (occurrence_count 합산)
                "total_occurrences": total_occurrences,
                "severity_distribution": [
                    {"level": r["log_level"] or "unknown",
                     "entries": int(r["entries"]),
                     "occurrences": int(r["occurrences"])}
                    for r in level_rows
                ],
                "top_sources": [
                    {"source": r["log_source"] or "unknown",
                     "entries": int(r["entries"]),
                     "occurrences": int(r["occurrences"])}
                    for r in source_rows
                ],
                "hourly_distribution": [
                    {"hour_kst": r["hour_kst"],
                     "entries": int(r["entries"]),
                     "occurrences": int(r["occurrences"])}
                    for r in hourly_rows
                ],
                "top_log_samples": [
                    {"level": r["log_level"], "source": r["log_source"],
                     "log": r["logs"] or "",   # 로그 원문 전체 반환 (잘림 없음)
                     "occurrence_count": int(r["occurrence_count"] or 0)}
                    for r in top_log_rows
                ],
                "integrated_report_samples": chunk_summaries,
            },
            "match_info": resolve.get("message", ""),
        }


# ─── 10. search_historical_data (RAG 벡터 검색 래핑) ───
async def tool_search_historical_data(
    search_query: str,
    categories: List[str] = None,
    top_k: int = 5,
    days_ago: int = None,
    hours_ago: int = None,
) -> Dict:
    """RAG 벡터 검색 → Observation
    days_ago: 최근 N일 이내 데이터만 검색 (None이면 전체 기간)
    hours_ago: 최근 N시간 이내 데이터만 검색 (days_ago보다 우선)
    """
    from app.services.vector_search import vector_search
    from datetime import datetime, timedelta

    # hours_ago / days_ago → start_time/end_time 필터 변환
    filters = {}
    if hours_ago and isinstance(hours_ago, int) and hours_ago > 0:
        now = datetime.now()
        filters["start_time"] = now - timedelta(hours=hours_ago)
        filters["end_time"] = now
    elif days_ago and isinstance(days_ago, int) and days_ago > 0:
        now = datetime.now()
        filters["start_time"] = now - timedelta(days=days_ago)
        filters["end_time"] = now

    try:
        results = await vector_search(
            query_text=search_query,
            top_k=top_k,
            categories=categories,
            filters=filters if filters else None,
        )

        if not results:
            return {
                "found": False,
                "count": 0,
                "data": [],
                "message": f"'{search_query}'에 대한 관련 데이터를 찾을 수 없습니다."
            }

        # 벡터 검색 결과를 Observation 형태로 변환
        data = []
        for r in results:
            item = {
                "source_table": r.get('source_table', ''),
                "category": r.get('category', ''),
                "similarity": round(r.get('similarity', 0), 4),
                "asset": r.get('asset_name') or r.get('asset', ''),
                "ip": r.get('ip', ''),
                "os": r.get('os', ''),
                "group_name": r.get('group_name', ''),
                "summary": r.get('summary', ''),
            }
            # [v1.1] 안정성·우선순위·수집시각 포함 — LLM 중요도 판단 및 시점 파악용
            if r.get('priority') is not None:
                item['priority'] = r['priority']
            if r.get('stability_level'):
                item['stability_level'] = r['stability_level']
            if r.get('source_updated_at') is not None:
                ts = r['source_updated_at']
                item['source_updated_at'] = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
            data.append(item)

        return {
            "found": True,
            "count": len(data),
            "data": data,
        }
    except Exception as e:
        logger.error(f"[RAG Search] 실패: {e}")
        return {
            "found": False,
            "count": 0,
            "data": [],
            "message": f"검색 중 오류 발생: {str(e)}"
        }


# ============================================================
# Tool 디스패치: tool_name → 실행 함수 매핑
# ============================================================
async def tool_correlate_events(
    host_query: str = "all",
    days_ago: int = 1,
    signals: Optional[List[str]] = None,
) -> Dict:
    """
    Phase 3-A: 멀티 시그널 상관 분석 도구.
    alerts + server_status + logs 를 동시에 조회해 시간 축으로 상관관계를 분석합니다.
    signals: ["alert", "cpu", "memory", "log"] 중 선택 (기본: 전체)
    """
    if signals is None:
        signals = ["alert", "cpu", "memory", "log"]

    results = {}

    # ── 1. Alert 수집 ──
    if "alert" in signals:
        try:
            alert_res = await tool_get_alerts(
                host_query=host_query, status_filter="all", limit=50
            )
            results["alerts"] = alert_res.get("data", [])
        except Exception as e:
            logger.warning(f"[correlate] alerts 수집 실패: {e}")
            results["alerts"] = []

    # ── 2. 서버 상태 (CPU/메모리) ──
    if "cpu" in signals or "memory" in signals:
        try:
            status_res = await tool_get_server_status(host_query=host_query)
            results["server_status"] = status_res.get("data", [])
        except Exception as e:
            logger.warning(f"[correlate] server_status 수집 실패: {e}")
            results["server_status"] = []

    # ── 3. 로그 이력 ──
    if "log" in signals:
        try:
            log_res = await tool_search_historical_data(
                search_query="에러 경고 장애 이상",
                categories=["log", "server"],
                days_ago=days_ago,
                top_k=20,
            )
            results["logs"] = log_res.get("data", [])
        except Exception as e:
            logger.warning(f"[correlate] logs 수집 실패: {e}")
            results["logs"] = []

    # ── 상관 분석 요약 생성 ──
    alert_count = len(results.get("alerts", []))
    log_count = len(results.get("logs", []))
    server_count = len(results.get("server_status", []))

    # 임계치 초과 서버 추출
    critical_servers = []
    for s in results.get("server_status", []):
        cpu = s.get("cpu_usage", 0) or 0
        mem = s.get("memory_usage", 0) or 0
        try:
            cpu = float(str(cpu).replace("%", "").strip())
            mem = float(str(mem).replace("%", "").strip())
        except (ValueError, TypeError):
            cpu, mem = 0, 0
        if cpu >= 80 or mem >= 80:
            critical_servers.append({
                "host": s.get("host", "unknown"),
                "cpu": f"{cpu:.1f}%",
                "memory": f"{mem:.1f}%",
            })

    # 심각도별 alert 집계
    sev_counts: Dict[str, int] = {}
    for a in results.get("alerts", []):
        sev = str(a.get("severity", a.get("심각도", "unknown"))).lower()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    correlation_summary = {
        "분석_기간": f"최근 {days_ago}일",
        "대상_호스트": host_query,
        "알림_총계": alert_count,
        "심각도별_알림": sev_counts,
        "임계치_초과_서버": critical_servers,
        "이상_로그_건수": log_count,
        "상관_판정": (
            "위험 — 알림·CPU/메모리 임계 초과·에러 로그 동시 발생"
            if (alert_count > 5 and critical_servers and log_count > 3)
            else "경고 — 일부 지표 이상"
            if (alert_count > 0 or critical_servers or log_count > 0)
            else "정상"
        ),
    }

    combined_data = []
    # 알림 항목 추가
    for a in results.get("alerts", [])[:10]:
        combined_data.append({
            "시각": a.get("time", a.get("시각", "")),
            "신호_유형": "알림",
            "내용": a.get("alert_name", a.get("이벤트", str(a)[:80])),
            "심각도": a.get("severity", a.get("심각도", "")),
            "호스트": a.get("host", host_query),
        })
    # 로그 항목 추가
    for lg in results.get("logs", [])[:10]:
        combined_data.append({
            "시각": lg.get("timestamp", lg.get("time", "")),
            "신호_유형": "로그",
            "내용": str(lg.get("message", lg.get("content", str(lg)[:80]))),
            "심각도": lg.get("severity", "info"),
            "호스트": lg.get("host", host_query),
        })

    # 시간 역순 정렬
    combined_data.sort(key=lambda x: x.get("시각", ""), reverse=True)

    total = alert_count + log_count
    logger.info(f"[correlate] host={host_query}, days={days_ago}, alerts={alert_count}, logs={log_count}, critical={len(critical_servers)}")

    return {
        "found": total > 0,
        "count": total,
        "data": combined_data,
        "correlation_summary": correlation_summary,
        "raw": results,
    }


def _vmware_format_vm_row(r) -> Dict:
    """vmware_info 조회 행 → 도구 결과 dict (실시간 VM)."""
    vmem_gb = round(float(r['vmem'] or 0) / (1024**3), 1)
    return {
        "vmware_type": "VM",
        "장비_분류": "VMware VM(가상머신)",
        "hostid": r['hostid'],
        "asset_name": r['asset_name'] or '',
        "ip": r['ip'] or '',
        "hypervisor_name": r['hypervisor_name'] or 'N/A',
        "cluster_name": r['cluster_name'] or 'N/A',
        "datacenter_name": r['datacenter_name'] or 'N/A',
        "power_state": r['power_state'] or 'N/A',
        "num_vcpu": r['num_vcpu'] or 0,
        "vmem_gb": vmem_gb,
        "cpu_usage_pct": round(float(r['cpu_usage_pct'] or 0), 2),
        "cpu_ready_ms": round(float(r['cpu_ready_ms'] or 0), 2),
        "host_mem_usage_pct": round(float(r['host_memory_usage_pct'] or 0), 2),
        "guest_memory_usage_mb": round(float(r['guest_memory_usage'] or 0) / (1024**2), 1),
        "host_memory_consumed_mb": round(float(r['host_memory_consumed'] or 0) / (1024**2), 1),
        "compressed_memory_mb": round(float(r['compressed_memory'] or 0) / (1024**2), 1),
        "snapshot_count": r['snapshot_count'] or 0,
        "snapshot_latest_date": r['snapshot_latest_date'] or 'N/A',
        "snapshot_consolidation_needed": r['snapshot_consolidation_needed'] or 'N/A',
        "vmware_tools_status": r['vmware_tools_status'] or 'N/A',
        "vmware_tools_version": r['vmware_tools_version'] or 'N/A',
        "uptime": r['uptime'] or 'N/A',
        "min_fs_free_pct": round(float(r['min_free_pct'] or 0), 2) if r['min_free_pct'] is not None else None,
        "min_fs_name": r['min_fs_name'] or '',
        "update_clock": _ts(r['update_clock']),
    }


async def _vmware_realtime_vms_by_cpu(
    conn,
    host_query: str,
    fetch_limit: int,
) -> list:
    """실시간 VMware VM 목록을 CPU 사용률 내림차순으로 조회 (anomaly 폴백용)."""
    where_parts = []
    params = []
    if host_query and host_query.lower() != "all":
        where_parts.append(
            "(vi.asset_name ILIKE $1 OR vi.ip ILIKE $1 "
            "OR vi.hypervisor_name ILIKE $1 "
            "OR CAST(vi.hostid AS TEXT) = $1)"
        )
        params.append(f"%{host_query}%")
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    vm_rows = await conn.fetch(f"""
        SELECT
            vi.hostid, vi.asset_name, vi.ip,
            vi.hypervisor_name, vi.cluster_name, vi.datacenter_name,
            vi.power_state, vi.num_vcpu, vi.vmem,
            vi.snapshot_count, vi.snapshot_latest_date,
            vi.snapshot_consolidation_needed,
            vi.vmware_tools_status, vi.vmware_tools_version,
            vi.uptime, vi.uptime_guest_os,
            vc.cpu_usage_pct, vc.cpu_ready_ms,
            vm.host_memory_usage_pct, vm.guest_memory_usage,
            vm.host_memory_consumed, vm.compressed_memory,
            fs.min_free_pct, fs.min_fs_name,
            vi.update_clock
        FROM vmware_info vi
        LEFT JOIN vmware_cpu vc ON vi.hostid = vc.hostid
        LEFT JOIN vmware_mem vm ON vi.hostid = vm.hostid
        LEFT JOIN (
            SELECT DISTINCT ON (hostid)
                hostid,
                free_disk_pct AS min_free_pct,
                filesystem    AS min_fs_name
            FROM vmware_fs
            WHERE flag = 'y' AND total_disk > 0
            ORDER BY hostid, free_disk_pct ASC NULLS LAST
        ) fs ON vi.hostid = fs.hostid
        {where_sql}
        ORDER BY vc.cpu_usage_pct DESC NULLS LAST, vi.asset_name
        LIMIT {fetch_limit}
    """, *params)

    return [_vmware_format_vm_row(r) for r in vm_rows]


# ─── 18. get_vmware ───
async def tool_get_vmware(
    host_query: str = "all",
    vmware_type: str = "all",
    mode: str = "realtime",
    priority: int = 0,
    limit: int = 20,
    days_ago: int = 1,
    keyword: str = None,
) -> Dict:
    """
    VMware 하이퍼바이저/VM 조회.
    mode='realtime' : Zabbix DB 직접 조회 (현재 상태)
    mode='anomaly'  : RAG DB embeddings_vmware 조회 (이상징후 이력)
    """
    _PRIORITY_LABEL = {0: "정상", 1: "Warning", 2: "Critical"}

    # ── realtime 모드 ─────────────────────────────────────────
    if mode == "realtime":
        async with (await get_zabbix_pool()).acquire() as conn:
            results = []

            if vmware_type in ("all", "Hypervisor"):
                where_parts = []
                params = []
                if host_query and host_query.lower() != "all":
                    where_parts.append(
                        "(hi.asset ILIKE $1 OR hi.asset ILIKE $1 "
                        "OR CAST(hi.hostid AS TEXT) = $1)"
                    )
                    params.append(f"%{host_query}%")
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

                hv_rows = await conn.fetch(f"""
                    SELECT
                        hi.hostid, hi.asset AS asset_name,
                        hi.cluster_name, hi.datacenter_name,
                        hi.overall_status, hi.connection_state,
                        hi.num_guest_vms, hi.version, hi.uptime,
                        ci.cpu_usage_pct, ci.cpu_cores, ci.cpu_threads, ci.cpu_model,
                        mi.total_memory, mi.used_memory, mi.ballooned_memory,
                        di.total_size AS ds_total_size, di.free_space_pct AS ds_free_pct,
                        di.read_latency AS ds_read_latency, di.write_latency AS ds_write_latency,
                        ni.bytes_received, ni.bytes_transmitted,
                        ni.errors_received, ni.errors_transmitted,
                        hi.update_clock
                    FROM vmware_hypervisor_info hi
                    LEFT JOIN vmware_cpu_info       ci ON hi.hostid = ci.hostid
                    LEFT JOIN vmware_memory_info    mi ON hi.hostid = mi.hostid
                    LEFT JOIN vmware_datastore_info di ON hi.hostid = di.hostid
                    LEFT JOIN vmware_network_info   ni ON hi.hostid = ni.hostid
                    {where_sql}
                    ORDER BY hi.asset
                    LIMIT {limit}
                """, *params)

                for r in hv_rows:
                    used_gb  = round(float(r['used_memory']  or 0) / (1024**3), 1)
                    total_gb = round(float(r['total_memory'] or 0) / (1024**3), 1)
                    bal_gb   = round(float(r['ballooned_memory'] or 0) / (1024**3), 1)
                    ds_free  = round(float(r['ds_free_pct'] or 0), 2) if r['ds_free_pct'] is not None else None
                    results.append({
                        "vmware_type": "Hypervisor",
                        "장비_분류": "VMware 하이퍼바이저(물리장비)",
                        "hostid": r['hostid'],
                        "asset_name": r['asset_name'] or '',
                        "cluster_name": r['cluster_name'] or 'N/A',
                        "datacenter_name": r['datacenter_name'] or 'N/A',
                        "overall_status": r['overall_status'] or 'N/A',
                        "connection_state": r['connection_state'] or 'N/A',
                        "num_guest_vms": r['num_guest_vms'] or 0,
                        "esxi_version": r['version'] or 'N/A',
                        "uptime": r['uptime'] or 'N/A',
                        "cpu_usage_pct": round(float(r['cpu_usage_pct'] or 0), 2),
                        "cpu_cores": r['cpu_cores'] or 0,
                        "cpu_threads": r['cpu_threads'] or 0,
                        "cpu_model": r['cpu_model'] or 'N/A',
                        "used_memory_gb": used_gb,
                        "total_memory_gb": total_gb,
                        "ballooned_memory_gb": bal_gb,
                        "ds_total_gb": round(float(r['ds_total_size'] or 0) / (1024**3), 1) if r['ds_total_size'] else None,
                        "ds_free_pct": ds_free,
                        "ds_read_latency_ms": round(float(r['ds_read_latency'] or 0), 2),
                        "ds_write_latency_ms": round(float(r['ds_write_latency'] or 0), 2),
                        "net_rx_bytes": r['bytes_received'] or 0,
                        "net_tx_bytes": r['bytes_transmitted'] or 0,
                        "net_errors_rx": r['errors_received'] or 0,
                        "net_errors_tx": r['errors_transmitted'] or 0,
                        "update_clock": _ts(r['update_clock']),
                    })

            if vmware_type in ("all", "VM"):
                where_parts = []
                params = []
                if host_query and host_query.lower() != "all":
                    where_parts.append(
                        "(vi.asset_name ILIKE $1 OR vi.ip ILIKE $1 "
                        "OR vi.hypervisor_name ILIKE $1 "
                        "OR CAST(vi.hostid AS TEXT) = $1)"
                    )
                    params.append(f"%{host_query}%")
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

                vm_rows = await conn.fetch(f"""
                    SELECT
                        vi.hostid, vi.asset_name, vi.ip,
                        vi.hypervisor_name, vi.cluster_name, vi.datacenter_name,
                        vi.power_state, vi.num_vcpu, vi.vmem,
                        vi.snapshot_count, vi.snapshot_latest_date,
                        vi.snapshot_consolidation_needed,
                        vi.vmware_tools_status, vi.vmware_tools_version,
                        vi.uptime, vi.uptime_guest_os,
                        vc.cpu_usage_pct, vc.cpu_ready_ms,
                        vm.host_memory_usage_pct, vm.guest_memory_usage,
                        vm.host_memory_consumed, vm.compressed_memory,
                        fs.min_free_pct, fs.min_fs_name,
                        vi.update_clock
                    FROM vmware_info vi
                    LEFT JOIN vmware_cpu vc ON vi.hostid = vc.hostid
                    LEFT JOIN vmware_mem vm ON vi.hostid = vm.hostid
                    LEFT JOIN (
                        SELECT DISTINCT ON (hostid)
                            hostid,
                            free_disk_pct AS min_free_pct,
                            filesystem    AS min_fs_name
                        FROM vmware_fs
                        WHERE flag = 'y' AND total_disk > 0
                        ORDER BY hostid, free_disk_pct ASC NULLS LAST
                    ) fs ON vi.hostid = fs.hostid
                    {where_sql}
                    ORDER BY vi.asset_name
                    LIMIT {limit}
                """, *params)

                for r in vm_rows:
                    results.append(_vmware_format_vm_row(r))

            return {
                "found": len(results) > 0,
                "count": len(results),
                "mode": "realtime",
                "vmware_type": vmware_type,
                "host_query": host_query,
                "data": results,
                "match_info": f"VMware {vmware_type} 실시간 조회: {len(results)}건",
            }

    # ── anomaly 모드 ──────────────────────────────────────────
    else:
        from datetime import datetime, timedelta, timezone
        if not isinstance(days_ago, int) or days_ago < 1:
            days_ago = 1
        elif days_ago > 365:
            days_ago = 365
        async with (await get_rag_pool()).acquire() as conn:
            where_parts = ["1=1"]
            params = []
            idx = 1

            # 기간 필터
            since = datetime.now(timezone.utc) - timedelta(days=days_ago)
            where_parts.append(f"source_updated_at >= ${idx}")
            params.append(since)
            idx += 1

            # vmware_type 필터
            if vmware_type.lower() != "all":
                where_parts.append(f"vmware_type = ${idx}")
                params.append(vmware_type)
                idx += 1

            # priority 필터
            if priority > 0:
                where_parts.append(f"priority >= ${idx}")
                params.append(priority)
                idx += 1

            # host 필터
            if host_query and host_query.lower() != "all":
                where_parts.append(
                    f"(asset_name ILIKE ${idx} OR ip ILIKE ${idx} "
                    f"OR CAST(hostid AS TEXT) = ${idx})"
                )
                params.append(f"%{host_query}%")
                idx += 1

            # keyword 필터
            if keyword:
                where_parts.append(
                    f"(anomaly_reason ILIKE ${idx} OR metric_summary ILIKE ${idx})"
                )
                params.append(f"%{keyword}%")
                idx += 1

            where_sql = "WHERE " + " AND ".join(where_parts)

            rows = await conn.fetch(f"""
                SELECT hostid, asset_name, ip, vmware_type,
                       cluster_name, datacenter_name, hypervisor_name,
                       priority, anomaly_reason, metric_summary,
                       source_updated_at
                FROM embeddings_vmware
                {where_sql}
                ORDER BY source_updated_at DESC, priority DESC
                LIMIT {limit}
            """, *params)

            data = []
            for r in rows:
                data.append({
                    "vmware_type": r['vmware_type'],
                    "장비_분류": "VMware 하이퍼바이저(물리장비)" if r['vmware_type'] == 'Hypervisor' else "VMware VM(가상머신)",
                    "hostid": r['hostid'],
                    "asset_name": r['asset_name'] or '',
                    "ip": r['ip'] or '',
                    "cluster_name": r['cluster_name'] or 'N/A',
                    "datacenter_name": r['datacenter_name'] or 'N/A',
                    "hypervisor_name": r['hypervisor_name'] or 'N/A',
                    "priority": r['priority'],
                    "priority_label": _PRIORITY_LABEL.get(r['priority'], 'N/A'),
                    "anomaly_reason": r['anomaly_reason'] or '',
                    "metric_summary": (r['metric_summary'] or '')[:300],
                    "detected_at": _ts(r['source_updated_at']),
                })

            # embeddings_vmware가 비어 있거나 동기화 전이면 0건 → 사용자에게 '아무것도 없음'만 보이는 문제 방지:
            # VM 범위 질의일 때 Zabbix 실시간 VM CPU 상위를 대체 표시(현재 시점 스냅샷, 주간 피크 아님).
            if (
                not data
                and str(vmware_type).lower() in ("all", "vm")
            ):
                try:
                    _lim = max(int(limit) if limit else 20, 40)
                    async with (await get_zabbix_pool()).acquire() as zconn:
                        fb = await _vmware_realtime_vms_by_cpu(zconn, host_query, _lim)
                    if fb:
                        logger.info(
                            "[get_vmware] anomaly 0건 → 실시간 CPU 상위 %s건 폴백",
                            len(fb),
                        )
                        return {
                            "found": True,
                            "count": len(fb),
                            "mode": "anomaly",
                            "vmware_type": vmware_type,
                            "host_query": host_query,
                            "days_ago": days_ago,
                            "priority_filter": priority,
                            "anomaly_row_count": 0,
                            "realtime_fallback": True,
                            "fallback_notice_kr": (
                                "RAG 이상징후 테이블(embeddings_vmware)에 해당 기간에 맞는 레코드가 없습니다. "
                                "아래는 Zabbix 실시간 VMware VM 목록(현재 CPU 사용률 높은 순)이며, "
                                "‘과거 1주일 최고치’가 아니라 ‘현재 시점’ 스냅샷입니다."
                            ),
                            "data": fb,
                            "match_info": (
                                f"VMware 이상징후 이력 0건 → 실시간 CPU 상위 {len(fb)}건 대체"
                            ),
                        }
                except Exception as ex:
                    logger.warning("[get_vmware] anomaly 폴백 실패: %s", ex)

            return {
                "found": len(data) > 0,
                "count": len(data),
                "mode": "anomaly",
                "vmware_type": vmware_type,
                "host_query": host_query,
                "days_ago": days_ago,
                "priority_filter": priority,
                "data": data,
                "match_info": (
                    f"VMware 이상징후 조회: {len(data)}건"
                    if data else "조회된 기간 내에 VMware 이상징후 데이터가 존재하지 않습니다."
                ),
            }


TOOL_EXECUTORS = {
    "get_server_status": tool_get_server_status,
    "get_server_cpu": tool_get_server_cpu,
    "get_server_memory": tool_get_server_memory,
    "get_server_filesystem": tool_get_server_filesystem,
    "get_network_device": tool_get_network_device,
    "get_network_interfaces": tool_get_network_interfaces,
    "get_alerts": tool_get_alerts,
    "get_processes": tool_get_processes,
    "get_all_assets": tool_get_all_assets,
    "get_server_interfaces": tool_get_server_interfaces,
    "get_listen_ports": tool_get_listen_ports,
    "get_connections": tool_get_connections,
    "get_server_logs": tool_get_server_logs,
    "get_errpt":       tool_get_errpt,
    "get_log_stats":   tool_get_log_stats,
    "search_historical_data": tool_search_historical_data,
    "correlate_events": tool_correlate_events,
    "get_vmware":      tool_get_vmware,
}


async def execute_tool(tool_name: str, params: Dict) -> Dict:
    """
    Tool 디스패치 + 실행.
    tool_name이 유효하지 않으면 에러 Observation 반환.
    """
    executor = TOOL_EXECUTORS.get(tool_name)
    if not executor:
        return {
            "found": False,
            "count": 0,
            "data": [],
            "error": f"알 수 없는 도구: {tool_name}",
            "available_tools": list(TOOL_EXECUTORS.keys())
        }

    try:
        # 파라미터 기본값 처리
        clean_params = {k: v for k, v in params.items() if v is not None}
        result = await executor(**clean_params)
        logger.info(f"[ToolExec] {tool_name}({clean_params}) → {result.get('count', 0)}건")
        return result
    except TypeError as e:
        # 파라미터 타입 오류
        logger.error(f"[ToolExec] {tool_name} 파라미터 오류: {e}")
        return {
            "found": False, "count": 0, "data": [],
            "error": f"파라미터 오류: {str(e)}"
        }
    except Exception as e:
        logger.error(f"[ToolExec] {tool_name} 실행 실패: {e}", exc_info=True)
        return {
            "found": False, "count": 0, "data": [],
            "error": f"도구 실행 실패: {str(e)}"
        }

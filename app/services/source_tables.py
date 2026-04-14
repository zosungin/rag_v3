# 도구별 데이터 출처 — 벡터(RAG) 테이블명 · Zabbix 테이블명 (Context Sources UI)
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

# tool_executor SQL 기준 주요 Zabbix 테이블
TOOL_ZABBIX_TABLES: Dict[str, List[str]] = {
    "get_server_status": ["svr_system_main", "svr_cpu_main"],
    "get_server_cpu": ["svr_cpu_main", "proc_stat"],
    "get_server_memory": ["svr_mem_main", "svr_swap_main"],
    "get_server_filesystem": ["svr_filesystem_main"],
    "get_network_device": ["nw_main1"],
    "get_network_interfaces": ["nw_if", "nw_main1"],
    "get_alerts": ["alert_trigger"],
    "get_processes": ["proc_stat"],
    "get_server_interfaces": ["svr_if_main"],
    "get_listen_ports": ["svr_listen_ports"],
    "get_connections": ["svr_connections"],
    "get_server_logs": ["svr_log_messages"],
    "get_errpt": ["svr_log_errpt"],
}

VMWARE_ZABBIX_REALTIME_TABLES = [
    "vmware_hypervisor_info",
    "vmware_cpu_info",
    "vmware_memory_info",
    "vmware_datastore_info",
    "vmware_network_info",
    "vmware_info",
    "vmware_fs",
    "vmware_cpu",
    "vmware_mem",
]


def _normalize_tool_name(tool_name: str) -> str:
    if not tool_name:
        return ""
    return tool_name.split("(", 1)[0].strip()


def _get_all_assets_tables(asset_type: str) -> List[str]:
    at = (asset_type or "all").lower()
    zb: List[str] = []
    if at in ("all", "server"):
        zb.append("svr_system_main")
    if at in ("all", "network"):
        zb.append("nw_main1")
    if at in ("all", "vmware"):
        zb.extend(VMWARE_ZABBIX_REALTIME_TABLES)
    return sorted(set(zb))


def infer_data_source_tables(
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    observation: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    vector_tables: [{"table": str, "count": int}, ...]  — search_historical_data 등
    zabbix_tables: [str, ...]
    note_kr: 선택적 보조 설명
    """
    tool_args = tool_args or {}
    observation = observation or {}
    t = _normalize_tool_name(tool_name)

    if t == "search_historical_data":
        rows = observation.get("data") or []
        c: Counter[str] = Counter()
        for r in rows:
            st = (r.get("source_table") or "").strip()
            if st:
                c[st] += 1
        vector = [{"table": name, "count": n} for name, n in c.most_common()]
        return {"vector_tables": vector, "zabbix_tables": []}

    if t == "get_vmware":
        mode = tool_args.get("mode") or observation.get("mode") or "realtime"
        if str(mode).lower() == "anomaly":
            return {
                "vector_tables": [
                    {"table": "embeddings_vmware", "count": int(observation.get("count") or 0)}
                ],
                "zabbix_tables": [],
            }
        return {"vector_tables": [], "zabbix_tables": list(VMWARE_ZABBIX_REALTIME_TABLES)}

    if t == "get_log_stats":
        return {
            "vector_tables": [
                {"table": "embeddings_server_logs", "count": 0},
                {"table": "embeddings_server_logs_chunked", "count": 0},
            ],
            "zabbix_tables": [],
        }

    if t == "get_all_assets":
        return {
            "vector_tables": [],
            "zabbix_tables": _get_all_assets_tables(str(tool_args.get("asset_type") or "all")),
        }

    if t == "correlate_events":
        return {
            "vector_tables": [],
            "zabbix_tables": ["alert_trigger", "svr_system_main"],
            "note_kr": "로그 이력은 내부적으로 RAG(search_historical_data)로 조회될 수 있음",
        }

    zabbix = list(TOOL_ZABBIX_TABLES.get(t, []))
    return {"vector_tables": [], "zabbix_tables": zabbix}


def attach_data_sources_to_trace_entry(
    entry: Dict[str, Any],
    observation: Optional[Dict[str, Any]],
    tool_args: Optional[Dict[str, Any]] = None,
) -> None:
    args = tool_args if tool_args is not None else entry.get("args")
    if not isinstance(args, dict):
        args = {}
    entry["data_sources"] = infer_data_source_tables(entry.get("tool") or "", args, observation)

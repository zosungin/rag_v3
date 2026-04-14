# /app/app/services/mcp_formatter.py
"""
MCP 결과 포매터 v5.0
- v4.1: 모든 MCP Tool에 직접 구조화 적용 (LLM 허위데이터 방지)
- v5.0: 가드레일 강화
  ① _build_all_assets: 조건 필터링 질의 시 조건 미충족 장비에 "정상" 표기
  ② format_mcp_context: LLM에게 전달하는 텍스트에 수치 인용 규칙 추가
  ③ 호스트 미매칭 시 "해당 장비 없음" 응답
"""

import logging
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 메인: MCP 결과 → 구조화된 응답 (LLM 우회)
# ============================================================
def build_structured_response(mcp_result: Dict, query: str) -> Optional[Dict]:
    """
    MCP 결과에서 직접 구조화된 JSON 응답 생성.
    반환값이 있으면 LLM에게는 분석 코멘트만 요청.
    """
    results = mcp_result.get('results', [])
    tools = mcp_result.get('tools_executed', [])

    if not results:
        return None

    # v5.0: 모든 tool이 0건 반환 시 → 호스트 미매칭
    total_records = sum(r.get('count', 0) for r in results)
    if total_records == 0:
        return _build_no_match_response(query, tools)

    tool_names = [t['tool'] for t in tools]

    # 우선순위: 가장 구체적인 Tool부터
    if 'network_interfaces' in tool_names:
        return _build_interface_list(results, query)
    if 'network_device' in tool_names and 'server_status' not in tool_names:
        return _build_network_device(results, query)
    if 'server_status' in tool_names and 'network_device' not in tool_names:
        return _build_server_list(results, query)
    if 'server_status' in tool_names and 'network_device' in tool_names:
        return _build_all_assets(results, query)
    if 'active_alerts' in tool_names or 'all_alerts' in tool_names:
        return _build_alert_list(results, query)
    if 'server_cpu' in tool_names:
        return _build_server_cpu(results, query)
    if 'server_memory' in tool_names:
        return _build_server_memory(results, query)
    if 'server_swap' in tool_names:
        return _build_server_swap(results, query)
    if 'server_filesystem' in tool_names:
        return _build_server_filesystem(results, query)
    if 'server_interface' in tool_names:
        return _build_server_interface(results, query)
    if 'process_list' in tool_names:
        return _build_process_list(results, query)
    if 'listen_ports' in tool_names:
        return _build_listen_ports(results, query)
    if 'connections' in tool_names:
        return _build_connections(results, query)
    if 'ssl_certificates' in tool_names:
        return _build_ssl(results, query)
    if 'vulnerability' in tool_names:
        return _build_vulnerability(results, query)
    if 'eos_summary' in tool_names:
        return _build_eos(results, query)

    return None


# ============================================================
# v5.0: 호스트 미매칭 응답
# ============================================================
def _build_no_match_response(query: str, tools: List[Dict]) -> Dict:
    """MCP 조회 결과가 0건일 때 — 해당 장비 없음 응답"""
    # 질의에서 호스트명 추출 시도
    host_hint = ""
    for t in tools:
        hk = t.get('params', {}).get('host_key')
        if hk:
            host_hint = hk
            break

    if host_hint:
        msg = f"현재 모니터링 데이터에서 '{host_hint}'에 해당하는 장비를 찾을 수 없습니다."
    else:
        msg = "현재 모니터링 데이터에서 조건에 해당하는 장비를 찾을 수 없습니다."

    return {
        "query_type": "general",
        "title": "조회 결과 없음",
        "answer": msg,
        "analysis": msg,
        "note": "장비명, IP 주소, 자산명을 정확히 입력해 주세요.",
        "_mcp_structured": True,
    }


# ============================================================
# 공통 유틸
# ============================================================
def _get_all_data(results: List[Dict]) -> List[Dict]:
    items = []
    for r in results:
        data = r.get('data', [])
        if isinstance(data, list):
            items.extend(data)
    return items


def _ts(val) -> str:
    if not val:
        return ''
    if isinstance(val, str):
        return val
    try:
        return datetime.fromtimestamp(val).strftime('%Y-%m-%d %H:%M:%S')
    except:
        return str(val)


def _pct(val, warn=80, crit=90) -> str:
    v = float(val or 0)
    return f"{v:.1f}%"


def _extract_threshold(query: str) -> Optional[float]:
    """v5.0: 질의에서 임계치 숫자 추출 (예: "50%를 넘는" → 50.0)"""
    match = re.search(r'(\d+(?:\.\d+)?)\s*(%|퍼센트)', query)
    if match:
        return float(match.group(1))
    return None


# ============================================================
# 네트워크 인터페이스 목록
# ============================================================
def _build_interface_list(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None

    total = len(items)
    up = sum(1 for i in items if i.get('link') == 'up')
    errors = [i for i in items if (i.get('in_errors', 0) > 0 or i.get('out_errors', 0) > 0)]

    is_top_query = bool(re.search(r'(top|상위|높은|순위|많은)', query.lower()))

    if is_top_query:
        analysis = f"트래픽 상위 {total}개 인터페이스."
        if items:
            top1 = items[0]
            analysis += f" 1위: {top1.get('interface','')[:30]} (수신 {top1.get('received_fmt','')}, 송신 {top1.get('sent_fmt','')})."
    else:
        analysis = f"총 {total}개 인터페이스. Link Up: {up}개, Down: {total - up}개."

    if errors:
        analysis += f" 에러 발생 포트 {len(errors)}개 주의."

    return {
        "query_type": "list",
        "title": query[:60],
        "columns": ["인터페이스", "Alias", "Link", "속도", "수신 트래픽", "송신 트래픽", "수신 에러", "송신 에러"],
        "items": [{
            "host": i.get('asset_name') or i.get('asset', ''),
            "ip": i.get('ip', ''),
            "interface": i.get('interface', ''),
            "alias": i.get('alias', ''),
            "link_status": i.get('link', ''),
            "bandwidth": i.get('speed_fmt', ''),
            "traffic_in": i.get('received_fmt', ''),
            "traffic_out": i.get('sent_fmt', ''),
            "in_errors": str(i.get('in_errors', 0)),
            "out_errors": str(i.get('out_errors', 0)),
        } for i in items],
        "total_count": total,
        "analysis": analysis,
        "measured_at": _ts(items[0].get('update_clock')) if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 네트워크 장비 상태
# ============================================================
def _build_network_device(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None

    if len(items) == 1:
        d = items[0]
        return {
            "query_type": "status",
            "title": f"{d.get('asset_name', '')} 네트워크 장비 상태",
            "host": {"asset": d.get('asset_name', ''), "ip": d.get('ip', ''), "os": d.get('ios', ''), "group": d.get('group_name', '')},
            "metrics": [
                {"label": "ICMP", "value": d.get('icmp_ping', ''), "unit": "", "status": "normal" if d.get('icmp_ping') == 'up' else "critical"},
                {"label": "CPU 사용률", "value": f"{d.get('cpu_usage', 0):.1f}", "unit": "%", "status": "warning" if d.get('cpu_usage', 0) > 80 else "normal"},
                {"label": "메모리 사용률", "value": f"{d.get('mem_usage', 0):.1f}", "unit": "%", "status": "warning" if d.get('mem_usage', 0) > 80 else "normal"},
                {"label": "Up 포트", "value": d.get('up_ports', 0), "unit": f"/ {d.get('total_ports', 0)}", "status": "normal"},
                {"label": "가동일", "value": d.get('uptime_days', 0), "unit": "일", "status": "normal"},
                {"label": "온도", "value": f"{d.get('temperature', 0):.0f}", "unit": "°C", "status": "warning" if d.get('temperature', 0) > 60 else "normal"},
            ],
            "analysis": f"장비 {d.get('asset_name', '')}({d.get('ip', '')}) ICMP {'정상' if d.get('icmp_ping')=='up' else '장애'}. 포트 {d.get('up_ports',0)}/{d.get('total_ports',0)} Up.",
            "measured_at": _ts(d.get('update_clock')),
            "note": None, "_mcp_structured": True,
        }

    return {
        "query_type": "list",
        "title": "네트워크 장비 목록",
        "columns": ["자산명", "IP", "OS", "그룹", "ICMP", "CPU", "메모리", "Up포트", "가동일"],
        "items": [{
            "host": d.get('asset_name', ''), "ip": d.get('ip', ''),
            "os": d.get('ios', ''), "group_name": d.get('group_name', ''),
            "status": d.get('icmp_ping', ''),
            "values": {
                "ICMP": d.get('icmp_ping', ''), "CPU": f"{d.get('cpu_usage',0):.1f}%",
                "메모리": f"{d.get('mem_usage',0):.1f}%",
                "Up포트": f"{d.get('up_ports',0)}/{d.get('total_ports',0)}",
                "가동일": f"{d.get('uptime_days',0)}일",
            }
        } for d in items],
        "total_count": len(items),
        "analysis": f"네트워크 장비 {len(items)}대 조회.",
        "measured_at": _ts(items[0].get('update_clock')) if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 서버 목록
# ============================================================
def _build_server_list(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None

    if len(items) == 1:
        d = items[0]
        cpu = d.get('cpu_utilization', 0)
        max_fs = d.get('max_fs', 0)
        return {
            "query_type": "status",
            "title": f"{d.get('asset_name') or d.get('asset', '')} 서버 상태",
            "host": {"asset": d.get('asset_name') or d.get('asset', ''), "ip": d.get('ip', ''), "os": d.get('os', ''), "group": d.get('group_name', '')},
            "metrics": [
                {"label": "CPU 사용률", "value": f"{cpu:.1f}", "unit": "%", "status": "critical" if cpu > 90 else "warning" if cpu > 80 else "normal"},
                {"label": "Swap 사용률", "value": f"{d.get('swap_utilization', 0):.1f}", "unit": "%", "status": "warning" if d.get('swap_utilization', 0) > 80 else "normal"},
                {"label": "최대 FS", "value": f"{max_fs:.1f}", "unit": f"% ({d.get('max_fs_name', '')})", "status": "critical" if max_fs > 90 else "warning" if max_fs > 80 else "normal"},
                {"label": "프로세스", "value": d.get('processes', 0), "unit": "개", "status": "normal"},
                {"label": "로그인 유저", "value": d.get('logined_user', 0), "unit": "명", "status": "normal"},
            ],
            "analysis": f"CPU {cpu:.1f}%{'(주의)' if cpu>80 else ''}, 최대 FS {max_fs:.1f}%{'(주의)' if max_fs>80 else ''}.",
            "measured_at": d.get('updated_at', ''),
            "note": None, "_mcp_structured": True,
        }

    return {
        "query_type": "list",
        "title": "서버 목록",
        "columns": ["자산명", "IP", "OS", "그룹", "CPU", "Swap", "최대FS", "상태"],
        "items": [{
            "host": d.get('asset_name') or d.get('asset', ''), "ip": d.get('ip', ''),
            "os": d.get('os', ''), "group_name": d.get('group_name', ''),
            "status": "정상",
            "values": {
                "CPU": f"{d.get('cpu_utilization',0):.1f}%",
                "Swap": f"{d.get('swap_utilization',0):.1f}%",
                "최대FS": f"{d.get('max_fs',0):.1f}%",
                "상태": "정상",
            }
        } for d in items],
        "total_count": len(items),
        "analysis": f"서버 {len(items)}대 조회.",
        "measured_at": items[0].get('updated_at', '') if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 전체 자산 (서버+네트워크) — v5.0: 조건 필터링 강화
# ============================================================
def _build_all_assets(results: List[Dict], query: str) -> Dict:
    servers = []
    devices = []
    for r in results:
        tool = r.get('tool', '')
        data = r.get('data', [])
        if 'server' in tool:
            servers.extend(data)
        elif 'network' in tool:
            devices.extend(data)

    # v5.0: 조건 필터링 질의 감지 (예: "CPU 50% 초과", "CPU가 높은 장비")
    threshold = _extract_threshold(query)
    query_lower = query.lower()
    is_cpu_filter = 'cpu' in query_lower and threshold is not None
    is_cpu_high = 'cpu' in query_lower and any(kw in query_lower for kw in ['높은', '넘는', '초과', '이상'])

    items = []
    matched_count = 0

    for s in servers:
        cpu_val = float(s.get('cpu_utilization', 0))
        item = {
            "host": s.get('asset_name') or s.get('asset', ''), "ip": s.get('ip', ''),
            "os": s.get('os', ''), "group_name": s.get('group_name', ''),
            "status": "정상",
            "values": {"유형": "서버", "CPU": f"{cpu_val:.1f}%", "상태": "정상"}
        }
        # 임계치 조건 체크
        if is_cpu_filter and cpu_val >= threshold:
            item["values"]["상태"] = "조건 해당"
            matched_count += 1
        items.append(item)

    for d in devices:
        cpu_val = float(d.get('cpu_usage', 0))
        item = {
            "host": d.get('asset_name') or d.get('asset', ''), "ip": d.get('ip', ''),
            "os": d.get('ios', ''), "group_name": d.get('group_name', ''),
            "status": d.get('icmp_ping', ''),
            "values": {"유형": "네트워크", "CPU": f"{cpu_val:.1f}%", "상태": d.get('icmp_ping', '')}
        }
        if is_cpu_filter and cpu_val >= threshold:
            item["values"]["상태"] = "조건 해당"
            matched_count += 1
        items.append(item)

    # v5.0: 조건 필터링 결과를 analysis에 정확히 반영
    total = len(items)
    if is_cpu_filter:
        if matched_count == 0:
            analysis = (f"전체 {total}대(서버 {len(servers)}대, 네트워크 {len(devices)}대) 조회. "
                        f"CPU {threshold:.0f}% 이상인 장비는 없습니다. "
                        f"모든 장비가 정상 범위 내에서 운영 중입니다.")
        else:
            analysis = (f"전체 {total}대 중 CPU {threshold:.0f}% 이상: {matched_count}대. "
                        f"서버 {len(servers)}대, 네트워크 {len(devices)}대.")
    else:
        analysis = f"서버 {len(servers)}대, 네트워크 장비 {len(devices)}대. 총 {total}대."

    return {
        "query_type": "list",
        "title": "전체 자산 목록",
        "columns": ["자산명", "IP", "OS", "그룹", "유형", "CPU", "상태"],
        "items": items,
        "total_count": total,
        "analysis": analysis,
        "measured_at": '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 장애 목록
# ============================================================
def _build_alert_list(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return {"query_type": "alert", "title": "장애 현황", "total_count": 0, "active_count": 0,
                "resolved_count": 0, "alerts": [], "analysis": "현재 활성 장애가 없습니다.", "note": None, "_mcp_structured": True}

    active = [i for i in items if i.get('status') == 'Problem']
    resolved = [i for i in items if i.get('status') != 'Problem']

    alerts = [{
        "host": i.get('asset', ''), "ip": "",
        "alert_name": i.get('name', ''),
        "severity": i.get('priority_text', ''),
        "severity_level": i.get('priority', 0),
        "status": "Active" if i.get('status') == 'Problem' else "Resolved",
        "time": i.get('time', ''),
    } for i in items]

    return {
        "query_type": "alert",
        "title": "장애 현황",
        "total_count": len(items),
        "active_count": len(active),
        "resolved_count": len(resolved),
        "alerts": alerts,
        "analysis": f"전체 {len(items)}건. 미해결 {len(active)}건, 해결 {len(resolved)}건.",
        "measured_at": items[0].get('time', '') if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 서버 CPU
# ============================================================
def _build_server_cpu(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    cpu_items = [i for i in items if 'cpu_utilization' in i]
    proc_items = [i for i in items if 'pid' in i]

    if not cpu_items:
        return None
    d = cpu_items[0]
    cpu = d.get('cpu_utilization', 0)

    resp = {
        "query_type": "status",
        "title": f"{d.get('asset', '')} CPU 상태",
        "host": {"asset": d.get('asset', ''), "ip": d.get('ip', ''), "os": d.get('os', '')},
        "metrics": [
            {"label": "CPU 사용률", "value": f"{cpu:.1f}", "unit": "%", "status": "critical" if cpu > 90 else "warning" if cpu > 80 else "normal"},
            {"label": "CPU 코어", "value": d.get('cores', ''), "unit": "개", "status": "normal"},
            {"label": "User", "value": f"{d.get('cpu_user', 0):.1f}", "unit": "%", "status": "normal"},
            {"label": "System", "value": f"{d.get('cpu_system', 0):.1f}", "unit": "%", "status": "normal"},
            {"label": "Idle", "value": f"{d.get('cpu_idle', 0):.1f}", "unit": "%", "status": "normal"},
        ],
        "analysis": f"CPU {cpu:.1f}%{'(주의!)' if cpu>80 else '(정상)'}. User {d.get('cpu_user',0):.1f}%, System {d.get('cpu_system',0):.1f}%.",
        "measured_at": d.get('updated_at', ''),
        "note": None, "_mcp_structured": True,
    }

    if proc_items:
        resp["sub_title"] = "CPU 상위 프로세스"
        resp["sub_columns"] = ["PID", "User", "CPU%", "MEM%", "Command"]
        resp["sub_items"] = [{
            "values": {"PID": str(p.get('pid','')), "User": p.get('user',''),
                       "CPU%": f"{p.get('cpu',0):.1f}", "MEM%": f"{p.get('mem',0):.1f}",
                       "Command": p.get('command','')}
        } for p in proc_items[:10]]

    return resp


# ============================================================
# 서버 메모리
# ============================================================
def _build_server_memory(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    d = items[0]
    used = d.get('used_pct', 0)
    return {
        "query_type": "status",
        "title": f"{d.get('asset', '')} 메모리 상태",
        "host": {"asset": d.get('asset', ''), "ip": d.get('ip', '')},
        "metrics": [
            {"label": "사용률", "value": f"{used:.1f}", "unit": "%", "status": "critical" if used > 90 else "warning" if used > 80 else "normal"},
            {"label": "전체", "value": f"{d.get('total_gb', 0):.1f}", "unit": "GB", "status": "normal"},
            {"label": "사용", "value": f"{d.get('used_gb', 0):.1f}", "unit": "GB", "status": "normal"},
            {"label": "가용", "value": f"{d.get('available_gb', 0):.1f}", "unit": "GB", "status": "normal"},
        ],
        "analysis": f"메모리 사용률 {used:.1f}%{'(주의)' if used>80 else '(정상)'}. 가용 {d.get('available_gb',0):.1f}GB.",
        "measured_at": d.get('updated_at', ''),
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 서버 Swap
# ============================================================
def _build_server_swap(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    d = items[0]
    used = d.get('used_pct', 0)
    return {
        "query_type": "status",
        "title": f"{d.get('asset', '')} Swap 상태",
        "host": {"asset": d.get('asset', ''), "ip": d.get('ip', '')},
        "metrics": [
            {"label": "사용률", "value": f"{used:.1f}", "unit": "%", "status": "warning" if used > 50 else "normal"},
            {"label": "전체", "value": f"{d.get('total_gb', 0):.1f}", "unit": "GB", "status": "normal"},
            {"label": "여유", "value": f"{d.get('free_gb', 0):.1f}", "unit": "GB", "status": "normal"},
        ],
        "analysis": f"Swap {used:.1f}% 사용{'(주의)' if used>50 else '(정상)'}.",
        "measured_at": d.get('updated_at', ''),
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 서버 파일시스템
# ============================================================
def _build_server_filesystem(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    warn_fs = [i for i in items if float(i.get('utilization', 0)) > 80]
    return {
        "query_type": "list",
        "title": f"{items[0].get('asset', '')} 파일시스템",
        "columns": ["자산명", "IP", "마운트", "사용률", "사용(MB)", "전체(MB)"],
        "items": [{
            "host": i.get('asset', ''), "ip": i.get('ip', ''),
            "values": {
                "마운트": i.get('fs_name', ''),
                "사용률": f"{float(i.get('utilization', 0)):.1f}%",
                "사용(MB)": f"{float(i.get('used_mb', 0)):.0f}",
                "전체(MB)": f"{float(i.get('total_mb', 0)):.0f}",
            }
        } for i in items],
        "total_count": len(items),
        "analysis": f"파일시스템 {len(items)}개. 80% 이상: {len(warn_fs)}개{'(주의)' if warn_fs else '(양호)'}.",
        "measured_at": items[0].get('updated_at', '') if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 서버 인터페이스
# ============================================================
def _build_server_interface(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list",
        "title": f"{items[0].get('asset', '')} 서버 인터페이스",
        "columns": ["자산명", "IP", "인터페이스", "상태", "속도(Gbps)", "수신(bps)", "송신(bps)"],
        "items": [{
            "host": i.get('asset', ''), "ip": i.get('ip', ''),
            "interface": i.get('interface', ''),
            "status": i.get('status', ''),
            "values": {
                "인터페이스": i.get('interface', ''),
                "상태": i.get('status', ''),
                "속도(Gbps)": str(i.get('speed_gbps', '')),
                "수신(bps)": str(i.get('received_bps', 0)),
                "송신(bps)": str(i.get('sent_bps', 0)),
            }
        } for i in items],
        "total_count": len(items),
        "analysis": f"서버 인터페이스 {len(items)}개 조회.",
        "measured_at": items[0].get('updated_at', '') if items else '',
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 프로세스 목록
# ============================================================
def _build_process_list(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list",
        "title": f"프로세스 목록 (CPU 상위)",
        "columns": ["자산명", "PID", "User", "CPU%", "MEM%", "Command"],
        "items": [{
            "host": i.get('asset', ''),
            "values": {
                "PID": str(i.get('pid', '')), "User": i.get('user', ''),
                "CPU%": f"{i.get('cpu', 0):.1f}", "MEM%": f"{i.get('mem', 0):.1f}",
                "Command": i.get('command', ''),
            }
        } for i in items],
        "total_count": len(items),
        "analysis": f"상위 프로세스 {len(items)}건.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# Listen 포트
# ============================================================
def _build_listen_ports(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list",
        "title": "Listen 포트 목록",
        "columns": ["자산명", "IP", "포트", "프로토콜", "프로세스"],
        "items": [{
            "host": i.get('asset', ''), "ip": i.get('ip', ''),
            "values": {
                "포트": str(i.get('port', '')),
                "프로토콜": i.get('protocol', ''),
                "프로세스": i.get('process', ''),
            }
        } for i in items],
        "total_count": len(items),
        "analysis": f"Listen 포트 {len(items)}개.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 연결
# ============================================================
def _build_connections(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list", "title": "연결 현황",
        "columns": ["자산명", "IP", "상태", "건수"],
        "items": [{
            "host": i.get('asset', ''), "ip": i.get('ip', ''),
            "values": {"상태": i.get('state', ''), "건수": str(i.get('count', ''))}
        } for i in items],
        "total_count": len(items), "analysis": f"연결 상태 {len(items)}건.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# SSL 인증서
# ============================================================
def _build_ssl(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list", "title": "SSL 인증서 현황",
        "columns": ["도메인", "만료일", "남은일수", "상태"],
        "items": [{
            "host": i.get('domain', ''),
            "values": {"도메인": i.get('domain', ''), "만료일": i.get('expiry', ''),
                       "남은일수": str(i.get('days_left', '')), "상태": i.get('status', '')}
        } for i in items],
        "total_count": len(items), "analysis": f"SSL 인증서 {len(items)}건.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# 취약점
# ============================================================
def _build_vulnerability(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    return {
        "query_type": "list", "title": "취약점 현황",
        "columns": ["자산명", "IP", "취약점", "심각도", "상태"],
        "items": [{
            "host": i.get('asset', ''), "ip": i.get('ip', ''),
            "values": {"취약점": i.get('name', ''), "심각도": i.get('severity', ''), "상태": i.get('status', '')}
        } for i in items],
        "total_count": len(items), "analysis": f"취약점 {len(items)}건.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# EoS
# ============================================================
def _build_eos(results: List[Dict], query: str) -> Dict:
    items = _get_all_data(results)
    if not items:
        return None
    expired = [i for i in items if i.get('status') == '경과']
    return {
        "query_type": "list", "title": "EoS 현황",
        "columns": ["자산명", "모델명", "제조사", "EoS 상태", "경과일", "EoS 날짜"],
        "items": [{
            "host": i.get('asset', ''),
            "values": {"모델명": i.get('model', ''), "제조사": i.get('vendor', ''),
                       "EoS 상태": i.get('status', ''), "경과일": str(i.get('days', '')),
                       "EoS 날짜": i.get('eos_date', '')}
        } for i in items],
        "total_count": len(items),
        "analysis": f"EoS 대상 {len(items)}건. 경과 {len(expired)}건.",
        "note": None, "_mcp_structured": True,
    }


# ============================================================
# format_mcp_context — v5.0: 수치 인용 가드레일 추가
# ============================================================
def format_mcp_context(mcp_result: Dict) -> str:
    """MCP 결과를 LLM 프롬프트용 텍스트로 변환 (2단계: 분석 코멘트용)"""
    results = mcp_result.get('results', [])
    tools = mcp_result.get('tools_executed', [])
    if not results:
        return ""

    parts = ["[MCP 실시간 조회 결과]"]

    # v5.0: 가드레일 — LLM에게 수치 정확성 재강조
    parts.append("※ 아래 수치는 DB 직접 조회 결과입니다. analysis에서 이 수치를 정확히 인용하세요. 임의 변경 금지.")

    for t in tools:
        parts.append(f"■ {t.get('tool','')}: {t.get('count',0)}건")

    for r in results:
        for row in r.get('data', [])[:15]:
            if isinstance(row, dict):
                fields = []
                for k, v in row.items():
                    if k not in ('hostid', 'update_clock', 'clock') and v:
                        fields.append(f"{k}={v}")
                parts.append(' | '.join(fields[:8]))

    return '\n'.join(parts[:50])


def format_mcp_sources(mcp_result: Dict) -> List[Dict]:
    sources = []
    for t in mcp_result.get('tools_executed', []):
        sources.append({
            "table": t.get('tool', ''),
            "asset": t.get('params', {}).get('host_key', ''),
            "ip": "",
            "summary_preview": f"MCP 실시간 조회: {t.get('tool','')} → {t.get('count',0)}건",
        })
    return sources


def build_mcp_query_info(mcp_result: Dict) -> List[Dict]:
    return mcp_result.get('tools_executed', [])

# /app/app/services/mcp_query.py
"""
MCP (Model Context Protocol) - Zabbix DB 실시간 직접 조회
- 16개 테이블 대상 실시간 쿼리 실행
- 질의 유형별 적절한 SQL 생성
- 결과를 구조화된 응답으로 변환
"""

import asyncpg
import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Any

from app.config import ZABBIX_DB

logger = logging.getLogger(__name__)


# ============================================================
# DB 연결
# ============================================================
async def get_zabbix_conn():
    """Zabbix DB 연결"""
    return await asyncpg.connect(
        host=ZABBIX_DB['host'],
        port=ZABBIX_DB['port'],
        database=ZABBIX_DB['database'],
        user=ZABBIX_DB['user'],
        password=ZABBIX_DB['password']
    )


# ============================================================
# MCP Tool 정의: 서버 관련
# ============================================================

async def mcp_server_status(host_key: str = None) -> Dict:
    """서버 종합 상태 조회 (svr_system_main)"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1 OR system_name ~* $1"
            params = [host_key]
        
        query = f"""
            SELECT hostid, group_name, ip, asset, asset_name, os, system_name,
                   number_of_processes, system_uptime, logined_user, zabbix_agent,
                   cpu_utilization, memory_utilization, swap_utilization,
                   max_fs, avg_fs, max_fs_name, system_description, updated_at
            FROM svr_system_main {where}
            ORDER BY updated_at DESC
        """
        rows = await conn.fetch(query, *params)
        
        results = []
        for r in rows:
            mem_bytes = r['memory_utilization'] or 0
            mem_gb = round(float(mem_bytes) / (1024**3), 2) if mem_bytes else 0
            
            results.append({
                'hostid': r['hostid'],
                'asset': r['asset'],
                'asset_name': r['asset_name'],
                'ip': r['ip'],
                'os': r['os'],
                'group_name': r['group_name'],
                'system_name': r['system_name'],
                'cpu_utilization': float(r['cpu_utilization'] or 0),
                'memory_gb': mem_gb,
                'swap_utilization': float(r['swap_utilization'] or 0),
                'max_fs': float(r['max_fs'] or 0),
                'max_fs_name': r['max_fs_name'] or '',
                'avg_fs': float(r['avg_fs'] or 0),
                'uptime': r['system_uptime'] or '',
                'processes': int(r['number_of_processes'] or 0),
                'logined_user': int(r['logined_user'] or 0),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        
        return {'tool': 'mcp_server_status', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_cpu(host_key: str = None) -> Dict:
    """서버 CPU 실시간 조회 (svr_cpu_main)"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, number_of_cpus,
                   cpu_utilization, cpu_idle_time, cpu_system_time, cpu_user_time, updated_at
            FROM svr_cpu_main {where}
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'], 'os': r['os'],
                'cores': r['number_of_cpus'],
                'cpu_utilization': float(r['cpu_utilization'] or 0),
                'cpu_idle': float(r['cpu_idle_time'] or 0),
                'cpu_system': float(r['cpu_system_time'] or 0),
                'cpu_user': float(r['cpu_user_time'] or 0),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        return {'tool': 'mcp_server_cpu', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_memory(host_key: str = None) -> Dict:
    """서버 메모리 실시간 조회 (svr_mem_main)"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, total_memory, available_memory,
                   memory_utilization, available_memory_in, updated_at
            FROM svr_mem_main {where}
        """, *params)
        
        results = []
        for r in rows:
            total_gb = round(float(r['total_memory'] or 0) / (1024**3), 2)
            avail_gb = round(float(r['available_memory'] or 0) / (1024**3), 2)
            used_gb = round(float(r['memory_utilization'] or 0) / (1024**3), 2)
            
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'], 'os': r['os'],
                'total_gb': total_gb, 'available_gb': avail_gb, 'used_gb': used_gb,
                'available_pct': float(r['available_memory_in'] or 0),
                'used_pct': round(100 - float(r['available_memory_in'] or 0), 2),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        return {'tool': 'mcp_server_memory', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_swap(host_key: str = None) -> Dict:
    """서버 Swap 실시간 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, total_swap_space, free_swap_space,
                   free_swap_space_pct, used_swap_percent, updated_at
            FROM svr_swap_main {where}
        """, *params)
        
        results = []
        for r in rows:
            total_gb = round(float(r['total_swap_space'] or 0) / (1024**3), 2)
            free_gb = round(float(r['free_swap_space'] or 0) / (1024**3), 2)
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'],
                'total_gb': total_gb, 'free_gb': free_gb,
                'free_pct': float(r['free_swap_space_pct'] or 0),
                'used_pct': float(r['used_swap_percent'] or 0),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        return {'tool': 'mcp_server_swap', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_filesystem(host_key: str = None) -> Dict:
    """서버 파일시스템 실시간 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, ip, asset, os, fs_name, total_space, used_space,
                   space_utilization, updated_at
            FROM svr_filesystem_main {where}
            ORDER BY space_utilization DESC
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'],
                'fs_name': r['fs_name'],
                'total_mb': float(r['total_space'] or 0),
                'used_mb': float(r['used_space'] or 0),
                'utilization': float(r['space_utilization'] or 0),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        return {'tool': 'mcp_server_filesystem', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_interface(host_key: str = None) -> Dict:
    """서버 인터페이스 실시간 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, ip, asset, os, interface_name,
                   bits_sent, bits_received, operation_status, speed, updated_at
            FROM svr_if_main {where}
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'],
                'interface': r['interface_name'],
                'sent_bps': int(r['bits_sent'] or 0),
                'received_bps': int(r['bits_received'] or 0),
                'status': 'active' if r['operation_status'] == 1 else 'inactive',
                'speed_gbps': round(float(r['speed'] or 0) / 1e9, 2),
                'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if r['updated_at'] else ''
            })
        return {'tool': 'mcp_server_interface', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_process_list(host_key: str = None, top_n: int = 10) -> Dict:
    """프로세스 목록 실시간 조회 (CPU 상위)"""
    conn = await get_zabbix_conn()
    try:
        where = "WHERE flag = 'y'"
        params = []
        if host_key:
            where += " AND (asset ~* $1 OR hostid::text = $1)"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, asset, pid, user_name, command, cpu, mem, cpu_time, stat, clock
            FROM proc_stat {where}
            ORDER BY cpu DESC
            LIMIT {top_n}
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'],
                'pid': r['pid'], 'user': r['user_name'],
                'command': (r['command'] or '')[:80],
                'cpu': float(r['cpu'] or 0), 'mem': float(r['mem'] or 0),
                'cpu_time': r['cpu_time'] or '', 'stat': r['stat'] or '',
                'clock': r['clock']
            })
        return {'tool': 'mcp_process_list', 'count': len(results), 'data': results}
    finally:
        await conn.close()


# ============================================================
# MCP Tool 정의: 네트워크 관련
# ============================================================

async def mcp_network_device(host_key: str = None) -> Dict:
    """네트워크 장비 상태 조회 (nw_main1)"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR ip ~* $1 OR asset_name ~* $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT hostid, group_name, asset, asset_name, device_name, ip, ios,
                   snmp_agent, uptime, total_interface, up_interface,
                   icmp_ping, icmp_response_time,
                   device_cpu_usage, device_mem_usage,
                   device_temperature_1, device_power_1, device_fan_1,
                   system_description, update_clock
            FROM nw_main1 {where}
        """, *params)
        
        results = []
        for r in rows:
            uptime_days = int(r['uptime'] or 0) // 86400
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'asset_name': r['asset_name'],
                'device_name': r['device_name'], 'ip': r['ip'], 'ios': r['ios'],
                'group_name': r['group_name'],
                'snmp': 'active' if r['snmp_agent'] == 1 else 'inactive',
                'icmp_ping': 'up' if r['icmp_ping'] == 1 else 'down',
                'icmp_ms': float(r['icmp_response_time'] or 0),
                'uptime_days': uptime_days,
                'total_ports': r['total_interface'] or 0,
                'up_ports': r['up_interface'] or 0,
                'down_ports': (r['total_interface'] or 0) - (r['up_interface'] or 0),
                'cpu_usage': float(r['device_cpu_usage'] or 0),
                'mem_usage': float(r['device_mem_usage'] or 0),
                'temperature': float(r['device_temperature_1'] or 0),
                'description': r['system_description'] or '',
                'update_clock': r['update_clock']
            })
        return {'tool': 'mcp_network_device', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_network_interfaces(host_key: str = None, link_filter: str = None, top_n: int = None) -> Dict:
    """네트워크 인터페이스 상태 조회 (nw_if) — v4.1: top_n 지원"""
    conn = await get_zabbix_conn()
    try:
        clauses = []
        params = []
        idx = 1
        
        if host_key:
            clauses.append(f"(n.asset ~* ${idx} OR n.hostid::text = ${idx} OR n.asset ~* ${idx})")
            params.append(host_key)
            idx += 1
        
        if link_filter == 'up':
            clauses.append("n.link = 1")
        elif link_filter == 'down':
            clauses.append("n.link = 0")
        
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        
        # top_n이 있으면 트래픽 기준 정렬 + LIMIT
        if top_n:
            order = "ORDER BY (COALESCE(n.received,0) + COALESCE(n.sent,0)) DESC"
            limit = f"LIMIT {top_n}"
        else:
            order = "ORDER BY n.interface"
            limit = ""
        
        # nw_main1 JOIN으로 IP 가져오기
        rows = await conn.fetch(f"""
            SELECT n.hostid, n.asset, n.interface, n.port_alias_name, n.link,
                   n.received, n.received_rate, n.sent, n.sent_rate, n.port_speed, n.type,
                   n.in_error_value, n.out_error_value, n.update_clock,
                   m.ip, m.asset_name
            FROM nw_if n
            LEFT JOIN nw_main1 m ON n.hostid = m.hostid
            {where}
            {order}
            {limit}
        """, *params)
        
        results = []
        for r in rows:
            # 인터페이스명 전체 (잘리지 않게)
            intf_full = r['interface'] or ''
            alias = r['port_alias_name'] or ''
            
            # 트래픽 포맷팅 (bps → 읽기 쉬운 단위)
            recv = int(r['received'] or 0)
            sent = int(r['sent'] or 0)
            speed = int(r['port_speed'] or 0)
            
            results.append({
                'hostid': r['hostid'],
                'asset': r['asset'],
                'asset_name': r.get('asset_name') or r['asset'],
                'ip': r.get('ip') or '',
                'interface': intf_full,
                'alias': alias,
                'link': 'up' if r['link'] == 1 else 'down',
                'received_bps': recv,
                'received_fmt': _format_bps(recv),
                'received_rate': float(r['received_rate'] or 0),
                'sent_bps': sent,
                'sent_fmt': _format_bps(sent),
                'sent_rate': float(r['sent_rate'] or 0),
                'speed': speed,
                'speed_fmt': _format_bps(speed),
                'type': r['type'] or '',
                'in_errors': int(r['in_error_value'] or 0),
                'out_errors': int(r['out_error_value'] or 0),
                'update_clock': r['update_clock']
            })
        
        logger.info(f"[MCP] network_interfaces: {len(results)}건 (host={host_key}, link={link_filter})")
        return {'tool': 'mcp_network_interfaces', 'count': len(results), 'data': results}
    finally:
        await conn.close()


def _format_bps(bps: int) -> str:
    """bps 값을 읽기 쉬운 단위로 변환"""
    if bps <= 0:
        return "0 bps"
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.2f} Kbps"
    return f"{bps} bps"


# ============================================================
# MCP Tool 정의: Alert/장애 관련
# ============================================================

async def mcp_active_alerts(host_key: str = None) -> Dict:
    """현재 활성 장애 조회 (alert_trigger)"""
    conn = await get_zabbix_conn()
    try:
        where = "WHERE value = 1 AND flag = 'y'"
        params = []
        if host_key:
            where += " AND (asset ~* $1 OR hostid::text = $1)"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT triggerid, asset, hostid, value, priority, name, clock, flag
            FROM alert_trigger {where}
            ORDER BY priority DESC, clock DESC
        """, *params)
        
        priority_map = {0: 'Not classified', 1: 'Information', 2: 'Warning', 3: 'Average', 4: 'High', 5: 'Disaster'}
        results = []
        for r in rows:
            results.append({
                'triggerid': r['triggerid'], 'asset': r['asset'], 'hostid': r['hostid'],
                'name': r['name'],
                'priority': r['priority'], 'priority_text': priority_map.get(r['priority'], ''),
                'status': 'Problem',
                'time': datetime.fromtimestamp(r['clock']).strftime('%Y-%m-%d %H:%M:%S')
            })
        return {'tool': 'mcp_active_alerts', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_all_alerts(host_key: str = None, limit: int = 20) -> Dict:
    """전체 장애 이력 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if host_key:
            where = "WHERE asset ~* $1 OR hostid::text = $1"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT triggerid, asset, hostid, value, priority, name, clock, flag
            FROM alert_trigger {where}
            ORDER BY clock DESC
            LIMIT {limit}
        """, *params)
        
        priority_map = {0: 'Not classified', 1: 'Information', 2: 'Warning', 3: 'Average', 4: 'High', 5: 'Disaster'}
        results = []
        for r in rows:
            is_problem = r['value'] == 1 and r['flag'] == 'y'
            results.append({
                'triggerid': r['triggerid'], 'asset': r['asset'], 'hostid': r['hostid'],
                'name': r['name'],
                'priority': r['priority'], 'priority_text': priority_map.get(r['priority'], ''),
                'status': 'Problem' if is_problem else 'Resolved',
                'time': datetime.fromtimestamp(r['clock']).strftime('%Y-%m-%d %H:%M:%S')
            })
        return {'tool': 'mcp_all_alerts', 'count': len(results), 'data': results}
    finally:
        await conn.close()


# ============================================================
# MCP Tool 정의: 기타
# ============================================================

async def mcp_ssl_certificates(domain: str = None) -> Dict:
    """SSL 인증서 만료일 조회 (eos_url)"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if domain:
            where = "WHERE domain ~* $1"
            params = [domain]
        
        rows = await conn.fetch(f"""
            SELECT domain, cn_name, ssl_start_date, ssl_end_date, days_left,
                   expired_flag, http_code, http_total_time, internet_status, updated_at
            FROM eos_url {where}
            ORDER BY days_left ASC
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'domain': r['domain'], 'cn_name': r['cn_name'],
                'ssl_start': r['ssl_start_date'], 'ssl_end': r['ssl_end_date'],
                'days_left': r['days_left'],
                'expired': r['expired_flag'] == 'y',
                'http_code': r['http_code'],
                'response_time': float(r['http_total_time'] or 0),
                'internet': r['internet_status']
            })
        return {'tool': 'mcp_ssl_certificates', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_listen_ports(host_key: str = None) -> Dict:
    """서버 LISTEN 포트 조회"""
    conn = await get_zabbix_conn()
    try:
        where = "WHERE status = 'active'"
        params = []
        if host_key:
            where += " AND (asset ~* $1 OR hostid::text = $1)"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT asset, hostid, server_ip, protocol, listen_port, bind_ip, bind_type, os, updated_at
            FROM svr_listen_ports {where}
            ORDER BY listen_port
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'asset': r['asset'], 'hostid': r['hostid'],
                'ip': str(r['server_ip']), 'protocol': r['protocol'],
                'port': r['listen_port'], 'bind_ip': str(r['bind_ip']),
                'bind_type': r['bind_type'], 'os': r['os']
            })
        return {'tool': 'mcp_listen_ports', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_connections(host_key: str = None) -> Dict:
    """서버 연결 상태 조회"""
    conn = await get_zabbix_conn()
    try:
        where = "WHERE flag = 'y'"
        params = []
        if host_key:
            where += " AND (asset ~* $1)"
            params = [host_key]
        
        rows = await conn.fetch(f"""
            SELECT asset, protocol, source_ip, source_port, target_ip, target_port,
                   service_name, direction, clock
            FROM svr_connections {where}
            ORDER BY clock DESC LIMIT 50
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'asset': r['asset'], 'protocol': r['protocol'],
                'source': f"{r['source_ip']}:{r['source_port']}",
                'target': f"{r['target_ip']}:{r['target_port']}",
                'service': r['service_name'] or '', 'direction': r['direction'],
                'time': datetime.fromtimestamp(r['clock']).strftime('%Y-%m-%d %H:%M:%S')
            })
        return {'tool': 'mcp_connections', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_vulnerability(host_key: str = None, vuln_only: bool = True) -> Dict:
    """취약점 점검 결과 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        clauses = []
        idx = 1
        
        if host_key:
            clauses.append(f"(asset ~* ${idx} OR hostid::text = ${idx})")
            params.append(host_key)
            idx += 1
        
        if vuln_only:
            clauses.append("result::numeric >= 1")
        
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        
        rows = await conn.fetch(f"""
            SELECT hostid, asset, ip, os, vul_code, result, clock
            FROM svr_vul {where}
            ORDER BY result::numeric DESC, vul_code
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'asset': r['asset'], 'ip': r['ip'], 'os': r['os'],
                'vul_code': r['vul_code'],
                'result': r['result'],
                'status': '취약' if int(r['result'] or 0) >= 1 else ('수동확인' if int(r['result'] or 0) == -1 else '양호'),
                'time': datetime.fromtimestamp(r['clock']).strftime('%Y-%m-%d %H:%M:%S')
            })
        return {'tool': 'mcp_vulnerability', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_server_logs(host_key: str = None, keyword: str = None) -> Dict:
    """서버 로그 조회 (svr_log_messages)"""
    conn = await get_zabbix_conn()
    try:
        clauses = []
        params = []
        idx = 1
        
        if host_key:
            clauses.append(f"(hostname ~* ${idx} OR hostid::text = ${idx})")
            params.append(host_key)
            idx += 1
        
        if keyword:
            clauses.append(f"logs ~* ${idx}")
            params.append(keyword)
            idx += 1
        
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        
        rows = await conn.fetch(f"""
            SELECT hostid, hostname, log_files, logs, severity, flag, clock, updated_at
            FROM svr_log_messages {where}
            ORDER BY clock DESC
            LIMIT 20
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'hostid': r['hostid'], 'hostname': r['hostname'],
                'log_file': r['log_files'] or '',
                'log': (r['logs'] or '')[:300],
                'severity': r['severity'] or '',
                'flag': r['flag'] or '',
                'time': datetime.fromtimestamp(r['clock']).strftime('%Y-%m-%d %H:%M:%S')
            })
        return {'tool': 'mcp_server_logs', 'count': len(results), 'data': results}
    finally:
        await conn.close()


async def mcp_eos_summary(keyword: str = None) -> Dict:
    """EoS 현황 조회"""
    conn = await get_zabbix_conn()
    try:
        where = ""
        params = []
        if keyword:
            where = """WHERE "자산명" ~* $1 OR "모델명" ~* $1 OR "제조사" ~* $1"""
            params = [keyword]
        
        rows = await conn.fetch(f"""
            SELECT "EoS 경과여부", "EoS 경과일", "자산명", "모델명", "수량",
                   "TYPE", "구분", "제조사", "제품구분", "EoS", "EoL"
            FROM eos_summary {where}
            ORDER BY "EoS 경과일"::numeric DESC
            LIMIT 30
        """, *params)
        
        results = []
        for r in rows:
            results.append({
                'status': r['EoS 경과여부'], 'days': r['EoS 경과일'],
                'asset': r['자산명'], 'model': r['모델명'],
                'qty': int(r['수량'] or 0), 'type': r['TYPE'],
                'category': r['구분'], 'vendor': r['제조사'],
                'product': r['제품구분'], 'eos_date': r['EoS']
            })
        return {'tool': 'mcp_eos_summary', 'count': len(results), 'data': results}
    finally:
        await conn.close()


# ============================================================
# MCP 라우터: 질의 → 적절한 Tool 선택 + 실행
# ============================================================

# Tool 매핑 테이블
MCP_TOOLS = {
    'server_status': mcp_server_status,
    'server_cpu': mcp_server_cpu,
    'server_memory': mcp_server_memory,
    'server_swap': mcp_server_swap,
    'server_filesystem': mcp_server_filesystem,
    'server_interface': mcp_server_interface,
    'process_list': mcp_process_list,
    'server_logs': mcp_server_logs,
    'network_device': mcp_network_device,
    'network_interfaces': mcp_network_interfaces,
    'active_alerts': mcp_active_alerts,
    'all_alerts': mcp_all_alerts,
    'ssl_certificates': mcp_ssl_certificates,
    'listen_ports': mcp_listen_ports,
    'connections': mcp_connections,
    'vulnerability': mcp_vulnerability,
    'eos_summary': mcp_eos_summary,
}


def select_mcp_tools(query: str) -> List[Dict]:
    """
    질의에서 필요한 MCP Tool과 파라미터 결정
    v4.0: 호스트명 추출 강화 + link_filter + 서버/네트워크 구분
    """
    import re
    query_lower = query.lower()
    tools_to_run = []
    
    # ── 호스트 키 추출 (v4.0 강화) ──
    host_key = None
    
    # 1) IP 패턴
    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', query)
    if ip_match:
        host_key = ip_match.group(1)
    
    # 2) 자산명 패턴: 영문/숫자/언더스코어/하이픈 조합 (3자 이상)
    if not host_key:
        # "test_network_devices", "int_network_d", "zabbix-server" 등
        asset_match = re.search(r'(?:^|\s)([a-zA-Z][\w.-]{2,})', query)
        if asset_match:
            candidate = asset_match.group(1).lower()
            # 일반 키워드 제외
            general_words = {
                'up', 'down', 'link', 'port', 'top', 'cpu', 'memory', 'disk',
                'swap', 'log', 'alert', 'status', 'list', 'network', 'server',
                'process', 'interface', 'the', 'and', 'for', 'from', 'with',
                'listen', 'connection', 'error', 'warning', 'active', 'inactive'
            }
            if candidate not in general_words and len(candidate) >= 3:
                host_key = asset_match.group(1)
    
    # ── link_filter 추출 ──
    link_filter = None
    if any(kw in query_lower for kw in ['up 상태', 'up 포트', 'link up', '활성 포트', 'up포트', 'up된', 'up port']):
        link_filter = 'up'
    elif re.search(r'\bup\b', query_lower) and any(kw in query_lower for kw in ['포트', 'port', '인터페이스', '목록']):
        link_filter = 'up'
    elif any(kw in query_lower for kw in ['down 상태', 'down 포트', 'link down', '비활성', '다운']):
        link_filter = 'down'
    
    logger.info(f"[MCP Select] host_key={host_key}, link_filter={link_filter}")
    
    # ── Tool 선택 ──
    
    # 장애
    if any(kw in query_lower for kw in ['장애', 'alert', '알림', '경고', '이벤트', 'problem']):
        if any(kw in query_lower for kw in ['현재', '활성', '미해결', '지금']):
            tools_to_run.append({'tool': 'active_alerts', 'params': {'host_key': host_key}})
        else:
            tools_to_run.append({'tool': 'all_alerts', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 네트워크 포트/인터페이스 (v4.0: network_interfaces만, network_device 제외)
    is_network_port = (
        any(kw in query_lower for kw in ['포트', 'port', '인터페이스', 'interface']) and
        any(kw in query_lower for kw in ['네트워크', 'network', 'nw', '장비', '스위치', 'switch'])
    )
    is_link_query = link_filter is not None and any(kw in query_lower for kw in ['포트', 'port', '인터페이스', '목록'])
    is_interface_list = any(kw in query_lower for kw in ['인터페이스', 'interface']) and any(kw in query_lower for kw in ['목록', '리스트', '출력', '조회'])
    # v4.1: 트래픽 top/상위/순위 패턴
    is_traffic_top = (
        any(kw in query_lower for kw in ['트래픽', 'traffic', '대역폭', 'bandwidth']) and
        any(kw in query_lower for kw in ['상위', 'top', '높은', '순위', '많은', '큰'])
    ) or bool(re.search(r'top\s*\d+', query_lower))
    
    if is_network_port or is_link_query or is_interface_list or is_traffic_top:
        params = {'host_key': host_key}
        if link_filter:
            params['link_filter'] = link_filter
        # top N 추출
        top_match = re.search(r'(?:top|상위)\s*(\d+)', query_lower)
        if top_match:
            params['top_n'] = int(top_match.group(1))
        elif is_traffic_top:
            params['top_n'] = 10  # 기본 top 10
        tools_to_run.append({'tool': 'network_interfaces', 'params': params})
        return tools_to_run
    
    # 목록 (서버/네트워크 구분)
    if any(kw in query_lower for kw in ['목록', '리스트', '전체', '모든', '몇 대', '몇대', '몇 개', '몇개']):
        is_server = any(kw in query_lower for kw in ['서버', 'server'])
        is_network = any(kw in query_lower for kw in ['네트워크', 'network', 'nw', '스위치', 'switch', '장비'])
        if is_server and not is_network:
            tools_to_run.append({'tool': 'server_status', 'params': {'host_key': host_key}})
        elif is_network and not is_server:
            tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        else:
            tools_to_run.append({'tool': 'server_status', 'params': {'host_key': host_key}})
            tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # CPU
    if any(kw in query_lower for kw in ['cpu', 'cpu 사용', '프로세서']):
        tools_to_run.append({'tool': 'server_cpu', 'params': {'host_key': host_key}})
        tools_to_run.append({'tool': 'process_list', 'params': {'host_key': host_key, 'top_n': 5}})
        return tools_to_run
    
    # 메모리
    if any(kw in query_lower for kw in ['메모리', 'memory', 'mem', '램']):
        tools_to_run.append({'tool': 'server_memory', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # Swap
    if any(kw in query_lower for kw in ['swap', '스왑']):
        tools_to_run.append({'tool': 'server_swap', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 디스크
    if any(kw in query_lower for kw in ['디스크', 'disk', '파일시스템', 'filesystem', '용량']):
        tools_to_run.append({'tool': 'server_filesystem', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 프로세스
    if any(kw in query_lower for kw in ['프로세스', 'process', '프로그램', 'pid']):
        tools_to_run.append({'tool': 'process_list', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 포트/인터페이스 (네트워크/서버 구분)
    if any(kw in query_lower for kw in ['포트', 'port', '인터페이스', 'interface', '트래픽', '대역폭', 'link']):
        if any(kw in query_lower for kw in ['listen', '리슨', '서비스 포트']):
            tools_to_run.append({'tool': 'listen_ports', 'params': {'host_key': host_key}})
        else:
            tools_to_run.append({'tool': 'server_interface', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 네트워크 장비
    if any(kw in query_lower for kw in ['네트워크', 'network', 'nw', 'aruba', '스위치']):
        tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # SSL
    if any(kw in query_lower for kw in ['ssl', '인증서', 'certificate', '만료', '도메인']):
        domain = None
        domain_match = re.search(r'([\w.-]+\.\w{2,})', query)
        if domain_match:
            domain = domain_match.group(1)
        tools_to_run.append({'tool': 'ssl_certificates', 'params': {'domain': domain}})
        return tools_to_run
    
    # 취약점
    if any(kw in query_lower for kw in ['취약', 'vulnerability', 'vul', '보안', '점검']):
        tools_to_run.append({'tool': 'vulnerability', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # EoS
    if any(kw in query_lower for kw in ['eos', 'eol', '지원종료', '수명', '유지보수']):
        tools_to_run.append({'tool': 'eos_summary', 'params': {'keyword': host_key}})
        return tools_to_run
    
    # 연결
    if any(kw in query_lower for kw in ['연결', 'connection', 'established', 'netstat']):
        tools_to_run.append({'tool': 'connections', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 로그
    if any(kw in query_lower for kw in ['로그', 'log', 'messages', 'syslog', 'error', 'fault', 'warning log']):
        log_keyword = None
        for kw in ['error', 'fault', 'warning', 'critical', 'shutdown', 'restart', 'fail']:
            if kw in query_lower:
                log_keyword = kw
                break
        tools_to_run.append({'tool': 'server_logs', 'params': {'host_key': host_key, 'keyword': log_keyword}})
        return tools_to_run
    
    # 리슨
    if any(kw in query_lower for kw in ['listen', '리슨', '서비스 포트', '대기']):
        tools_to_run.append({'tool': 'listen_ports', 'params': {'host_key': host_key}})
        return tools_to_run
    
    # 기본: 서버 상태 종합
    tools_to_run.append({'tool': 'server_status', 'params': {'host_key': host_key}})
    return tools_to_run


async def execute_mcp_query(query: str) -> Dict:
    """MCP 질의 실행: Tool 선택 → 실행 → 결과 취합"""
    tools_to_run = select_mcp_tools(query)
    
    all_results = []
    executed_queries = []
    
    for tool_info in tools_to_run:
        tool_name = tool_info['tool']
        params = tool_info['params']
        
        tool_func = MCP_TOOLS.get(tool_name)
        if not tool_func:
            continue
        
        try:
            # 파라미터에서 None 제거
            clean_params = {k: v for k, v in params.items() if v is not None}
            result = await tool_func(**clean_params)
            all_results.append(result)
            
            # 실행된 쿼리 정보 기록
            executed_queries.append({
                'tool': tool_name,
                'params': clean_params,
                'count': result.get('count', 0)
            })
        except Exception as e:
            logger.error(f"MCP Tool {tool_name} 실행 실패: {e}")
            executed_queries.append({
                'tool': tool_name,
                'params': params,
                'error': str(e)
            })
    
    return {
        'source': 'mcp',
        'tools_executed': executed_queries,
        'results': all_results,
        'total_records': sum(r.get('count', 0) for r in all_results)
    }

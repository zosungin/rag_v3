# ============================================================
# MCP 라우터: 질의 → 적절한 Tool 선택 + 실행
# v3.3: 호스트명 추출 강화 + link_filter 전달 + 불필요한 tool 제거
# ============================================================

# 이 파일의 select_mcp_tools 함수와 execute_mcp_query 함수만 교체하세요.
# 나머지 MCP Tool 함수들(mcp_server_status, mcp_network_interfaces 등)은 그대로 유지.

import re

# Tool 매핑 테이블 (기존과 동일)
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
    v3.3: 호스트명 추출 강화 + link_filter + 불필요한 tool 호출 방지
    """
    query_lower = query.lower()
    tools_to_run = []

    # ── 호스트 키 추출 (v3.3 강화) ──
    host_key = None

    # 1) IP 패턴
    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', query)
    if ip_match:
        host_key = ip_match.group(1)

    # 2) 자산명 패턴: 영문/숫자/언더스코어/하이픈 조합 (3자 이상)
    #    예: test_network_devices, int_network_d, zabbix-server
    if not host_key:
        asset_match = re.search(r'(?:^|\s)([a-zA-Z][\w.-]{2,})', query)
        if asset_match:
            candidate = asset_match.group(1).lower()
            # 일반적인 키워드가 아닌 경우만 호스트명으로 인식
            general_words = {
                'up', 'down', 'link', 'port', 'top', 'cpu', 'memory', 'disk',
                'swap', 'log', 'alert', 'status', 'list', 'network', 'server',
                'process', 'interface', 'the', 'and', 'for', 'from', 'with'
            }
            if candidate not in general_words and len(candidate) >= 3:
                host_key = asset_match.group(1)

    # ── link_filter 추출 ──
    link_filter = None
    if any(kw in query_lower for kw in ['up 상태', 'up 포트', 'link up', '활성 포트', 'up포트']):
        link_filter = 'up'
    elif re.search(r'\bup\b', query_lower) and any(kw in query_lower for kw in ['포트', 'port', '인터페이스', '목록']):
        link_filter = 'up'
    elif any(kw in query_lower for kw in ['down 상태', 'down 포트', 'link down', '비활성', '다운']):
        link_filter = 'down'

    logger.info(f"[MCP Select] host_key={host_key}, link_filter={link_filter}")

    # ── Tool 선택 (v3.3: 더 정밀한 분기) ──

    # 장애 질의
    if any(kw in query_lower for kw in ['장애', 'alert', '알림', '경고', '이벤트', 'problem']):
        if any(kw in query_lower for kw in ['현재', '활성', '미해결', '지금']):
            tools_to_run.append({'tool': 'active_alerts', 'params': {'host_key': host_key}})
        else:
            tools_to_run.append({'tool': 'all_alerts', 'params': {'host_key': host_key}})
        return tools_to_run

    # 네트워크 포트/인터페이스 질의 (v3.3: network_interfaces만, server_status 제외)
    is_network_port_query = (
        any(kw in query_lower for kw in ['포트', 'port', '인터페이스', 'interface']) and
        any(kw in query_lower for kw in ['네트워크', 'network', 'nw', '장비', '스위치', 'switch'])
    )
    # "UP 포트", "Down 포트" 같은 질의도 네트워크 포트로 분류
    is_link_query = link_filter is not None and any(kw in query_lower for kw in ['포트', 'port', '인터페이스', '목록'])

    if is_network_port_query or is_link_query:
        params = {'host_key': host_key}
        if link_filter:
            params['link_filter'] = link_filter
        tools_to_run.append({'tool': 'network_interfaces', 'params': params})
        return tools_to_run

    # 네트워크 장비 상태 (포트 아닌 일반 장비 질의)
    if any(kw in query_lower for kw in ['네트워크', 'network', 'nw', '스위치', 'switch']):
        tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        return tools_to_run

    # 서버 리슨 포트
    if any(kw in query_lower for kw in ['listen', '리슨', '서비스 포트', '오픈 포트']):
        tools_to_run.append({'tool': 'listen_ports', 'params': {'host_key': host_key}})
        return tools_to_run

    # 목록 질의 (서버/네트워크 구분)
    if any(kw in query_lower for kw in ['목록', '리스트', '전체', '모든', '몇 대', '몇대', '몇 개', '몇개']):
        is_server = any(kw in query_lower for kw in ['서버', 'server'])
        is_network = any(kw in query_lower for kw in ['네트워크', 'network', 'nw', '스위치', 'switch', '장비'])

        if is_server and not is_network:
            # "서버 목록" → 서버만
            tools_to_run.append({'tool': 'server_status', 'params': {'host_key': host_key}})
        elif is_network and not is_server:
            # "네트워크 장비 목록" → 네트워크만
            tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        else:
            # 구분 불가 → 둘 다
            tools_to_run.append({'tool': 'server_status', 'params': {'host_key': host_key}})
            tools_to_run.append({'tool': 'network_device', 'params': {'host_key': host_key}})
        return tools_to_run

    # CPU 질의
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
        tools_to_run.append({'tool': 'server_interface', 'params': {'host_key': host_key}})
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
    if any(kw in query_lower for kw in ['로그', 'log', 'messages', 'syslog', 'error', 'fault']):
        log_keyword = None
        for kw in ['error', 'fault', 'warning', 'critical', 'shutdown', 'restart', 'fail']:
            if kw in query_lower:
                log_keyword = kw
                break
        tools_to_run.append({'tool': 'server_logs', 'params': {'host_key': host_key, 'keyword': log_keyword}})
        return tools_to_run

    # 기본: 서버 상태
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
            clean_params = {k: v for k, v in params.items() if v is not None}
            result = await tool_func(**clean_params)
            all_results.append(result)

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

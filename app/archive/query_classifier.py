# /app/app/services/query_classifier.py
"""
질의 유형 분류 (8개 유형) + 시간 필터 파싱 + MCP/RAG 라우팅
v3.4: embeddings_inventory 연동 강화
  - INVENTORY_KEYWORDS 확장
  - get_recommended_categories에서 infra 카테고리 추천 강화
  - 장비/자산 관련 질의 시 infra 카테고리가 항상 포함되도록 수정
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# ============================================================
# MCP vs RAG 라우팅 (v3.3 강화)
# ============================================================

# MCP 우선 키워드: 실시간/현재 데이터가 필수인 질의
MCP_KEYWORDS = [
    '현재', '지금', '실시간', '지금 현재', '이 순간',
    '현재 상태', '지금 상태', '현재값', '최신값'
]

# MCP 필수 패턴: 벡터 검색으로는 정확한 결과를 얻기 어려운 질의
# → SQL WHERE 조건이 필요한 목록/필터링 질의
MCP_REQUIRED_PATTERNS = [
    # 네트워크 포트 UP/DOWN 목록
    (r'(포트|port|인터페이스|interface).*(목록|리스트|list|현황)', 'network_interfaces'),
    (r'(up|down|업|다운).*(포트|port|인터페이스)', 'network_interfaces'),
    (r'(포트|port|인터페이스).*(up|down|업|다운)', 'network_interfaces'),
    (r'link\s*(up|down)', 'network_interfaces'),
    # 트래픽/대역폭 상위/순위 (v4.1: MCP로 라우팅)
    (r'(트래픽|traffic|대역폭|bandwidth).*(상위|top|높은|순위|많은|큰)', 'network_interfaces'),
    (r'(상위|top|높은|순위|많은).*(트래픽|traffic|대역폭|bandwidth)', 'network_interfaces'),
    (r'top\s*\d+', 'network_interfaces'),
    # 서버 리슨 포트 목록
    (r'(listen|리슨|오픈|open).*(포트|port)', 'listen_ports'),
    (r'(포트|port).*(listen|리슨|오픈|open)', 'listen_ports'),
    # 현재 활성 장애
    (r'(현재|활성|미해결|발생\s*중).*(장애|alert|경고)', 'active_alerts'),
    (r'(장애|alert|경고).*(현재|활성|미해결|발생\s*중)', 'active_alerts'),
    # 현재 프로세스 목록
    (r'(현재|지금).*(프로세스|process)', 'process_list'),
    # 현재 연결 상태
    (r'(현재|지금).*(연결|connection|세션)', 'connections'),
    # 서버/장비 목록 (전체)
    (r'(전체|모든|몇\s*대|몇\s*개).*(서버|장비|자산|호스트)', 'server_status'),
    (r'서버.*(목록|리스트|list|몇\s*대|몇\s*개)', 'server_list'),
    (r'네트워크.*(장비|디바이스).*(목록|리스트|list)', 'network_list'),
    (r'(장비|자산|호스트).*(목록|리스트|list|몇\s*대|몇\s*개)', 'server_status'),
    # CPU/메모리/디스크 상태 조회
    (r'(cpu|메모리|memory|디스크|disk|swap).*(상태|사용률|현황)', 'server_status'),
    (r'(서버|장비).*(상태|현황|정보)', 'server_status'),
]


def classify_data_source(query: str) -> str:
    """
    MCP(실시간 직접조회) vs RAG(벡터검색) vs Hybrid 판별

    v3.3 로직:
    1. MCP_REQUIRED_PATTERNS에 매치 → 'mcp' (SQL 필터링 필수)
    2. MCP_KEYWORDS 포함 → 'mcp' (실시간 데이터 필요)
    3. 시간범위/분석 키워드 → 'rag' (트렌드/비교 분석)
    4. 실시간 + 분석 혼합 → 'hybrid'
    5. 기본 → 'rag'
    """
    query_lower = query.lower().strip()

    # 1. MCP 필수 패턴 체크 — SQL WHERE가 필요한 질의
    for pattern, tool_hint in MCP_REQUIRED_PATTERNS:
        if re.search(pattern, query_lower):
            logger.info(f"[DataSource] MCP 필수 패턴 매치: {pattern} → tool={tool_hint}")
            return 'mcp'

    # 2. 시간범위/분석 키워드 체크
    time_range_patterns = [
        r'지난\s*\d+\s*(일|시간)', r'최근\s*\d+\s*(일|시간)',
        r'\d+\s*(일|시간)\s*(동안|간|이내)', r'오늘', r'어제', r'이번\s*주',
        r'추세', r'변화', r'추이', r'비교'
    ]
    has_time_range = any(re.search(p, query_lower) for p in time_range_patterns)

    analysis_keywords = ['가장', '높은', '낮은', '최대', '최소', 'top', '상위', '하위',
                         '표준편차', '안정성', '추세', '변화', '비교', '분석', '순위', '변동', '통계']
    has_analysis = any(kw in query_lower for kw in analysis_keywords)

    has_realtime = any(kw in query_lower for kw in MCP_KEYWORDS)

    # 3. 분기
    if has_time_range or has_analysis:
        if has_realtime:
            return 'hybrid'
        return 'rag'

    if has_realtime:
        return 'mcp'

    # 4. 목록 질의인데 MCP_REQUIRED에 안 걸린 경우 → rag
    list_keywords = ['목록', '리스트', '전체', '모든', '몇 대', '몇대', '나열']
    if any(kw in query_lower for kw in list_keywords):
        return 'rag'

    return 'rag'


# ============================================================
# 질의 유형별 키워드 정의
# ============================================================
ALERT_KEYWORDS = [
    '장애', 'alert', '알림', '알럿', '경고', '이벤트',
    'warning', 'disaster', 'high', 'problem', 'resolved',
    '트리거', 'trigger', '장애명', '심각도', '미해결', '발생', '해결'
]

STATUS_KEYWORDS = [
    '상태', '현재', '지금', '얼마', '어때', '어떤가',
    '정보', '확인', 'uptime', '가동', '점검'
]

LIST_KEYWORDS = [
    '목록', '리스트', '전체', '모든', '어떤 것',
    '몇 대', '몇 개', '몇대', '몇개', '개수',
    '무엇', '뭐가', '나열', '보여줘', '알려줘',
    'link down', '다운된', '비활성'
]

ANALYSIS_KEYWORDS = [
    '가장', '높은', '낮은', '최대', '최소',
    'top', '상위', '하위', '비교', '분석',
    '추세', '변화', '표준편차', '안정성', '불안정',
    '순위', '랭킹', '많은', '적은', '큰', '작은',
    '과부하', '임계', '위험', '많이', '크게', '변동'
]

RESOURCE_DETAIL_KEYWORDS = [
    '파일시스템', 'filesystem', '디스크', 'disk',
    '인터페이스', 'interface', '포트', 'port',
    '대역폭', 'bandwidth', '트래픽', 'traffic',
    'up/down', 'link up', 'link down', '현황',
    '연결', 'connection', '세션', 'session'
]

LOG_KEYWORDS = [
    '로그', 'log', 'messages', 'syslog', '/var/log',
    'errpt', 'error log', '에러 로그', '시스템 로그',
    '에러', 'error', 'fault', 'warning log',
    '로그파일', '로그 파일'
]

PROCESS_KEYWORDS = [
    '프로세스', 'process', 'pid', 'cpu 사용', 'cpu사용',
    '메모리 사용', '메모리사용', '자원 사용', '리소스 사용',
    'top 10', 'top10', '상위 프로세스', '프로세스 트렌드',
    '자원 점유', '리소스 점유', '프로세스 분석'
]

# v3.4: 인벤토리 키워드 확장 — 장비/자산 기본정보 질의를 폭넓게 캐치
INVENTORY_KEYWORDS = [
    '자산', '인벤토리', 'inventory', '사양', '스펙',
    '서버 정보', '장비 정보', '자산 프로필', '자산 현황',
    '통합 현황', '통합 프로필', '전체 현황 요약',
    # v3.4 추가: 장비 기본정보 질의
    '장비 목록', '장비 현황', '장비목록', '장비현황',
    '자산 목록', '자산목록', '자산 정보', '자산정보',
    '장비 조회', '자산 조회', '호스트 목록', '호스트목록',
    '운영 장비', '관리 장비', '등록 장비', '등록된 장비',
    '서버 목록', '서버목록', '네트워크 장비', '네트워크장비',
    '몇 대', '몇대', '어떤 장비', '어떤 서버',
    '장비 구성', '인프라 현황', '인프라 구성',
    'device_type', 'server', 'network',
]

HOST_PATTERN = re.compile(
    r'(192\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.\d+\.\d+\.\d+|'
    r'61\.\d+\.\d+\.\d+|\d+\.\d+\.\d+\.\d+|'
    r'zabbix\s*server|localhost)',
    re.IGNORECASE
)

TIME_PATTERNS = [
    (r'지난\s*(\d+)\s*일', 'days'),
    (r'최근\s*(\d+)\s*일', 'days'),
    (r'(\d+)\s*일\s*(동안|간|이내|내)', 'days'),
    (r'지난\s*(\d+)\s*시간', 'hours'),
    (r'최근\s*(\d+)\s*시간', 'hours'),
    (r'(\d+)\s*시간\s*(동안|간|이내|내)', 'hours'),
    (r'오늘', 'today'),
    (r'어제', 'yesterday'),
    (r'이번\s*주', 'week'),
]


# ============================================================
# 질의 유형 분류 (8개 유형)
# v3.4: inventory 분류 강화
# ============================================================
def classify_query(query: str) -> str:
    query_lower = query.lower().strip()

    alert_score = sum(1 for kw in ALERT_KEYWORDS if kw in query_lower)
    analysis_score = sum(1 for kw in ANALYSIS_KEYWORDS if kw in query_lower)
    list_score = sum(1 for kw in LIST_KEYWORDS if kw in query_lower)
    status_score = sum(1 for kw in STATUS_KEYWORDS if kw in query_lower)
    resource_detail = sum(1 for kw in RESOURCE_DETAIL_KEYWORDS if kw in query_lower)
    log_score = sum(1 for kw in LOG_KEYWORDS if kw in query_lower)
    process_score = sum(1 for kw in PROCESS_KEYWORDS if kw in query_lower)
    inventory_score = sum(1 for kw in INVENTORY_KEYWORDS if kw in query_lower)
    has_host = bool(HOST_PATTERN.search(query))

    logger.info(f"분류 점수: alert={alert_score}, analysis={analysis_score}, list={list_score}, "
                f"status={status_score}, resource={resource_detail}, log={log_score}, "
                f"process={process_score}, inventory={inventory_score}")

    if alert_score >= 2 or any(kw in query_lower for kw in ['장애', 'alert', '알럿', '트리거']):
        logger.info("질의 유형: alert")
        return 'alert'

    if log_score >= 2 or any(kw in query_lower for kw in ['로그', 'errpt', 'syslog', '/var/log']):
        if analysis_score >= 2:
            return 'analysis'
        return 'log'

    if process_score >= 2 or (process_score >= 1 and any(kw in query_lower for kw in ['top', '상위', '트렌드', '시간대'])):
        if analysis_score >= 2 and process_score < 3:
            return 'analysis'
        return 'process'

    # v3.4: inventory 분류 강화 — 점수 1 이상이면서 자산/장비 키워드 포함 시
    if inventory_score >= 2 or any(kw in query_lower for kw in ['인벤토리', 'inventory', '자산 프로필']):
        return 'inventory'

    # v3.4: "장비 정보", "자산 현황" 등 단일 키워드라도 inventory로 분류
    if inventory_score >= 1 and any(kw in query_lower for kw in [
        '장비', '자산', '인프라', '호스트', '등록', '구성'
    ]):
        # 단, 특정 리소스(CPU, 메모리 등) 질의가 아닌 경우만
        if resource_detail == 0 and analysis_score == 0:
            return 'inventory'

    if resource_detail >= 1 and analysis_score >= 1:
        return 'analysis'

    if resource_detail >= 2:
        return 'analysis'

    if analysis_score >= 2:
        return 'analysis'

    if list_score >= 2 or any(kw in query_lower for kw in ['몇 대', '몇 개', '몇대', '몇개', '목록']):
        # v3.4: 목록 질의 중 장비/자산 관련이면 inventory로 전환
        if any(kw in query_lower for kw in ['장비', '자산', '서버', '호스트', '인프라']):
            if resource_detail == 0:
                return 'inventory'
        return 'list'

    if has_host and process_score >= 1:
        return 'process'

    if has_host and resource_detail >= 1:
        return 'analysis'

    if has_host and status_score >= 1:
        return 'status'

    if status_score >= 2:
        return 'status'

    if analysis_score >= 1 or resource_detail >= 1:
        return 'analysis'
    if process_score >= 1:
        return 'process'
    if log_score >= 1:
        return 'log'
    if alert_score >= 1:
        return 'alert'
    if status_score >= 1:
        return 'status'

    return 'general'


# ============================================================
# 시간 필터 파싱
# ============================================================
def parse_time_filter(query: str) -> Optional[Dict]:
    query_lower = query.lower().strip()
    now = datetime.now()

    for pattern, unit in TIME_PATTERNS:
        match = re.search(pattern, query_lower)
        if match:
            if unit == 'today':
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                return {'start_time': start, 'end_time': now, 'description': '오늘'}
            elif unit == 'yesterday':
                yesterday = now - timedelta(days=1)
                start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
                end = yesterday.replace(hour=23, minute=59, second=59)
                return {'start_time': start, 'end_time': end, 'description': '어제'}
            elif unit == 'week':
                start = now - timedelta(days=7)
                return {'start_time': start, 'end_time': now, 'description': '이번 주'}
            elif unit == 'days':
                days = int(match.group(1))
                start = now - timedelta(days=days)
                return {'start_time': start, 'end_time': now, 'description': f'지난 {days}일'}
            elif unit == 'hours':
                hours = int(match.group(1))
                start = now - timedelta(hours=hours)
                return {'start_time': start, 'end_time': now, 'description': f'지난 {hours}시간'}
    return None


# ============================================================
# 카테고리 추천 (8개 유형 대응)
# v3.4: infra 카테고리 추천 강화
# ============================================================
def get_recommended_categories(query_type: str, query: str) -> Optional[List[str]]:
    query_lower = query.lower()

    if query_type == 'alert':
        return ['alert']
    if query_type == 'log':
        return ['server']
    if query_type == 'process':
        return ['server']

    # v3.4: inventory 유형이면 infra를 항상 포함
    if query_type == 'inventory':
        return ['infra', 'server', 'network']

    if query_type in ('status', 'list', 'analysis', 'general'):
        nw_keywords = ['네트워크', 'network', '스위치', 'switch',
                       'snmp', 'icmp', 'aruba', '대역폭', 'bandwidth', '트래픽', 'traffic']
        if any(kw in query_lower for kw in nw_keywords):
            # v3.4: 네트워크 질의에서도 장비 정보 질의면 infra 포함
            infra_hints = ['장비', '자산', '정보', '현황', '목록', '몇 대', '몇대']
            if any(kw in query_lower for kw in infra_hints):
                return ['network', 'infra']
            return ['network']

        svr_keywords = ['서버', 'server', 'cpu', '메모리', 'memory', 'swap',
                        '디스크', 'disk', '파일시스템', 'filesystem',
                        '프로세스', 'process', 'zabbix',
                        '로그', 'log', 'errpt', '연결', 'connection',
                        '포트', 'listen', '리슨']
        if any(kw in query_lower for kw in svr_keywords):
            # v3.4: 서버 질의에서도 장비 기본정보 질의면 infra 포함
            infra_hints = ['장비', '자산', '정보', '현황', '목록', '몇 대', '몇대', '호스트']
            if any(kw in query_lower for kw in infra_hints):
                return ['server', 'infra']
            return ['server']

        infra_keywords = ['자산', '인벤토리', 'inventory', '사양', '통합',
                          '장비', '호스트', '인프라', '구성', '등록']
        if any(kw in query_lower for kw in infra_keywords):
            return ['infra', 'server', 'network']

        return None

    return None

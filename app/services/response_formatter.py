# /app/app/services/response_formatter.py
"""
LLM JSON 응답 → 유형별 포맷 변환/검증 v2.1
- 7개 query_type 지원 (status, list, alert, analysis, general, log, process)
- v2.1: inventory query_type 삭제 (embeddings_inventory 테이블 제거됨)
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 유형별 기본값 정의 (7개)
# ============================================================
DEFAULT_VALUES = {
    'status': {
        'query_type': 'status',
        'title': '상태 정보',
        'host': {
            'hostid': None, 'asset': '', 'ip': '', 'os': '',
            'group_name': '', 'system_name': ''
        },
        'metrics': [],
        'analysis': '',
        'measured_at': ''
    },
    'list': {
        'query_type': 'list',
        'title': '목록 조회',
        'columns': [],
        'items': [],
        'total_count': 0,
        'analysis': '',
        'measured_at': ''
    },
    'alert': {
        'query_type': 'alert',
        'title': '장애 이벤트',
        'total_count': 0,
        'active_count': 0,
        'resolved_count': 0,
        'alerts': [],
        'analysis': '',
        'measured_at': ''
    },
    'analysis': {
        'query_type': 'analysis',
        'title': '분석 결과',
        'summary': '',
        'columns': [],
        'items': [],
        'recommendation': '',
        'measured_at': ''
    },
    'general': {
        'query_type': 'general',
        'title': '답변',
        'answer': '',
        'note': None
    },
    'log': {
        'query_type': 'log',
        'title': '로그 조회',
        'host': {'asset': '', 'ip': '', 'os': ''},
        'log_source': '',
        'total_count': 0,
        'logs': [],
        'analysis': '',
        'recommendation': '',
        'measured_at': ''
    },
    'process': {
        'query_type': 'process',
        'title': '프로세스 분석',
        'host': {'hostid': None, 'asset': '', 'ip': '', 'os': ''},
        'time_range': '',
        'total_snapshots': 0,
        'process_trend': [],
        'persistent_processes': [],
        'analysis': '',
        'recommendation': '',
        'measured_at': ''
    }
}


# ============================================================
# 응답 검증 및 보정
# ============================================================
def validate_and_fill(data: Dict[str, Any], query_type: str) -> Dict[str, Any]:
    """LLM 응답을 검증하고 누락 필드를 기본값으로 채움"""
    if not data:
        return DEFAULT_VALUES.get(query_type, DEFAULT_VALUES['general']).copy()
    
    actual_type = data.get('query_type', query_type)
    if actual_type not in DEFAULT_VALUES:
        actual_type = query_type
    
    data['query_type'] = actual_type
    defaults = DEFAULT_VALUES.get(actual_type, DEFAULT_VALUES['general'])
    
    for key, default_value in defaults.items():
        if key not in data or data[key] is None:
            data[key] = default_value
        elif isinstance(default_value, dict) and isinstance(data.get(key), dict):
            for sub_key, sub_default in default_value.items():
                if sub_key not in data[key] or data[key][sub_key] is None:
                    data[key][sub_key] = sub_default
    
    # 유형별 추가 검증
    validator = _VALIDATORS.get(actual_type)
    if validator:
        data = validator(data)
    
    return data


# ============================================================
# 유형별 검증 함수
# ============================================================
def _validate_status(data: Dict) -> Dict:
    if not isinstance(data.get('metrics'), list):
        data['metrics'] = []
    for metric in data['metrics']:
        if not isinstance(metric, dict):
            continue
        metric.setdefault('label', '')
        metric.setdefault('value', '')
        metric.setdefault('unit', '')
        metric.setdefault('status', 'normal')
        status = metric.get('status', 'normal').lower()
        if status not in ('normal', 'warning', 'critical'):
            metric['status'] = 'normal'
    return data


def _validate_list(data: Dict) -> Dict:
    if not isinstance(data.get('items'), list):
        data['items'] = []
    if not isinstance(data.get('columns'), list):
        data['columns'] = []
    if data.get('total_count', 0) == 0:
        data['total_count'] = len(data['items'])
    return data


def _validate_alert(data: Dict) -> Dict:
    if not isinstance(data.get('alerts'), list):
        data['alerts'] = []
    for alert in data['alerts']:
        if not isinstance(alert, dict):
            continue
        alert.setdefault('time', '')
        alert.setdefault('severity', 'Information')
        alert.setdefault('severity_level', 1)
        alert.setdefault('host', '')
        alert.setdefault('ip', '')
        alert.setdefault('alert_name', '')
        alert.setdefault('status', 'Resolved')
    if data.get('total_count', 0) == 0:
        data['total_count'] = len(data['alerts'])
    if 'active_count' not in data:
        data['active_count'] = sum(1 for a in data['alerts'] if a.get('status') == 'Active')
    if 'resolved_count' not in data:
        data['resolved_count'] = sum(1 for a in data['alerts'] if a.get('status') == 'Resolved')
    return data


def _validate_analysis(data: Dict) -> Dict:
    if not isinstance(data.get('items'), list):
        data['items'] = []
    if not isinstance(data.get('columns'), list):
        data['columns'] = []
    for item in data['items']:
        if not isinstance(item, dict):
            continue
        item.setdefault('rank', None)
        item.setdefault('host', '')
        item.setdefault('ip', '')
        item.setdefault('value', '')
        item.setdefault('std', '')
        item.setdefault('stability', '')
    return data


def _validate_log(data: Dict) -> Dict:
    if not isinstance(data.get('logs'), list):
        data['logs'] = []
    for log_entry in data['logs']:
        if not isinstance(log_entry, dict):
            continue
        log_entry.setdefault('time', '')
        log_entry.setdefault('log_file', '')
        log_entry.setdefault('severity', 'Information')
        log_entry.setdefault('source', '')
        log_entry.setdefault('content', '')
        log_entry.setdefault('status', 'Resolved')
    if data.get('total_count', 0) == 0:
        data['total_count'] = len(data['logs'])
    return data


def _validate_process(data: Dict) -> Dict:
    if not isinstance(data.get('process_trend'), list):
        data['process_trend'] = []
    if not isinstance(data.get('persistent_processes'), list):
        data['persistent_processes'] = []
    if data.get('total_snapshots', 0) == 0:
        data['total_snapshots'] = len(data['process_trend'])
    return data


# 검증 함수 매핑
_VALIDATORS = {
    'status': _validate_status,
    'list': _validate_list,
    'alert': _validate_alert,
    'analysis': _validate_analysis,
    'log': _validate_log,
    'process': _validate_process,
}


# ============================================================
# 검색 소스 포맷팅
# ============================================================
def format_sources(search_results: list) -> list:
    """검색 결과를 소스 정보로 변환"""
    sources = []
    seen = set()
    for result in search_results[:10]:
        key = f"{result.get('source_table')}_{result.get('hostid')}_{result.get('ip')}"
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            'table': result.get('source_table', ''),
            'category': result.get('category', ''),
            'similarity': round(result.get('similarity', 0), 4),
            'hostid': result.get('hostid'),
            'asset': result.get('asset', ''),
            'ip': result.get('ip', ''),
            'summary_preview': (result.get('summary', '')[:100] + '...') if result.get('summary') else ''
        })
    return sources

# /app/app/services/agent.py
"""
Agentic RAG — Agent Loop v8.3
================================
v8.3 변경사항 (v22.2):
  K. [Fix v22.2] _force_answer 데이터 없음 early-return:
     observation_block이 비어있거나 모든 도구 결과가 found=false인 경우
     LLM 재호출 없이 즉시 '조회 결과 없음' JSON 반환 → 할루시네이션 완전 차단.

v8.2 변경사항 (v22.1):
  H. [Fix v22.1-1] network 카테고리 search_historical_data 결과 없을 때
     서버 도구(get_server_cpu/get_processes)로 폴백하던 로직 제거.
     → 네트워크 질의는 반드시 network 카테고리 재검색만 수행 (서버 데이터 오염 차단).
  I. [Fix v22.1-2] _force_answer 내 is_analysis 판별 수정:
     _has_network_search 플래그 도입 — network search_historical_data 결과가
     있는 경우 analysis 분기(서버 피크 프롬프트) 진입 차단, else 분기로 유도.
  J. [Fix v22.1-3] _sanitize_tool_args에 _SEARCH_TOP_K_MIN=5 적용:
     LLM이 top_k=1 등 과소 설정 시 자동으로 최솟값 5로 보정.
     → "가장 큰 것 1개" 질의에서 검색 결과 청크 누락 방지.

v8.1 변경사항 (v21.14):
  G. _force_answer: 도구 조회 결과(Observation)를 프롬프트에 명시적 삽입
     — 이전 대화 컨텍스트 오염 방지, 검색 결과 외 데이터 사용 금지 제약 추가
     — 네트워크 질의에 CPU/서버 데이터가 혼입되는 할루시네이션 문제 해결
v8.0 변경사항:
  A. 네트워크 Top-N 쿼리: get_network_interfaces 우선 + 상세 처리 흐름
  B. 시스템 프롬프트: 목록형 details 구조 강제 + 할루시네이션 금지 규칙
  C. _force_answer: 네트워크/목록 질의 테이블 형식 details 명시
  D. _compress_observation: 네트워크 interfaces 데이터 보존 강화
  E. parse_agent_answer: query_type 정규화 + 테이블 항목 변환 개선
  F. 할루시네이션 가드: 도구 미호출 시 eth0 등 가상 포트명 반환 차단
v6.0 변경사항:
  1. Ollama native tool_calls API 사용 (프롬프트 시뮬레이션 제거)
  2. num_ctx Ollama payload 전달
  3. categories string→array 타입 교정
  4. max_tokens 증가 + observation 압축 개선
  5. Gemini SLIM tool 적용
  6. _call_llm 타임아웃 각 백엔드 cfg에서 읽음
"""

import json
import logging
import time
import asyncio
import re
from datetime import datetime
import zoneinfo
import httpx
from typing import Dict, List, Optional, Any

from app.config import (
    OPENAI_API_KEY, OPENAI_API_URL,
    AVAILABLE_MODELS,
    AGENT_MODEL,
    AGENT_LOOP_MODEL,
    AGENT_FINAL_MODEL,
    AGENT_MAX_ITERATIONS, AGENT_MAX_ITERATIONS_BY_BACKEND,
    TOOL_CALLING_MODELS,
    OLLAMA_API_URL,
)
from app.services.tools import TOOL_DEFINITIONS, TOOL_MAP, TOOL_NAMES, to_gemini_tools
from app.services.tool_executor import execute_tool
from app.services.source_tables import attach_data_sources_to_trace_entry

logger = logging.getLogger(__name__)


# ============================================================
# Phase 2-C/2-A: 도구 한국어 이름 매핑 + meta 빌더
# ============================================================
_TOOL_KR_NAME: Dict[str, str] = {
    "get_server_status":        "서버 상태 조회",
    "get_server_cpu":           "CPU 상세 분석",
    "get_server_memory":        "메모리 상세 조회",
    "get_server_filesystem":    "디스크/파일시스템 조회",
    "get_server_interfaces":    "서버 인터페이스 조회",
    "get_processes":            "프로세스 목록 조회",
    "get_listen_ports":         "리슨 포트 조회",
    "get_connections":          "네트워크 연결 조회",
    "get_server_logs":          "서버 로그 조회",
    "get_errpt":                "AIX ERRPT 로그 조회",
    "get_vmware":               "VMware 하이퍼바이저/VM 조회",
    "get_network_device":       "네트워크 장비 조회",
    "get_network_interfaces":   "네트워크 인터페이스 조회",
    "get_alerts":               "알림/이벤트 조회",
    "get_all_assets":           "자산 인벤토리 조회",
    "search_historical_data":   "이력/추이 데이터 검색",
}

# 도구별 데이터 신선도(분) 기준
_TOOL_FRESHNESS_MIN: Dict[str, int] = {
    "get_server_status":        1,
    "get_server_cpu":           1,
    "get_server_memory":        1,
    "get_server_filesystem":    5,
    "get_server_interfaces":    1,
    "get_processes":            1,
    "get_listen_ports":         5,
    "get_connections":          1,
    "get_server_logs":          5,
    "get_errpt":               5,
    "get_vmware":              2,
    "get_network_device":       2,
    "get_network_interfaces":   1,
    "get_alerts":               1,
    "get_all_assets":           60,
    "search_historical_data":   30,
}

# 신뢰도 기준: found=True 도구 수 비율로 계산
def _build_response_meta(tool_trace: List[Dict]) -> Dict:
    """
    Phase 2-A: tool_trace 에서 meta.data_source / confidence / freshness 자동 계산.
    반환 형식:
      {
        "data_source": ["get_server_status", "get_alerts", ...],
        "data_source_kr": ["서버 상태 조회", "알림/이벤트 조회", ...],
        "freshness_minutes": 1,          # 가장 자주 갱신되는 도구 기준
        "confidence_level": "high",      # high/medium/low
        "confidence_score": 0.85,        # 0.0~1.0
        "record_count": 42,
        "tool_count": 3,
      }
    """
    if not tool_trace:
        return {
            "data_source": [], "data_source_kr": [],
            "freshness_minutes": 60, "confidence_level": "low",
            "confidence_score": 0.0, "record_count": 0, "tool_count": 0,
        }

    sources = [t.get("tool", "") for t in tool_trace]
    # 중복 제거 (fallback 도구는 기본 이름으로 정규화)
    clean_sources = []
    seen = set()
    for s in sources:
        base = s.split("(")[0].split("-")[0].strip()
        if base not in seen:
            seen.add(base)
            clean_sources.append(base)

    source_kr = [_TOOL_KR_NAME.get(s, s) for s in clean_sources]

    # 신선도: 단일 출처면 min(빠른 갱신). RAG(search_historical_data)가 섞이면 임베딩 동기화 지연이
    # 있으므로 min만 쓰면 get_alerts(1분) 때문에 '실시간'으로 뜨며, '이력/추이 검색' 라벨과 모순됨.
    _rag_tool = "search_historical_data"
    _per_tool = [_TOOL_FRESHNESS_MIN.get(s, 30) for s in clean_sources]
    base_min = min(_per_tool, default=30)
    if _rag_tool in clean_sources:
        rag_floor = _TOOL_FRESHNESS_MIN.get(_rag_tool, 30)
        freshness = max(rag_floor, base_min)
    else:
        freshness = base_min

    # 신뢰도 계산
    found_count = sum(1 for t in tool_trace if t.get("found") is True)
    total = len(tool_trace)
    ratio = found_count / total if total > 0 else 0.0
    record_count = sum(t.get("count", 0) or 0 for t in tool_trace)

    if ratio >= 0.8 and record_count > 0:
        confidence_level = "high"
    elif ratio >= 0.5 or record_count > 0:
        confidence_level = "medium"
    else:
        confidence_level = "low"

    out_meta = {
        "data_source":       clean_sources,
        "data_source_kr":    source_kr,
        "freshness_minutes": freshness,
        "confidence_level":  confidence_level,
        "confidence_score":  round(ratio, 2),
        "record_count":      record_count,
        "tool_count":        len(clean_sources),
    }
    if _rag_tool in clean_sources and len(clean_sources) > 1:
        out_meta["freshness_mix"] = "rag_plus_live"
        out_meta["freshness_note_kr"] = (
            "실시간 Zabbix 조회와 벡터 DB 이력 검색을 함께 사용했습니다. "
            "이력·요약 수치는 임베딩 동기화 주기에 따라 지연될 수 있습니다."
        )
    return out_meta


# ============================================================
# 경량화된 Tool 정의 (토큰 절약 — "사용 예:" 제거)
# ============================================================
def _get_slim_tools() -> List[Dict]:
    slim = []
    for tool in TOOL_DEFINITIONS:
        t = json.loads(json.dumps(tool))
        desc = t["function"]["description"]
        for marker in ["사용 예:", "사용 예 :"]:
            idx = desc.find(marker)
            if idx > 0:
                desc = desc[:idx].rstrip()
                break
        t["function"]["description"] = desc

        props = t["function"].get("parameters", {}).get("properties", {})
        for pname, pinfo in props.items():
            pdesc = pinfo.get("description", "")
            pidx = pdesc.find("예:")
            if pidx > 0:
                pinfo["description"] = pdesc[:pidx].rstrip()

        slim.append(t)
    return slim

SLIM_TOOL_DEFINITIONS = _get_slim_tools()


# ============================================================
# Gemini용 SLIM tool 변환 (Fix 6: 기존 전체 tool 대신 slim 사용)
# ============================================================
def to_gemini_slim_tools() -> List[Dict]:
    """SLIM_TOOL_DEFINITIONS → Gemini functionDeclarations 형식"""
    declarations = []
    for tool in SLIM_TOOL_DEFINITIONS:
        func = tool["function"]
        declarations.append({
            "name": func["name"],
            "description": func["description"],
            "parameters": func["parameters"],
        })
    return [{"functionDeclarations": declarations}]


# ============================================================
# Agent System Prompt (품질 강화 v4) — app/prompts/agent_system.txt 에서 로드
# ============================================================
def _load_agent_system_prompt() -> str:
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "prompts" / "agent_system.txt"
    if not p.is_file():
        raise FileNotFoundError(f"Agent system prompt missing: {p}")
    return p.read_text(encoding="utf-8")


AGENT_SYSTEM_PROMPT = _load_agent_system_prompt()


# ============================================================
# ★ 파라미터 자동 보정 (Fix 3: categories string→array 추가)
# ============================================================
# search_historical_data top_k 최솟값 (LLM이 1 등 너무 작은 값을 보낼 경우 강제 보정)
_SEARCH_TOP_K_MIN = 5


def _sanitize_tool_args(tool_name: str, args: Dict) -> Dict:
    """
    LLM이 보낸 파라미터를 Tool schema에 맞게 자동 보정.
    - schema에 없는 파라미터 제거
    - period → search_query에 병합
    - host_query(search_historical_data에 없음) → search_query에 병합
    - 타입 교정 (string→int, string→array 등)  ← Fix 3 추가
    - [Fix v22.1] search_historical_data top_k 최솟값 강제 (_SEARCH_TOP_K_MIN)
    """
    func_def = TOOL_MAP.get(tool_name)
    if not func_def:
        return args

    props = func_def.get("parameters", {}).get("properties", {})
    required = set(func_def.get("parameters", {}).get("required", []))
    sanitized = {}
    extra_context = []

    for key, value in args.items():
        if key in props:
            sanitized[key] = _coerce_value(value, props[key])
        else:
            logger.info(f"[Agent] 파라미터 보정: {tool_name}에서 '{key}'={value} 제거 (schema 외)")
            if value and str(value).strip():
                extra_context.append(str(value))

    # extra_context를 search_query에 병합
    if extra_context and "search_query" in props:
        existing = sanitized.get("search_query", "")
        merged = " ".join(filter(None, [existing] + extra_context))
        sanitized["search_query"] = merged
        logger.info(f"[Agent] 파라미터 병합: search_query='{merged}'")

    # 필수 파라미터 누락 시 기본값 설정
    for req_key in required:
        if req_key not in sanitized:
            if req_key == "host_query":
                sanitized["host_query"] = "Zabbix server"
                logger.info(f"[Agent] 필수 파라미터 기본값 설정: host_query='Zabbix server'")
            elif req_key == "search_query":
                sanitized["search_query"] = " ".join(extra_context) if extra_context else "모니터링 데이터"
                logger.info(f"[Agent] 필수 파라미터 기본값 설정: search_query='{sanitized['search_query']}'")

    # [Fix v22.1] search_historical_data top_k 최솟값 강제
    # LLM이 '가장 큰 것 1개' 등으로 판단해 top_k=1로 보내면 정답 청크를 놓칠 수 있음
    if tool_name == "search_historical_data" and "top_k" in sanitized:
        original_top_k = sanitized["top_k"]
        if isinstance(original_top_k, int) and original_top_k < _SEARCH_TOP_K_MIN:
            sanitized["top_k"] = _SEARCH_TOP_K_MIN
            logger.info(
                f"[Agent] top_k 최솟값 보정: {original_top_k} → {_SEARCH_TOP_K_MIN} "
                f"(search_historical_data 검색 결과 누락 방지)"
            )

    return sanitized


def _coerce_value(value: Any, prop_schema: Dict) -> Any:
    """
    단일 값의 타입 교정 — prop_schema 전체를 받아 array/enum도 처리.
    Fix 3: string → array 변환 추가
    """
    expected_type = prop_schema.get("type")

    # ── array 타입 교정 ──
    if expected_type == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            # "server" → ["server"] / "server,network" → ["server","network"]
            items = [v.strip() for v in re.split(r'[,\s]+', value) if v.strip()]
            # enum 검증
            item_enum = prop_schema.get("items", {}).get("enum")
            if item_enum:
                items = [i for i in items if i in item_enum]
            logger.info(f"[Agent] array 타입 교정: '{value}' → {items}")
            return items if items else None
        return value

    # ── 기존 타입 교정 ──
    if expected_type == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    elif expected_type == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    elif expected_type == "boolean" and isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return value


# ============================================================
# ★ 한자(중국어) 감지 및 한국어 재질의 (v19.0)
# ============================================================
_CJK_PATTERN = re.compile(
    r'[\u4e00-\u9fff'        # CJK Unified Ideographs
    r'\u3400-\u4dbf'         # CJK Extension A
    r'\uff00-\uffef]+'       # Fullwidth Forms
)

def _has_chinese(text: str) -> bool:
    """한자(중국어 문자)가 포함되어 있으면 True"""
    return bool(_CJK_PATTERN.search(text))


def _strip_chinese_from_json(answer: str) -> Optional[str]:
    """한자 비율이 5% 이상이면 None 반환 (재질의 필요), 미만이면 원본 반환"""
    total = len(answer)
    if total == 0:
        return answer
    chinese_count = sum(len(m) for m in _CJK_PATTERN.findall(answer))
    ratio = chinese_count / total
    if ratio >= 0.05:
        logger.warning(f"[Agent] 한자 비율 {ratio:.1%} 감지 — 재질의 필요")
        return None
    return answer


async def _retry_korean(messages: List[Dict], model: str, original_answer: str) -> str:
    """한자 감지 시 한국어로 재작성 요청 1회"""
    retry_prompt = (
        "이전 응답에 중국어(한자)가 포함되어 있습니다. "
        "analysis, recommendation, summary 등 모든 텍스트 필드를 **한국어로만** 다시 작성하세요. "
        "JSON 구조와 수치 데이터는 그대로 유지하고, 텍스트만 한국어로 교체하세요."
    )
    retry_messages = messages + [
        {"role": "assistant", "content": original_answer},
        {"role": "user", "content": retry_prompt},
    ]
    try:
        resp = await _call_llm(retry_messages, model, tools=None)
        new_answer = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        if new_answer and not _has_chinese(new_answer):
            logger.info("[Agent] 한국어 재질의 성공")
            return new_answer.strip()
        logger.warning("[Agent] 재질의 후에도 한자 잔존 — 원본 사용")
    except Exception as e:
        logger.error(f"[Agent] 한국어 재질의 실패: {e}")
    return original_answer


# ============================================================
# Observation 압축 (Fix 5: 핵심 summary 필드 보존 강화)
# ============================================================
def _compress_observation(obs: Dict, max_items: int = 10, max_chars: int = 3000,
                          is_log_query: bool = False, tool_name: Optional[str] = None) -> str:
    """
    Observation → 압축 JSON 문자열.
    v12: max_chars/max_items를 모델별로 파라미터화하여 TPM 초과 방지.
    v23.1: is_log_query=True 시 로그 원문(log/description) 잘림 없이 전체 보존.
    """
    # 로그 조회 시: 잘림 없이 충분한 한도 적용
    if is_log_query:
        max_chars = max(max_chars, 50000)   # 로그 원문 전체 보존용
        max_items = max(max_items, 100)     # 요청 건수 전체 보존
    # 자산 전체 목록: 행이 작아도 20건 압축 → "총 22대" vs "20건만" UX 모순 방지
    if tool_name == "get_all_assets":
        max_items = max(max_items, 120)
        max_chars = max(max_chars, 16000)
    compressed = {}

    # 메타 필드 보존
    for key in ('found', 'count', 'message', 'match_info',
                'available_hosts', 'candidates', 'ambiguous',
                'active_count', 'resolved_count', 'matched_count',
                'threshold', 'server_count', 'network_count',
                'error', 'available_tools'):
        if key in obs:
            val = obs[key]
            # available_hosts 목록은 최대 5개만 (토큰 절약)
            if key == 'available_hosts' and isinstance(val, list) and len(val) > 5:
                compressed[key] = val[:5]
                compressed['available_hosts_total'] = len(val)
            else:
                compressed[key] = val

    # summary 길이: max_chars 기준 비례 적용 (로그 조회 시 잘림 없음)
    summary_max = 0 if is_log_query else min(300, max_chars // 10)

    # data 배열: 각 항목의 summary 필드는 summary_max자로 자름
    # 로그 조회(is_log_query=True) 시: summary/log/description 잘림 없이 전체 보존
    if 'data' in obs:
        data = obs['data']
        if isinstance(data, list):
            trimmed = []
            for item in data[:max_items]:
                if isinstance(item, dict):
                    item = dict(item)
                    if not is_log_query:
                        # 일반 조회: summary만 자름
                        if 'summary' in item and isinstance(item['summary'], str):
                            item['summary'] = item['summary'][:summary_max]
                    # 로그 조회: log/description/summary 모두 원문 그대로 보존
                trimmed.append(item)
            compressed['data'] = trimmed
            compressed['data_row_total'] = len(data)
            compressed['data_rows_included'] = len(trimmed)
            if len(data) > max_items:
                compressed['data_truncated'] = f"전체 {len(data)}건 중 {max_items}건만 표시"
        else:
            compressed['data'] = data

    # 보조 배열 (네트워크 interfaces 포함)
    for key in ('swap', 'top_processes', 'matched', 'all_assets_summary', 'interfaces'):
        if key in obs:
            val = obs[key]
            if isinstance(val, list) and len(val) > max_items:
                compressed[key] = val[:max_items]
            else:
                compressed[key] = val

    # 네트워크 인터페이스 데이터 보존 강화
    if 'data' in compressed and isinstance(compressed.get('data'), list):
        sample = compressed['data'][0] if compressed['data'] else {}
        is_iface_data = any(k in sample for k in ('rx_bps', 'tx_bps', 'rx_rate', 'tx_rate', 'in_rate', 'out_rate', 'recv', 'send', 'interface'))
        if is_iface_data:
            full_data = obs.get('data', [])
            compressed['data'] = full_data[:20] if len(full_data) > 20 else full_data
            if 'data_truncated' in compressed:
                del compressed['data_truncated']

    # summary 필드 (search_historical_data의 직접 summary)
    if 'summary' in obs and 'summary' not in compressed:
        s = obs['summary']
        if is_log_query:
            compressed['summary'] = s  # 로그 조회: 전체 보존
        else:
            compressed['summary'] = s[:summary_max * 2] if isinstance(s, str) else s

    result = json.dumps(compressed, ensure_ascii=False, default=str)

    # 전체 크기 제한: max_chars 초과 시 data 추가 압축
    # 로그 조회(is_log_query=True) 시: 크기 압축/절단 없이 전체 보존
    if not is_log_query and len(result) > max_chars:
        logger.info(f"[Agent] Observation 크기 초과 ({len(result)}자 > {max_chars}자), 추가 압축")
        if 'data' in compressed and isinstance(compressed['data'], list):
            half = max(1, max_items // 2)
            compressed['data'] = compressed['data'][:half]
            compressed['data_truncated'] = f"크기 제한으로 {half}건만 표시"
            compressed['data_rows_included'] = len(compressed['data'])
        # top_processes도 축소
        if 'top_processes' in compressed and isinstance(compressed['top_processes'], list):
            compressed['top_processes'] = compressed['top_processes'][:3]
        result = json.dumps(compressed, ensure_ascii=False, default=str)
        # 그래도 초과 시 강제 절단
        if len(result) > max_chars:
            result = result[:max_chars]
            logger.warning(f"[Agent] Observation 강제 절단: {max_chars}자")

    return result


# ============================================================
# failed_generation 파싱 (Groq 400 복구)
# ============================================================
def _parse_failed_generation(error_body: str) -> Optional[Dict]:
    try:
        error = json.loads(error_body)
        failed_gen = error.get("error", {}).get("failed_generation", "")
    except (json.JSONDecodeError, AttributeError):
        return None

    if not failed_gen:
        return None

    # ── Case A: llama-4 등이 생성하는 JSON 배열 형식 ──
    # 예: [{"name":"get_server_cpu","parameters":{"host_query":"...","include_top_processes":"false"}}]
    try:
        parsed = json.loads(failed_gen)
        if isinstance(parsed, list) and parsed:
            item = parsed[0]
            func_name = item.get("name", "")
            # "parameters" 또는 "arguments" 키 모두 허용
            raw_args = item.get("parameters") or item.get("arguments") or {}
            if func_name in TOOL_NAMES:
                if isinstance(raw_args, str):
                    raw_args = json.loads(raw_args)
                args = _sanitize_tool_args(func_name, raw_args)
                logger.info(f"[Agent] failed_generation JSON배열 파싱 성공: {func_name}({args})")
                return {"name": func_name, "arguments": args}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # ── Case B: 기존 <function=...> 태그 형식 ──
    match = re.search(r'<function=(\w+)>?\s*(\{.*?\})\s*<', failed_gen, re.DOTALL)
    if not match:
        match = re.search(r'<function=(\w+)>?\s*(\{.*\})', failed_gen, re.DOTALL)

    if not match:
        # ── Case C: 마크다운 코드블록 최종 답변 (llama-4 iteration 2 패턴) ──
        md_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', failed_gen, re.DOTALL)
        if not md_match:
            md_match = re.search(r'```(?:json)?\s*(\{.*)', failed_gen, re.DOTALL)
        if md_match:
            content_str = md_match.group(1).strip().rstrip('`').strip()
            logger.info("[Agent] failed_generation 마크다운 JSON 감지 — content 답변으로 처리")
            return {"is_content": True, "content": content_str}
        logger.warning(f"[Agent] failed_generation 파싱 실패: {failed_gen[:200]}")
        return None

    func_name = match.group(1)
    args_str = match.group(2)

    if func_name not in TOOL_NAMES:
        logger.warning(f"[Agent] 알 수 없는 함수: {func_name}")
        return None

    try:
        args = json.loads(args_str)
    except json.JSONDecodeError:
        logger.warning(f"[Agent] 인자 파싱 실패: {args_str[:200]}")
        return None

    args = _sanitize_tool_args(func_name, args)
    logger.info(f"[Agent] failed_generation 파싱 성공: {func_name}({args})")
    return {"name": func_name, "arguments": args}


# ============================================================
# 429 대기 시간 추출
# ============================================================
def _extract_retry_seconds(error_body: str, headers: dict = None) -> Optional[float]:
    if headers:
        ra = headers.get('retry-after')
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
    m = re.search(r'try again in (\d+\.?\d*)s', error_body)
    if m:
        return float(m.group(1))
    m = re.search(r'[Rr]etry in (\d+\.?\d*)s', error_body)
    if m:
        return float(m.group(1))
    return None


def _parse_time_window_from_query(q: str) -> tuple[Optional[int], Optional[int]]:
    """질문에서 '지난/최근 N일' 또는 'N시간' 추출. (days_ago, hours_ago) 한쪽만."""
    m = re.search(r"(?:지난|최근)\s*(\d+)\s*일", q, re.IGNORECASE)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:지난|최근)\s*(\d+)\s*시간", q, re.IGNORECASE)
    if m:
        return None, int(m.group(1))
    return None, None


def _build_search_window_args(
    user_query: str,
    search_top_k: int,
    *,
    min_top_k: int = 8,
) -> Dict[str, Any]:
    """search_historical_data용 기간·top_k (hours_ago가 days_ago보다 우선)."""
    d, h = _parse_time_window_from_query(user_query)
    args: Dict[str, Any] = {
        "search_query": user_query,
        "categories": ["network"],
        "top_k": max(search_top_k, min_top_k),
    }
    if h is not None:
        args["hours_ago"] = h
    elif d is not None:
        args["days_ago"] = d
    else:
        args["days_ago"] = 7
    return args


# ============================================================
# 메시지 정리
# ============================================================
def _sanitize_messages(messages: List[Dict]) -> List[Dict]:
    sanitized = []
    for msg in messages:
        m = dict(msg)
        if m.get('role') == 'assistant':
            if not m.get('tool_calls') and not m.get('content'):
                m['content'] = ""
        sanitized.append(m)
    return sanitized


# ============================================================
# Groq API — 400 fallback + 429 재시도
# ============================================================
async def _call_groq(
    messages: List[Dict],
    model: str,
    tools: Optional[List[Dict]] = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 60.0,
    max_retries: int = 2,
) -> Dict:
    messages = _sanitize_messages(messages)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(GROQ_API_URL, json=payload, headers=headers)

                if resp.status_code == 200:
                    return resp.json()

                error_body = resp.text[:1500]

                if resp.status_code == 400 and "tool_use_failed" in error_body:
                    parsed_call = _parse_failed_generation(error_body)
                    if parsed_call and parsed_call.get("is_content"):
                        # Case C: 마크다운 최종 답변 → content 응답으로 반환
                        logger.info("[Agent/Groq] 400 복구: 마크다운 JSON → content 답변")
                        return {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": parsed_call["content"],
                                    "tool_calls": None,
                                }
                            }]
                        }
                    elif parsed_call:
                        logger.info(f"[Agent/Groq] 400 복구: {parsed_call['name']}()")
                        return {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [{
                                        "id": f"call_{parsed_call['name']}_{int(time.time()*1000)}",
                                        "type": "function",
                                        "function": {
                                            "name": parsed_call["name"],
                                            "arguments": json.dumps(parsed_call["arguments"], ensure_ascii=False),
                                        }
                                    }]
                                }
                            }]
                        }
                    else:
                        logger.error(f"[Agent/Groq] 400 복구 실패: {error_body[:300]}")
                        resp.raise_for_status()

                elif resp.status_code == 400:
                    logger.error(f"[Agent/Groq] 400: {error_body[:500]}")
                    resp.raise_for_status()

                elif resp.status_code == 429:
                    if attempt < max_retries:
                        wait = _extract_retry_seconds(error_body, dict(resp.headers))
                        wait = (wait or 5.0 * (attempt + 1)) + 1.0
                        logger.info(f"[Agent/Groq] 429 재시도 {attempt+1}: {wait:.1f}초 대기")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()

                else:
                    logger.error(f"[Agent/Groq] HTTP {resp.status_code}: {error_body[:500]}")
                    resp.raise_for_status()

        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            raise

    raise Exception("Groq API 최대 재시도 초과")


# ============================================================
# OpenAI API — Function Calling 지원 (v21.0 추가)
# ============================================================
async def _call_openai(
    messages: List[Dict],
    model: str,
    tools: Optional[List[Dict]] = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    timeout: float = 120.0,
    max_retries: int = 5,
    use_max_completion_tokens: bool = False,
    no_temperature: bool = False,
) -> Dict:
    """OpenAI API 호출.
    - use_max_completion_tokens=True: gpt-5 시리즈는 max_tokens 대신 max_completion_tokens 사용
    - no_temperature=True: gpt-5 시리즈는 temperature 파라미터 미지원 (기본값=1 고정)
    """
    messages = _sanitize_messages(messages)

    payload: Dict = {
        "model": model,
        "messages": messages,
    }
    # gpt-5 시리즈: max_completion_tokens 사용, temperature 미지원
    if use_max_completion_tokens:
        payload["max_completion_tokens"] = max_tokens
        # no_temperature=True이면 temperature 파라미터 생략 (API가 기본값=1 사용)
    else:
        payload["max_tokens"] = max_tokens
        if not no_temperature:
            payload["temperature"] = temperature

    if tools:
        # OpenAI function calling 형식 (Groq와 동일한 OpenAI 호환 포맷)
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(OPENAI_API_URL, json=payload, headers=headers)

                if resp.status_code == 200:
                    return resp.json()

                error_body = resp.text[:1500]

                if resp.status_code == 429:
                    if attempt < max_retries:
                        wait = _extract_retry_seconds(error_body, dict(resp.headers))
                        wait = (wait or 8.0 * (attempt + 1)) + 2.0
                        wait = min(wait, 120.0)
                        logger.info(f"[Agent/OpenAI] 429 재시도 {attempt+1}: {wait:.1f}초 대기")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                else:
                    logger.error(f"[Agent/OpenAI] HTTP {resp.status_code}: {error_body[:500]}")
                    resp.raise_for_status()

        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            raise

    raise Exception("OpenAI API 최대 재시도 초과")


# ============================================================
# Gemini API — 429 재시도 + SLIM tools 사용 (Fix 6)
# ============================================================
async def _call_gemini(
    messages: List[Dict],
    model: str,
    tools: Optional[List[Dict]] = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 60.0,
    max_retries: int = 2,
) -> Dict:
    """Gemini API 호출 — 429 재시도 + SLIM tool 사용"""
    contents = []
    system_text = ""

    for msg in messages:
        role = msg['role']
        if role == 'system':
            system_text = msg['content']
        elif role == 'user':
            contents.append({"role": "user", "parts": [{"text": msg['content']}]})
        elif role == 'assistant':
            tool_calls = msg.get('tool_calls', [])
            if tool_calls:
                parts = []
                for tc in tool_calls:
                    args = tc['function']['arguments']
                    if isinstance(args, str):
                        args = json.loads(args)
                    parts.append({"functionCall": {"name": tc['function']['name'], "args": args}})
                contents.append({"role": "model", "parts": parts})
            elif msg.get('content'):
                contents.append({"role": "model", "parts": [{"text": msg['content']}]})
        elif role == 'tool':
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {
                    "name": msg.get('name', 'unknown'),
                    "response": {"result": msg['content']}
                }}]
            })

    url = f"{GEMINI_API_URL}/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": contents,
        # Fix 6: 전체 tool 대신 SLIM tool 사용
        "tools": to_gemini_slim_tools(),
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)

                if resp.status_code == 200:
                    return _normalize_gemini_response(resp.json())

                error_body = resp.text[:1500]

                if resp.status_code == 429:
                    if attempt < max_retries:
                        wait = _extract_retry_seconds(error_body)
                        wait = (wait or 10.0 * (attempt + 1)) + 1.0
                        logger.info(f"[Agent/Gemini] 429 재시도 {attempt+1}: {wait:.1f}초 대기")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"[Agent/Gemini] 429 최대 재시도 초과")
                    resp.raise_for_status()

                else:
                    logger.error(f"[Agent/Gemini] HTTP {resp.status_code}: {error_body[:500]}")
                    resp.raise_for_status()

        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            raise

    raise Exception("Gemini API 최대 재시도 초과")


def _normalize_gemini_response(gemini_resp: Dict) -> Dict:
    """Gemini 응답 → OpenAI 호환 형식"""
    candidates = gemini_resp.get('candidates', [])
    if not candidates:
        return {"choices": [{"message": {"role": "assistant", "content": "응답 생성 실패"}}]}

    parts = candidates[0].get('content', {}).get('parts', [])
    tool_calls = []
    text_parts = []

    for part in parts:
        if 'functionCall' in part:
            fc = part['functionCall']
            tool_calls.append({
                "id": f"call_{fc['name']}_{int(time.time()*1000)}",
                "type": "function",
                "function": {
                    "name": fc['name'],
                    "arguments": json.dumps(fc.get('args', {}), ensure_ascii=False),
                }
            })
        elif 'text' in part:
            text_parts.append(part['text'])

    message = {"role": "assistant"}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    else:
        message["content"] = "\n".join(text_parts)

    return {"choices": [{"message": message}]}


# ============================================================
# ★ Ollama API — Native Tool Calling (Fix 2 + Fix 4)
# ============================================================
async def _call_ollama(
    messages: List[Dict],
    model: str,
    tools: Optional[List[Dict]] = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    num_ctx: int = 8192,
    timeout: float = 600.0,
    max_retries: int = 1,
) -> Dict:
    """
    Ollama /api/chat 호출 — Native Tool Calling 사용 (Fix 2).
    - tools 파라미터를 payload에 직접 전달 (OpenAI 형식 호환)
    - Ollama 0.3+ qwen2.5 모델은 tool_calls를 message에 직접 반환
    - num_ctx 옵션 전달로 컨텍스트 오버플로우 방지 (Fix 4)
    """
    # tool 결과(role=tool)를 Ollama 호환 형식으로 변환
    # Ollama는 role=tool을 지원하므로 그대로 전달 가능 (단, 일부 버전 호환성 위해 변환)
    ollama_messages = []
    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        if role == 'tool':
            # Ollama native tool result 형식
            ollama_messages.append({
                "role": "tool",
                "content": content or "",
            })
        elif role == 'assistant':
            tool_calls = msg.get('tool_calls', [])
            if tool_calls:
                # Ollama native tool_calls 형식으로 변환
                formatted_tcs = []
                for tc in tool_calls:
                    raw_args = tc['function']['arguments']
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            args = {}
                    else:
                        args = raw_args
                    formatted_tcs.append({
                        "id": tc.get("id", f"call_{tc['function']['name']}"),
                        "type": "function",
                        "function": {
                            "name": tc['function']['name'],
                            "arguments": args,
                        }
                    })
                ollama_messages.append({
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": formatted_tcs,
                })
            elif content:
                ollama_messages.append({"role": "assistant", "content": content})
        else:
            ollama_messages.append({"role": role, "content": content or ""})

    # Fix 2: tools 파라미터를 payload에 직접 전달
    # Fix 4: num_ctx 옵션 추가
    payload: Dict[str, Any] = {
        "model": model,
        "messages": ollama_messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": num_ctx,  # Fix 4: 컨텍스트 윈도우 명시
        }
    }
    if tools:
        payload["tools"] = tools  # Fix 2: native tool calling

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{OLLAMA_API_URL}/api/chat", json=payload)

                if resp.status_code == 200:
                    data = resp.json()
                    msg_data = data.get("message", {})
                    content_out = msg_data.get("content", "")
                    native_tool_calls = msg_data.get("tool_calls")  # Ollama native

                    # ── Case A: Native tool_calls 반환 (Fix 2 핵심) ──
                    if native_tool_calls:
                        formatted = []
                        for tc in native_tool_calls:
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {}
                            if name in TOOL_NAMES:
                                # 파라미터 자동 보정 적용
                                args = _sanitize_tool_args(name, args)
                                formatted.append({
                                    "id": f"call_{name}_{int(time.time()*1000)}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args, ensure_ascii=False),
                                    }
                                })
                                logger.info(f"[Agent/Ollama] Native tool_call: {name}({args})")

                        if formatted:
                            return {
                                "choices": [{
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": formatted,
                                    }
                                }]
                            }

                    # ── Case B: 텍스트 응답 ──
                    return {"choices": [{"message": {"role": "assistant", "content": content_out}}]}

                else:
                    error_body = resp.text[:500]
                    logger.error(f"[Agent/Ollama] HTTP {resp.status_code}: {error_body}")
                    if attempt < max_retries:
                        await asyncio.sleep(2.0)
                        continue
                    resp.raise_for_status()

        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2.0)
                continue
            raise

    raise Exception("Ollama API 최대 재시도 초과")


# ============================================================
# 범용 LLM 호출 — 백엔드 분기
# ============================================================
async def _call_llm(
    messages: List[Dict],
    model: str,
    tools: Optional[List[Dict]] = None,
) -> Dict:
    cfg = AVAILABLE_MODELS.get(model, {})
    backend = cfg.get('backend', 'openai')
    temp = cfg.get('temperature', 0.2)
    max_tok = cfg.get('max_tokens', 2048)
    tout = cfg.get('timeout', 60.0)
    # Fix 4: num_ctx Ollama 전용
    num_ctx = cfg.get('num_ctx', 8192)

    logger.info(f"[Agent] _call_llm: backend={backend}, model={model}, tools={'Y' if tools else 'N'}")

    if backend == 'ollama':
        return await _call_ollama(messages, model, tools, temp, max_tok, num_ctx, tout)
    elif backend == 'openai':
        use_mct = cfg.get('use_max_completion_tokens', False)
        no_temp = cfg.get('no_temperature', False)
        return await _call_openai(messages, model, tools, temp, max_tok, tout,
                                  use_max_completion_tokens=use_mct,
                                  no_temperature=no_temp)
    else:
        # 알 수 없는 백엔드는 openai 로 폴백
        use_mct = cfg.get('use_max_completion_tokens', False)
        no_temp = cfg.get('no_temperature', False)
        return await _call_openai(messages, model, tools, temp, max_tok, tout,
                                  use_max_completion_tokens=use_mct,
                                  no_temperature=no_temp)


# ============================================================
# ★ Agent Loop v6.0
# ============================================================
async def run_agent(
    user_query: str,
    model: str = None,
    chat_history: List[Dict] = None,
    max_iterations: int = None,
    session_context: str = "",   # Phase 1-A: 세션 컨텍스트 블록 (기간/호스트/토픽)
    progress_callback=None,      # Phase 2-C: async callable(tool_name_kr, step, total) or None
) -> Dict:
    if not model:
        model = AGENT_MODEL
    user_model = model

    # ── 루프/최종 모델 분리 (v26) ──────────────────────────────────────
    # · UI에서 qwen2.5:7b 선택 시: loop=qwen2.5:7b, final=qwen2.5:7b (로컬 전용)
    # · UI에서 gpt-5.4 선택 시  : loop=gpt-4.1-mini(빠름), final=gpt-5.4(고품질)
    # · 환경변수 AGENT_LOOP_MODEL / AGENT_FINAL_MODEL 으로 개별 오버라이드 가능
    _loop_cfg  = AVAILABLE_MODELS.get(model, {})
    _is_ollama = _loop_cfg.get('backend', 'openai') == 'ollama'

    if _is_ollama:
        # 로컬 Ollama 선택 시: 루프·최종 모두 동일 모델 (외부 API 불필요)
        loop_model  = model
        final_model = model
    else:
        # OpenAI 계열: 루프=gpt-4.1-mini(빠름), 최종=gpt-5.4(고품질)
        loop_model  = AGENT_LOOP_MODEL   # 기본 gpt-4.1-mini
        final_model = AGENT_FINAL_MODEL  # 기본 gpt-5.4

    if max_iterations is None:
        cfg_tmp    = AVAILABLE_MODELS.get(loop_model, {})
        backend_tmp = cfg_tmp.get('backend', 'openai')
        max_iterations = AGENT_MAX_ITERATIONS_BY_BACKEND.get(backend_tmp, AGENT_MAX_ITERATIONS)
        logger.info(f"[Agent] max_iterations={max_iterations} (backend={backend_tmp})")

    cfg     = AVAILABLE_MODELS.get(loop_model, {})
    backend = cfg.get('backend', 'openai')

    # v12: Observation 압축 한도·top_k 는 루프 모델 기준 (데이터 수집 단계)
    obs_max_chars = cfg.get('obs_max_chars', 3000)
    obs_max_items = cfg.get('obs_max_items', 10)
    search_top_k  = cfg.get('top_k', 10)
    logger.info(
        f"[Agent] 모델 설정: loop={loop_model} final={final_model} "
        f"obs_max_chars={obs_max_chars}, obs_max_items={obs_max_items}, top_k={search_top_k}"
    )

    if backend not in ('ollama',) and loop_model not in TOOL_CALLING_MODELS:
        logger.warning(f"[Agent] {loop_model}은 Tool Calling 미지원. {AGENT_LOOP_MODEL} 으로 폴백")
        loop_model = AGENT_LOOP_MODEL
        cfg = AVAILABLE_MODELS.get(loop_model, {})
        backend = cfg.get('backend', 'openai')
        obs_max_chars = cfg.get('obs_max_chars', 3000)
        obs_max_items = cfg.get('obs_max_items', 10)
        search_top_k = cfg.get('top_k', 10)

    start_time = time.time()
    tool_trace = []

    _now = datetime.now(zoneinfo.ZoneInfo("Asia/Seoul"))
    _now_str = _now.strftime("%Y-%m-%d %H:%M:%S (KST)")
    # Phase 1-A: 세션 컨텍스트를 시스템 프롬프트 말미에 주입
    _ctx_suffix = f"\n\n{session_context}" if session_context else ""
    _system_content = f"현재 날짜/시간: {_now_str}\n\n{AGENT_SYSTEM_PROMPT}{_ctx_suffix}"
    messages = [{"role": "system", "content": _system_content}]

    if chat_history:
        for h in chat_history[-6:]:
            content = h.get("content", "")
            if content:
                messages.append({"role": h.get("role", "user"), "content": content})

    messages.append({"role": "user", "content": user_query})

    # ─────────────────────────────────────────────────────────────
    # v23.6: 이전 로그 데이터 재활용 — 후속 분석 질의 시 get_server_logs 재호출 방지
    # 조건:
    #   1) chat_history에 "[이전 로그 조회 결과 — N건]" 블록이 존재
    #   2) 현재 질의가 신규 로그 조회가 아닌 분석/원인/대응 질의
    # 처리:
    #   이전 로그 블록을 파싱해서 pre_logs를 구성 → _force_answer 직접 호출
    # ─────────────────────────────────────────────────────────────
    _ANALYSIS_KEYWORDS = (
        "분석", "원인", "대응", "조치", "해결", "왜", "이유", "방안",
        "상세", "설명", "어떻게", "발생 원인", "재발 방지", "긴급",
    )
    _NEW_LOG_FETCH_KEYWORDS = (
        "최신", "최근", "오늘", "어제", "새로운", "방금", "지금",
        "현재", "다시 조회", "재조회", "새로 조회",
    )
    _is_analysis_followup = (
        any(kw in user_query for kw in _ANALYSIS_KEYWORDS)
        and not any(kw in user_query for kw in _NEW_LOG_FETCH_KEYWORDS)
    )
    if _is_analysis_followup and chat_history:
        # 가장 최근 assistant 메시지에서 "[이전 로그 조회 결과 — N건]" 블록 추출
        _prev_log_block = None
        for _ch in reversed(chat_history[-6:]):
            if _ch.get("role") == "assistant":
                _c = _ch.get("content", "")
                if "[이전 로그 조회 결과" in _c:
                    _prev_log_block = _c
                    break
        if _prev_log_block:
            # 블록에서 로그 라인 파싱
            # 형식: [2026-03-19 14:48:25] /var/log/syslog [Warning] containerd: message
            import re as _re
            _log_pattern = _re.compile(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\S+)\s+\[(\w+)\]\s+([^:]+):\s*(.*)"
            )
            _parsed_prev_logs = []
            for _line in _prev_log_block.splitlines():
                _line = _line.strip()
                _m = _log_pattern.match(_line)
                if _m:
                    _parsed_prev_logs.append({
                        "time":     _m.group(1),
                        "log_file": _m.group(2),
                        "severity": _m.group(3),
                        "source":   _m.group(4).strip(),
                        "content":  _m.group(5).strip(),
                    })
            if _parsed_prev_logs:
                logger.info(
                    f"[Agent] v23.6: 이전 로그 재활용 — {len(_parsed_prev_logs)}건, "
                    f"user_query='{user_query[:60]}'"
                )
                # _force_answer와 동일한 방식으로 직접 분석 요청
                _DATA_CONSTRAINT_LOCAL = (
                    "【절대 규칙】아래 '도구 조회 결과'에 포함된 데이터만 사용하여 답변하세요.\n"
                    "도구 결과에 없는 정보를 절대 생성하지 마세요.\n\n"
                )
                _log_lines_str = "\n".join(
                    f"[{lg['time']}] {lg['log_file']} [{lg['severity']}] {lg['source']}: {lg['content']}"
                    for lg in _parsed_prev_logs
                )
                _force_content = (
                    _DATA_CONSTRAINT_LOCAL
                    + f"──── 이전 조회 로그 ({len(_parsed_prev_logs)}건) ────\n"
                    + _log_lines_str
                    + "\n──── 끝 ────\n\n"
                    + f"【사용자 질의】{user_query}\n\n"
                    + "위 로그 데이터를 기반으로 아래 JSON 형식으로 답변하세요.\n"
                    "logs 배열은 이미 별도로 구성되므로 제외하고, summary/analysis/recommendation만 작성하세요.\n"
                    "analysis와 recommendation을 충분히 상세하게 작성하세요.\n"
                    "```json\n"
                    "{\n"
                    "  \"query_type\": \"로그/에러 조회\",\n"
                    "  \"data\": {\n"
                    "    \"summary\": \"(로그 건수와 주요 내용 1~2줄 요약)\",\n"
                    "    \"analysis\": \"(로그 패턴·원인·영향 분석, 5문장 이상)\",\n"
                    "    \"recommendation\": \"(권장 조치·해결책, 2문장 이상)\"\n"
                    "  }\n"
                    "}\n"
                    "```"
                )
                _force_msgs = [
                    {
                        "role": "system",
                        "content": (
                            "당신은 서버 로그 분석 전문가입니다. "
                            "제공된 로그 데이터를 바탕으로 정확하고 유용한 분석과 권장 조치를 제공하세요. "
                            "반드시 JSON 형식으로만 답변하세요."
                        )
                    },
                    {"role": "user", "content": _force_content}
                ]
                _llm_resp = await _call_llm(_force_msgs, final_model, tools=None)
                _llm_text = (
                    _llm_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    or _llm_resp.get("content", "")
                ).strip()
                _parsed_llm2 = None
                try:
                    _t2 = _llm_text
                    if _t2.startswith("```"):
                        _lines2 = _t2.split("\n")
                        _t2 = "\n".join(_lines2[1:-1]).strip()
                    if not _t2.startswith("{"):
                        _t2 = _t2[_t2.find("{"):_t2.rfind("}")+1]
                    _parsed_llm2 = json.loads(_t2)
                except Exception:
                    pass
                if _parsed_llm2:
                    _llm_data2 = (
                        _parsed_llm2.get("data")
                        if isinstance(_parsed_llm2.get("data"), dict)
                        else _parsed_llm2
                    )
                else:
                    _llm_data2 = {}
                logger.info(
                    f"[Agent] v23.6 재활용 분석 결과: "
                    f"summary={bool(_llm_data2.get('summary'))}, "
                    f"analysis_len={len(_llm_data2.get('analysis') or '')}, "
                    f"rec_len={len(_llm_data2.get('recommendation') or '')}"
                )
                _final_answer2 = json.dumps({
                    "query_type": "로그/에러 조회",
                    "data": {
                        "summary": _llm_data2.get("summary") or f"로그 {len(_parsed_prev_logs)}건 분석 완료",
                        "logs": _parsed_prev_logs,
                        "analysis": _llm_data2.get("analysis") or "",
                        "recommendation": _llm_data2.get("recommendation") or "",
                    }
                }, ensure_ascii=False)
                return {
                    "answer": _final_answer2,
                    "tool_trace": [],
                    "iterations": 0,
                    "model": final_model,
                    "elapsed_ms": int((time.time() - start_time) * 1000),
                    "force_reason": "이전 로그 재활용 (재조회 방지)",
                    "response_meta": {},
                }

    for iteration in range(max_iterations):
        logger.info(f"[Agent] Iteration {iteration+1}/{max_iterations} loop_model={loop_model} backend={backend}")

        try:
            response = await _call_llm(messages, loop_model, tools=SLIM_TOOL_DEFINITIONS)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            logger.error(f"[Agent] API 오류: {status} — {error_body[:200]}")

            if status == 429 and tool_trace:
                return await _force_answer(messages, final_model, tool_trace, start_time, "API 속도 제한")

            # ── v13: 413 Payload Too Large — Fix-C 공격적 재압축 + Fix-A fallback 재검색 ──
            if status == 413:
                logger.warning(f"[Agent] 413 Payload Too Large — 재압축 시도 (iter={iteration+1})")
                # Fix-C: system prompt(~4500토큰) 포함 전체 예산 고려
                # tool 메시지 목표: 800자(≈200토큰) — 50% 대신 공격적 축소
                _TARGET_TOOL_CHARS = 800
                recompressed = False
                for m in messages:
                    if m.get("role") == "tool" and isinstance(m.get("content"), str):
                        cur_len = len(m["content"])
                        if cur_len > _TARGET_TOOL_CHARS:
                            # summary 필드만 남기고 나머지 제거
                            try:
                                obj = json.loads(m["content"])
                                slim = {
                                    "summary": str(obj.get("summary", ""))[:400],
                                    "count":   obj.get("count", obj.get("found", "")),
                                    "data":    obj.get("data", [])[:2],
                                }
                                m["content"] = json.dumps(slim, ensure_ascii=False)
                            except Exception:
                                m["content"] = m["content"][:_TARGET_TOOL_CHARS]
                            recompressed = True
                            logger.info(
                                f"[Agent] tool 메시지 공격적 재압축: "
                                f"{cur_len}자 → {len(m['content'])}자 (목표 {_TARGET_TOOL_CHARS}자)"
                            )
                if recompressed:
                    await asyncio.sleep(0.5)
                    continue
                # Fix-A: 재압축도 불가 → fallback 모델로 전환 + tool 메시지 전체 제거 후 재검색
                fallback_model = "llama-3.3-70b-versatile"
                if loop_model != fallback_model:
                    logger.warning(
                        f"[Agent] 413 재압축 실패 → {fallback_model} 전환 + 컨텍스트 초기화 후 재검색"
                    )
                    loop_model    = fallback_model
                    cfg           = AVAILABLE_MODELS.get(loop_model, {})
                    backend       = cfg.get('backend', 'groq')
                    obs_max_chars = cfg.get('obs_max_chars', 6000)
                    obs_max_items = cfg.get('obs_max_items', 10)
                    search_top_k  = cfg.get('top_k', 10)
                    # tool/강제주입 user 메시지 제거 → 깨끗한 컨텍스트
                    messages = [
                        m for m in messages
                        if m.get("role") in ("system", "user")
                        and not str(m.get("content", "")).startswith("search_historical_data 결과")
                    ]
                    # 새 모델 기준으로 재검색
                    _recat  = ["server"]
                    _re_obs = await execute_tool("search_historical_data", {
                        "search_query": user_query,
                        "categories":   _recat,
                        "top_k":        search_top_k,
                    })
                    tool_trace.append({
                        "tool":      "search_historical_data(413-fallback)",
                        "args":      {"search_query": user_query, "categories": _recat},
                        "found":     _re_obs.get("found"),
                        "count":     _re_obs.get("count", 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(
                        tool_trace[-1],
                        _re_obs,
                        {"search_query": user_query, "categories": _recat},
                    )
                    _re_compressed = _compress_observation(
                        _re_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name="search_historical_data",
                    )
                    messages.append({
                        "role":        "tool",
                        "tool_call_id": f"fallback_{iteration}",
                        "name":        "search_historical_data",
                        "content":     _re_compressed,
                    })
                    logger.info(
                        f"[Agent] fallback 재검색 완료: "
                        f"found={_re_obs.get('found')} count={_re_obs.get('count', 0)}"
                    )
                    await asyncio.sleep(0.5)
                    continue
                return _error_result(f"API 오류 (코드: {status})", tool_trace, iteration, final_model, start_time)

            return _error_result(f"API 오류 (코드: {status})", tool_trace, iteration, final_model, start_time)

        except Exception as e:
            logger.error(f"[Agent] LLM 호출 실패: {e}", exc_info=True)
            return _error_result(str(e)[:200], tool_trace, iteration, final_model, start_time)

        choice = response.get('choices', [{}])[0]
        msg = choice.get('message', {})
        tool_calls = msg.get('tool_calls')

        # ── Case 1: Tool Calls ──
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.get('content'),
                "tool_calls": tool_calls,
            })

            # ── 병렬 실행 가능 도구 쌍 감지 ──
            # 조건: (1) tool_calls가 정확히 2개, (2) 두 도구가 독립 쌍 목록에 해당,
            #       (3) 폴백 체인에 엮이지 않는 도구만 허용
            # 독립 쌍: 서로 다른 DB/API를 사용하고 결과가 서로의 입력에 영향을 주지 않는 조합
            _PARALLEL_PAIRS = {
                frozenset({"get_log_stats", "search_historical_data"}),
                frozenset({"get_server_status", "get_server_cpu"}),
                frozenset({"get_server_status", "get_server_memory"}),
                frozenset({"get_server_cpu",    "get_server_memory"}),
                frozenset({"get_server_status", "get_alerts"}),
            }
            _tool_name_set = frozenset(
                tc['function']['name'] for tc in tool_calls
            )
            _can_parallel = (
                len(tool_calls) == 2
                and _tool_name_set in _PARALLEL_PAIRS
            )

            if _can_parallel:
                # ── 병렬 실행 경로 ──
                logger.info(
                    f"[Agent] 병렬 실행: "
                    f"{[tc['function']['name'] for tc in tool_calls]}"
                )

                # 파라미터 파싱 + 보정 (순서 유지)
                _parsed = []
                for tc in tool_calls:
                    _tname = tc['function']['name']
                    try:
                        _raw = tc['function']['arguments']
                        _targs = json.loads(_raw) if isinstance(_raw, str) else _raw
                    except (json.JSONDecodeError, TypeError):
                        _targs = {}
                    _orig = dict(_targs)
                    _targs = _sanitize_tool_args(_tname, _targs)
                    if _orig != _targs:
                        logger.info(f"[Agent] 파라미터 보정됨: {_orig} → {_targs}")
                        tc['function']['arguments'] = json.dumps(_targs, ensure_ascii=False)
                    _parsed.append((_tname, _targs, tc))

                # SSE progress 콜백 (병렬이므로 첫 도구만 전송)
                if progress_callback:
                    try:
                        _kr0 = _TOOL_KR_NAME.get(_parsed[0][0], _parsed[0][0])
                        await progress_callback(
                            tool_name=_parsed[0][0],
                            tool_name_kr=_kr0,
                            step=len(tool_trace) + 1,
                        )
                    except Exception:
                        pass

                # asyncio.gather 로 동시 실행
                _t0 = asyncio.get_event_loop().time()
                _obs_list = await asyncio.gather(
                    execute_tool(_parsed[0][0], _parsed[0][1]),
                    execute_tool(_parsed[1][0], _parsed[1][1]),
                    return_exceptions=False,
                )
                _elapsed = asyncio.get_event_loop().time() - _t0
                logger.info(
                    f"[Agent] 병렬 실행 완료: "
                    f"{_parsed[0][0]}+{_parsed[1][0]} elapsed={_elapsed:.3f}s"
                )

                # 결과를 순서대로 messages에 append
                for (_tname, _targs, _tc), _obs in zip(_parsed, _obs_list):
                    tool_trace.append({
                        "tool":      _tname,
                        "args":      _targs,
                        "found":     _obs.get('found'),
                        "count":     _obs.get('count', 0),
                        "iteration": iteration + 1,
                        "parallel":  True,
                    })
                    attach_data_sources_to_trace_entry(tool_trace[-1], _obs, _targs)
                    _compressed = _compress_observation(
                        _obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        is_log_query=(_tname in {"get_server_logs", "get_errpt"}),
                        tool_name=_tname,
                    )
                    try:
                        _co = json.loads(_compressed)
                        _dt = _co.get("data_truncated")
                        if _dt:
                            tool_trace[-1]["data_truncated"] = _dt
                            tool_trace[-1]["available_hosts_total"] = _co.get("available_hosts_total")
                    except Exception:
                        pass
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": _tc.get('id', f'call_{_tname}'),
                        "name":         _tname,
                        "content":      _compressed,
                    })

                await asyncio.sleep(0.5)
                continue

            # ── 순차 실행 경로 (기존 로직 유지) ──
            for tc in tool_calls:
                tool_name = tc['function']['name']
                try:
                    raw_args = tc['function']['arguments']
                    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    tool_args = {}

                original_args = dict(tool_args)
                tool_args = _sanitize_tool_args(tool_name, tool_args)
                if original_args != tool_args:
                    logger.info(f"[Agent] 파라미터 보정됨: {original_args} → {tool_args}")
                    tc['function']['arguments'] = json.dumps(tool_args, ensure_ascii=False)

                # search_historical_data(network): 질문에 '지난 N일/시간'이 있는데 기간 파라미터 생략 시 보정
                if tool_name == "search_historical_data":
                    _cats = tool_args.get("categories") or []
                    _catset = set(_cats) if isinstance(_cats, list) else set()
                    if "network" in _catset and not tool_args.get("hours_ago") and not tool_args.get("days_ago"):
                        _d, _h = _parse_time_window_from_query(user_query)
                        if _h is not None:
                            tool_args["hours_ago"] = _h
                            logger.info(f"[Agent] search_historical_data: 질문에서 hours_ago={_h} 주입")
                        elif _d is not None:
                            tool_args["days_ago"] = _d
                            logger.info(f"[Agent] search_historical_data: 질문에서 days_ago={_d} 주입")
                        tc['function']['arguments'] = json.dumps(tool_args, ensure_ascii=False)
                    _hist_period_tool = re.compile(
                        r"(?:지난|최근)\s*\d+\s*(?:일|시간)",
                        re.IGNORECASE,
                    )
                    if (
                        "network" in _catset
                        and _hist_period_tool.search(user_query)
                        and re.search(r"가장|최고|상위|포트|인터페이스|트래픽", user_query)
                    ):
                        _tk = tool_args.get("top_k")
                        if isinstance(_tk, int) and _tk < 15:
                            tool_args["top_k"] = 15
                            logger.info("[Agent] search_historical_data: 포트/이력 순위 질의 → top_k 최소 15로 보정")
                            tc['function']['arguments'] = json.dumps(tool_args, ensure_ascii=False)

                logger.info(f"[Agent] Tool: {tool_name}({tool_args})")

                # ── Phase 2-C: SSE progress 콜백 ──
                if progress_callback:
                    try:
                        _kr = _TOOL_KR_NAME.get(tool_name, tool_name)
                        await progress_callback(
                            tool_name=tool_name,
                            tool_name_kr=_kr,
                            step=len(tool_trace) + 1,
                        )
                    except Exception:
                        pass

                observation = await execute_tool(tool_name, tool_args)

                # ── get_network_interfaces → 서버 자산 폴백: get_server_interfaces 자동 재시도 ──
                # tool_executor에서 is_server_asset=True 반환 시 올바른 도구로 자동 전환
                if (
                    tool_name == "get_network_interfaces"
                    and observation.get("is_server_asset") is True
                ):
                    fallback_host = tool_args.get("host_query", "all")
                    logger.info(
                        f"[Agent] get_network_interfaces: '{fallback_host}'는 서버 자산 → "
                        f"get_server_interfaces 자동 재시도"
                    )
                    fallback_obs = await execute_tool("get_server_interfaces", {
                        "host_query": fallback_host,
                        "status_filter": "all",
                    })
                    if fallback_obs.get("found") and fallback_obs.get("count", 0) > 0:
                        observation = fallback_obs
                        tool_name = "get_server_interfaces"
                        tool_args = {"host_query": fallback_host, "status_filter": "all"}
                        logger.info(
                            f"[Agent] get_server_interfaces 폴백 성공: "
                            f"count={fallback_obs.get('count')}, host='{fallback_host}'"
                        )
                    else:
                        logger.warning(
                            f"[Agent] get_server_interfaces 폴백도 결과 없음 "
                            f"(host='{fallback_host}'). 데이터 없음으로 응답."
                        )

                # ── search_historical_data 결과 없을 때 폴백 처리 ──
                if (
                    tool_name == "search_historical_data"
                    and (not observation.get('found') or observation.get('count', 0) == 0)
                ):
                    req_categories = tool_args.get("categories") or []
                    _log_cats = {"log"}
                    _is_log_query = bool(_log_cats & set(req_categories))

                    if _is_log_query:
                        # 로그 질의 fallback: server_logs/server_logs_chunked 테이블 포함하여 재시도
                        fallback_search_query = tool_args.get("search_query", "")
                        logger.info(
                            f"[Agent] search_historical_data(log) 결과 없음 → "
                            f"categories=['server','log'] 로 재시도: query={fallback_search_query[:50]}"
                        )
                        fallback_obs = await execute_tool("search_historical_data", {
                            "search_query": fallback_search_query,
                            "categories": ["server", "log"],
                        })
                        if fallback_obs.get('found') and fallback_obs.get('count', 0) > 0:
                            observation = fallback_obs
                            tool_args = {"search_query": fallback_search_query, "categories": ["server", "log"]}
                            logger.info(
                                f"[Agent] search_historical_data(server,log) 재시도 성공: "
                                f"count={fallback_obs.get('count')}"
                            )
                        else:
                            logger.warning(
                                f"[Agent] 로그 데이터 없음 — "
                                f"categories=['log','server'] 모두 결과 없음 "
                                f"(query='{fallback_search_query[:60]}')"
                            )
                    else:
                        # [Fix v22.1] 카테고리별 분기: network 질의는 서버 도구로 절대 폴백하지 않음
                        _is_network_query_flag = bool({"network"} & set(req_categories))
                        if _is_network_query_flag:
                            # 네트워크 질의: search_query 보강 후 network 카테고리로 재시도만 수행
                            orig_query = tool_args.get("search_query", "")
                            retry_query = f"네트워크 인터페이스 트래픽 변동폭 표준편차 {orig_query}"
                            logger.info(
                                f"[Agent] search_historical_data(network) 결과 없음 → "
                                f"쿼리 보강 후 network 재시도: '{retry_query[:60]}'"
                            )
                            fallback_net_obs = await execute_tool("search_historical_data", {
                                "search_query": retry_query,
                                "categories": ["network"],
                                "top_k": max(search_top_k, 5),
                            })
                            if fallback_net_obs.get('found') and fallback_net_obs.get('count', 0) > 0:
                                observation = fallback_net_obs
                                tool_args = {"search_query": retry_query, "categories": ["network"]}
                                logger.info(
                                    f"[Agent] search_historical_data(network) 보강 재시도 성공: "
                                    f"count={fallback_net_obs.get('count')}"
                                )
                            else:
                                logger.warning(
                                    f"[Agent] 네트워크 이력 데이터 없음 — 서버 폴백 차단 "
                                    f"(query='{orig_query[:60]}'). 데이터 없음으로 응답."
                                )
                                # 네트워크 데이터 없음 — observation 그대로 유지 (found=False)
                        else:
                            # [Fix v3.8] 서버/기타 이력 질의: hostname/IP 패턴일 때만 실시간 서버 도구 폴백 허용
                            # 문장형 쿼리(공백 2개 이상 or 길이 20+ or 한글 포함)는 폴백 차단 → 데이터 없음 응답
                            fallback_search_str = tool_args.get("search_query", "")
                            _is_host_pattern = bool(
                                re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', fallback_search_str.strip())  # IP
                                or (
                                    len(fallback_search_str) < 20
                                    and len(fallback_search_str.split()) <= 2
                                    and not re.search(r'[가-힣]', fallback_search_str)
                                )
                            )
                            if _is_host_pattern:
                                fallback_host = fallback_search_str or "Zabbix server"
                                logger.info(
                                    f"[Agent] search_historical_data(server) 결과 없음 → "
                                    f"hostname 패턴 감지, get_server_cpu 재시도 (host={fallback_host[:40]})"
                                )
                                fallback_obs = await execute_tool("get_server_cpu", {
                                    "host_query": fallback_host,
                                    "include_top_processes": True,
                                })
                                if fallback_obs.get('found') and fallback_obs.get('count', 0) > 0:
                                    observation = fallback_obs
                                    tool_name = "get_server_cpu"
                                    tool_args = {"host_query": fallback_host, "include_top_processes": True}
                                    logger.info(f"[Agent] get_server_cpu 재시도 성공: count={fallback_obs.get('count')}")
                                else:
                                    fallback_obs2 = await execute_tool("get_processes", {
                                        "host_query": fallback_host,
                                        "top_n": 5,
                                    })
                                    if fallback_obs2.get('found'):
                                        observation = fallback_obs2
                                        tool_name = "get_processes"
                                        tool_args = {"host_query": fallback_host, "top_n": 5}
                                        logger.info(f"[Agent] get_processes 재시도 성공: count={fallback_obs2.get('count')}")
                            else:
                                logger.warning(
                                    f"[Agent] search_historical_data(server) 결과 없음 + 문장형 쿼리 감지 → "
                                    f"서버 폴백 차단 (query='{fallback_search_str[:60]}'). 데이터 없음으로 응답."
                                )

                tool_trace.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "found": observation.get('found'),
                    "count": observation.get('count', 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], observation, tool_args)
                # LLM이 JSON details를 비워도 UI 표 복구용 — get_vmware 관측 행을 보존
                if tool_name == "get_vmware" and observation.get("found"):
                    try:
                        _data = observation.get("data") or []
                        _fb = _vmware_tool_data_to_list_items(_data)
                        if _fb:
                            tool_trace[-1]["vmware_list_fallback"] = _fb
                    except Exception as _e:
                        logger.warning(f"[Agent] vmware_list_fallback 생성 실패: {_e}")

                # v23.1: 로그 조회 도구(get_server_logs/get_errpt)는 원문 잘림 없이 전체 보존
                _LOG_TOOLS = {"get_server_logs", "get_errpt"}
                _is_log_tool = tool_name in _LOG_TOOLS
                compressed = _compress_observation(
                    observation,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    is_log_query=_is_log_tool,
                    tool_name=tool_name,
                )
                # v16: data_truncated 정보를 tool_trace에 기록 (프론트 절삭 알림용)
                try:
                    _comp_obj = json.loads(compressed)
                    _dt = _comp_obj.get("data_truncated")
                    if _dt:
                        tool_trace[-1]["data_truncated"] = _dt
                        tool_trace[-1]["available_hosts_total"] = _comp_obj.get("available_hosts_total")
                except Exception:
                    pass
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get('id', f'call_{tool_name}'),
                    "name": tool_name,
                    "content": compressed,
                })

            await asyncio.sleep(0.5)
            continue

        # ── Case 2: 최종 텍스트 답변 ──
        answer_text = msg.get('content', '')
        if answer_text:
            # ── 도구 미호출 + 로그/이력 키워드 감지 시 강제 search_historical_data 호출 ──
            if not tool_trace and iteration == 0:
                _LOG_HISTORY_KEYWORDS = [
                    "/var/log", "syslog", "messages", "kern.log", "auth.log",
                    "로그 분석", "로그 항목", "로그 빈도", "에러 로그", "경고 로그",
                    "이력", "변동폭", "트렌드", "추이", "표준편차",
                    "지난 N일", "지난 1일", "지난 2일", "지난 3일", "지난 7일",
                    "최근 1일", "최근 2일", "최근 3일", "최근 7일", "최근 일주일",
                    "이번 주", "어제", "오늘",
                    "yesterday", "last ", "trend", "history",
                ]
                user_q_lower = user_query.lower()
                needs_search = any(kw.lower() in user_q_lower for kw in _LOG_HISTORY_KEYWORDS)
                if needs_search:
                    logger.warning(
                        f"[Agent] 도구 미호출 + 로그/이력 키워드 감지 → "
                        f"search_historical_data 강제 호출 (iter={iteration+1})"
                    )
                    # categories 결정: 로그 파일 경로/키워드면 log, 그 외 이력이면 general
                    _log_file_kw = ["/var/log", "syslog", "kern.log", "auth.log", "로그 항목", "로그 분석", "에러 로그", "경고 로그"]
                    forced_categories = (
                        ["log"] if any(kw.lower() in user_q_lower for kw in _log_file_kw)
                        else ["network", "server"]
                    )
                    forced_obs = await execute_tool("search_historical_data", {
                        "search_query": user_query,
                        "categories": forced_categories,
                        "top_k": search_top_k,
                    })
                    tool_trace.append({
                        "tool": "search_historical_data",
                        "args": {"search_query": user_query, "categories": forced_categories},
                        "found": forced_obs.get('found'),
                        "count": forced_obs.get('count', 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(
                        tool_trace[-1],
                        forced_obs,
                        {"search_query": user_query, "categories": forced_categories},
                    )
                    compressed = _compress_observation(
                        forced_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name="search_historical_data",
                    )
                    # 현재 assistant 응답 + 도구 결과를 메시지에 추가 후 재시도
                    messages.append({"role": "assistant", "content": answer_text.strip()})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"search_historical_data 결과:\n{compressed}\n\n"
                            "위 실제 데이터를 바탕으로 분석하고, "
                            "반드시 JSON 형식(query_type='비교/분석')으로 답변하세요."
                        ),
                    })
                    logger.info(
                        f"[Agent] 강제 search_historical_data 완료: "
                        f"found={forced_obs.get('found')} count={forced_obs.get('count', 0)}"
                    )
                    continue  # 다음 iteration으로 재시도

            # ── 품질 가드 1: 로그 원인/대응 질의는 search_historical_data(log) 병행 강제 ──
            _used_tools = {t.get("tool", "") for t in tool_trace}
            _q_lower = user_query.lower()
            _is_log_rootcause_q = (
                any(k in _q_lower for k in ("원인", "대응", "조치", "분석"))
                and any(k in _q_lower for k in ("로그", "error", "errpt"))
            )
            if (
                _is_log_rootcause_q
                and ("search_historical_data" not in _used_tools)
                and any(t in _used_tools for t in ("get_server_logs", "get_errpt"))
            ):
                logger.warning("[Agent] 로그 원인/대응 질의인데 search_historical_data 누락 → 강제 보강")
                forced_args = {
                    "search_query": user_query,
                    "categories": ["log"],
                    "days_ago": 1,
                    "top_k": search_top_k,
                }
                forced_obs = await execute_tool("search_historical_data", forced_args)
                tool_trace.append({
                    "tool": "search_historical_data",
                    "args": forced_args,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, forced_args)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="search_historical_data",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        f"추가 이력 조회 결과(search_historical_data/log):\n{compressed}\n\n"
                        "위 로그 원문 + 이력 통계를 결합해 원인/대응을 다시 JSON으로 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 1b: 자원·기간·특이사항 질의는 RAG(search_historical_data) 병행 ──
            # Context Sources UI의 vector_tables는 이 도구 관측값에서만 집계됨(Zabbix 전용 도구만 쓰면 비어 있음).
            _has_rag_trace = any(
                (t.get("tool") or "").startswith("search_historical_data")
                for t in tool_trace
            )
            if not _has_rag_trace:
                _resource_kw = (
                    "자원", "리소스", "cpu", "메모리", "memory", "디스크", "disk",
                    "스왑", "swap", "부하", "성능", "사용", "사용률", "점유",
                )
                _time_kw = (
                    "이번주", "이번 주", "금주", "최근 일주일", "일주일", "주간",
                    "지난주", "지난 주", "최근 7일", "7일", "지난 7일",
                    "오늘", "금일", "어제", "최근 24시간",
                )
                _issue_kw = (
                    "특이사항", "이상", "추이", "변동", "종합", "점검", "요약",
                    "문제", "병목",
                )
                _res_hit = any(k in _q_lower for k in _resource_kw)
                _time_hit = any(k in _q_lower for k in _time_kw)
                _issue_hit = any(k in _q_lower for k in _issue_kw)
                if _res_hit and (_time_hit or _issue_hit):
                    logger.warning(
                        "[Agent] 자원/기간/특이사항 질의인데 search_historical_data 누락 → RAG 보강"
                    )
                    _pd, _ph = _parse_time_window_from_query(user_query)
                    _forced_args = {
                        "search_query": user_query,
                        "categories": ["server", "alert"],
                        "top_k": max(search_top_k, 12),
                    }
                    if _ph is not None:
                        _forced_args["hours_ago"] = _ph
                    elif _pd is not None:
                        _forced_args["days_ago"] = _pd
                    else:
                        if any(
                            x in _q_lower
                            for x in (
                                "이번주", "이번 주", "금주", "일주일", "주간",
                                "7일", "지난 7일", "최근 7일",
                            )
                        ):
                            _forced_args["days_ago"] = 7
                        elif any(x in _q_lower for x in ("오늘", "금일")):
                            _forced_args["hours_ago"] = 24
                        elif "어제" in _q_lower:
                            _forced_args["days_ago"] = 2
                        else:
                            _forced_args["days_ago"] = 7
                    forced_obs = await execute_tool("search_historical_data", _forced_args)
                    tool_trace.append({
                        "tool": "search_historical_data",
                        "args": _forced_args,
                        "found": forced_obs.get("found"),
                        "count": forced_obs.get("count", 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(
                        tool_trace[-1], forced_obs, _forced_args
                    )
                    compressed = _compress_observation(
                        forced_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name="search_historical_data",
                    )
                    messages.append({"role": "assistant", "content": answer_text.strip()})
                    messages.append({
                        "role": "user",
                        "content": (
                            "추가 RAG 이력 검색(search_historical_data, 임베딩·청킹 테이블):\n"
                            f"{compressed}\n\n"
                            "위 벡터 검색 결과를 실제 데이터 출처로 반영하고, "
                            "기존 도구 결과와 함께 종합하여 JSON으로 답변하세요."
                        ),
                    })
                    continue

            # ── 품질 가드 2: 종합/특이사항 질의는 최소 2개 도구 교차 확인 ──
            _is_comprehensive_q = any(k in _q_lower for k in ("종합", "특이사항", "이상 징후", "점검", "변동내역"))
            if _is_comprehensive_q and len([t for t in _used_tools if t]) < 2:
                logger.warning("[Agent] 종합 질의인데 단일 도구 응답 감지 → 도메인 도구 보강")
                candidate_calls = [
                    ("get_alerts", {"host_query": "all", "status_filter": "all", "limit": 20}),
                    ("get_server_status", {"host_query": "all"}),
                ]
                if any(k in _q_lower for k in ("네트워크", "스위치", "라우터", "포트", "인터페이스")):
                    if not any(
                        k in _q_lower
                        for k in ("vmware", "vcenter", "하이퍼바이저", "가상머신", "esxi", "_vcenter")
                    ):
                        candidate_calls.append(("get_network_device", {"host_query": "all"}))

                added = 0
                for tool_name, tool_args in candidate_calls:
                    if tool_name in _used_tools:
                        continue
                    forced_obs = await execute_tool(tool_name, tool_args)
                    tool_trace.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "found": forced_obs.get("found"),
                        "count": forced_obs.get("count", 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, tool_args)
                    compressed = _compress_observation(
                        forced_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name=tool_name,
                    )
                    messages.append({
                        "role": "user",
                        "content": f"[강제 보강:{tool_name}]\n{compressed}",
                    })
                    added += 1
                    if len([t.get("tool", "") for t in tool_trace if t.get("tool")]) >= 2:
                        break
                if added > 0:
                    messages.append({"role": "assistant", "content": answer_text.strip()})
                    messages.append({
                        "role": "user",
                        "content": "추가 도구 결과를 반영해 종합 특이사항을 다시 JSON으로 답변하세요.",
                    })
                    continue

            # ── 품질 가드 3: VMware 질의인데 네트워크 장비 도구만 쓴 경우 → get_vmware 보강 ──
            _vm_pat = re.search(
                r"vmware|vcenter|하이퍼바이저|가상머신|esxi|[\w]+_vcenter|openbase_vcenter",
                user_query,
                re.IGNORECASE,
            )
            _is_vmware_intent = bool(_vm_pat)
            _nw_misuse = bool(
                _used_tools & {"get_network_device", "get_network_interfaces"}
            ) and "get_vmware" not in _used_tools
            if _is_vmware_intent and _nw_misuse:
                logger.warning("[Agent] VMware 의도인데 네트워크 도구만 사용됨 → get_vmware(realtime) 강제 보강")
                vhost = "all"
                m2 = re.search(r"([\w]+_vcenter|openbase_vcenter)", user_query, re.IGNORECASE)
                if m2:
                    vhost = m2.group(1).strip()
                forced_obs = await execute_tool(
                    "get_vmware",
                    {
                        "host_query": vhost,
                        "mode": "realtime",
                        "vmware_type": "all",
                        "limit": 40,
                    },
                )
                tool_trace.append({
                    "tool": "get_vmware",
                    "args": {"host_query": vhost, "mode": "realtime", "vmware_type": "all"},
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(
                    tool_trace[-1],
                    forced_obs,
                    {"host_query": vhost, "mode": "realtime", "vmware_type": "all"},
                )
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="get_vmware",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:VMware]\n"
                        f"{compressed}\n\n"
                        "위는 VMware 하이퍼바이저/VM의 실시간 CPU 등 지표입니다. "
                        "네트워크 스위치 장비 수치와 혼동하지 말고, 질문에 맞게 JSON으로 다시 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 4a: N-PEAK — **시각/언제** 피크 질의에 이력 검색 누락 시 강제
            # (주의: '가장 높았던 포트'처럼 **어느 포트** 질의는 4b로 분리 — 시각이 아님)
            _port_or_traffic_re = re.compile(
                r"\d+/\d+/\d+|포트|트래픽|인터페이스|nic|대역폭|bps|사용률",
                re.IGNORECASE,
            )
            _time_peak_intent_re = re.compile(
                r"언제|몇\s*시|시각|시점|높았던\s*시간|피크|peak|최고였\s*때|"
                r"가장\s*높았던\s*시간|가장\s*높은\s*시각|몇\s*시경",
                re.IGNORECASE,
            )
            _hist_period_re = re.compile(
                r"(?:지난|최근)\s*\d+\s*(?:일|시간)",
                re.IGNORECASE,
            )
            _is_npeak_time_q = bool(_time_peak_intent_re.search(user_query)) and bool(
                _port_or_traffic_re.search(user_query)
            )
            _is_nw_port_rank_hist_q = (
                bool(_hist_period_re.search(user_query))
                and bool(
                    re.search(
                        r"가장|최고|상위|제일|높은|높았|많았|큰\s*포트|트래픽.*(높|많|큰)",
                        user_query,
                    )
                )
                and bool(_port_or_traffic_re.search(user_query))
                and any(
                    k in _q_lower
                    for k in ("네트워크", "스위치", "장비", "인프라", "포트", "인터페이스")
                )
                and not _is_npeak_time_q
            )
            if _is_npeak_time_q and "search_historical_data" not in _used_tools:
                logger.warning("[Agent] N-PEAK(피크 시각) 질의인데 search_historical_data 누락 → 강제 보강")
                forced_args = _build_search_window_args(user_query, search_top_k, min_top_k=8)
                forced_obs = await execute_tool("search_historical_data", forced_args)
                tool_trace.append({
                    "tool": "search_historical_data",
                    "args": forced_args,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, forced_args)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="search_historical_data",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:N-PEAK 이력]\n"
                        f"{compressed}\n\n"
                        "질문은 **언제/몇 시에** 트래픽·사용률이 높았는지입니다. "
                        "get_network_interfaces 등 스냅샷만으로는 과거 피크 시각을 말할 수 없습니다. "
                        "위 이력 청크의 시간·수치·인터페이스·장비명만 근거로 답하고, "
                        "질문에 없는 장비명을 이전 대화에서 임의로 끌어오지 마세요. JSON으로 다시 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 4b: 지난 N일 **어느 포트/인터페이스** 트래픽이 가장 컸는지 — 이력 검색 + 한계 명시
            if _is_nw_port_rank_hist_q and "search_historical_data" not in _used_tools:
                logger.warning(
                    "[Agent] N-HIST-PORT(이력 기준 포트 순위) 질의인데 search_historical_data 누락 → 강제 보강"
                )
                forced_args = _build_search_window_args(user_query, search_top_k, min_top_k=20)
                forced_obs = await execute_tool("search_historical_data", forced_args)
                tool_trace.append({
                    "tool": "search_historical_data",
                    "args": forced_args,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, forced_args)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="search_historical_data",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:네트워크 이력 순위]\n"
                        f"{compressed}\n\n"
                        "이 검색은 **유사도 상위 K개 청크**만 반환합니다. "
                        "전체 포트·전체 구간을 집계한 **절대적 최댓값**으로 단정하지 마세요.\n"
                        "반드시: 청크 summary에 적힌 **장비명·인터페이스·수치·시각**만 인용하고, "
                        "여러 청크의 수치를 비교해 **검색된 이력 범위 안에서** 상대적으로 가장 큰 트래픽이 드러난 포트를 제시하세요. "
                        "근거가 약하면 '검색된 이력 일부에 한함'·'추가 집계 필요'라고 명시하세요. JSON으로 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 4c: 4b 유형에서 이력만 있고 스냅샷 없음 → 현재 포트 트래픽과 구분해 교차 참고
            if (
                _is_nw_port_rank_hist_q
                and "search_historical_data" in _used_tools
                and "get_network_interfaces" not in _used_tools
            ):
                logger.warning(
                    "[Agent] N-HIST-PORT 질의 → get_network_interfaces(현재 스냅샷) 교차 참고"
                )
                fa = {
                    "host_query": "all",
                    "sort_by": "traffic",
                    "top_n": 15,
                    "link_filter": "all",
                }
                forced_obs = await execute_tool("get_network_interfaces", fa)
                tool_trace.append({
                    "tool": "get_network_interfaces",
                    "args": fa,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, fa)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="get_network_interfaces",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:현재 스냅샷 get_network_interfaces]\n"
                        f"{compressed}\n\n"
                        "위는 **현재(최신) 스냅샷** 기준 포트 트래픽 순서입니다. "
                        "질문의 '지난 N일 이력'과는 시점이 다를 수 있습니다. "
                        "이력 청크에서 제시한 포트와 혼동하지 말고, 답변에 **이력 근거 vs 현재 스냅샷**을 구분해 JSON으로 작성하세요."
                    ),
                })
                continue

            # ── 품질 가드 5: 네트워크 알림 필터·‘서버 제외’ 질의 → get_alerts 필수 ──
            _nw_only_m = re.search(
                r"(네트워크|스위치|장비|인프라)\s*만|만\s*(네트워크|스위치|장비)",
                _q_lower,
            )
            _nw_alert_filter = (
                any(k in _q_lower for k in ("알림", "장애", "이벤트"))
                and any(k in _q_lower for k in ("네트워크", "스위치", "장비", "인프라"))
                and (
                    any(k in _q_lower for k in ("제외", "무관", "필터", "빼"))
                    or ("서버" in _q_lower and any(k in _q_lower for k in ("제외", "아닌", "빼")))
                    or bool(_nw_only_m)
                )
            )
            if _nw_alert_filter and "get_alerts" not in _used_tools:
                logger.warning("[Agent] 네트워크 알림 필터 질의인데 get_alerts 누락 → 강제 보강")
                fa = {"host_query": "all", "status_filter": "all", "limit": 50}
                forced_obs = await execute_tool("get_alerts", fa)
                tool_trace.append({
                    "tool": "get_alerts",
                    "args": fa,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, fa)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="get_alerts",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:get_alerts]\n"
                        f"{compressed}\n\n"
                        "위는 실제 알림 목록입니다. 네트워크 관련만 골라 요약하고, "
                        "도구 없이 ‘필터했다’고만 쓰지 마세요. JSON으로 다시 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 6: Gi/인터페이스 실시간 순위(피크 **시각** 질의 아님) → get_network_interfaces ──
            _peak_only_for_guard6 = re.compile(
                r"언제|몇\s*시|시각|시점|높았던\s*시간|피크|peak|최고였\s*때|"
                r"가장\s*높았던\s*시간|가장\s*높은\s*시각|몇\s*시경",
                re.IGNORECASE,
            )
            _iface_rank_live = re.compile(
                r"gi\s*\d*/\d*|gigabitethernet|인터페이스.*순위|트래픽.*순위|트래픽이\s*큰|대역폭.*순|상위\s*\d+\s*포트",
                re.IGNORECASE,
            )
            _is_iface_rank_not_peak = bool(_iface_rank_live.search(user_query)) and not bool(
                _peak_only_for_guard6.search(user_query)
            )
            if _is_iface_rank_not_peak and "get_network_interfaces" not in _used_tools:
                logger.warning(
                    "[Agent] 인터페이스 실시간 순위 질의인데 get_network_interfaces 누락 → 강제 보강"
                )
                fa = {
                    "host_query": "all",
                    "sort_by": "traffic",
                    "top_n": 15,
                    "link_filter": "all",
                }
                forced_obs = await execute_tool("get_network_interfaces", fa)
                tool_trace.append({
                    "tool": "get_network_interfaces",
                    "args": fa,
                    "found": forced_obs.get("found"),
                    "count": forced_obs.get("count", 0),
                    "iteration": iteration + 1,
                })
                attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, fa)
                compressed = _compress_observation(
                    forced_obs,
                    max_items=obs_max_items,
                    max_chars=obs_max_chars,
                    tool_name="get_network_interfaces",
                )
                messages.append({"role": "assistant", "content": answer_text.strip()})
                messages.append({
                    "role": "user",
                    "content": (
                        "[강제 보강:get_network_interfaces]\n"
                        f"{compressed}\n\n"
                        "위는 현재 스냅샷 기준 포트/인터페이스 트래픽입니다. "
                        "Gi·Ethernet 이름 매칭과 순위를 질문에 맞게 JSON으로 답변하세요."
                    ),
                })
                continue

            # ── 품질 가드 7: 네트워크 인프라 ‘위험/신호’ 질의 → 장비+알림 보강 ──
            _nw_risk_q = (
                any(k in _q_lower for k in ("위험", "신호", "경고"))
                and any(k in _q_lower for k in ("네트워크", "인프라", "스위치", "장비"))
                and not any(k in _q_lower for k in ("vmware", "vcenter", "esxi", "가상"))
            )
            if _nw_risk_q and (
                "get_network_device" not in _used_tools or "get_alerts" not in _used_tools
            ):
                logger.warning("[Agent] 네트워크 위험/신호 질의 → get_network_device·get_alerts 보강")
                messages.append({"role": "assistant", "content": answer_text.strip()})
                for tool_name, tool_args in (
                    ("get_network_device", {"host_query": "all"}),
                    ("get_alerts", {"host_query": "all", "status_filter": "all", "limit": 30}),
                ):
                    if tool_name in _used_tools:
                        continue
                    forced_obs = await execute_tool(tool_name, tool_args)
                    tool_trace.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "found": forced_obs.get("found"),
                        "count": forced_obs.get("count", 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, tool_args)
                    _used_tools.add(tool_name)
                    compressed = _compress_observation(
                        forced_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name=tool_name,
                    )
                    messages.append({
                        "role": "user",
                        "content": f"[강제 보강:{tool_name}]\n{compressed}",
                    })
                messages.append({
                    "role": "user",
                    "content": (
                        "위 장비 상태·알림을 근거로 네트워크 인프라 위험 신호를 "
                        "3가지 이내로 JSON으로 요약하세요."
                    ),
                })
                continue

            # ── 품질 가드 8: 포트 가동률·낮은 스위치 → 장비+인터페이스 보강 ──
            _low_util_q = (
                any(k in _q_lower for k in ("가동률", "가동 율", "가동률이"))
                and any(k in _q_lower for k in ("스위치", "네트워크", "포트", "장비"))
            )
            if _low_util_q and (
                "get_network_device" not in _used_tools
                or "get_network_interfaces" not in _used_tools
            ):
                logger.warning("[Agent] 가동률/포트 활용 질의 → 장비+인터페이스 보강")
                messages.append({"role": "assistant", "content": answer_text.strip()})
                seq = [
                    ("get_network_device", {"host_query": "all"}),
                    (
                        "get_network_interfaces",
                        {"host_query": "all", "sort_by": "traffic", "top_n": 48, "link_filter": "all"},
                    ),
                ]
                for tool_name, tool_args in seq:
                    if tool_name in _used_tools:
                        continue
                    forced_obs = await execute_tool(tool_name, tool_args)
                    tool_trace.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "found": forced_obs.get("found"),
                        "count": forced_obs.get("count", 0),
                        "iteration": iteration + 1,
                    })
                    attach_data_sources_to_trace_entry(tool_trace[-1], forced_obs, tool_args)
                    _used_tools.add(tool_name)
                    compressed = _compress_observation(
                        forced_obs,
                        max_items=obs_max_items,
                        max_chars=obs_max_chars,
                        tool_name=tool_name,
                    )
                    messages.append({
                        "role": "user",
                        "content": f"[강제 보강:{tool_name}]\n{compressed}",
                    })
                messages.append({
                    "role": "user",
                    "content": (
                        "Link Up/Down 비율과 트래픽 분포로 가동률이 낮게 보이는 스위치를 "
                        "JSON으로 정리하세요."
                    ),
                })
                continue

            # ── Fix-B: tool_trace 있는데 자연어(비JSON) 답변 → _force_answer 로 JSON 재요청 ──
            _is_json_answer = False
            _stripped = answer_text.strip()
            if _stripped.startswith("{") or "```json" in _stripped.lower():
                _is_json_answer = True

            # v23.3: 로그 도구(get_server_logs/get_errpt) 사용 시 반드시 _force_answer 경유
            # — LLM이 JSON을 직접 만들면 content 필드가 길어 잘리는 문제 방지
            _LOG_TOOLS_SET = {"get_server_logs", "get_errpt"}
            _has_log_tool = any(t.get("tool", "") in _LOG_TOOLS_SET for t in tool_trace)
            if _has_log_tool and _is_json_answer:
                logger.info(
                    f"[Agent] v23.3: 로그 도구 사용 감지 → _force_answer 강제 경유 "
                    f"(iter={iteration+1}) — pre_logs 직접 파싱으로 JSON 잘림 방지"
                )
                return await _force_answer(
                    messages, final_model, tool_trace, start_time, "로그 도구 → pre_logs 직접 파싱"
                )

            if tool_trace and not _is_json_answer:
                logger.warning(
                    f"[Agent] v13 Fix-B: tool_trace 있으나 자연어 답변 감지 "
                    f"(iter={iteration+1}, len={len(_stripped)}) → _force_answer 호출"
                )
                return await _force_answer(
                    messages, final_model, tool_trace, start_time, "자연어 답변 → JSON 재요청"
                )

            # ── v21.3 Fix-B2: tool_trace 없고 자연어 답변 → 그대로 general 반환 ──
            if not tool_trace and not _is_json_answer and len(_stripped) > 30:
                logger.info(
                    f"[Agent] v21.3 Fix-B2: 도구 없이 자연어 답변 "
                    f"(iter={iteration+1}, len={len(_stripped)}) → general 구조로 반환"
                )
                elapsed = int((time.time() - start_time) * 1000)
                return {
                    "answer": _stripped,
                    "tool_trace": tool_trace,
                    "iterations": iteration + 1,
                    "model": final_model,
                    "elapsed_ms": elapsed,
                    "response_meta": _build_response_meta(tool_trace),  # Phase 2-A
                }

            elapsed = int((time.time() - start_time) * 1000)
            logger.info(f"[Agent] 완료 iter={iteration+1} {elapsed}ms tools={len(tool_trace)}개")

            # ── v19.0: 한자 감지 시 한국어 재질의 1회 ──
            final_answer = answer_text.strip()
            if final_answer and _has_chinese(final_answer):
                logger.warning("[Agent] 최종 답변에 한자 감지 — 한국어 재질의 시도")
                final_answer = await _retry_korean(messages, final_model, final_answer)

            return {
                "answer": final_answer,
                "tool_trace": tool_trace,
                "iterations": iteration + 1,
                "model": final_model,
                "elapsed_ms": elapsed,
                "response_meta": _build_response_meta(tool_trace),  # Phase 2-A
            }

        # ── Case 3: 빈 응답 ──
        logger.warning(f"[Agent] 빈 응답 (iter={iteration+1})")
        if tool_trace:
            return await _force_answer(messages, final_model, tool_trace, start_time, "빈 응답")
        messages.append({"role": "assistant", "content": ""})
        messages.append({"role": "user", "content": "도구를 사용하여 데이터를 조회하거나, 최종 답변을 JSON으로 작성하세요."})

    logger.warning(f"[Agent] max_iterations({max_iterations}) 도달")
    return await _force_answer(messages, final_model, tool_trace, start_time, f"최대 반복({max_iterations}회)")


# ============================================================
# 헬퍼
# ============================================================
def _is_network_query(tool_trace: list) -> bool:
    """tool_trace에 네트워크 인터페이스 도구 호출이 있는지 확인"""
    network_tools = {"get_network_interfaces", "get_network_device", "get_server_interfaces"}
    return any(t.get("tool") in network_tools for t in tool_trace)


def _is_server_nic_query(tool_trace: list) -> bool:
    """tool_trace에 서버 NIC 도구(get_server_interfaces) 호출이 있는지 확인"""
    return any(t.get("tool") == "get_server_interfaces" for t in tool_trace)


async def _force_answer(messages, model, tool_trace, start_time, reason):
    # ── v21.14: 도구 결과(Observation) 원본을 명시적으로 포함 ──
    # _force_answer 호출 시 LLM이 이전 컨텍스트(CPU/서버 등)로 오염되는 것을 방지하기 위해
    # 실제 도구 결과를 직접 프롬프트에 삽입하고, "이 데이터만 사용" 제약을 추가한다.
    tool_observations = []
    for m in messages:
        if m.get("role") == "tool" and m.get("content"):
            tool_name = m.get("name", "unknown")
            tool_observations.append(f"[{tool_name} 결과]\n{m['content']}")
    observation_block = "\n\n".join(tool_observations) if tool_observations else ""

    # ── [v22.2] 데이터 없음 early-return ──
    # observation_block이 비어 있거나 모든 도구 결과가 found=false 인 경우
    # LLM에 재호출하지 않고 즉시 "조회 결과 없음" JSON 반환 → 할루시네이션 방지
    _all_not_found = False
    if not observation_block:
        _all_not_found = True
    else:
        # tool_trace에서 found 여부 확인 (found=False 만 있으면 데이터 없음)
        found_flags = [t.get("result", {}).get("found") for t in tool_trace if "result" in t]
        if found_flags and all(f is False for f in found_flags):
            _all_not_found = True

    if _all_not_found:
        logger.warning(
            f"[Agent] _force_answer: 도구 결과 없음 → LLM 재호출 없이 '조회 결과 없음' 반환 "
            f"(reason={reason})"
        )
        no_data_answer = json.dumps({
            "query_type": "일반 질문",
            "data": {
                "summary": "조회된 데이터가 없습니다.",
                "details": [{"label": "결과", "value": "해당 조건에 맞는 데이터를 찾을 수 없습니다."}],
                "analysis": "도구 조회 결과가 없으므로 분석할 수 없습니다. 검색 조건을 확인하거나 기간을 변경해 주세요.",
                "recommendation": "조회 기간 또는 검색 키워드를 변경하여 다시 시도해 주세요."
            }
        }, ensure_ascii=False)
        return {
            "answer": no_data_answer,
            "tool_trace": tool_trace,
            "iterations": len(set(t.get('iteration', 0) for t in tool_trace)),
            "model": model,
            "elapsed_ms": int((time.time() - start_time) * 1000),
            "force_reason": reason,
            "response_meta": _build_response_meta(tool_trace),  # Phase 2-A
        }

    # 네트워크 포트/인터페이스 조회인지 판별
    is_network = _is_network_query(tool_trace)
    is_server_nic = _is_server_nic_query(tool_trace)

    # [Fix v22.1] 분석/이력 질의 판별: 네트워크 도구가 포함된 경우 analysis 분기 차단
    # network tool_trace가 있으면 is_network=True이므로 is_analysis 계산 자체는 하되
    # is_network가 True면 아래 분기에서 network 경로로 먼저 처리됨 (순서 보장)
    _ANALYSIS_TOOLS = {"search_historical_data", "get_server_cpu", "get_server_memory",
                       "get_processes", "get_server_status"}
    # network 카테고리 search_historical_data가 포함된 경우: analysis 분기가 아닌 network 분기로 유도
    _has_network_search = any(
        t.get("tool", "") in ("search_historical_data", "search_historical_data(413-fallback)")
        and "network" in str(t.get("args", {}).get("categories", []))
        for t in tool_trace
    )
    is_analysis = (
        not _has_network_search
        and any(t.get("tool", "").replace("(413-fallback)", "").strip() in _ANALYSIS_TOOLS
                for t in tool_trace)
    )

    # v21.14: 공통 데이터 제약 접두사 — 반드시 아래 데이터만 기반으로 답변
    _DATA_CONSTRAINT = (
        "【절대 규칙】아래 '도구 조회 결과'에 포함된 데이터만 사용하여 답변하세요.\n"
        "도구 결과에 없는 정보(다른 카테고리 데이터, 임의 수치, 가상 항목 등)를 절대 생성하지 마세요.\n"
        "질문이 네트워크에 관한 것이면 네트워크 데이터만, 서버에 관한 것이면 서버 데이터만 사용하세요.\n\n"
    )

    # v23.1: 로그 조회 도구 판별
    _LOG_TOOLS = {"get_server_logs", "get_errpt"}
    is_log_tool_query = any(t.get("tool", "") in _LOG_TOOLS for t in tool_trace)

    if is_log_tool_query:
        # v23.5: 로그 조회 — tool 결과에서 직접 logs 배열 구성 후 LLM에 분석만 요청
        # 마지막 로그 도구 결과만 사용 (중복 방지: 이전 질의의 결과 포함 방지)
        pre_logs = []
        _last_log_tool_msg = None
        for m in messages:
            if m.get("role") == "tool" and m.get("name") in _LOG_TOOLS:
                _last_log_tool_msg = m  # 마지막 로그 도구 메시지만 유지
        if _last_log_tool_msg:
            try:
                tool_result = json.loads(_last_log_tool_msg["content"])
                raw_data = tool_result.get("data", [])
                for r in raw_data:
                    entry = {
                        "time":     r.get("time") or r.get("errpt_time") or "",
                        "log_file": r.get("log_file") or r.get("log_files") or r.get("resource_name") or "",
                        "severity": r.get("severity") or r.get("type") or "Information",
                        "source":   r.get("hostname") or r.get("source") or "",
                        "content":  r.get("log") or r.get("description") or r.get("content") or "",
                    }
                    pre_logs.append(entry)
            except Exception:
                pass

        if pre_logs:
            # 원본 사용자 질의 추출 — 마지막 user 메시지 = 현재 질의
            _user_query = ""
            for _m in reversed(messages):
                if _m.get("role") == "user":
                    _user_query = (_m.get("content") or "")
                    if isinstance(_user_query, list):
                        _user_query = " ".join(
                            p.get("text", "") for p in _user_query if isinstance(p, dict)
                        )
                    break  # 마지막 user 메시지 = 현재 질의

            # 로그 데이터를 직접 확보했으므로 LLM에는 분석/요약만 요청
            force_content = (
                _DATA_CONSTRAINT
                + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
                + (f"【사용자 질의】{_user_query}\n\n" if _user_query else "")
                + "위 로그 데이터를 기반으로 아래 JSON 형식으로 답변하세요.\n"
                "logs 배열은 이미 별도로 구성되므로 제외하고, summary/analysis/recommendation만 작성하세요.\n"
                "사용자가 분석·해결책·원인 파악 등을 요청한 경우 analysis와 recommendation을 충분히 상세하게 작성하세요.\n"
                "```json\n"
                "{\n"
                "  \"query_type\": \"로그/에러 조회\",\n"
                "  \"data\": {\n"
                "    \"summary\": \"(로그 건수와 주요 내용 1~2줄 요약)\",\n"
                "    \"analysis\": \"(로그 패턴·원인·영향 분석, 5문장 이상. 단순 조회 요청이면 빈 문자열)\",\n"
                "    \"recommendation\": \"(권장 조치·해결책, 2문장 이상. 단순 조회 요청이면 빈 문자열)\"\n"
                "  }\n"
                "}\n"
                "```"
            )
            logger.info(f"[Agent] _force_answer pre_logs: user_query='{_user_query[:80]}'")
            # pre_logs를 tool_trace에 임시 보관 (parse_agent_answer에서 꺼내 씀)
            # → _force_answer 반환값에 pre_logs 직접 포함
            force_msgs = [
                {
                    "role": "system",
                    "content": (
                        "당신은 서버 로그 분석 전문가입니다. "
                        "제공된 로그 데이터를 바탕으로 정확하고 유용한 분석과 권장 조치를 제공하세요. "
                        "반드시 JSON 형식으로만 답변하세요."
                    )
                },
                {"role": "user", "content": force_content}
            ]
            llm_resp = await _call_llm(force_msgs, model, tools=None)
            llm_text = (
                llm_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                or llm_resp.get("content", "")  # Ollama 폴백
            ).strip()
            # JSON 파싱 시도
            parsed_llm = None
            try:
                _t = llm_text
                if _t.startswith("```"):
                    _lines = _t.split("\n"); _t = "\n".join(_lines[1:-1]).strip()
                if not _t.startswith("{"):
                    _t = _t[_t.find("{"):_t.rfind("}")+1]
                parsed_llm = json.loads(_t)
            except Exception:
                pass
            # data 키가 있으면 data 안을, 없으면 최상위를 사용
            if parsed_llm:
                if "data" in parsed_llm and isinstance(parsed_llm["data"], dict):
                    llm_data = parsed_llm["data"]
                else:
                    # data 래퍼 없이 최상위에 summary/analysis 직접 반환된 경우
                    llm_data = parsed_llm
            else:
                llm_data = {}
            logger.info(
                f"[Agent] _force_answer LLM 분석 결과: "
                f"summary={bool(llm_data.get('summary'))}, "
                f"analysis_len={len(llm_data.get('analysis') or '')}, "
                f"rec_len={len(llm_data.get('recommendation') or '')}, "
                f"llm_text_preview={llm_text[:120]!r}"
            )
            # 최종 응답 구성 — logs는 pre_logs로 직접 채움
            final_answer = json.dumps({
                "query_type": "로그/에러 조회",
                "data": {
                    "summary": llm_data.get("summary") or f"로그 {len(pre_logs)}건 조회 완료",
                    "logs": pre_logs,
                    "analysis": llm_data.get("analysis") or "",
                    "recommendation": llm_data.get("recommendation") or "",
                }
            }, ensure_ascii=False)
            return {
                "answer": final_answer,
                "tool_trace": tool_trace,
                "iterations": len(set(t.get('iteration', 0) for t in tool_trace)),
                "model": model,
                "elapsed_ms": int((time.time() - start_time) * 1000),
                "force_reason": reason,
                "response_meta": _build_response_meta(tool_trace),
            }
        else:
            # pre_logs가 없으면 기존 방식으로 LLM에 전체 요청
            force_content = (
                _DATA_CONSTRAINT
                + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
                "위 로그 데이터를 기반으로 도구 호출 없이 최종 답변을 JSON으로 작성하세요.\n"
                "【로그 원문 출력 절대 규칙】\n"
                "1. query_type은 반드시 \"로그/에러 조회\"로 설정하세요.\n"
                "2. logs 배열에 도구가 반환한 모든 로그 항목을 빠짐없이 포함하세요.\n"
                "3. 각 항목: {\"time\":\"날짜시각\",\"log_file\":\"파일명\",\"severity\":\"Warning\",\"source\":\"발생원\",\"content\":\"원문 전체\"}\n"
                "4. content는 절대 요약·자르지 마세요.\n"
                "출력 예:\n"
                "{\"query_type\":\"로그/에러 조회\",\"data\":{\"summary\":\"요약\","
                "\"logs\":[{\"time\":\"2025-01-01 10:00:00\",\"log_file\":\"/var/log/syslog\","
                "\"severity\":\"Warning\",\"source\":\"dockerd\",\"content\":\"원문\"}],"
                "\"analysis\":\"분석\",\"recommendation\":\"권장 조치\"}}"
            )
    elif is_server_nic:
        # 서버 NIC(get_server_interfaces) 조회: 서버 인터페이스 전용 포맷
        force_content = (
            _DATA_CONSTRAINT
            + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
            "위 데이터를 기반으로 도구 호출 없이 최종 답변을 JSON으로 작성하세요. "
            "수집된 데이터의 수치를 그대로 인용하세요. "
            "query_type은 반드시 \"목록 조회\"로 설정하세요. "
            "details 배열에 **각 NIC/인터페이스를 1개의 JSON 객체**로 표현하세요 "
            "(예: {\"순위\":\"1위\",\"인터페이스\":\"eth0\",\"상태\":\"active\","
            "\"수신\":\"1.23 Gbps\",\"송신\":\"456 Mbps\",\"속도\":\"10 Gbps\"}). "
            "label/value 세로 나열 형식은 절대 사용하지 마세요. "
            "도구가 반환하지 않은 임의 인터페이스명을 절대 생성하지 마세요. "
            "details가 비어 있으면 안 됩니다. "
            "【단위 변환 필수】bps 단위 숫자는 반드시 읽기 좋은 단위로 변환: "
            "≥1,000,000,000→Gbps / ≥1,000,000→Mbps / ≥1,000→Kbps / 미만→bps. "
            "예) 252080→252.08 Kbps, 1500000→1.50 Mbps. "
            "동일 표 내 모든 수신/송신 값은 통일된 단위로 표시하세요."
        )
    elif is_network:
        force_content = (
            _DATA_CONSTRAINT
            + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
            "위 데이터를 기반으로 도구 호출 없이 최종 답변을 JSON으로 작성하세요. "
            "수집된 데이터의 수치를 그대로 인용하세요. "
            "query_type은 반드시 \"목록 조회\"로 설정하세요. "
            "details 배열에 **각 포트를 1개의 JSON 객체**로 표현하세요 "
            "(예: {\"순위\":\"1위\",\"인터페이스\":\"1/1/5\",\"링크\":\"up\",\"수신\":\"2.77 Gbps\",\"송신\":\"415 Mbps\",\"속도\":\"10G\",\"에러\":\"0\"}). "
            "label/value 세로 나열 형식은 절대 사용하지 마세요. "
            "도구가 반환하지 않은 eth0, eth1 등 가상 포트명을 절대 생성하지 마세요. "
            "details가 비어 있으면 안 됩니다. "
            "【단위 변환 필수】bps 단위 숫자는 반드시 읽기 좋은 단위로 변환: "
            "≥1,000,000,000→Gbps / ≥1,000,000→Mbps / ≥1,000→Kbps / 미만→bps. "
            "동일 표 내 모든 수신/송신 값은 통일된 단위로 표시하세요."
        )
    elif is_analysis:
        # v13 Fix-B / v18.3: 분석/이력 질의 전용 — 반드시 비교/분석 JSON 형식
        force_content = (
            _DATA_CONSTRAINT
            + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
            "위 데이터를 기반으로 도구 호출 없이 최종 답변을 **반드시 아래 JSON 형식**으로 작성하세요.\n"
            "자연어 답변은 절대 금지입니다. 오직 JSON만 출력하세요.\n\n"
            "【필수 규칙】\n"
            "1. label에 날짜를 반드시 괄호로 포함하세요: \"CPU 피크 시각 (2026-03-02)\" 형식\n"
            "   → 이 형식이어야 표(table)로 자동 변환됩니다.\n"
            "2. 실제 데이터가 있는 날짜만 details에 포함하세요.\n"
            "   → 데이터가 없거나 value가 '-', '—', '없음'인 행은 절대 추가하지 마세요.\n"
            "3. 같은 날짜의 항목은 label 앞부분만 다르게 하고 괄호 날짜는 동일하게 유지하세요.\n"
            "4. 【단위 변환 필수】트래픽/대역폭 값에 bps 단위 숫자가 있으면 반드시 변환: "
            "≥1,000,000,000→Gbps / ≥1,000,000→Mbps / ≥1,000→Kbps / 미만→bps. "
            "예) 252080→252.08 Kbps, 322340→322.34 Kbps, 2770000000→2.77 Gbps. "
            "동일 표(컬럼) 내 단위는 통일하세요.\n\n"
            "```json\n"
            "{\n"
            "  \"query_type\": \"비교/분석\",\n"
            "  \"data\": {\n"
            "    \"summary\": \"(2-3문장 핵심 요약)\",\n"
            "    \"details\": [\n"
            "      {\"label\": \"CPU 피크 시각 (2026-03-02)\", \"value\": \"09:00, 10:00\"},\n"
            "      {\"label\": \"주요 원인 프로세스 (2026-03-02)\", \"value\": \"ollama (761%)\"},\n"
            "      {\"label\": \"CPU 피크 시각 (2026-03-01)\", \"value\": \"10:00\"},\n"
            "      {\"label\": \"주요 원인 프로세스 (2026-03-01)\", \"value\": \"ollama (548%)\"}\n"
            "    ],\n"
            "    \"analysis\": \"(상세 원인 분석)\",\n"
            "    \"recommendation\": \"(권고 조치)\"\n"
            "  }\n"
            "}\n"
            "```\n\n"
            "수집된 데이터 수치를 그대로 인용하세요. details는 label/value 형식만 허용합니다.\n"
            "데이터가 없는 날짜 행은 추가하지 마세요."
        )
    else:
        force_content = (
            _DATA_CONSTRAINT
            + f"──── 도구 조회 결과 ────\n{observation_block}\n──── 끝 ────\n\n"
            "위 데이터를 기반으로 도구 호출 없이 최종 답변을 JSON으로 작성하세요. "
            "수집된 데이터의 수치를 그대로 인용하세요. "
            "목록/Top N 질의인 경우 details 배열의 각 항목을 "
            "{\"순위\":\"1위\",\"항목명\":\"값\", ...} 형식의 가로 객체로 채우세요. "
            "반드시 details 배열에 도구에서 받은 항목들을 채우세요. "
            "details가 비어있으면 안 됩니다."
        )

    messages.append({"role": "user", "content": force_content})
    try:
        response = await _call_llm(messages, model, tools=None)
        answer = response.get('choices', [{}])[0].get('message', {}).get('content', '')
    except Exception as e:
        logger.error(f"[Agent] 강제 답변 실패: {e}")
        answer = json.dumps({
            "query_type": "일반 질문",
            "data": {"summary": f"답변 생성 오류 ({reason})", "analysis": str(e)[:200]}
        }, ensure_ascii=False)

    # ── v19.0: 한자 감지 시 한국어 재질의 1회 ──
    if answer and _has_chinese(answer):
        logger.warning("[Agent] _force_answer 결과에 한자 감지 — 한국어 재질의 시도")
        answer = await _retry_korean(messages, model, answer)

    return {
        "answer": answer.strip() if answer else "",
        "tool_trace": tool_trace,
        "iterations": len(set(t.get('iteration', 0) for t in tool_trace)),
        "model": model,
        "elapsed_ms": int((time.time() - start_time) * 1000),
        "force_reason": reason,
        "response_meta": _build_response_meta(tool_trace),  # Phase 2-A
    }


def _error_result(error_msg, tool_trace, iteration, model, start_time):
    return {
        "answer": json.dumps({
            "query_type": "일반 질문",
            "data": {
                "summary": "시스템 오류가 발생했습니다.",
                "analysis": error_msg,
                "recommendation": "잠시 후 다시 시도해 주세요."
            }
        }, ensure_ascii=False),
        "tool_trace": tool_trace,
        "iterations": iteration + 1,
        "model": model,
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


# ============================================================
# Agent 답변 후처리 — 프론트엔드 호환 구조로 변환
# ============================================================
QUERY_TYPE_MAP = {
    "상태 조회": "status", "장애 조회": "alert", "목록 조회": "list",
    "비교/분석": "analysis", "프로세스 분석": "process", "일반 질문": "general",
    "로그/에러 조회": "log",
    # 네트워크 관련 추가 타입 (LLM이 다양하게 출력하는 경우 대응)
    "네트워크 조회": "list", "네트워크 장비 조회": "list",
    "네트워크 장비 포트 상태 조회": "list", "포트 조회": "list",
    "트래픽 조회": "list", "인터페이스 조회": "list",
    "포트 트래픽 조회": "list", "네트워크 포트 조회": "list",
    # 이력/트렌드/변동폭 → analysis
    "변동폭 분석": "analysis", "트렌드 분석": "analysis", "이력 분석": "analysis",
    "성능 분석": "analysis", "트래픽 분석": "analysis", "포트 분석": "analysis",
    "트래픽 변동 분석": "analysis", "네트워크 성능 분석": "analysis",
    # 로그 분석 → analysis
    "로그 분석": "analysis", "로그 조회": "analysis", "에러 로그 분석": "analysis",
    "로그 패턴 분석": "analysis", "이벤트 로그 분석": "analysis", "시스템 로그 분석": "analysis",
    "로그 빈도 분석": "analysis", "경고 로그 분석": "analysis", "로그 통계": "analysis",
    # 영어 타입도 그대로 통과
    "status": "status", "alert": "alert", "list": "list",
    "analysis": "analysis", "process": "process", "general": "general",
    "log": "log",
}

# 할루시네이션 의심 포트명 패턴 (실제 장비에 없을 가능성이 높은 범용 이름)
_HALLUCINATION_PORT_PATTERNS = re.compile(
    r'^(eth|ens|enp|eno|veth|docker|br-|virbr|lo|tun|tap)\d*',
    re.IGNORECASE
)


def _has_hallucinated_ports(details: list) -> bool:
    """
    details 배열에 eth0, ens33 등 할루시네이션 의심 포트명이 있는지 확인.
    네트워크 포트 객체(인터페이스 필드 포함)에만 적용.
    """
    for item in details:
        if not isinstance(item, dict):
            continue
        # 인터페이스/포트명 필드 찾기
        iface_val = (
            item.get('인터페이스') or item.get('interface') or
            item.get('Interface') or item.get('포트') or
            item.get('port') or ''
        )
        if iface_val and _HALLUCINATION_PORT_PATTERNS.match(str(iface_val)):
            return True
    return False


def _convert_repeated_labels_to_items(details: list) -> list:
    """
    label/value 세로 반복 나열을 항목별 가로 객체로 변환.

    입력 예:
      [{"label":"인터페이스","value":"1/1/5"},
       {"label":"수신 트래픽","value":"2.77 Gbps"},
       {"label":"인터페이스","value":"lag128_"},   ← 같은 label 반복 → 새 항목 시작
       {"label":"수신 트래픽","value":"243 Mbps"}]

    출력 예:
      [{"인터페이스":"1/1/5","수신 트래픽":"2.77 Gbps"},
       {"인터페이스":"lag128_","수신 트래픽":"243 Mbps"}]
    """
    if not details:
        return details

    # 반복되는 label이 있는지 확인
    labels = [d.get("label", "") for d in details if isinstance(d, dict)]
    if len(labels) == len(set(labels)):
        # label 중복 없음 → 단일 항목 → 변환 불필요
        return details

    # 첫 번째 반복 label을 기준으로 묶음 분리
    first_label = labels[0] if labels else None
    if not first_label:
        return details

    items = []
    current_item = {}
    for d in details:
        if not isinstance(d, dict):
            continue
        lbl = d.get("label", "")
        val = d.get("value", "")
        # 첫 번째 label이 다시 등장하면 새 항목 시작
        if lbl == first_label and current_item:
            items.append(current_item)
            current_item = {}
        current_item[lbl] = val

    if current_item:
        items.append(current_item)

    return items if len(items) >= 2 else details


# ── v14: analysis label/value → 날짜 기준 가로 행 그룹화 ──────────────────────
# v15: 날짜 패턴 확장 — YYYY-MM-DD, YYYY-MM, YYYY-MM월, YYYY-MM-Wn, 주간 표현 지원
_DATE_IN_LABEL = re.compile(
    r'\('
    r'(\d{4}-\d{2}(?:-\d{2})?'   # YYYY-MM 또는 YYYY-MM-DD (기본)
    r'(?:-W\d{1,2})?'              # 선택적 주차: -W1, -W12
    r'(?:\s*(?:월|주|분기|반기))?'  # 선택적 한글 단위: 월, 주 등
    r'[^)]*)'                       # 기타 추가 텍스트 허용
    r'\)'
)

def _group_analysis_details_by_date(details: list) -> Optional[list]:
    """
    analysis 타입의 label/value details에서 label 안에 날짜가 포함된 경우
    날짜별로 가로 행으로 그룹화하여 표 렌더링용 rows를 반환.

    v18.3 추가: 날짜가 label 괄호 안에 없고 value에 날짜가 있는 경우도 처리
    (LLM이 날짜를 label 대신 value에 넣었을 때 폴백 그룹화)

    입력 예 (정상):
      [{"label":"CPU 피크 시각 (2026-03-02)","value":"15:00"}, ...]
    입력 예 (폴백):
      [{"label":"날짜","value":"2026-03-02"},
       {"label":"CPU 피크 시각","value":"09:00"},
       {"label":"주요 프로세스","value":"ollama (761%)"}, ...]

    날짜 그룹화가 불가능하면 None 반환 (기존 렌더링 유지).
    """
    if not details or not isinstance(details, list):
        return None

    _EMPTY_VALUES = {"", "—", "-", "없음", "n/a", "null", "none", "해당없음", "해당 없음"}
    _DATE_VALUE_RE = re.compile(r'^\d{4}-\d{2}(-\d{2})?$')

    groups: dict = {}
    order: list = []

    # ── 1차: label 괄호 안 날짜 패턴 (정상 경로) ──────────────────────────────
    for item in details:
        if not isinstance(item, dict):
            continue
        label = item.get("label", "")
        value = item.get("value", "")
        m = _DATE_IN_LABEL.search(label)
        if m:
            date_key = m.group(1).strip()
            base_label = _DATE_IN_LABEL.sub("", label).strip()
            if not base_label:
                base_label = "값"
            if date_key not in groups:
                groups[date_key] = {"날짜": date_key}
                order.append(date_key)
            groups[date_key][base_label] = str(value)

    # ── 2차: label 괄호 날짜가 0개이면 폴백 — value에서 날짜 탐지 ────────────
    # LLM이 {"label":"날짜","value":"2026-03-02"}, {"label":"항목","value":"값"} 패턴으로
    # 생성한 경우: "날짜" label의 value를 날짜 키로 삼아 순서대로 그룹화
    if not groups:
        current_date = None
        current_row: dict = {}
        for item in details:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            # "날짜" 또는 "일자" label이면 날짜 키 시작
            if label in ("날짜", "일자", "date", "Date") or _DATE_VALUE_RE.match(value):
                # 이전 그룹 저장
                if current_date and len(current_row) > 1:
                    if current_date not in groups:
                        groups[current_date] = {"날짜": current_date}
                        order.append(current_date)
                    groups[current_date].update(current_row)
                # 새 날짜 그룹 시작
                date_candidate = value if _DATE_VALUE_RE.match(value) else None
                current_date = date_candidate
                current_row = {}
            elif current_date:
                current_row[label] = value
        # 마지막 그룹 저장
        if current_date and len(current_row) > 1:
            if current_date not in groups:
                groups[current_date] = {"날짜": current_date}
                order.append(current_date)
            groups[current_date].update(current_row)

    # v18.3: 모든 데이터 컬럼이 빈값/대시인 행 제거 (LLM이 생성한 빈 날짜 행 필터링)
    def _is_empty_row(row: dict) -> bool:
        """날짜 키를 제외한 모든 값이 빈값/대시이면 True"""
        data_vals = [v for k, v in row.items() if k != "날짜"]
        if not data_vals:
            return True
        return all(str(v).strip().lower() in _EMPTY_VALUES for v in data_vals)

    order = [k for k in order if not _is_empty_row(groups[k])]
    groups = {k: groups[k] for k in order}

    # v15: 날짜 그룹 1개 + 항목 2개 이상 → 단일 행 표 (날짜별 세부 항목 요약)
    if len(groups) == 1 and len(list(groups.values())[0]) >= 3:
        return [groups[k] for k in order]   # 1행 표로 렌더링

    if len(groups) < 2:
        return None   # 날짜 없거나 1개 항목 → metric-card 유지

    return [groups[k] for k in order]


def _recover_partial_json(text: str) -> Optional[Dict]:
    """
    v12: 절단된 JSON에서 핵심 필드(summary, analysis, recommendation)를
    정규식으로 부분 추출하여 analysis 타입 결과로 재구성.
    파싱 실패 시 raw JSON 화면 노출 방지.
    """
    if not text:
        return None

    def _extract_field(src: str, field: str) -> str:
        """JSON 문자열에서 특정 필드 값을 정규식으로 추출"""
        # "field":"값" 또는 "field": "값" 패턴
        pattern = rf'"{field}"\s*:\s*"((?:[^"\\]|\\.){{0,3000}})'
        m = re.search(pattern, src, re.DOTALL)
        if m:
            raw = m.group(1)
            # 미완성 이스케이프 제거
            raw = re.sub(r'\\$', '', raw)
            return raw.strip()
        return ""

    summary        = _extract_field(text, "summary")
    analysis       = _extract_field(text, "analysis")
    recommendation = _extract_field(text, "recommendation")

    # 최소한 summary 또는 analysis 중 하나는 있어야 복구 의미 있음
    if not summary and not analysis:
        return None

    # query_type 추출 시도
    qt_match = re.search(r'"query_type"\s*:\s*"([^"]{1,40})"', text)
    raw_qt = qt_match.group(1) if qt_match else "비교/분석"
    query_type = QUERY_TYPE_MAP.get(raw_qt, "analysis")

    return {
        "query_type": query_type,
        "title": summary[:80] if summary else "분석 결과",
        "answer": summary,
        "analysis": analysis,
        "recommendation": recommendation,
        "note": "[부분 복구: 응답이 잘려 핵심 내용만 표시됩니다]",
        "items": [],
        "total_count": 0,
    }


def _repair_json(text: str) -> str:
    """잘린 JSON 복구 시도"""
    text = text.rstrip()
    text = re.sub(r',\s*$', '', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    if text.count('"') % 2 != 0:
        text += '"'
    text = re.sub(r',\s*$', '', text)
    opens = text.count('[') - text.count(']')
    braces = text.count('{') - text.count('}')
    text += ']' * max(0, opens)
    text += '}' * max(0, braces)
    return text


_NET_UNIT_PATTERN = re.compile(r'(\d+(?:\.\d+)?)\s*(GB/s|MB/s|KB/s|B/s)\b', re.IGNORECASE)
_NET_HINT_PATTERN = re.compile(r'(트래픽|대역폭|수신|송신|interface|포트|port|bps)', re.IGNORECASE)
_DISK_HINT_PATTERN = re.compile(r'(disk|디스크|filesystem|파일시스템|mount|inode|용량|여유)', re.IGNORECASE)


def _normalize_network_units_text(text: str) -> str:
    """GB/s, MB/s, KB/s, B/s 표기를 Gbps/Mbps/Kbps/bps로 정규화."""
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        val = float(m.group(1))
        unit = m.group(2).lower()
        bps = val
        if unit == "gb/s":
            bps = val * 8_000_000_000
        elif unit == "mb/s":
            bps = val * 8_000_000
        elif unit == "kb/s":
            bps = val * 8_000
        elif unit == "b/s":
            bps = val * 8

        if bps >= 1_000_000_000:
            return f"{bps/1_000_000_000:.2f} Gbps"
        if bps >= 1_000_000:
            return f"{bps/1_000_000:.2f} Mbps"
        if bps >= 1_000:
            return f"{bps/1_000:.2f} Kbps"
        return f"{bps:.0f} bps"

    return _NET_UNIT_PATTERN.sub(_repl, text)


def _normalize_network_units_obj(obj):
    if isinstance(obj, str):
        return _normalize_network_units_text(obj)
    if isinstance(obj, list):
        return [_normalize_network_units_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _normalize_network_units_obj(v) for k, v in obj.items()}
    return obj


def _looks_like_disk_query(result: Dict) -> bool:
    text = " ".join(
        str(result.get(k, "")) for k in ("title", "summary", "answer", "analysis", "recommendation")
    )
    if _DISK_HINT_PATTERN.search(text or ""):
        return True
    for row in result.get("details", []) or []:
        if isinstance(row, dict) and _DISK_HINT_PATTERN.search(" ".join(map(str, row.keys()))):
            return True
    return False


def _vmware_tool_data_to_list_items(data: Any) -> List[Dict]:
    """get_vmware observation data[] → 목록 UI용 행(한글 컬럼)."""
    if not isinstance(data, list):
        return []
    vm_rows: List[Dict] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        if r.get("vmware_type") != "VM":
            continue
        cpu = r.get("cpu_usage_pct")
        try:
            cpu_s = f"{float(cpu):.2f}%" if cpu is not None else ""
        except (TypeError, ValueError):
            cpu_s = str(cpu) if cpu is not None else ""
        vm_rows.append({
            "가상머신명": r.get("asset_name") or "",
            "CPU 사용률": cpu_s,
            "IP": r.get("ip") or "",
            "호스트": r.get("hypervisor_name") or "",
        })
    try:
        vm_rows.sort(
            key=lambda x: float(str(x.get("CPU 사용률", "0")).replace("%", "").strip() or 0),
            reverse=True,
        )
    except Exception:
        pass
    for i, row in enumerate(vm_rows):
        row["순위"] = f"{i + 1}위"
    return vm_rows


_IFACE_TABLE_KEY_HINTS = frozenset({
    "인터페이스", "interface", "interface_name", "iface", "name",
    "수신", "송신", "traffic_in", "traffic_out", "rx_bps", "tx_bps",
    "recv", "send", "in_rate", "out_rate", "rx", "tx",
    "link", "link_status", "링크", "대역폭", "bandwidth", "speed_fmt",
})


def _details_look_like_interface_table(details: List[Any]) -> bool:
    """status 타입인데 label/value가 아닌 인터페이스·트래픽 표 행인지 (목록 UI로 전환)."""
    if not details:
        return False
    for row in details[:3]:
        if not isinstance(row, dict):
            return False
        if set(row.keys()) & _IFACE_TABLE_KEY_HINTS:
            return True
    return False


def _apply_vmware_list_fallback_from_tool_trace(result: Dict, tool_trace: Optional[List]) -> None:
    """LLM이 details를 비운 목록(list) 응답에 대해 마지막 get_vmware 관측 행으로 표 데이터 복구."""
    if not tool_trace or result.get("query_type") != "list":
        return
    items = result.get("items") or []
    if items:
        return
    for tr in reversed(tool_trace):
        fb = tr.get("vmware_list_fallback")
        if isinstance(fb, list) and fb:
            result["items"] = fb
            result["details"] = fb
            cnt = tr.get("count")
            result["total_count"] = int(cnt) if cnt is not None else len(fb)
            logger.info(f"[Agent/parse] VMware tool_trace 폴백으로 list items {len(fb)}건 복구")
            return


def parse_agent_answer(answer: str, tool_trace: Optional[List] = None) -> Dict:
    """
    Agent 답변 JSON을 기존 프론트엔드 호환 구조로 변환.
    tool_trace: get_vmware 등 관측 폴백용 (선택).
    """
    if not answer:
        return {"query_type": "general", "title": "답변 없음",
                "answer": "답변을 생성하지 못했습니다.", "note": None}

    text = answer.strip()

    # ```json ... ``` 제거
    if text.startswith('```'):
        lines = text.split('\n')
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith('```'):
                end = i
                break
        text = '\n'.join(lines[start:end]).strip()

    # JSON 블록 추출
    if not text.startswith('{'):
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace > first_brace:
            extracted = text[first_brace:last_brace + 1]
            logger.info(f"[Agent] JSON 블록 추출: 앞 {first_brace}자, 뒤 {len(text) - last_brace - 1}자 제거")
            text = extracted

    # JSON 파싱
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(text))
            logger.info(f"[Agent] JSON repair 성공")
        except json.JSONDecodeError:
            logger.warning(f"[Agent] JSON 파싱 실패 (길이={len(text)}), 원문: {text[:300]}")

    if parsed is None:
        # v12: 절단된 JSON에서 summary/analysis 부분 추출 후 analysis 타입으로 재구성
        recovered = _recover_partial_json(text)
        if recovered:
            logger.info(f"[Agent] 부분 JSON 복구 성공: summary={bool(recovered.get('summary'))}")
            return recovered
        # v21.3: JSON 복구 실패 — 자연어/마크다운 텍스트면 그대로 general 타입으로 반환
        # (오류 안내 메시지 대신 실제 답변 내용 노출)
        if text and len(text) > 30:
            logger.info(f"[Agent] JSON 복구 실패 — 자연어 답변 그대로 반환 (길이={len(text)})")
            return {
                "query_type": "general",
                "title": "분석 결과",
                "answer": text,
                "analysis": "",
                "note": None,
                "items": [],
                "total_count": 0,
            }
        logger.warning(f"[Agent] JSON 복구 실패 — 안내 메시지 반환 (길이={len(text)})")
        return {
            "query_type": "general",
            "title": "분석 결과",
            "answer": "분석 데이터를 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            "analysis": "",
            "note": None,
            "items": [],
            "total_count": 0,
        }

    has_data_block = isinstance(parsed.get("data"), dict)
    has_answer_field = "answer" in parsed and isinstance(parsed.get("answer"), str)

    if has_answer_field and not has_data_block:
        raw_type = parsed.get("query_type", "general")
        parsed["query_type"] = QUERY_TYPE_MAP.get(raw_type, raw_type)
        if "title" not in parsed:
            parsed["title"] = parsed.get("answer", "")[:80]
        if "note" not in parsed:
            parsed["note"] = None
        # v12: items 키 보장
        if "items" not in parsed:
            parsed["items"] = []
        if "total_count" not in parsed:
            parsed["total_count"] = len(parsed["items"])
        return parsed

    raw_type = parsed.get("query_type", "일반 질문")
    query_type = QUERY_TYPE_MAP.get(raw_type, raw_type)

    data = parsed.get("data", {})
    if not isinstance(data, dict):
        data = {}

    summary = data.get("summary", "") or parsed.get("summary", "")
    details = data.get("details", []) or parsed.get("details", [])
    # LLM이 스키마를 어겨 data.items / 최상위 items 만 채운 경우 → details 폴백 (목록 UI 깨짐 방지)
    if not details or (isinstance(details, list) and len(details) == 0):
        alt_items = data.get("items")
        if not isinstance(alt_items, list) or not alt_items:
            alt_items = parsed.get("items", [])
        if isinstance(alt_items, list) and alt_items and all(isinstance(x, dict) for x in alt_items):
            details = alt_items
            logger.info(f"[Agent/parse] items→details 폴백: {len(details)}건")
    analysis = data.get("analysis", "") or parsed.get("analysis", "")
    recommendation = data.get("recommendation", "") or parsed.get("recommendation", "")
    suggested_queries = data.get("suggested_queries", []) or parsed.get("suggested_queries", [])

    # ── 디버그 로그: details 구조 확인 ──
    logger.info(f"[Agent/parse] query_type={query_type}, details 수={len(details) if isinstance(details, list) else 'N/A'}")
    if details and isinstance(details, list) and len(details) > 0:
        logger.info(f"[Agent/parse] details[0] 키={list(details[0].keys()) if isinstance(details[0], dict) else type(details[0])}")

    # ── [Fix] 로그 목록: '로그' 키 → '이벤트' 키 자동 리네이밍 ──
    # LLM이 L1 규칙을 위반하여 '로그' 키를 사용했을 때 프론트 renderTimeline 호환을 위해 변환
    if query_type == "list" and isinstance(details, list) and details:
        first = details[0] if isinstance(details[0], dict) else {}
        if '로그' in first and '이벤트' not in first:
            logger.info("[Agent/parse] '로그' 키 → '이벤트' 키 자동 리네이밍 (L1 규칙 보정)")
            renamed = []
            for item in details:
                if isinstance(item, dict) and '로그' in item and '이벤트' not in item:
                    new_item = {}
                    for k, v in item.items():
                        new_item['이벤트' if k == '로그' else k] = v
                    renamed.append(new_item)
                else:
                    renamed.append(item)
            details = renamed

    answer_parts = []
    if summary:
        answer_parts.append(summary)
    if analysis:
        answer_parts.append(analysis)
    if recommendation:
        answer_parts.append(f"\n권장 조치: {recommendation}")
    answer_text = "\n\n".join(answer_parts) if answer_parts else summary or str(data)[:500]

    result = {
        "query_type": query_type,
        "title": summary or raw_type,
        "summary": summary,           # v14: renderAnalysis d.summary 용
        "answer": answer_text,
        "analysis": analysis,
        "recommendation": recommendation,  # v14: renderAnalysis d.recommendation 용
        "suggested_queries": suggested_queries if isinstance(suggested_queries, list) else [],
        "note": None,
    }

    if details and isinstance(details, list):
        # ── 할루시네이션 가드: 네트워크 포트 조회에서 eth0 등 가상 포트명 감지 ──
        if query_type in ("list", "analysis") and _has_hallucinated_ports(details):
            logger.warning(f"[Agent] 할루시네이션 감지: details에 가상 포트명 포함 — details 무효화")
            details = []
            result["hallucination_detected"] = True
            result["answer"] = (
                f"{answer_text}\n\n⚠️ 주의: 일부 모델이 실제 존재하지 않는 포트 정보를 생성했습니다. "
                "도구 조회 결과가 없어 상세 정보를 표시할 수 없습니다."
            )

    # details가 있든 없든 result에 설정 (빈 배열도 포함)
    result["details"] = details

    if details and isinstance(details, list):
        # ── 목록형 details 감지: 각 항목이 이미 객체(행)인 경우 ──
        # 예: [{"순위":"1위","인터페이스":"...","수신":"2.77 Gbps"}, ...]
        # label/value 키가 없고, 여러 필드를 가진 객체면 테이블 행으로 판단
        is_label_value = all(
            isinstance(d, dict) and "label" in d and "value" in d
            for d in details[:3]
        ) if details else False
        is_table_row = (
            not is_label_value
            and bool(details)
            and all(isinstance(d, dict) and len(d) >= 2 for d in details[:3])
            and query_type not in ("alert", "status")  # alert/status는 테이블 행 변환 금지
        )

        # ── label/value 세로 나열을 가로 테이블 items로 자동 변환 ──
        # 단, analysis/비교분석/alert 타입은 변환 금지
        # - analysis/general: 단일 항목 분석값이므로 변환 금지
        # - alert: 변환 시 severity/time/host 키가 사라져 renderAlert()에서 빈 박스 발생
        if is_label_value and len(details) > 3 and query_type not in ("analysis", "general", "alert"):
            converted = _convert_repeated_labels_to_items(details)
            if converted and len(converted) > 1:
                is_table_row = True
                is_label_value = False
                details = converted
                result["details"] = details
                logger.info(f"[Agent] details 세로→가로 변환: {len(converted)}개 항목")
        elif is_label_value and query_type in ("analysis", "general", "alert"):
            # alert/analysis/general 타입의 label/value는 변환하지 않고 그대로 유지
            logger.info(f"[Agent] {query_type} label/value 유지 (변환 생략): {len(details)}개")
            is_table_row = False  # 가로 변환 강제 차단

        if query_type == "status":
            if not is_label_value and _details_look_like_interface_table(details):
                logger.info("[Agent/parse] status + 인터페이스/트래픽 표 행 → list로 전환")
                result["query_type"] = "list"
                result["items"] = list(details)
                result["total_count"] = len(details)
            else:
                metrics = []
                for d in details:
                    if not isinstance(d, dict):
                        continue
                    val_str = str(d.get("value", ""))
                    num_match = re.search(r'(\d+\.?\d*)', val_str)
                    num_val = float(num_match.group(1)) if num_match else 0
                    label_lower = str(d.get("label", "")).lower()

                    status = "normal"
                    if any(k in label_lower for k in ["cpu", "메모리", "memory", "swap", "파일시스템", "디스크", "disk"]):
                        if num_val >= 90:
                            status = "critical"
                        elif num_val >= 70:
                            status = "warning"

                    metrics.append({
                        "label": d.get("label", ""),
                        "value": val_str,
                        "status": status,
                    })
                metrics = [
                    m for m in metrics
                    if (str(m.get("label") or "").strip() or str(m.get("value") or "").strip())
                ]
                if metrics:
                    result["metrics"] = metrics

        elif query_type in ("analysis", "general") and is_label_value:
            # v14: 날짜 그룹화 시도 → 성공 시 analysis 타입 표 렌더링
            grouped = _group_analysis_details_by_date(details)
            if grouped:
                logger.info(f"[Agent] analysis label/value → 날짜 그룹화 표 변환: {len(grouped)}행")
                result["query_type"] = "analysis"
                result["details"] = details          # 원본 유지
                result["items"]   = grouped          # 표 렌더링용
                result["total_count"] = len(grouped)
            elif len(details) >= 3:
                # v21.1: 날짜 그룹 불가 + 항목이 3개 이상이면 label/value를 단일 행 테이블로 변환
                # → "트래픽 변동폭", "CPU 분석" 등 분석 결과를 박스 대신 표로 렌더링
                logger.info(f"[Agent] analysis label/value {len(details)}개 → 단일 행 테이블 변환")
                single_row = {d["label"]: d["value"] for d in details if "label" in d and "value" in d}
                if single_row:
                    result["query_type"] = "analysis"
                    result["items"] = [single_row]
                    result["total_count"] = 1
                else:
                    result["query_type"] = "general"
            else:
                # 날짜 그룹 불가, 항목 2개 이하 → 기존 general 렌더러(metric-card)
                logger.info(f"[Agent] {query_type} label/value (소량) → general 렌더러 사용")
                result["query_type"] = "general"

        elif query_type == "analysis" and is_table_row:
            # v21.1: analysis 타입인데 table row 형식으로 반환된 경우 (트래픽/CPU 이력 분석)
            logger.info(f"[Agent] analysis table_row {len(details)}행 → items로 설정")
            result["query_type"] = "analysis"
            result["items"] = details
            result["total_count"] = len(details)

        elif query_type == "alert":
            # alert 타입은 is_table_row 여부와 무관하게 항상 alerts 렌더러로 전달
            # ── [Fix] T1 형식 한국어 필드명 → renderAlert 호환 영문 필드명으로 정규화 ──
            _KR_FIELD_MAP = {
                "시각": "time", "일시": "time", "발생시각": "time",
                "이벤트": "alert_name", "알림명": "alert_name", "알림": "alert_name", "이벤트명": "alert_name",
                "심각도": "severity", "등급": "severity",
                "상태": "status",
                "호스트": "host", "서버": "host", "장비": "host",
                "관련지표": "detail", "지표": "detail", "내용": "detail",
                "ip": "ip", "IP": "ip",
            }
            normalized_details = []
            for item in details:
                if not isinstance(item, dict):
                    normalized_details.append(item)
                    continue
                norm = {}
                for k, v in item.items():
                    eng_key = _KR_FIELD_MAP.get(k, k)
                    norm[eng_key] = v
                # severity_level 자동 산출 (없는 경우)
                if "severity_level" not in norm:
                    sev = str(norm.get("severity", "")).lower()
                    if sev in ("disaster", "critical"):
                        norm["severity_level"] = 5
                    elif sev in ("high", "high"):
                        norm["severity_level"] = 4
                    elif sev in ("average",):
                        norm["severity_level"] = 3
                    elif sev in ("warning", "경고"):
                        norm["severity_level"] = 2
                    else:
                        norm["severity_level"] = 1
                normalized_details.append(norm)
            result["alerts"] = normalized_details
            result["total_count"] = len(normalized_details)
            # active_count: status 또는 상태 필드 모두 검사
            result["active_count"] = sum(
                1 for d in normalized_details
                if str(d.get("status", d.get("상태", ""))).lower() in ("active", "미해결")
            )
            result["resolved_count"] = len(normalized_details) - result["active_count"]

        elif query_type == "log":
            # v23.3: log 타입 — logs 배열 처리 (다중 폴백)
            # 우선순위: data.logs > data.details > parsed.logs (최상위 레벨)
            raw_logs = data.get("logs", []) or []

            # 폴백1: data.details로 반환된 경우
            if not raw_logs and details:
                raw_logs = []
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    entry = {
                        "time":     item.get("time") or item.get("시각") or item.get("일시") or "",
                        "log_file": item.get("log_file") or item.get("로그파일") or item.get("source") or item.get("발생원") or "",
                        "severity": item.get("severity") or item.get("심각도") or "Information",
                        "source":   item.get("source") or item.get("발생원") or item.get("hostname") or "",
                        "content":  (item.get("content") or item.get("이벤트") or item.get("로그")
                                     or item.get("message") or item.get("description") or item.get("log") or ""),
                    }
                    raw_logs.append(entry)
                logger.info(f"[Agent] log type: details→logs 변환 {len(raw_logs)}건")

            # 폴백2: parsed 최상위에 logs가 있는 경우 (data 래퍼 없이 직접 반환)
            if not raw_logs:
                top_logs = parsed.get("logs", []) or []
                if top_logs and isinstance(top_logs, list):
                    for item in top_logs:
                        if not isinstance(item, dict):
                            continue
                        entry = {
                            "time":     item.get("time") or item.get("시각") or "",
                            "log_file": item.get("log_file") or item.get("source") or "",
                            "severity": item.get("severity") or "Information",
                            "source":   item.get("source") or item.get("hostname") or "",
                            "content":  (item.get("content") or item.get("log") or item.get("message") or ""),
                        }
                        raw_logs.append(entry)
                    logger.info(f"[Agent] log type: 최상위 logs→변환 {len(raw_logs)}건")

            result["logs"] = raw_logs
            result["total_count"] = len(raw_logs)
            result["log_source"] = data.get("log_source", "")
            result["host"] = data.get("host", {})
            logger.info(f"[Agent] log type: {len(raw_logs)}건 로그 설정 완료")

        elif query_type in ("list", "process") and is_label_value and not is_table_row:
            # 단순 label/value 나열이면 label→항목, value→내용 2컬럼 테이블로 변환
            converted_lv = [{"항목": d["label"], "내용": d["value"]} for d in details if "label" in d and "value" in d]
            if converted_lv:
                result["query_type"] = "list"
                result["items"] = converted_lv
                result["total_count"] = len(converted_lv)
                logger.info(f"[Agent] label/value → 항목/내용 2컬럼 테이블 변환: {len(converted_lv)}행")
            else:
                result["query_type"] = "list"
                result["items"] = []
                result["total_count"] = 0

        elif query_type == "list" or is_table_row:
            # 테이블 행 구조 → items로 설정
            result["query_type"] = "list"
            result["items"] = details
            result["total_count"] = len(details)

    else:
        # details가 비어있는 경우
        if query_type == "log":
            # ── v23.4: details 없이도 data.logs 직접 읽기 ──
            # LLM이 data.logs 에 올바르게 반환했는데 details가 비어있는 경우
            raw_logs = data.get("logs", []) or []

            # 폴백: parsed 최상위 logs
            if not raw_logs:
                top_logs = parsed.get("logs", []) or []
                if top_logs and isinstance(top_logs, list):
                    raw_logs = []
                    for item in top_logs:
                        if not isinstance(item, dict):
                            continue
                        raw_logs.append({
                            "time":     item.get("time") or item.get("시각") or "",
                            "log_file": item.get("log_file") or item.get("source") or "",
                            "severity": item.get("severity") or "Information",
                            "source":   item.get("source") or item.get("hostname") or "",
                            "content":  (item.get("content") or item.get("log")
                                         or item.get("message") or ""),
                        })
                    logger.info(f"[Agent] log type(else): 최상위 logs→변환 {len(raw_logs)}건")

            result["logs"] = raw_logs
            result["total_count"] = len(raw_logs)
            result["log_source"] = data.get("log_source", "")
            result["host"] = data.get("host", {})
            logger.info(f"[Agent] log type(else): {len(raw_logs)}건 로그 설정 완료")

        elif query_type == "list":
            # details가 비어있어도 query_type=list이면 items=[] 설정
            # → 프론트에서 "목록 조회" 표시 + 분석 텍스트는 보이도록
            result["items"] = []
            result["total_count"] = 0
            logger.warning(f"[Agent] query_type=list이지만 details 비어있음 — items=[] 설정")

    # ── v12: items 키 보장 — 모든 경로에서 items가 반드시 존재 ──
    if "items" not in result:
        result["items"] = []
    if "total_count" not in result:
        result["total_count"] = len(result.get("logs", result["items"]))

    # alert 오분류 보정: 디스크/파일시스템 질의가 alert로 분류된 경우 목록형으로 복구
    if result.get("query_type") == "alert" and _looks_like_disk_query(result):
        alerts = result.get("alerts", []) or []
        has_alert_name = any(isinstance(a, dict) and a.get("alert_name") for a in alerts)
        if not has_alert_name:
            logger.info("[Agent/parse] disk/filesystem 질의의 alert 오분류 보정 → list")
            result["query_type"] = "list"
            if result.get("details"):
                result["items"] = result.get("details", [])
            elif isinstance(alerts, list):
                result["items"] = alerts
            result["total_count"] = len(result.get("items", []))
            result.pop("alerts", None)

    # 네트워크 단위 표기 품질 보정: GB/s, MB/s, KB/s, B/s → Gbps/Mbps/Kbps/bps
    joined = " ".join(str(result.get(k, "")) for k in ("title", "summary", "answer", "analysis"))
    if _NET_HINT_PATTERN.search(joined):
        for key in ("title", "summary", "answer", "analysis", "recommendation"):
            if isinstance(result.get(key), str):
                result[key] = _normalize_network_units_text(result[key])
        for key in ("details", "items", "logs"):
            if key in result:
                result[key] = _normalize_network_units_obj(result[key])

    # LLM이 목록 details를 비운 경우 마지막 get_vmware 관측으로 표 복구
    _apply_vmware_list_fallback_from_tool_trace(result, tool_trace)

    # ── v16: truncated_notice 전달 — LLM 원본 JSON에서 data_truncated 필드가 있으면 전달 ──
    # _compress_observation에서 data_truncated를 설정했고 LLM이 JSON에 포함할 수 있음
    # 또한 details나 items에서 직접 총 개수와 표시 개수 불일치 감지
    if not result.get("truncated_notice"):
        raw_items = result.get("items", [])
        raw_total = result.get("total_count", len(raw_items))
        if raw_total > len(raw_items) and len(raw_items) > 0:
            result["truncated_notice"] = f"전체 {raw_total}건 중 {len(raw_items)}건 표시"

    logger.info(f"[Agent] parse 완료: type={query_type}, items={len(result['items'])}개, title={summary[:50]}")
    return result


# /app/app/routers/chat.py
"""
채팅 API v8.0 — Agent 전면 통합 (MCP 제거)
- 모든 모델(Groq/Gemini/Ollama)이 Agent 방식으로 동작
- MCP fallback 완전 제거
- 대화 이력 DB 영속화 (chat_sessions / chat_messages)

v8.0 변경:
  - AGENT_ENABLED 플래그 제거 (항상 Agent)
  - TOOL_CALLING_MODELS 분기 제거 (Ollama도 Agent 시뮬레이션)
  - 기존 classify/mcp_query/rag_chain 경로 완전 제거
  - Agent 실패 시 에러 응답 (fallback 없음)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json
import logging
import re
import time
import asyncio
from datetime import datetime

from app.config import (
    AVAILABLE_MODELS,
    LLM_MODEL,
    UI_MODEL_ORDER,
    EMBEDDING_MODEL,
    RAG_EMBED_SYNC_INTERVAL_HOURS,
    RAG_CHUNK_SYNC_INTERVAL_HOURS,
)
from app.services.agent import run_agent, parse_agent_answer
from app.services.tool_executor import get_zabbix_pool
from app.db.connection import get_pool

# ── 백엔드별 Agent 타임아웃 (초) ──
_BACKEND_TIMEOUT = {
    "ollama": 600.0,
    "openai": 120.0,
}

logger = logging.getLogger(__name__)

router = APIRouter()

# 대화 히스토리 최대 전달 건수 (토큰 제한 방지)
MAX_HISTORY_TURNS = 4


class ChatRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    query: str
    top_k: int = 5
    categories: Optional[List[str]] = None
    model_id: Optional[str] = None
    session_id: Optional[str] = None
    clarify_answer: Optional[str] = None   # v17: 재문의 응답 (대상/기간 보완)


class HistorySaveRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    session_id: str
    query: str
    result: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    model_info: Dict[str, Any] = {}


# ============================================================
# DB 기반 세션/메시지 CRUD
# ============================================================
async def db_create_session(session_id: str, title: str):
    """세션 생성 (없으면 INSERT, 있으면 무시)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO chat_sessions (session_id, title)
            VALUES ($1, $2)
            ON CONFLICT (session_id) DO NOTHING
        """, session_id, title[:200])


async def db_update_session(session_id: str, title: str = None):
    """세션 updated_at 갱신"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if title:
            await conn.execute("""
                UPDATE chat_sessions SET updated_at = NOW(), title = $2
                WHERE session_id = $1
            """, session_id, title[:200])
        else:
            await conn.execute("""
                UPDATE chat_sessions SET updated_at = NOW()
                WHERE session_id = $1
            """, session_id)


async def db_save_message(session_id: str, query: str,
                          result_data: dict = None, sources: list = None,
                          model_info: dict = None):
    """메시지 저장"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO chat_messages (session_id, query, result_data, sources, model_info)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)
        """,
            session_id,
            query,
            json.dumps(result_data or {}, ensure_ascii=False),
            json.dumps(sources or [], ensure_ascii=False),
            json.dumps(model_info or {}, ensure_ascii=False),
        )
        # updated_at 갱신
        await conn.execute("""
            UPDATE chat_sessions SET updated_at = NOW() WHERE session_id = $1
        """, session_id)


async def db_get_sessions(limit: int = 50):
    """세션 목록 조회 (최신순)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON s.session_id = m.session_id
            GROUP BY s.session_id, s.title, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def db_get_session_messages(session_id: str):
    """특정 세션의 메시지 목록 조회"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 세션 정보
        session = await conn.fetchrow("""
            SELECT session_id, title, created_at, updated_at
            FROM chat_sessions WHERE session_id = $1
        """, session_id)
        if not session:
            return None

        # 메시지 목록
        rows = await conn.fetch("""
            SELECT id, query, result_data, sources, model_info, created_at
            FROM chat_messages
            WHERE session_id = $1
            ORDER BY created_at ASC
        """, session_id)

        messages = []
        for r in rows:
            messages.append({
                "id": r["id"],
                "query": r["query"],
                "result": json.loads(r["result_data"]) if r["result_data"] else {},
                "sources": json.loads(r["sources"]) if r["sources"] else [],
                "model_info": json.loads(r["model_info"]) if r["model_info"] else {},
                "timestamp": r["created_at"].timestamp() if r["created_at"] else 0,
            })

        return {
            "session_id": dict(session)["session_id"],
            "title": dict(session)["title"],
            "created_at": session["created_at"].timestamp() if session["created_at"] else 0,
            "updated_at": session["updated_at"].timestamp() if session["updated_at"] else 0,
            "messages": messages,
        }


async def db_delete_session(session_id: str):
    """세션 삭제 (CASCADE로 메시지도 삭제)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM chat_sessions WHERE session_id = $1", session_id)


async def db_get_recent_messages(session_id: str, limit: int = MAX_HISTORY_TURNS):
    """최근 N개 메시지 조회 (대화 컨텍스트용)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT query, result_data, model_info
            FROM chat_messages
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, session_id, limit)
        # 시간순 정렬로 반환
        out = []
        for r in rows:
            d = dict(r)
            if d.get("model_info") and isinstance(d["model_info"], str):
                try:
                    d["model_info"] = json.loads(d["model_info"])
                except Exception:
                    d["model_info"] = {}
            out.append(d)
        return list(reversed(out))


# ============================================================
# 대화 히스토리 → LLM 컨텍스트 변환
# ============================================================
async def build_conversation_context(session_id: str, current_query: str) -> str:
    """
    이전 대화를 요약하여 LLM 프롬프트에 추가.
    DB에서 최근 메시지를 가져와 컨텍스트 구성.
    """
    if not session_id:
        return ""

    try:
        messages = await db_get_recent_messages(session_id, MAX_HISTORY_TURNS)
    except Exception as e:
        logger.warning(f"[Context] DB 조회 실패: {e}")
        return ""

    if not messages:
        return ""

    parts = ["[이전 대화 맥락]"]
    for i, msg in enumerate(messages, 1):
        q = msg.get("query", "")
        result_raw = msg.get("result_data")
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except:
                result = {}
        else:
            result = result_raw or {}

        title = result.get("title", "")
        answer_summary = ""

        if result.get("query_type") == "list":
            items = result.get("items", [])
            count = result.get("total_count", len(items))
            answer_summary = f"총 {count}건"
            names = [it.get("host", "") for it in items[:5] if it.get("host")]
            if names:
                answer_summary += f" ({', '.join(names)})"
        elif result.get("answer"):
            answer_summary = result["answer"][:150]
        elif result.get("analysis"):
            answer_summary = result["analysis"][:150]
        elif result.get("summary"):
            answer_summary = result["summary"][:150]

        parts.append(f"Q{i}: {q}")
        parts.append(f"A{i}: {title}. {answer_summary}")

    parts.append(f"\n[현재 질문]: {current_query}")
    parts.append("위 대화 맥락을 참고하여 현재 질문에 답변하세요. '이', '그', '위' 같은 대명사는 이전 대화에서 언급된 대상을 가리킵니다.")

    return "\n".join(parts)


# ============================================================
# 대화 맥락에서 대명사 해소 (질의 보강) - DB 기반
# ============================================================
async def resolve_query(session_id: str, query: str) -> str:
    """
    "이 장비의 포트" → "test_network_devices 장비의 포트"
    이전 대화에서 호스트명/IP를 찾아 대명사를 치환.
    또한 "~했습니다", "~됩니다" 등 완료형 서술문을 능동 요청문으로 재해석.
    """
    query_lower = query.lower().strip()

    # ── 완료형/서술형 → 능동 요청형 변환 ──
    # "분석했습니다" → "분석해줘", "조회했습니다" → "조회해줘" 등
    _passive_patterns = [
        ("분석했습니다", "분석해줘"),
        ("분석 결과입니다", "분석해줘"),
        ("조회했습니다", "조회해줘"),
        ("확인했습니다", "확인해줘"),
        ("검색했습니다", "검색해줘"),
        ("정리했습니다", "정리해줘"),
    ]
    resolved_query = query
    for passive, active in _passive_patterns:
        if passive in resolved_query:
            resolved_query = resolved_query.replace(passive, active)
            logger.info(f"[Resolve] 완료형 재해석: '{passive}' → '{active}'")

    query_lower = resolved_query.lower().strip()

    pronouns = ['이 ', '이것', '그 ', '그것', '위 ', '해당 ', '이 장비', '그 장비', '이 서버', '그 서버']
    has_pronoun = any(p in query_lower for p in pronouns)
    if not has_pronoun:
        return resolved_query

    if not session_id:
        return resolved_query

    try:
        messages = await db_get_recent_messages(session_id, 3)
    except Exception:
        return resolved_query

    if not messages:
        return resolved_query

    # 최근 답변에서 호스트명/IP 추출
    last_host = None
    last_ip = None
    for msg in reversed(messages):
        result_raw = msg.get("result_data")
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except:
                result = {}
        else:
            result = result_raw or {}

        host = result.get("host", {})
        if isinstance(host, dict):
            if host.get("asset"):
                last_host = host["asset"]
            if host.get("ip"):
                last_ip = host["ip"]
        if not last_host:
            items = result.get("items", [])
            if items:
                _names = extract_list_host_names_from_items(items[:1])
                if _names:
                    last_host = _names[0]
                    if isinstance(items[0], dict):
                        last_ip = str(items[0].get("ip", "") or "")
        if not last_host:
            alerts = result.get("alerts", [])
            if alerts and isinstance(alerts[0], dict):
                last_host = alerts[0].get("host", "")
        if last_host:
            break

    if not last_host:
        return resolved_query

    resolved = resolved_query
    replacements = [
        ('이 장비', last_host + ' 장비'),
        ('그 장비', last_host + ' 장비'),
        ('이 서버', last_host + ' 서버'),
        ('그 서버', last_host + ' 서버'),
        ('해당 장비', last_host + ' 장비'),
        ('해당 서버', last_host + ' 서버'),
    ]
    for old, new in replacements:
        if old in resolved:
            resolved = resolved.replace(old, new, 1)
            logger.info(f"[Resolve] '{old}' → '{new}'")
            return resolved

    if has_pronoun and last_host:
        for p in ['이 ', '그 ', '위 ']:
            if p in resolved:
                resolved = resolved.replace(p, last_host + ' ', 1)
                logger.info(f"[Resolve] 대명사 '{p.strip()}' → '{last_host}'")
                return resolved

    return resolved


# ============================================================
# v17: 모호성 감지 + 자산 수 조회
# ============================================================

# ── v21.1: 연계 질의 컨텍스트 주입 (함수 본문은 _TARGET_CLEAR_PATTERNS·헬퍼 정의 이후 로드 시점에만 호출)
async def inject_context_from_history(session_id: str, query: str) -> str:
    """
    이전 대화에서 언급된 호스트/장비명을 추출하여 현재 질의에 암묵적 대상이 없을 때 보강.
    예: 질문1="zabbix 서버의 상태는?" → 질문2="3일동안 cpu 사용률이 높았던 시기와 원인은?"
         → "zabbix 서버의 3일동안 cpu 사용률이 높았던 시기와 원인은?" 으로 보강
    """
    return await _inject_context_from_history_impl(session_id, query)


_TARGET_CLEAR_PATTERNS = re.compile(
    r'(zabbix|test_[\w\-]+|[\w\-]+\s*서버|[\w\-]+\s*server|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|상위\s*\d+\s*대|top\s*\d+|높은\s*\d+|cpu\s*높은|메모리\s*높은|all|전체|모든|목록|리스트'
    r'|서버\s*전체|장비\s*전체|전\s*서버|모든\s*서버|서버\s*목록|호스트'
    r'|네트워크\s*장비|장비\s*현황|장비\s*목록|장비\s*상태|네트워크\s*현황'
    r'|vmware|vcenter|하이퍼바이저|가상머신|esxi|openbase_vcenter|[\w]+_vcenter'  # VMware
    r'|\d+/\d+/\d+)',  # 네트워크 인터페이스 슬롯(1/1/5 등) — 대상 명시로 간주
    re.IGNORECASE
)

# 기간이 명확한 키워드 (재문의 불필요)
_PERIOD_CLEAR_PATTERNS = re.compile(
    r'(오늘|어제|지난\s*\d+\s*일|최근\s*\d+\s*일|이번\s*주|지난\s*주|이번\s*달|지난\s*달'
    r'|\d+\s*일\s*간|\d+\s*시간|현재|지금|방금|실시간'
    r'|\d{4}[-/]\d{1,2}|이번\s*주|last\s*\d+|today|yesterday'
    r'|\d+\s*동안'          # v21.3: "10동안", "3일동안" 등 숫자+동안
    r'|최근\s*\d+'          # v21.3: "최근 10" (단위 없는 경우도 허용)
    r'|\d+\s*일\s*동안'     # v21.3: "10일 동안" 명시 패턴
    r'|지난\s*\d+)',        # v21.3: "지난 10" 단위 없는 경우
    re.IGNORECASE
)

# 기간 정보가 필요한 키워드
_PERIOD_NEEDED_KEYWORDS = [
    '추이', '이력', '변동', '트렌드', '언제', '높았던', '낮았던', '많았던', '적었던',
    '비교', '변화', '패턴', '기간', '동안', '최근', '과거', '히스토리', 'history',
]

# 대상 정보가 필요한 키워드 (단독 사용 시 대상 불명확)
_TARGET_NEEDED_KEYWORDS = [
    '사용률', '사용량', '상태', '현황', '현재', '알려줘', '보여줘', '조회',
    '확인', '어떠', '얼마', '몇', 'cpu', '메모리', '디스크', '네트워크',
    # v18.2: 이력/분석 질의도 대상이 필요
    '높았던', '낮았던', '많았던', '적었던', '원인', '시기', '찾아줘', '분석',
    '추이', '이력', '변동', '트렌드', '패턴', '변화',
]

# 목록 행에서 호스트/자산명 추출 (세션·주입 공통)
_LIST_ITEM_HOST_KEYS = (
    "가상머신명", "asset_name", "ASSET_NAME", "ASSET", "VM명", "vm_name",
    "host", "HOST", "hostname",
)

_CATEGORY_SERVER_PREFIXES = frozenset({
    "윈도우", "윈도", "원도우", "windows", "리눅스", "linux", "unix", "유닉스",
    "데비안", "debian", "ubuntu", "우분투", "centos", "redhat", "레드햇", "rocky",
    "물리", "가상", "web", "웹", "db", "database", "파일", "메일", "mail", "dns",
    "proxy", "프록시", "backup", "백업", "app", "앱",
    # AIX·Unix 계열: "AIX 서버" 접두 aix 가 호스트명으로 오인되면 연계 질의가 깨짐 (ISS: OS 범주)
    "aix", "solaris", "sunos", "freebsd", "openbsd", "netbsd", "hpux", "hp-ux",
})


def extract_list_host_names_from_items(items: List[Any]) -> List[str]:
    names: List[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        for key in _LIST_ITEM_HOST_KEYS:
            v = it.get(key)
            if v and str(v).strip():
                s = str(v).strip()
                if s not in names:
                    names.append(s)
                break
    return names


def is_global_scope_target_hint(matched: str) -> bool:
    """전체/목록 등 단일 호스트 주입이 부적절한 범위 표현."""
    s = (matched or "").strip().lower()
    if not s:
        return False
    if s in ("all", "전체", "모든", "목록", "리스트", "호스트"):
        return True
    if re.match(r"^상위\s*\d+\s*대$", matched.strip(), re.I):
        return True
    if re.match(r"^top\s*\d+$", s):
        return True
    if re.match(r"^(높은|cpu\s*높은|메모리\s*높은)\s*\d+", s):
        return True
    if "전체" in s and ("서버" in s or "장비" in s):
        return True
    if "모든" in s and "서버" in s:
        return True
    return False


def is_concrete_host_hint(matched: str) -> bool:
    """정규식이 집은 문자열이 실제 호스트/IP 등 구체 대상인지 (윈도우 서버 같은 범주어는 False)."""
    s = (matched or "").strip()
    if not s:
        return False
    sl = s.lower()
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", s):
        return True
    if sl.startswith("zabbix"):
        return True
    if re.match(r"^test_[\w\-]+", s, re.I):
        return True
    if re.match(r"^\d+/\d+/\d+$", s):
        return True
    if sl in ("vmware", "vcenter", "esxi") or "_vcenter" in sl:
        return True
    m_ko = re.match(r"^(.+?)\s*서버$", s, re.I)
    m_en = re.match(r"^(.+?)\s*server$", sl)
    prefix = None
    if m_ko:
        prefix = m_ko.group(1).strip().lower()
    elif m_en:
        prefix = m_en.group(1).strip().lower()
    if prefix:
        if "_" in prefix or any(ch.isdigit() for ch in prefix):
            return True
        if prefix in _CATEGORY_SERVER_PREFIXES:
            return False
        if re.fullmatch(r"[\u3131-\u318e\uac00-\ud7a3]+", prefix):
            return False
        if re.match(r"^[a-z0-9][a-z0-9_.-]*$", prefix) and len(prefix) >= 2:
            return True
        return False
    # 단일 토큰: AIX·LINUX 등 OS명은 호스트명이 아님
    if sl in _CATEGORY_SERVER_PREFIXES:
        return False
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$", s) and " " not in s:
        return True
    return False


def query_blocks_host_injection(query: str) -> bool:
    """단일 호스트 자동 주입을 막아야 하는지 (_TARGET_CLEAR_PATTERNS 매칭 기준)."""
    m = _TARGET_CLEAR_PATTERNS.search(query)
    if not m:
        return False
    g = m.group(0).strip()
    if is_global_scope_target_hint(g):
        return True
    return is_concrete_host_hint(g)


def query_mentions_concrete_host_token(query: str) -> bool:
    """질의에 패턴 밖의 구체 호스트(underscore 자산명, IP 등)가 이미 있는지."""
    if re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", query):
        return True
    if re.search(r"(?<![\w/])([A-Za-z0-9][A-Za-z0-9_-]*_[A-Za-z0-9][A-Za-z0-9_.-]*)\b", query):
        return True
    if re.search(r"\btest_[\w\-]+", query, re.I):
        return True
    return False


async def _inject_context_from_history_impl(session_id: str, query: str) -> str:
    q_lower = query.lower().strip()

    if _DOMAIN_VMWARE_KEYWORDS.search(query):
        return query

    if re.search(r"\d+/\d+/\d+", query):
        return query

    if query_blocks_host_injection(query) or query_mentions_concrete_host_token(query):
        return query

    if not any(kw in q_lower for kw in _TARGET_NEEDED_KEYWORDS):
        return query

    try:
        messages = await db_get_recent_messages(session_id, 5)
    except Exception:
        return query

    if not messages:
        return query

    last_single: Optional[str] = None
    last_multi: Optional[List[str]] = None

    for msg in reversed(messages):
        result_raw = msg.get("result_data")
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except Exception:
                result = {}
        else:
            result = result_raw or {}

        items = result.get("items") or []
        if result.get("query_type") == "list" and items:
            names = extract_list_host_names_from_items(items)
            if len(names) == 1:
                last_single = names[0]
                break
            if len(names) > 1:
                last_multi = names[:20]
                break

        prev_query = msg.get("query", "")
        m = _TARGET_CLEAR_PATTERNS.search(prev_query)
        if m and is_concrete_host_hint(m.group(0).strip()):
            last_single = m.group(0).strip()
            break

        host = result.get("host", {})
        if isinstance(host, dict) and host.get("asset"):
            last_single = host["asset"]
            break

        if items and isinstance(items[0], dict):
            one = extract_list_host_names_from_items(items[:1])
            if one:
                last_single = one[0]
                break

    if last_multi:
        return query

    if last_single:
        injected = f"{last_single}의 {query}"
        logger.info(f"[Context] 연계 질의 보강: '{query}' → '{injected}'")
        return injected

    return query


# ============================================================
# Phase 1-A: 세션 컨텍스트 추출 — 기간/호스트/토픽 누적
# ============================================================
_PERIOD_EXTRACT = re.compile(
    r'(오늘|어제|지난\s*\d+\s*일|최근\s*\d+\s*시간\s*동안|최근\s*\d+\s*시간|최근\s*\d+\s*일|이번\s*주|지난\s*주|이번\s*달|지난\s*달'
    r'|\d+\s*일\s*간|\d+\s*시간\s*동안|\d+\s*시간|\d+\s*일\s*동안|\d{4}-\d{2}-\d{2}'
    r'|today|yesterday|최근\s*\d+)',
    re.IGNORECASE,
)

_TOPIC_KEYWORDS = [
    'containerd', 'docker', 'kubernetes', 'k8s', 'nginx', 'ollama', 'zabbix',
    'cpu', 'memory', 'disk', '메모리', '디스크', '로그', '장애', '경고', '알림',
    'warning', 'error', 'critical', '프로세스', '네트워크', '인터페이스',
    'vmware', 'vcenter', '가상머신', '하이퍼바이저',
]

# ISS-04: 도메인 식별 키워드 — 서버 vs 네트워크 도메인 전환 감지
_DOMAIN_SERVER_KEYWORDS = re.compile(
    r'(서버|cpu|메모리|memory|디스크|disk|프로세스|process|로그|log|zabbix|ollama|containerd|nginx|docker)',
    re.IGNORECASE,
)
_DOMAIN_NETWORK_KEYWORDS = re.compile(
    r'(네트워크\s*장비|스위치|라우터|switch|router|방화벽|firewall|인터페이스\s*트래픽|포트\s*트래픽'
    r'|네트워크\s*현황|장비\s*현황|장비\s*목록|네트워크\s*상태)',
    re.IGNORECASE,
)
# VMware는 네트워크 스위치 CPU와 혼동되기 쉬우므로 별도 도메인으로 우선 판별
_DOMAIN_VMWARE_KEYWORDS = re.compile(
    r'(vmware|vcenter|하이퍼바이저|hypervisor|가상머신|esxi|openbase_vcenter|[\w]+_vcenter|데이터스토어|datastore|balloon|스냅샷|snapshot)',
    re.IGNORECASE,
)

_VM_RANK_NAME_RE = re.compile(r"\d+\s*위\s+([^\s#\n|]+)", re.IGNORECASE)


def extract_vmware_vm_names_from_text(text: str) -> List[str]:
    """직전 답에 '1위 Veeam'처럼만 남은 경우 VM명 후보 추출."""
    if not text:
        return []
    names: List[str] = []
    for m in _VM_RANK_NAME_RE.finditer(text):
        n = (m.group(1) or "").strip()
        if n and n not in names:
            names.append(n)
    return names


def _looks_like_vmware_turn(
    result: Dict[str, Any],
    query: str,
    model_info: Optional[Dict] = None,
) -> bool:
    if _DOMAIN_VMWARE_KEYWORDS.search(query or ""):
        return True
    mi = model_info or {}
    ds = str(mi.get("data_source") or "").lower()
    if "vmware" in ds:
        return True
    blob = " ".join(
        str(result.get(k) or "")
        for k in ("answer", "summary", "title", "analysis")
    )
    if isinstance(result.get("data"), dict):
        blob += " " + str(result["data"].get("summary") or "")
    bl = blob.lower()
    if "vmware" in bl or "가상머신" in blob or "vcenter" in bl:
        return True
    return False


def _detect_query_domain(query: str) -> str:
    """질의 도메인 감지: 'vmware' | 'server' | 'network' | 'unknown'"""
    if _DOMAIN_VMWARE_KEYWORDS.search(query):
        return "vmware"
    has_network = bool(_DOMAIN_NETWORK_KEYWORDS.search(query))
    has_server = bool(_DOMAIN_SERVER_KEYWORDS.search(query))
    if has_network and not has_server:
        return "network"
    if has_server and not has_network:
        return "server"
    return "unknown"


async def extract_session_context(session_id: str, current_query: str = "") -> dict:
    """
    Phase 1-A: 이전 대화 이력에서 세션 컨텍스트(기간, 호스트, 토픽)를 추출.
    - resolved_period : 최근 대화에서 확립된 조회 기간 (예: "오늘", "최근 3일")
    - resolved_host   : 최근 대화의 주요 서버/장비명
    - last_topic      : 최근 대화의 주요 주제 키워드
    ISS-04: 도메인 전환(서버↔네트워크) 감지 시 host 컨텍스트 초기화
    """
    ctx = {
        "resolved_period": None,
        "resolved_host": None,
        "list_context_hosts": None,
        "last_topic": None,
        "vmware_context_names": None,
    }
    try:
        messages = await db_get_recent_messages(session_id, 6)
    except Exception:
        return ctx

    if not messages:
        return ctx

    # ISS-04: 현재 질의 도메인 감지
    current_domain = _detect_query_domain(current_query) if current_query else "unknown"

    for msg in reversed(messages):
        q = msg.get("query", "") or ""
        result_raw = msg.get("result_data")
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except Exception:
                result = {}
        else:
            result = result_raw or {}

        # 기간 추출: 질의 텍스트에서 우선
        if not ctx["resolved_period"]:
            m = _PERIOD_EXTRACT.search(q)
            if m:
                ctx["resolved_period"] = m.group(0).strip()
            # 결과 summary에서도 추출 시도
            if not ctx["resolved_period"]:
                summary = ""
                if isinstance(result.get("data"), dict):
                    summary = result["data"].get("summary", "") or ""
                elif isinstance(result.get("summary"), str):
                    summary = result["summary"]
                m2 = _PERIOD_EXTRACT.search(summary)
                if m2:
                    ctx["resolved_period"] = m2.group(0).strip()

        # 호스트 추출: list 결과의 자산명을 질의 정규식보다 우선 (ISS-04 유지)
        items = result.get("items") or []
        if (
            not ctx["resolved_host"]
            and not ctx.get("list_context_hosts")
            and result.get("query_type") == "list"
            and items
        ):
            names = extract_list_host_names_from_items(items)
            if names:
                prev_domain = _detect_query_domain(q)
                if current_domain == "vmware" and prev_domain == "network":
                    pass
                elif current_domain == "unknown" or prev_domain == "unknown" or current_domain == prev_domain:
                    if len(names) == 1:
                        ctx["resolved_host"] = names[0]
                    else:
                        ctx["list_context_hosts"] = names[:20]

        if not ctx["resolved_host"] and not ctx.get("list_context_hosts"):
            m = _TARGET_CLEAR_PATTERNS.search(q)
            if m:
                prev_host = m.group(0).strip()
                if is_concrete_host_hint(prev_host):
                    prev_domain = _detect_query_domain(q)
                    if current_domain == "vmware" and prev_domain == "network":
                        logger.info(
                            f"[SessionCtx] VMware 질의인데 이전 턴이 네트워크 도메인 → host 상속 생략 ({prev_host})"
                        )
                    elif current_domain == "unknown" or prev_domain == "unknown" or current_domain == prev_domain:
                        ctx["resolved_host"] = prev_host
                    else:
                        logger.info(
                            f"[SessionCtx] ISS-04: 도메인 전환 감지 ({prev_domain}→{current_domain}), "
                            f"host 상속 차단: {prev_host}"
                        )
            if not ctx["resolved_host"] and items and isinstance(items[0], dict):
                one = extract_list_host_names_from_items(items[:1])
                if one:
                    candidate_host = one[0]
                    prev_domain = _detect_query_domain(q)
                    if current_domain == "vmware" and prev_domain == "network":
                        pass
                    elif current_domain == "unknown" or prev_domain == "unknown" or current_domain == prev_domain:
                        ctx["resolved_host"] = candidate_host

        # 토픽 추출: 질의에 포함된 키워드
        if not ctx["last_topic"]:
            q_lower = q.lower()
            for kw in _TOPIC_KEYWORDS:
                if kw in q_lower:
                    ctx["last_topic"] = kw
                    break

        _host_ctx = ctx["resolved_host"] or ctx.get("list_context_hosts")
        if ctx["resolved_period"] and _host_ctx and ctx["last_topic"]:
            break

    # 직전 VMware 조회에서 VM 표시명 추출 (후속 질의 시 get_server_* 오조회 방지)
    for msg in reversed(messages):
        q = msg.get("query", "") or ""
        result_raw = msg.get("result_data")
        if isinstance(result_raw, str):
            try:
                result = json.loads(result_raw)
            except Exception:
                result = {}
        else:
            result = result_raw or {}
        model_info = msg.get("model_info")
        if isinstance(model_info, str):
            try:
                model_info = json.loads(model_info)
            except Exception:
                model_info = {}
        if not isinstance(model_info, dict):
            model_info = {}
        if not _looks_like_vmware_turn(result, q, model_info):
            continue
        names: List[str] = []
        for it in result.get("items") or []:
            if not isinstance(it, dict):
                continue
            for key in ("가상머신명", "asset_name", "VM명", "vm_name"):
                v = it.get(key)
                if v and str(v).strip():
                    s = str(v).strip()
                    if s not in names:
                        names.append(s)
                    break
        if not names:
            blob = "\n".join(
                str(result.get(k) or "")
                for k in ("answer", "summary", "title", "analysis")
            )
            if isinstance(result.get("data"), dict):
                blob += "\n" + str(result["data"].get("summary") or "")
            names = extract_vmware_vm_names_from_text(blob)
        if names:
            ctx["vmware_context_names"] = names[:12]
        break

    return ctx


def data_freshness_context_block() -> str:
    """실시간 Zabbix 조회 vs RAG 벡터(동기화 지연) 구분을 에이전트에 전달."""
    return (
        "## 데이터 출처·신선도\n"
        "- 실시간 도구(get_* 계열): Zabbix DB 직접 조회.\n"
        f"- 이력·패턴 검색(search_historical_data): RAG 벡터 DB이며, 임베딩 동기화 주기 약 "
        f"{RAG_EMBED_SYNC_INTERVAL_HOURS}시간, 청킹 갱신 약 {RAG_CHUNK_SYNC_INTERVAL_HOURS}시간 단위입니다. "
        "답변에서 실시간 수치와 검색 기반 요약을 구분해 안내하세요.\n"
    )


def build_session_context_block(ctx: dict, period_from_query: bool = False) -> str:
    """세션 컨텍스트 딕셔너리를 시스템 프롬프트 주입용 텍스트 블록으로 변환.

    period_from_query=True이면 resolved_period는 이전 대화 상속이 아닌
    현재 질문에서 직접 추출된 기간임을 구분하여 다른 헤더로 표시.
    → H2-1 규칙이 적용되므로 [이전 대화 기간 상속] prefix 생성을 막는다.
    """
    if not any(
        v
        for v in ctx.values()
        if v is not None and not (isinstance(v, (list, tuple)) and len(v) == 0)
    ):
        return ""

    # 직전 턴이 다중 목록만 있는 경우 (VMware 전용 블록과 대칭)
    list_only = (
        ctx.get("list_context_hosts")
        and not ctx.get("resolved_period")
        and not ctx.get("resolved_host")
        and not ctx.get("last_topic")
        and not ctx.get("vmware_context_names")
    )
    if list_only:
        return (
            "## 현재 세션 컨텍스트 (이전 대화 목록 조회 — 후속 질의용)\n"
            f"직전에 조회된 서버/자산 후보: {', '.join(ctx['list_context_hosts'])}\n"
            "※ 후보가 여러 대일 때 사용자가 특정 호스트를 지정하지 않았다면 한 대로 단정하지 말고, "
            "어떤 호스트인지 확인하거나 host_query=all 등으로 범위를 명시한 뒤 도구를 호출하세요.\n"
        )

    # VMware VM명만 이전 턴에서 온 경우 (기간/호스트/토픽 없음)
    vm_only = (
        ctx.get("vmware_context_names")
        and not ctx.get("resolved_period")
        and not ctx.get("resolved_host")
        and not ctx.get("last_topic")
        and not ctx.get("list_context_hosts")
    )
    if vm_only:
        return (
            "## 현재 세션 컨텍스트 (이전 대화 VMware 조회 — 후속 질의용)\n"
            f"직전 VMware VM 후보(표시명): {', '.join(ctx['vmware_context_names'])}\n"
            "※ VMware 게스트 VM에는 get_vmware(host_query=VM명 또는 부분일치, mode=realtime)를 사용하세요. "
            "get_server_cpu / get_server_status / get_processes 등 물리 서버 호스트 도구는 "
            "Zabbix 호스트명이 게스트 VM 이름과 다를 수 있어 '조회 결과 없음'이 될 수 있습니다.\n"
            "※ 벡터DB 검색(search_historical_data, vmware) 또는 이상 이력(get_vmware anomaly)이 비어 있어도, "
            "위 VM이 직전 턴 **실시간 조회**에서 CPU가 높게 나온 적이 있었다면 "
            "「이력 없음 = 해당 VM이 높지 않았다」로 단정하지 마세요. "
            "RAG/이상 DB 부재와 실시간 스냅샷은 다릅니다. 필요 시 get_vmware(realtime)로 재확인하세요."
        )

    # 현재 질의 기간만 있고 나머지(host/topic)가 없는 경우
    # → '이전 대화 자동 상속' 헤더 불필요, 간단한 기간 힌트만 전달
    period_only = (ctx.get("resolved_period") and
                   not ctx.get("resolved_host") and
                   not ctx.get("last_topic") and
                   not ctx.get("vmware_context_names") and
                   not ctx.get("list_context_hosts"))

    if period_only and period_from_query:
        # 현재 질문에서 기간이 명시된 경우 — LLM에게 기간 힌트만 전달
        # '이전 대화 기간 상속' 문구를 사용하지 않아 H2-2 규칙이 트리거되지 않음
        return (
            f"## 현재 질문 기간\n"
            f"기간: {ctx['resolved_period']}\n"
            f"※ 이 기간은 현재 질문에서 직접 명시된 값입니다. "
            f"summary에 [이전 대화 기간 상속] 문구를 절대 붙이지 마세요."
        )

    if period_only and not period_from_query:
        # 이전 대화에서 기간이 상속된 경우 (host/topic 없음)
        lines = ["## 현재 세션 컨텍스트 (이전 대화에서 확립된 정보 — 자동 상속)"]
        lines.append(f"기간: {ctx['resolved_period']}")
        lines.append("※ 위 기간은 이전 대화에서 확립된 값입니다. 현재 질문에 기간이 명시된 경우 현재 질문의 기간을 우선하세요.")
        return "\n".join(lines)

    # host/topic 등 풍부한 컨텍스트가 있는 일반 케이스
    if period_from_query:
        header = "## 현재 세션 컨텍스트"
    else:
        header = "## 현재 세션 컨텍스트 (이전 대화에서 확립된 정보 — 자동 상속)"
    lines = [header]
    if ctx.get("resolved_period"):
        if period_from_query:
            lines.append(f"기간: {ctx['resolved_period']} (현재 질문에서 명시됨)")
        else:
            lines.append(f"기간: {ctx['resolved_period']}")
    if ctx.get("resolved_host"):
        lines.append(f"대상 서버/장비: {ctx['resolved_host']}")
    if ctx.get("list_context_hosts"):
        lines.append(f"직전 목록 조회 후보(다중): {', '.join(ctx['list_context_hosts'])}")
        lines.append(
            "※ 여러 대 중 특정 호스트가 질문에 없으면 한 대로 단정하지 말고 확인하거나 "
            "host_query=all 등으로 범위를 명시하세요."
        )
    if ctx.get("last_topic"):
        lines.append(f"주요 주제: {ctx['last_topic']}")
    if ctx.get("vmware_context_names"):
        lines.append(
            f"직전 VMware VM 후보(표시명): {', '.join(ctx['vmware_context_names'])}"
        )
        lines.append(
            "※ VMware 게스트 VM 후속 질의는 get_vmware(host_query=VM명 또는 부분일치, mode=realtime) 우선. "
            "get_server_cpu / get_server_status / get_processes 등은 게스트 이름과 Zabbix 호스트명이 "
            "다를 수 있어 조회 결과가 비어 나올 수 있습니다."
        )
        lines.append(
            "※ 벡터·이상 DB만 조회해 '해당 기간 CPU 높은 VM 없음'이라고 할 때는, 직전 실시간 조회에서 "
            "위 후보 VM이 높았을 수 있음을 부정하지 말고 출처(실시간 vs RAG)를 구분해 설명하세요."
        )
    if period_from_query:
        lines.append("※ 기간은 현재 질문에서 직접 명시된 값입니다. summary에 [이전 대화 기간 상속] 문구를 절대 붙이지 마세요.")
    else:
        lines.append("※ 위 컨텍스트는 사용자가 명시적으로 변경하지 않는 한 현재 질의에 자동 적용하세요.")
    return "\n".join(lines)


def extract_period_from_query(query: str) -> str:
    """
    현재 질문 텍스트에서 기간 키워드를 직접 추출합니다.
    세션 히스토리 없이 현재 질문만으로 기간을 판별할 때 사용.
    반환값: 추출된 기간 문자열 또는 빈 문자열
    """
    m = _PERIOD_EXTRACT.search(query)
    return m.group(0).strip() if m else ""


def detect_ambiguity(query: str) -> Dict:
    """
    v18.2: 질의의 대상/기간 모호성 감지.
    - _is_history_query 예외 제거: 이력/분석 질의도 대상이 불명확하면 재문의
    - len(q) < 30 조건 제거: 긴 질의도 대상 없으면 재문의
    Returns: {
        "needs_target": bool,   # 대상 서버/장비 불명확
        "needs_period": bool,   # 조회 기간 불명확
        "target_question": str,
        "period_question": str,
        "period_buttons": list,
        "target_buttons": list,
    }
    """
    q = query.strip()
    q_lower = q.lower()

    # 이미 명확한 대상이 있으면 재문의 불필요 (범주어 '윈도우 서버' 등은 명확하지 않음)
    has_clear_target = False
    _m = _TARGET_CLEAR_PATTERNS.search(q)
    if _m:
        _g = _m.group(0).strip()
        has_clear_target = is_global_scope_target_hint(_g) or is_concrete_host_hint(_g)
    if not has_clear_target and query_mentions_concrete_host_token(q):
        has_clear_target = True

    # 기간 명확성
    has_clear_period = bool(_PERIOD_CLEAR_PATTERNS.search(q))

    # 대상 필요 여부:
    #   - 명확한 대상(호스트명/IP/전체/상위N 등)이 없고
    #   - 대상 관련 키워드(cpu/메모리/분석/추이 등)가 포함된 경우
    #   ※ v18.2: _is_history_query 예외 제거 — 이력 질의도 대상을 물어봄
    #   ※ v18.2: len(q) 길이 제한 제거 — 긴 질의도 대상 없으면 재문의
    needs_target = (
        not has_clear_target
        and any(kw in q_lower for kw in _TARGET_NEEDED_KEYWORDS)
    )

    # 기간 필요 여부: 이력/추이 키워드가 있고 명확한 기간이 없을 때
    needs_period = (
        not has_clear_period
        and any(kw in q_lower for kw in _PERIOD_NEEDED_KEYWORDS)
    )

    # '현재/지금/실시간'이 명시된 경우 기간 재문의 불필요
    if '현재' in q_lower or '지금' in q_lower or '실시간' in q_lower:
        needs_period = False

    return {
        "needs_target": needs_target,
        "needs_period": needs_period,
        "target_question": "어떤 서버 또는 장비를 조회할까요?",
        "period_question": "조회 기간을 선택해 주세요.",
        "target_buttons": ["전체 서버", "전체 네트워크 장비", "CPU 높은 상위 10대", "Zabbix 서버"],
        "period_buttons": ["오늘", "어제", "최근 3일", "최근 7일", "최근 30일"],
    }


async def get_asset_counts() -> Dict:
    """v22.6: Zabbix DB Pool에서 서버/네트워크 장비 수 조회 (재문의 메시지용)"""
    try:
        pool = await get_zabbix_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                  COUNT(*) FILTER (WHERE status = 0) AS total,
                  COUNT(*) FILTER (WHERE status = 0 AND
                    LOWER(name) NOT LIKE '%switch%' AND LOWER(name) NOT LIKE '%router%'
                    AND LOWER(name) NOT LIKE '%firewall%' AND LOWER(name) NOT LIKE '%network%'
                  ) AS server_count,
                  COUNT(*) FILTER (WHERE status = 0 AND (
                    LOWER(name) LIKE '%switch%' OR LOWER(name) LIKE '%router%'
                    OR LOWER(name) LIKE '%firewall%' OR LOWER(name) LIKE '%network%'
                  )) AS network_count
                FROM hosts
                WHERE status IN (0, 1) AND flags = 0
            """)
            if rows:
                r = dict(rows[0])
                return {
                    "total": int(r.get("total", 0)),
                    "server_count": int(r.get("server_count", 0)),
                    "network_count": int(r.get("network_count", 0)),
                }
    except Exception as e:
        logger.warning(f"[Clarify] 자산 수 조회 실패: {e}")
    return {"total": 0, "server_count": 0, "network_count": 0}


# ============================================================
# top_k 자동 조정
# ============================================================
def adjust_top_k(query: str, base_top_k: int) -> int:
    query_lower = query.lower()
    has_specific_host = bool(re.search(
        r'(\d+\.\d+\.\d+\.\d+|test_|zabbix\s*server|[a-zA-Z][\w.-]+_[\w.-]+)',
        query, re.IGNORECASE
    ))
    detail_keywords = ['분석', '상세', '전체', '모든', '포트', '인터페이스',
                       '현황', '트렌드', '추이', '특이사항', '로그']
    has_detail = any(kw in query_lower for kw in detail_keywords)
    if has_specific_host and has_detail:
        return max(base_top_k, 10)
    elif has_specific_host:
        return max(base_top_k, 8)
    return base_top_k


# ============================================================
# GET /api/models
# ============================================================
@router.get("/api/models")
async def get_available_models():
    """AVAILABLE_MODELS 전체가 아니라 UI_MODEL_ORDER만 반환 (구버전 배포·캐시 혼선 방지)."""
    models = []
    for model_id in UI_MODEL_ORDER:
        config = AVAILABLE_MODELS.get(model_id)
        if not config:
            continue
        models.append({
            "id": model_id,
            "backend": config["backend"],
            "display_name": config["display_name"],
            "description": config["description"],
        })
    default_id = LLM_MODEL if LLM_MODEL in UI_MODEL_ORDER else UI_MODEL_ORDER[0]
    return {"models": models, "default": default_id}


# ============================================================
# POST /api/chat - SSE 스트리밍
# ============================================================
@router.post("/api/chat")
async def chat(request: ChatRequest):
    model_id = request.model_id
    if model_id and model_id not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 모델: {model_id}")

    async def event_stream():
        _saved = False   # 중복 DB 저장 방지 플래그
        try:
            used_model = model_id or LLM_MODEL
            config = AVAILABLE_MODELS.get(used_model, {})

            # ── 1) 대명사 해소 (대화 이어가기) — DB 기반 ──
            resolved_query = await resolve_query(request.session_id, request.query)
            if resolved_query != request.query:
                logger.info(f"[Chat] 질의 보강: '{request.query}' → '{resolved_query}'")

            # ── v21.1: 연계 질의 컨텍스트 주입 — 이전 대화의 대상 호스트를 resolved_query에 포함 ──
            # "3일동안 cpu 사용률이 높았던 시기"처럼 이전 대화의 서버를 암묵적으로 가리키는 경우 처리
            if request.session_id and not request.clarify_answer:
                resolved_query = await inject_context_from_history(
                    request.session_id, resolved_query
                )

            # ── v17: 재문의 응답이 있으면 원본 질의에 결합 ──
            if request.clarify_answer:
                resolved_query = f"{resolved_query} — {request.clarify_answer}"
                logger.info(f"[Chat] 재문의 결합: '{resolved_query}'")

            logger.info(f"[Chat SSE] query={resolved_query[:50]}, model={used_model}")

            # ── Agent 실행 (v8.0: 모든 모델 통합) ──
            try:
                # chat_history에서 최근 대화 추출
                recent_history = []
                if request.session_id:
                    try:
                        recent_msgs = await db_get_recent_messages(request.session_id, 6)
                        # v22.6: 모델 컨텍스트 크기에 따라 히스토리 절단 길이 동적 결정
                        # obs_max_chars가 클수록 대용량 컨텍스트 모델 → 히스토리도 더 많이 보존
                        _obs_chars = config.get("obs_max_chars", 5000)
                        if _obs_chars >= 12000:
                            _history_max_chars = 2000   # gpt-5.4 등 대용량 컨텍스트
                        elif _obs_chars >= 8000:
                            _history_max_chars = 1500
                        elif _obs_chars >= 5000:
                            _history_max_chars = 1000
                        else:
                            _history_max_chars = 500
                        for msg in recent_msgs:
                            q = msg.get("query", "")
                            result_raw = msg.get("result_data")
                            if isinstance(result_raw, str):
                                try:
                                    result = json.loads(result_raw)
                                except:
                                    result = {}
                            else:
                                result = result_raw or {}

                            if q:
                                recent_history.append({
                                    "role": "user",
                                    "content": q,
                                })
                            # v22.6: query_type별 압축 전략
                            # list/분석 타입은 summary만, status/general은 전문 보존
                            _qtype = result.get("query_type", "")
                            if _qtype == "log":
                                # v23.5: 로그 타입 — 이전 로그 원본을 히스토리에 포함
                                # (후속 '분석해줘' 질의 시 재조회 방지)
                                _prev_logs = result.get("logs", [])
                                _summary = result.get("summary", "") or result.get("answer", "") or ""
                                if _prev_logs:
                                    # 로그 원본을 JSON으로 히스토리에 삽입 (최대 20건, 각 content 500자 제한)
                                    _log_lines = []
                                    for _lg in _prev_logs[:20]:
                                        _c = str(_lg.get("content", ""))[:500]
                                        _log_lines.append(
                                            f"[{_lg.get('time','')}] {_lg.get('log_file','')} "
                                            f"[{_lg.get('severity','')}] {_lg.get('source','')}: {_c}"
                                        )
                                    _log_block = "\n".join(_log_lines)
                                    answer_text = (
                                        f"[이전 로그 조회 결과 — {len(_prev_logs)}건]\n"
                                        + _log_block
                                        + (f"\n요약: {_summary}" if _summary else "")
                                    )
                                else:
                                    answer_text = _summary
                            elif _qtype in ("목록 조회", "비교/분석", "list", "analysis"):
                                # 목록/분석은 summary(핵심 1줄) + 전체 건수만 전달
                                _summary = result.get("data", {}).get("summary", "") if isinstance(result.get("data"), dict) else ""
                                answer_text = _summary or result.get("summary", "") or result.get("answer", "") or ""
                            else:
                                answer_text = (
                                    result.get("answer", "")
                                    or result.get("analysis", "")
                                    or result.get("summary", "")
                                    or ""
                                )
                            if answer_text:
                                recent_history.append({
                                    "role": "assistant",
                                    "content": answer_text[:_history_max_chars],
                                })
                    except Exception as e:
                        logger.warning(f"[Agent] 히스토리 로드 실패: {e}")

                # ── Phase 1-A: 세션 컨텍스트 추출 (기간/호스트/토픽 누적) ──
                session_ctx_block = ""
                if request.session_id:
                    try:
                        # ISS-04: current_query 전달 → 도메인 전환 감지 후 host 초기화
                        _sctx = await extract_session_context(request.session_id, current_query=resolved_query)

                        # ISS-06: 현재 질문에 기간이 명시된 경우 세션 컨텍스트보다 우선 적용
                        # v23.8 Fix-A: period_from_query 플래그로 "이전 대화 상속" vs "현재 질문 명시" 구분
                        _current_period = extract_period_from_query(resolved_query)
                        _period_from_query = False
                        if _current_period:
                            _sctx["resolved_period"] = _current_period
                            _period_from_query = True
                            logger.info(f"[Agent] 현재 질문 기간 감지 → 우선 적용: {_current_period}")

                        session_ctx_block = build_session_context_block(
                            _sctx, period_from_query=_period_from_query
                        )
                        if session_ctx_block:
                            logger.info(f"[Agent] 세션 컨텍스트 주입: period={_sctx.get('resolved_period')}, "
                                        f"host={_sctx.get('resolved_host')}, topic={_sctx.get('last_topic')}, "
                                        f"period_from_query={_period_from_query}")
                    except Exception as e:
                        logger.warning(f"[Agent] 세션 컨텍스트 추출 실패: {e}")
                else:
                    # session_id 없어도 현재 질문의 기간은 LLM에 전달 (ISS-06)
                    # v23.8 Fix-A: 현재 질문 기간 → period_from_query=True로 전달
                    _current_period = extract_period_from_query(resolved_query)
                    if _current_period:
                        _sctx_minimal = {"resolved_period": _current_period, "resolved_host": None, "last_topic": None}
                        session_ctx_block = build_session_context_block(
                            _sctx_minimal, period_from_query=True
                        )
                        logger.info(f"[Agent] 세션 없음 - 현재 질문 기간만 주입: {_current_period}")

                _fresh = data_freshness_context_block()
                session_ctx_block = (
                    session_ctx_block + "\n\n" + _fresh if session_ctx_block else _fresh
                )

                # meta 이벤트
                meta = {
                    "type": "meta",
                    "query_type": "agent",
                    "time_filter": None,
                    "data_source": "agent",
                    "mcp_queries": [],
                    "llm_model": config.get("display_name", used_model),
                    "embed_model": EMBEDDING_MODEL,
                }
                yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"

                # ★ Agent 실행 (백엔드별 타임아웃 적용)
                _backend = AVAILABLE_MODELS.get(used_model, {}).get("backend", "groq")
                _timeout = _BACKEND_TIMEOUT.get(_backend, 120.0)

                # Phase 2-C: SSE progress 큐 — tool 호출 시마다 실시간 이벤트 전송
                _progress_queue: asyncio.Queue = asyncio.Queue()

                async def _progress_cb(tool_name: str, tool_name_kr: str, step: int):
                    await _progress_queue.put({
                        "type": "progress",
                        "tool": tool_name,
                        "tool_kr": tool_name_kr,
                        "step": step,
                        "message": f"[{step}] {tool_name_kr} 조회 중…",
                    })

                async def _run_agent_with_progress():
                    return await run_agent(
                        user_query=resolved_query,
                        model=used_model,
                        chat_history=recent_history,
                        session_context=session_ctx_block,
                        progress_callback=_progress_cb,
                    )

                # agent를 태스크로 실행하면서 progress 이벤트를 SSE로 전송
                _agent_task = asyncio.ensure_future(_run_agent_with_progress())
                try:
                    _deadline = asyncio.get_event_loop().time() + _timeout
                    while not _agent_task.done():
                        # 큐에 progress 이벤트가 있으면 전송
                        try:
                            _prog = _progress_queue.get_nowait()
                            yield f"data: {json.dumps(_prog, ensure_ascii=False)}\n\n"
                        except asyncio.QueueEmpty:
                            pass
                        # 타임아웃 체크
                        if asyncio.get_event_loop().time() > _deadline:
                            _agent_task.cancel()
                            raise asyncio.TimeoutError()
                        await asyncio.sleep(0.05)

                    # 큐에 남은 이벤트 모두 전송
                    while not _progress_queue.empty():
                        try:
                            _prog = _progress_queue.get_nowait()
                            yield f"data: {json.dumps(_prog, ensure_ascii=False)}\n\n"
                        except asyncio.QueueEmpty:
                            break

                    # 태스크 예외 확인
                    _exc = _agent_task.exception()
                    if _exc:
                        raise _exc
                    agent_result = _agent_task.result()

                except asyncio.TimeoutError:
                    if not _agent_task.done():
                        _agent_task.cancel()
                    logger.warning(f"[Agent] 타임아웃 {_timeout:.0f}초 초과 (backend={_backend})")
                    raise Exception(f"Agent 타임아웃 ({_timeout:.0f}초)")

                # 답변 파싱
                parsed = parse_agent_answer(
                    agent_result["answer"],
                    tool_trace=agent_result.get("tool_trace"),
                )

                # v16: tool_trace에서 data_truncated 알림을 parsed에 주입
                if not parsed.get("truncated_notice"):
                    for _tr in agent_result.get("tool_trace", []):
                        _dt = _tr.get("data_truncated")
                        if _dt:
                            parsed["truncated_notice"] = _dt
                            break

                # 파싱 결과 디버그 로그
                logger.info(
                    f"[Chat/parse] query_type={parsed.get('query_type')}, "
                    f"has_items={'items' in parsed}, "
                    f"items_count={len(parsed.get('items', []))}, "
                    f"has_details={'details' in parsed}, "
                    f"details_count={len(parsed.get('details', []))}, "
                    f"has_logs={'logs' in parsed}, "
                    f"logs_count={len(parsed.get('logs', []))}"
                )
                # raw answer 앞부분 로그 (details 비어있을 때 원인 추적)
                if not parsed.get('items') and parsed.get('query_type') == 'list':
                    logger.warning(
                        f"[Chat/parse] list 타입인데 items 없음! "
                        f"raw_answer[:300]={agent_result['answer'][:300]}"
                    )
                if parsed.get('query_type') == 'log' and not parsed.get('logs'):
                    logger.warning(
                        f"[Chat/parse] log 타입인데 logs 없음! "
                        f"raw_answer[:500]={agent_result['answer'][:500]}"
                    )

                # Phase 2-A: response_meta 추출 (data_source/confidence/freshness)
                _resp_meta = agent_result.get("response_meta", {})

                # Phase 3-B: suggested_queries 추출
                _suggested = parsed.get("suggested_queries", [])
                if not isinstance(_suggested, list):
                    _suggested = []

                # tool_trace를 sources에 포함
                sources = {
                    "method": "agent",
                    "model": agent_result["model"],
                    "iterations": agent_result["iterations"],
                    "elapsed_ms": agent_result["elapsed_ms"],
                    "tool_trace": agent_result.get("tool_trace", []),
                    "response_meta": _resp_meta,  # Phase 2-A
                }

                # SSE 응답 전송 (기존 프론트엔드 호환) — response_meta + suggested_queries 포함
                yield f"data: {json.dumps({'type': 'result', 'data': parsed, 'sources': sources, 'response_meta': _resp_meta, 'suggested_queries': _suggested}, ensure_ascii=False)}\n\n"

                # sources 이벤트 (Agent tool_trace를 소스 형식으로 변환)
                source_list = []
                _now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                for trace in agent_result.get("tool_trace", []):
                    _ds = trace.get("data_sources") or {}
                    _vt = _ds.get("vector_tables") or []
                    _zt = _ds.get("zabbix_tables") or []
                    source_list.append({
                        "category": "agent",
                        "table": trace.get("tool", ""),
                        "similarity": 1.0,
                        "asset": "",
                        "ip": "",
                        "summary_preview": f"{trace.get('tool', '')}({json.dumps(trace.get('args', {}), ensure_ascii=False)[:80]})",
                        "retrieved_at": _now_str,   # v17: 조회 시각
                        "found": trace.get("found"),
                        "count": trace.get("count", 0),
                        "vector_tables": _vt,
                        "zabbix_tables": _zt,
                        "source_note_kr": _ds.get("note_kr"),
                    })
                yield f"data: {json.dumps({'type': 'sources', 'data': source_list}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

                # DB에 세션/메시지 저장 (중복 방지)
                if request.session_id and not _saved:
                    _saved = True
                    try:
                        await db_create_session(request.session_id, request.query[:200])
                        await db_save_message(
                            session_id=request.session_id,
                            query=resolved_query,
                            result_data=parsed,
                            sources=source_list,
                            model_info={
                                "llm": config.get("display_name", used_model),
                                "embed": EMBEDDING_MODEL,
                                "method": "agent",
                                "iterations": agent_result["iterations"],
                                "elapsed_ms": agent_result["elapsed_ms"],
                            },
                        )
                    except Exception as e:
                        logger.error(f"[Agent] DB 저장 실패: {e}")

            except Exception as e:
                logger.error(f"[Agent] 실패: {e}", exc_info=True)
                error_data = {
                    "query_type": "general",
                    "title": "오류 발생",
                    "answer": f"질의 처리 중 오류가 발생했습니다: {str(e)[:200]}",
                    "note": None,
                }
                yield f"data: {json.dumps({'type': 'result', 'data': error_data}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'sources', 'data': []}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"[Chat SSE] 오류: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


# ============================================================
# 히스토리 API — DB 기반
# ============================================================
@router.get("/api/history")
async def get_history():
    """세션 목록 조회"""
    try:
        sessions = await db_get_sessions(limit=50)
        result = []
        for s in sessions:
            result.append({
                "session_id": s["session_id"],
                "title": s["title"] or "",
                "message_count": s["message_count"],
                "created_at": s["created_at"].timestamp() if s["created_at"] else 0,
                "updated_at": s["updated_at"].timestamp() if s["updated_at"] else 0,
            })
        return {"sessions": result}
    except Exception as e:
        logger.error(f"[History] 목록 조회 실패: {e}")
        return {"sessions": []}


@router.get("/api/history/{session_id}")
async def get_session(session_id: str):
    """특정 세션의 전체 메시지 조회"""
    try:
        data = await db_get_session_messages(session_id)
        if not data:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[History] 세션 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/history")
async def save_history(req: HistorySaveRequest):
    """프론트엔드에서 직접 저장 요청 (호환용)"""
    try:
        await db_create_session(req.session_id, req.query[:200])
        await db_save_message(
            session_id=req.session_id,
            query=req.query,
            result_data=req.result,
            sources=req.sources,
            model_info=req.model_info,
        )
        return {"status": "ok", "session_id": req.session_id}
    except Exception as e:
        logger.error(f"[History] 저장 실패: {e}")
        return {"status": "error", "message": str(e)}


@router.delete("/api/history/{session_id}")
async def delete_session(session_id: str):
    """세션 삭제 (CASCADE로 메시지도 삭제)"""
    try:
        await db_delete_session(session_id)
    except Exception as e:
        logger.error(f"[History] 삭제 실패: {e}")
    return {"status": "ok"}

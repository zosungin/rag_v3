# /app/app/services/rag_chain.py
"""
RAG 체인 구현 v4.0
- v3.2: Gemini maxOutputTokens 8192로 증가 (응답 잘림 방지)
- v3.2: 잘린 JSON repair 강화
- v3.2: 컨텍스트 전달 건수 제한 (LLM 과부하 방지)
- v4.0: 가드레일 강화 — MCP/RAG 공통 수치 정확성 규칙
         quality_instruction에 환각 방지 + 비교 질의 + 미존재 장비 규칙 추가
"""

import httpx
import json
import re
import logging
from typing import List, Dict, AsyncGenerator, Optional

from app.config import (
    OLLAMA_URL, GEMINI_API_KEY, GEMINI_API_URL, GEMINI_MODEL,
    GROQ_API_KEY, GROQ_API_URL,
    OPENAI_API_KEY, OPENAI_API_URL,
    AVAILABLE_MODELS, get_model_config
)

from app.prompts.system_prompt import SYSTEM_PROMPT_BASE
from app.prompts.status_prompt import STATUS_PROMPT
from app.prompts.list_prompt import LIST_PROMPT
from app.prompts.alert_prompt import ALERT_PROMPT
from app.prompts.analysis_prompt import ANALYSIS_PROMPT
from app.prompts.general_prompt import GENERAL_PROMPT
from app.prompts.log_prompt import LOG_PROMPT
from app.prompts.inventory_prompt import INVENTORY_PROMPT
from app.prompts.process_prompt import PROCESS_PROMPT

logger = logging.getLogger(__name__)

# ============================================================
# 컨텍스트 최대 건수 — 질의 유형 + 백엔드에 따라 동적 결정
# ============================================================
def get_max_context(query_type: str, backend: str = "ollama") -> int:
    if backend in ("gemini", "groq", "openai"):
        limits = {'list': 20, 'alert': 15, 'analysis': 12, 'log': 12,
                  'process': 10, 'inventory': 8, 'status': 8, 'general': 7}
    else:
        limits = {'list': 15, 'alert': 12, 'analysis': 10, 'log': 10,
                  'process': 8, 'inventory': 7, 'status': 7, 'general': 5}
    return limits.get(query_type, 7)

# ============================================================
# 8개 유형 프롬프트 매핑
# ============================================================
PROMPT_MAP = {
    'status': STATUS_PROMPT,
    'list': LIST_PROMPT,
    'alert': ALERT_PROMPT,
    'analysis': ANALYSIS_PROMPT,
    'general': GENERAL_PROMPT,
    'log': LOG_PROMPT,
    'inventory': INVENTORY_PROMPT,
    'process': PROCESS_PROMPT,
}


# ============================================================
# v4.0: 강화된 품질 지침 (모든 질의에 공통 적용)
# ============================================================
QUALITY_INSTRUCTION = """

[★ 필수 준수 규칙 — 위반 시 응답이 무효 처리됩니다 ★]

1. **수치 정확성**: 응답에 포함하는 모든 숫자(CPU%, 메모리%, 트래픽 등)는 위 데이터에 있는 원본 숫자를 그대로 사용하세요. 데이터에 "6.1%"이면 "6.1%"로, "13.0%"이면 "13.0%"로 정확히 기재하세요. "60%", "55%" 같이 임의로 변형하면 안 됩니다.

2. **데이터 부재**: 질문에서 요구하는 정보(예: 업타임, 특정 장비, 특정 메트릭)가 위 데이터에 없으면, "제공된 데이터에서 해당 정보를 찾을 수 없습니다"라고 명시하세요. 절대로 추측하거나 다른 장비의 데이터를 대체 사용하지 마세요.

3. **장비 미존재**: 질문에서 언급한 장비명(예: "DB 서버")이 위 데이터의 어떤 호스트와도 일치하지 않으면, "현재 모니터링 데이터에서 해당 장비를 찾을 수 없습니다"라고 답하고, 현재 조회 가능한 장비 목록을 안내하세요.

4. **비교 질의**: "가장 높은", "더 불안정한" 같은 비교 질의 시, 비교 대상 모두의 데이터가 있어야 비교할 수 있습니다. 한쪽만 있으면 "데이터가 부족하여 정확한 비교가 어렵습니다"라고 답하세요.

5. **목록 질의**: "목록"을 요청하면 반드시 개별 항목을 하나하나 나열하세요. 건수만 말하지 마세요.

6. **서버/네트워크 구분**: "서버 목록"은 서버만, "네트워크 장비 목록"은 네트워크 장비만 출력하세요.

7. **임계치**: CPU 6.1%는 "정상"입니다. 70% 미만은 절대 "높다", "주의", "과부하"라고 쓰지 마세요.

8. **JSON 형식**: 반드시 유효한 JSON으로 응답하세요. JSON 외의 텍스트를 넣지 마세요.
"""


# ============================================================
# v4.0: MCP 분석 코멘트 전용 프롬프트 (구조화 데이터 인용 강제)
# ============================================================
MCP_ANALYSIS_INSTRUCTION = """

[MCP 구조화 데이터 분석 규칙]
위 [MCP 실시간 조회 결과]의 수치는 DB에서 직접 조회한 정확한 값입니다.
- 이 수치를 analysis 필드에 그대로 인용하세요.
- "cpu_utilization=6.1" → "CPU 사용률 6.1%"로 정확히 기술하세요.
- 이 수치를 임의로 변경, 반올림, 추정하지 마세요.
- 2대 장비가 조회되면 2대만 언급하세요. 존재하지 않는 장비를 추가하지 마세요.
- 조회 결과에서 질문 조건(예: CPU 50% 초과)에 해당하는 장비가 없으면 "조건에 해당하는 장비가 없습니다"라고 답하세요.
"""


def _build_prompt(query_type: str, query: str, context: str,
                  conversation_context: str = None,
                  is_mcp: bool = False) -> tuple:
    """system_prompt + user_prompt 생성 (v4.0: 가드레일 강화)"""
    prompt_template = PROMPT_MAP.get(query_type, GENERAL_PROMPT)
    user_prompt = prompt_template.format(query=query, context=context)

    # 대화 컨텍스트가 있으면 user_prompt 앞에 추가
    if conversation_context:
        user_prompt = conversation_context + "\n\n" + user_prompt

    # v4.0: 강화된 품질 지침 (항상 적용)
    user_prompt = user_prompt + QUALITY_INSTRUCTION

    # v4.0: MCP 경로인 경우 추가 가드레일
    if is_mcp:
        user_prompt = user_prompt + MCP_ANALYSIS_INSTRUCTION

    return SYSTEM_PROMPT_BASE, user_prompt


# ============================================================
# 모델 사전 로드 (Ollama)
# ============================================================
async def warmup_model():
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            from app.config import LLM_MODEL
            logger.info(f"[Warmup] 모델 로딩 시작: {LLM_MODEL}")
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": "hi", "stream": False, "options": {"num_predict": 1}}
            )
            logger.info(f"[Warmup] 모델 로딩 완료 (status={resp.status_code})")
    except Exception as e:
        logger.warning(f"[Warmup] 실패 (무시): {e}")


# ============================================================
# 중복 제거 (v3.1: 청킹 데이터의 시간대별 구분 보존)
# ============================================================
def deduplicate_results(results: List[Dict]) -> List[Dict]:
    seen = {}
    deduped = []
    for r in results:
        summary_text = r.get('summary', '')
        source_table = r.get('source_table', '')

        if 'chunked' in source_table:
            key = f"{source_table}_{r.get('hostid')}_{summary_text[:200]}"
        else:
            key = f"{source_table}_{r.get('hostid')}_{summary_text[:80]}"

        if key not in seen:
            seen[key] = True
            deduped.append(r)
    return deduped


# ============================================================
# 컨텍스트 생성 (v3.2: 최대 건수 제한)
# ============================================================
def format_search_results(results: List[Dict], time_info: str = None, max_context: int = 7) -> str:
    if not results:
        return "관련 데이터를 찾을 수 없습니다."

    results = deduplicate_results(results)
    results = results[:max_context]

    context_parts = []

    if time_info:
        context_parts.append(f"[조회 기간: {time_info}]")

    for idx, result in enumerate(results, 1):
        is_alert = 'alert' in result.get('category', '').lower()
        category_label = "ALERT 이벤트" if is_alert else result.get('category', '').upper()

        summary_text = result['summary'] or ''
        if len(summary_text) > 800:
            summary_text = summary_text[:800] + '...(이하 생략)'

        context_parts.append(f"""[데이터 {idx}] ({category_label} - {result['source_table']})
유사도: {result['similarity']:.2%}
호스트: {result.get('asset_name', 'N/A')} ({result.get('ip', 'N/A')})
OS: {result.get('os', 'N/A')} | 그룹: {result.get('group_name', 'N/A')}
상세:
{summary_text}""")

    return "\n\n".join(context_parts)


# ============================================================
# JSON 파싱/복구 (v3.2: 잘린 JSON 강화 처리)
# ============================================================
def extract_json_from_response(text: str) -> Optional[Dict]:
    if not text or not text.strip():
        return None

    # 1) 완전한 ```json ... ``` 블록
    json_match = re.search(r'```json\s*\n(.*?)\n\s*```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            repaired = repair_json(json_match.group(1).strip())
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # 2) ```json으로 시작하지만 닫는 ```가 없는 경우
    truncated_match = re.search(r'```json\s*\n(.*)', text, re.DOTALL)
    if truncated_match and not json_match:
        raw = truncated_match.group(1).strip()
        raw = re.sub(r'```\s*$', '', raw).strip()
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                repaired = repair_json(raw)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    logger.warning(f"[JSON] ```json 블록 repair 실패, len={len(raw)}")

    # 3) { ... } 블록
    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            repaired = repair_json(brace_match.group(0))
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # 4) { 로 시작하지만 닫는 }가 없는 경우
    open_brace = text.find('{')
    if open_brace >= 0:
        raw = text[open_brace:].strip()
        repaired = repair_json(raw)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            logger.warning(f"[JSON] 열린 brace repair 실패, len={len(raw)}")

    # 5) 직접 파싱
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    return None


def repair_json(json_str: str) -> str:
    if not json_str:
        return json_str

    json_str = json_str.rstrip()

    # trailing comma 제거
    json_str = re.sub(r',\s*$', '', json_str)
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

    # 짝이 안 맞는 따옴표 처리
    quote_count = json_str.count('"')
    if quote_count % 2 != 0:
        json_str += '"'

    # trailing comma 다시 제거
    json_str = re.sub(r',\s*$', '', json_str)

    # 닫히지 않은 대괄호/중괄호 닫기
    open_brackets = json_str.count('[') - json_str.count(']')
    open_braces = json_str.count('{') - json_str.count('}')

    json_str += ']' * max(0, open_brackets)
    json_str += '}' * max(0, open_braces)

    return json_str


def create_fallback_response(query_type: str, raw_text: str, query: str) -> Dict:
    clean = raw_text.strip()
    clean = re.sub(r'^```json\s*\n?', '', clean)
    clean = re.sub(r'\n?```\s*$', '', clean)
    clean = clean.strip()

    if not clean:
        return {
            "query_type": query_type, "title": query[:50],
            "answer": "응답을 생성하지 못했습니다.", "note": None
        }

    paragraphs = [p.strip() for p in clean.split('\n\n') if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in clean.split('\n') if p.strip()]

    title = query[:50]
    analysis = clean

    result = {
        "query_type": query_type,
        "title": title,
    }

    if query_type in ('status', 'general'):
        result["summary"] = paragraphs[0] if paragraphs else clean[:200]
        result["details"] = []
        for p in paragraphs[1:]:
            kv = re.match(r'^(.+?)[：:]\s*(.+)$', p)
            if kv:
                result["details"].append({"label": kv.group(1).strip(), "value": kv.group(2).strip()})
            else:
                result["details"].append({"label": "정보", "value": p})
        result["analysis"] = clean
        result["answer"] = clean

    elif query_type == 'list':
        result["items"] = []
        for line in clean.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and len(line) > 3:
                result["items"].append({"host": line[:80]})
        result["total_count"] = len(result["items"])
        result["answer"] = clean

    else:
        result["analysis"] = clean
        result["answer"] = clean

    result["note"] = None

    return result


# ============================================================
# Ollama 백엔드
# ============================================================
async def _call_ollama(system_prompt: str, user_prompt: str, model_id: str) -> str:
    config = get_model_config(model_id)

    payload = {
        "model": model_id,
        "prompt": user_prompt,
        "temperature": config.get("temperature", 0.3),
        "stream": False,
        **({"format": "json"} if config.get("force_json") else {}),
        "options": {
            "num_predict": config.get("max_tokens", 2000),
            "num_ctx": config.get("num_ctx", 8192)
        }
    }
    if system_prompt:
        payload["system"] = system_prompt

    timeout = config.get("timeout", 300.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        response.raise_for_status()
        result = response.json()
        raw_answer = result.get('response', '')
        done_reason = result.get('done_reason', 'unknown')

        logger.info(f"[Ollama] 응답: {len(raw_answer)}자, done_reason={done_reason}")
        if done_reason == 'length':
            logger.warning("[Ollama] 응답이 num_predict 제한으로 잘림!")

        return raw_answer


# ============================================================
# Gemini 백엔드
# ============================================================
async def _call_gemini(system_prompt: str, user_prompt: str, model_id: str = None) -> str:
    config = get_model_config(model_id or "gemini-2.5-flash")
    model_name = model_id or GEMINI_MODEL

    url = f"{GEMINI_API_URL}/{model_name}:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": user_prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": config.get("temperature", 0.3),
            "maxOutputTokens": 16384,
        }
    }

    if system_prompt:
        payload["systemInstruction"] = {
            "parts": [{"text": system_prompt}]
        }

    timeout = config.get("timeout", 60.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"[Gemini] API 오류 {response.status_code}: {error_body[:500]}")
            raise Exception(f"Gemini API 오류: {response.status_code} - {error_body[:200]}")

        result = response.json()

        candidates = result.get("candidates", [])
        if not candidates:
            raise Exception("Gemini 응답에 candidates가 없습니다.")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise Exception("Gemini 응답에 parts가 없습니다.")

        raw_answer = parts[0].get("text", "")
        finish_reason = candidates[0].get("finishReason", "unknown")

        logger.info(f"[Gemini] 응답: {len(raw_answer)}자, finishReason={finish_reason}")
        if finish_reason == "MAX_TOKENS":
            logger.warning("[Gemini] 응답이 maxOutputTokens 제한으로 잘림!")

        return raw_answer


# ============================================================
# OpenAI 백엔드 (v21.0 추가)
# ============================================================
async def _call_openai(system_prompt: str, user_prompt: str, model_id: str) -> str:
    config = get_model_config(model_id)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    # gpt-5 시리즈: max_completion_tokens 사용, temperature 제거
    use_mct = config.get("use_max_completion_tokens", False)
    no_temp = config.get("no_temperature", False)
    max_tok = config.get("max_tokens", 8192)

    payload: dict = {
        "model": model_id,
        "messages": messages,
    }
    if use_mct:
        payload["max_completion_tokens"] = max_tok
    else:
        payload["max_tokens"] = max_tok
        payload["temperature"] = config.get("temperature", 0.3)

    timeout = config.get("timeout", 120.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"[OpenAI] API 오류 {response.status_code}: {error_body[:500]}")
            raise Exception(f"OpenAI API 오류: {response.status_code} - {error_body[:200]}")

        result = response.json()
        choices = result.get("choices", [])
        if not choices:
            raise Exception("OpenAI 응답에 choices가 없습니다.")

        raw_answer = choices[0].get("message", {}).get("content", "")
        finish_reason = choices[0].get("finish_reason", "unknown")
        usage = result.get("usage", {})

        logger.info(f"[OpenAI] 응답: {len(raw_answer)}자, finish={finish_reason}, "
                    f"tokens={usage.get('total_tokens', '?')}")

        if finish_reason == "length":
            logger.warning("[OpenAI] 응답이 max_tokens 제한으로 잘림!")

        return raw_answer


# ============================================================
# Groq 백엔드
# ============================================================
async def _call_groq(system_prompt: str, user_prompt: str, model_id: str) -> str:
    config = get_model_config(model_id)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": config.get("temperature", 0.3),
        "max_tokens": config.get("max_tokens", 4096),
    }

    timeout = config.get("timeout", 60.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code != 200:
            error_body = response.text
            logger.error(f"[Groq] API 오류 {response.status_code}: {error_body[:500]}")
            raise Exception(f"Groq API 오류: {response.status_code} - {error_body[:200]}")

        result = response.json()

        choices = result.get("choices", [])
        if not choices:
            raise Exception("Groq 응답에 choices가 없습니다.")

        raw_answer = choices[0].get("message", {}).get("content", "")
        finish_reason = choices[0].get("finish_reason", "unknown")
        usage = result.get("usage", {})

        logger.info(f"[Groq] 응답: {len(raw_answer)}자, finish={finish_reason}, "
                     f"tokens={usage.get('total_tokens', '?')}")

        if finish_reason == "length":
            logger.warning("[Groq] 응답이 max_tokens 제한으로 잘림!")

        return raw_answer


# ============================================================
# 통합 LLM 호출 (백엔드 자동 선택)
# ============================================================
async def _call_llm(system_prompt: str, user_prompt: str, model_id: str = None) -> str:
    if not model_id:
        from app.config import LLM_MODEL
        model_id = LLM_MODEL

    config = get_model_config(model_id)
    backend = config.get("backend", "ollama")

    logger.info(f"[LLM] 백엔드={backend}, 모델={model_id}")

    if backend == "gemini":
        return await _call_gemini(system_prompt, user_prompt, model_id)
    elif backend == "groq":
        return await _call_groq(system_prompt, user_prompt, model_id)
    elif backend == "openai":
        return await _call_openai(system_prompt, user_prompt, model_id)
    else:
        return await _call_ollama(system_prompt, user_prompt, model_id)


# ============================================================
# LLM 답변 생성 (비스트리밍)
# ============================================================
async def generate_answer(
    query: str,
    search_results: List[Dict],
    query_type: str = 'general',
    time_info: str = None,
    model_id: str = None,
    temperature: float = None,
    mcp_context: str = None,
    conversation_context: str = None,
) -> Dict:
    if not model_id:
        from app.config import LLM_MODEL
        model_id = LLM_MODEL

    config = get_model_config(model_id)
    backend = config.get("backend", "ollama")

    logger.info(f"LLM 답변 생성: type={query_type}, model={model_id} ({backend}), 검색결과={len(search_results)}건")

    max_ctx = get_max_context(query_type, backend)
    context = format_search_results(search_results, time_info, max_context=max_ctx)

    # MCP 컨텍스트 병합
    is_mcp = bool(mcp_context)
    if mcp_context:
        if context and context != "관련 데이터를 찾을 수 없습니다.":
            context = mcp_context + "\n\n" + context
        else:
            context = mcp_context

    logger.info(f"[LLM] 컨텍스트 최대 {max_ctx}건 적용 (type={query_type}, backend={backend}), MCP={'있음' if is_mcp else '없음'}")

    # v4.0: is_mcp 플래그 전달하여 MCP 가드레일 적용
    system_prompt, user_prompt = _build_prompt(
        query_type, query, context, conversation_context, is_mcp=is_mcp
    )

    total_prompt_len = len(system_prompt) + len(user_prompt)
    logger.info(f"[LLM] 프롬프트 총 길이: {total_prompt_len}자 (system={len(system_prompt)}, user={len(user_prompt)})")

    try:
        raw_answer = await _call_llm(system_prompt, user_prompt, model_id)

        parsed = extract_json_from_response(raw_answer)
        if parsed:
            logger.info(f"JSON 파싱 성공: query_type={parsed.get('query_type')}")
            return parsed

        logger.warning(f"JSON 파싱 실패, 폴백. len={len(raw_answer)}, raw_preview={raw_answer[:500]}")
        return create_fallback_response(query_type, raw_answer, query)

    except httpx.ReadTimeout:
        return {"query_type": query_type, "title": "응답 시간 초과", "answer": "LLM 응답 시간이 초과되었습니다. 질문을 간결하게 하거나 더 빠른 모델을 선택해 보세요.", "note": None}
    except Exception as e:
        logger.error(f"LLM 답변 생성 실패: {e}")
        return {"query_type": query_type, "title": "오류 발생", "answer": f"오류: {str(e)}", "note": None}


# ============================================================
# LLM 답변 생성 (스트리밍) — Ollama 전용
# ============================================================
async def generate_answer_stream(
    query: str,
    search_results: List[Dict],
    query_type: str = 'general',
    time_info: str = None,
    model_id: str = None,
    temperature: float = None
) -> AsyncGenerator[str, None]:
    if not model_id:
        from app.config import LLM_MODEL
        model_id = LLM_MODEL

    config = get_model_config(model_id)
    backend = config.get("backend", "ollama")

    if backend in ("gemini", "groq", "openai"):
        result = await generate_answer(query, search_results, query_type, time_info, model_id)
        yield json.dumps({"type": "result", "data": result}, ensure_ascii=False)
        return

    logger.info(f"LLM 스트리밍 시작: model={model_id}")

    max_ctx = get_max_context(query_type, "ollama")
    context = format_search_results(search_results, time_info, max_context=max_ctx)
    system_prompt, user_prompt = _build_prompt(query_type, query, context)

    full_response = ""

    try:
        ollama_config = get_model_config(model_id)
        timeout = ollama_config.get("timeout", 300.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            payload = {
                "model": model_id,
                "prompt": user_prompt,
                "temperature": ollama_config.get("temperature", 0.3),
                "stream": True,
                **({"format": "json"} if ollama_config.get("force_json") else {}),
                "options": {
                    "num_predict": ollama_config.get("max_tokens", 2000),
                    "num_ctx": ollama_config.get("num_ctx", 8192)
                }
            }
            if system_prompt:
                payload["system"] = system_prompt

            async with client.stream('POST', f"{OLLAMA_URL}/api/generate", json=payload) as response:
                response.raise_for_status()

                chunk_count = 0
                async for line in response.aiter_lines():
                    if line.strip():
                        try:
                            chunk = json.loads(line)
                            if 'response' in chunk:
                                full_response += chunk['response']
                                chunk_count += 1
                                if chunk_count % 20 == 0:
                                    yield json.dumps({"type": "progress", "chars": len(full_response)}, ensure_ascii=False)
                            if chunk.get('done', False):
                                break
                        except json.JSONDecodeError:
                            continue

        parsed = extract_json_from_response(full_response)
        if parsed:
            yield json.dumps({"type": "result", "data": parsed}, ensure_ascii=False)
        else:
            fallback = create_fallback_response(query_type, full_response, query)
            yield json.dumps({"type": "result", "data": fallback}, ensure_ascii=False)

    except httpx.ReadTimeout:
        yield json.dumps({"type": "error", "message": "응답 시간이 초과되었습니다."}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"스트리밍 실패: {e}")
        yield json.dumps({"type": "error", "message": f"오류: {str(e)}"}, ensure_ascii=False)


# ============================================================
# RAG 파이프라인
# ============================================================
async def rag_pipeline(
    query: str,
    top_k: int = 5,
    categories: List[str] = None,
    query_type: str = None,
    model_id: str = None
) -> Dict:
    from app.services.vector_search import vector_search
    from app.services.query_classifier import classify_query, get_recommended_categories, parse_time_filter

    if not query_type:
        query_type = classify_query(query)

    if not categories:
        categories = get_recommended_categories(query_type, query)

    time_filter = parse_time_filter(query)
    filters = {}
    time_info = None
    if time_filter:
        filters['start_time'] = time_filter['start_time']
        filters['end_time'] = time_filter['end_time']
        time_info = time_filter['description']

    search_results = await vector_search(
        query_text=query, top_k=top_k, categories=categories,
        filters=filters if filters else None
    )

    if not search_results:
        return {"query_type": query_type, "title": "검색 결과 없음", "answer": "관련 데이터를 찾을 수 없습니다.", "note": None}

    return await generate_answer(query, search_results, query_type, time_info, model_id=model_id)

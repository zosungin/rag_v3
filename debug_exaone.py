#!/usr/bin/env python3
"""
exaone-2.4b 응답 0자 원인 정밀 진단 (v3)
docker exec -it rag-fastapi python3 /app/debug_exaone.py
"""

import asyncio
import httpx
import json
import os
import sys

_ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, _ROOT)
from app.config import OLLAMA_URL

# ── 테스트 1: config 설정 확인 ────────────────────────────────────
def test_config():
    print("\n[1] config 설정값")
    from app.config import LLM_MODEL, LLM_FORCE_JSON, LLM_NUM_CTX, LLM_MAX_TOKENS, LLM_TEMPERATURE
    from app.services.rag_chain import _IS_SMALL_MODEL
    print(f"  LLM_MODEL      = {LLM_MODEL}")
    print(f"  LLM_FORCE_JSON = {LLM_FORCE_JSON}  ({'✅' if not LLM_FORCE_JSON else '❌'})")
    print(f"  _IS_SMALL_MODEL= {_IS_SMALL_MODEL}   ({'✅' if _IS_SMALL_MODEL else '❌'})")
    print(f"  LLM_MAX_TOKENS = {LLM_MAX_TOKENS}")
    print(f"  LLM_NUM_CTX    = {LLM_NUM_CTX}")

# ── 테스트 2: 가장 단순한 프롬프트로 응답 확인 ───────────────────
async def test_minimal():
    print("\n[2] 최소 프롬프트 테스트 (한 줄 질문)")
    async with httpx.AsyncClient(timeout=120) as c:
        payload = {
            "model": "exaone-2.4b:latest",
            "prompt": "서울의 수도는 어디입니까?",
            "stream": False,
            "options": {"num_predict": 200, "num_ctx": 2048}
        }
        print(f"  전송: {payload['prompt']}")
        r = await c.post(f"{OLLAMA_URL}/api/generate", json=payload)
        d = r.json()
        resp = d.get('response', '')
        print(f"  HTTP 상태코드  : {r.status_code}")
        print(f"  done_reason    : {d.get('done_reason')}")
        print(f"  응답 길이      : {len(resp)}자")
        print(f"  응답 내용      : {repr(resp[:300])}")
        print(f"  전체 응답 키   : {list(d.keys())}")
        return len(resp) > 0

# ── 테스트 3: 실제 앱 프롬프트 (context 포함) ──────────────────
async def test_with_context():
    print("\n[3] 실제 앱 프롬프트 테스트 (context 포함)")
    from app.services.rag_chain import _build_prompt
    from app.config import LLM_MODEL, LLM_MAX_TOKENS, LLM_NUM_CTX, LLM_TEMPERATURE

    ctx = "호스트: zabbix server\nCPU: 5%\n메모리: 30%"
    qry = "zabbix server 운영현황 분석해줘"
    sys_p, usr_p = _build_prompt('analysis', qry, ctx)

    print(f"  system 길이    : {len(sys_p)}자")
    print(f"  user 길이      : {len(usr_p)}자")
    print(f"  user 전체 내용 :\n{'='*40}\n{usr_p}\n{'='*40}")

    async with httpx.AsyncClient(timeout=120) as c:
        payload = {
            "model": LLM_MODEL,
            "prompt": usr_p,
            "temperature": LLM_TEMPERATURE,
            "stream": False,
            "options": {"num_predict": LLM_MAX_TOKENS, "num_ctx": LLM_NUM_CTX}
        }
        if sys_p:
            payload["system"] = sys_p

        print(f"\n  Ollama 호출 중... (최대 120초)")
        r = await c.post(f"{OLLAMA_URL}/api/generate", json=payload)
        d = r.json()
        resp = d.get('response', '')
        print(f"  HTTP 상태코드  : {r.status_code}")
        print(f"  done_reason    : {d.get('done_reason')}")
        print(f"  응답 길이      : {len(resp)}자")
        print(f"  응답 앞 500자  : {repr(resp[:500])}")
        print(f"  전체 응답 키   : {list(d.keys())}")

        if len(resp) == 0:
            print("\n  ❌ 0자 응답 - 원인 추가 분석:")
            print(f"     prompt_eval_count : {d.get('prompt_eval_count', '없음')} 토큰")
            print(f"     eval_count        : {d.get('eval_count', '없음')} 토큰")
            print(f"     eval_duration     : {d.get('eval_duration', 0)/1e9:.2f}초")
        return len(resp) > 0

# ── 테스트 4: 스트리밍으로 raw 응답 확인 ─────────────────────────
async def test_streaming():
    print("\n[4] 스트리밍으로 raw 응답 확인")
    from app.services.rag_chain import _build_prompt
    from app.config import LLM_MODEL, LLM_MAX_TOKENS, LLM_NUM_CTX, LLM_TEMPERATURE

    ctx = "호스트: zabbix server\nCPU: 5%\n메모리: 30%"
    qry = "zabbix server 운영현황 분석해줘"
    sys_p, usr_p = _build_prompt('analysis', qry, ctx)

    async with httpx.AsyncClient(timeout=120) as c:
        payload = {
            "model": LLM_MODEL,
            "prompt": usr_p,
            "temperature": LLM_TEMPERATURE,
            "stream": True,
            "options": {"num_predict": LLM_MAX_TOKENS, "num_ctx": LLM_NUM_CTX}
        }
        if sys_p:
            payload["system"] = sys_p

        print(f"  스트리밍 호출 중... (최대 120초)")
        full = ""
        chunk_count = 0
        done_reason = "unknown"

        async with c.stream('POST', f"{OLLAMA_URL}/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    try:
                        chunk = json.loads(line)
                        token = chunk.get('response', '')
                        full += token
                        chunk_count += 1
                        if chunk.get('done'):
                            done_reason = chunk.get('done_reason', 'unknown')
                            print(f"  prompt_eval_count : {chunk.get('prompt_eval_count', '?')} 토큰")
                            print(f"  eval_count        : {chunk.get('eval_count', '?')} 토큰")
                            print(f"  eval_duration     : {chunk.get('eval_duration', 0)/1e9:.2f}초")
                            break
                    except:
                        pass

        print(f"  done_reason    : {done_reason}")
        print(f"  청크 수        : {chunk_count}")
        print(f"  전체 응답 길이 : {len(full)}자")
        print(f"  전체 응답 내용 : {repr(full[:500])}")

        if chunk_count == 0:
            print("\n  ❌ 청크 0개 → Ollama가 토큰을 전혀 생성하지 않음")
            print("     → VRAM 부족 또는 모델 손상 가능성")
        elif len(full) == 0:
            print("\n  ❌ 청크는 왔지만 response 필드가 비어있음")

# ── 테스트 5: ollama show로 모델 정보 확인 ───────────────────────
async def test_model_info():
    print("\n[5] 모델 상세 정보")
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"{OLLAMA_URL}/api/show",
                             json={"name": "exaone-2.4b:latest"})
            d = r.json()
            details = d.get('details', {})
            print(f"  parameter_size   : {details.get('parameter_size', '?')}")
            print(f"  quantization     : {details.get('quantization_level', '?')}")
            print(f"  format           : {details.get('format', '?')}")
            minfo = d.get('model_info', {})
            for k, v in minfo.items():
                if 'context' in k.lower() or 'ctx' in k.lower():
                    print(f"  {k}: {v}")
        except Exception as e:
            print(f"  ❌ 실패: {e}")

async def main():
    print("=" * 55)
    print("  exaone-2.4b 응답 0자 원인 정밀 진단 (v3)")
    print("=" * 55)

    test_config()
    await test_model_info()

    ok2 = await test_minimal()
    if not ok2:
        print("\n  ⛔ 최소 프롬프트도 0자 → 모델/VRAM 문제")
        print("     docker exec -it rag-ollama ollama run exaone-2.4b '안녕'")
    else:
        print("\n  ✅ 단순 질문은 응답 있음 → 앱 프롬프트 문제")
        await test_with_context()
        await test_streaming()

    print("\n" + "=" * 55)
    print("  진단 완료")
    print("=" * 55)

asyncio.run(main())

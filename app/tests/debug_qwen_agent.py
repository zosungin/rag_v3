"""
Qwen Ollama Agent 디버그 — tool 호출 패턴 확인
docker cp debug_qwen_agent.py rag-fastapi:/app/
docker exec rag-fastapi python /app/debug_qwen_agent.py
"""
import asyncio
import json
import time

async def main():
    from app.services.agent import run_agent, parse_agent_answer, SLIM_TOOL_DEFINITIONS
    from app.config import AVAILABLE_MODELS

    # Ollama 모델 찾기
    ollama_models = [m for m, c in AVAILABLE_MODELS.items() if c.get('backend') == 'ollama']
    print(f"Ollama 모델: {ollama_models}")

    if not ollama_models:
        print("❌ Ollama 모델이 config에 없습니다")
        return

    model = ollama_models[0]
    print(f"테스트 모델: {model}")
    print(f"Tool 개수: {len(SLIM_TOOL_DEFINITIONS)}")
    print()

    # === 테스트 1: 간단한 질의 ===
    queries = [
        "zabbix 서버 상태는?",
        "지난 2일 동안 ZABBIX 서버의 cpu 사용률이 높은 top5 상세 내역을 알려줘",
    ]

    for query in queries:
        print(f"{'='*60}")
        print(f"질의: {query}")
        print(f"모델: {model}")
        start = time.time()

        try:
            result = await run_agent(
                user_query=query,
                model=model,
                max_iterations=3,
            )
            elapsed = time.time() - start
            print(f"소요: {elapsed:.1f}초")
            print(f"반복: {result.get('iterations')}회")
            print(f"모델: {result.get('model')}")

            # tool_trace 확인
            traces = result.get('tool_trace', [])
            print(f"Tool 호출: {len(traces)}건")
            for t in traces:
                icon = "✅" if t.get("found") else "❌"
                print(f"  {icon} {t['tool']}({t.get('args', {})})")

            # 강제답변 여부
            if result.get('force_reason'):
                print(f"⚠ 강제답변: {result['force_reason']}")

            # raw answer 확인
            raw = result.get('answer', '')
            print(f"\nraw answer ({len(raw)}자):")
            print(raw[:500])

            # 파싱 확인
            parsed = parse_agent_answer(raw)
            print(f"\nparsed query_type: {parsed.get('query_type')}")
            print(f"parsed title: {parsed.get('title', '')[:80]}")
            answer = parsed.get('answer', '')
            print(f"parsed answer ({len(answer)}자):")
            print(answer[:300])

            details = parsed.get('details', [])
            metrics = parsed.get('metrics', [])
            print(f"details: {len(details)}건, metrics: {len(metrics)}건")

        except Exception as e:
            print(f"❌ 오류: {e}")

        print()
        await asyncio.sleep(2)

    # === Ollama 직접 호출 테스트 (tool 시뮬레이션 확인) ===
    print(f"{'='*60}")
    print("=== Ollama 직접 tool_call 패턴 테스트 ===")

    from app.services.agent import _build_ollama_tool_list, _parse_ollama_tool_call

    tool_list = _build_ollama_tool_list(SLIM_TOOL_DEFINITIONS[:3])  # 처음 3개만
    print(f"Tool 목록 (처음 3개):\n{tool_list[:500]}")
    print()

    # Ollama에 직접 요청
    try:
        from app.config import AVAILABLE_MODELS
        import httpx

        try:
            from app.config import OLLAMA_API_URL
        except ImportError:
            OLLAMA_API_URL = "http://172.202.0.21:11434"

        system_prompt = f"""당신은 Zabbix 모니터링 시스템 분석가입니다.

사용 가능한 도구:
{tool_list}

도구 호출이 필요하면 반드시 아래 JSON 형식으로만 응답하세요:
{{"tool_call": {{"name": "도구명", "arguments": {{파라미터}}}}}}

도구 호출이 불필요하면 최종 답변을 작성하세요."""

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "zabbix 서버 상태를 알려줘"}
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 512}
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{OLLAMA_API_URL}/api/chat", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                print(f"Ollama 직접 응답 ({len(content)}자):")
                print(content[:600])
                print()

                # tool_call 파싱 시도
                tc = _parse_ollama_tool_call(content)
                if tc:
                    print(f"✅ tool_call 파싱 성공: {tc['name']}({tc['arguments']})")
                else:
                    print(f"❌ tool_call 패턴 없음 — Ollama가 바로 텍스트 답변을 생성함")
                    print(f"   → 이것이 '세부 사항 없는 응답'의 원인")
            else:
                print(f"❌ Ollama 응답 오류: {resp.status_code}")

    except Exception as e:
        print(f"❌ 직접 테스트 실패: {e}")

asyncio.run(main())

import asyncio
import json
import time
from app.services.agent import run_agent, parse_agent_answer

async def test(query, model):
    print(f"\n{'='*60}")
    print(f"질의: {query}")
    print(f"모델: {model}")
    start = time.time()
    r = await run_agent(query, model)
    elapsed = time.time() - start
    print(f"소요: {elapsed:.1f}초")
    print(f"반복: {r['iterations']}회, Tools: {len(r['tool_trace'])}건")
    if r.get('force_reason'):
        print(f"⚠ 강제답변: {r['force_reason']}")
    for t in r['tool_trace']:
        icon = "✅" if t["found"] else "❌"
        print(f"  {icon} {t['tool']}({t['args']})")
    
    parsed = parse_agent_answer(r["answer"])
    print(f"query_type: {parsed.get('query_type')}")
    print(f"metrics: {len(parsed.get('metrics', []))}건, details: {len(parsed.get('details', []))}건")
    
    answer = parsed.get('answer', '')
    analysis = parsed.get('analysis', '')
    print(f"\n📋 answer ({len(answer)}자):")
    print(answer[:300])
    if analysis and analysis not in answer:
        print(f"\n🔍 analysis ({len(analysis)}자):")
        print(analysis[:300])

async def main():
    model = "llama-3.1-8b-instant"  # 빠른 테스트용

    # 문제 1: CPU top5 → 네트워크 포트가 나왔던 질의
    await test("지난 2일 동안 cpu 사용률이 높은 top5 상세 내역을 알려줘", model)
    await asyncio.sleep(3)
    
    # 문제 2: 프로세스 → 빈 테이블
    await test("프로세스별 자원사용현황은 ?", model)
    await asyncio.sleep(3)
    
    # 문제 3: CPU 특이사항 → 빈약한 분석
    await test("zabbix server 의 지난 2일 동안의 cpu 특이사항은 ?", model)

asyncio.run(main())

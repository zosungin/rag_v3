import asyncio
import json
import time
from app.services.agent import run_agent, parse_agent_answer

async def test(query, model):
    print(f"\n{'='*60}")
    print(f"모델: {model}")
    print(f"질의: {query}")
    start = time.time()
    r = await run_agent(query, model)
    elapsed = time.time() - start
    print(f"소요: {elapsed:.1f}초")
    
    raw = r["answer"]
    # JSON 순수성 체크
    stripped = raw.strip()
    starts_with_json = stripped.startswith('{')
    ends_with_json = stripped.endswith('}')
    has_extra_text = not starts_with_json or not ends_with_json
    
    print(f"raw 길이: {len(raw)}자")
    print(f"JSON 시작: {'✅' if starts_with_json else '❌ 앞에 텍스트 있음'}")
    print(f"JSON 종료: {'✅' if ends_with_json else '❌ 뒤에 텍스트 있음'}")
    
    if has_extra_text:
        print(f"  앞 50자: {stripped[:50]}")
        print(f"  뒤 50자: {stripped[-50:]}")
    
    parsed = parse_agent_answer(raw)
    print(f"query_type: {parsed.get('query_type')}")
    print(f"answer 길이: {len(parsed.get('answer', ''))}자")
    print(f"analysis 길이: {len(parsed.get('analysis', ''))}자")
    
    # JSON 문자열이 answer에 노출되는지 체크
    answer = parsed.get('answer', '')
    if '{"query_type"' in answer or '"data":{' in answer:
        print(f"⚠️ answer에 raw JSON 노출됨!")
    else:
        print(f"✅ answer 정상 (텍스트)")

async def main():
    query = "zabbix server 상태 알려줘"
    models = [
        "llama-3.1-8b-instant",
        "llama-4-scout-17b-16e-instruct",
        "llama-3.3-70b-versatile",
    ]
    
    for model in models:
        await test(query, model)
        print(f"\n⏳ 30초 대기 (429 방지)...")
        await asyncio.sleep(30)

asyncio.run(main())

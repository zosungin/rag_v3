# API 방식 질의·응답 테스트

브라우저 없이 HTTP로 동일한 기능을 검증합니다. **베이스 URL**은 스테이징과 운영 중 하나를 수동으로 정한 뒤 `TEST_API_BASE_URL`에 넣습니다(두 환경을 코드가 자동 구분하지 않음).

## 1. 사전 점검

```bash
python scripts/check_env_gates.py --base-url http://127.0.0.1:8000
```

## 2. RAG 검색만 (`POST /api/search`)

```bash
set TEST_API_BASE_URL=http://127.0.0.1:8000
python sync/test_search_quality.py
```

결과: `artifacts/search_quality_report.json`

## 3. 채팅 SSE·tool_trace (`POST /api/chat`)

**pytest (골든 세트):**

```bash
set RUN_AGENT_E2E=1
set TEST_API_BASE_URL=http://127.0.0.1:8000
pytest tests/test_agent_e2e.py --run-integration -q
```

- SSE 파싱: [tests/agent_e2e_utils.py](../tests/agent_e2e_utils.py)
- 질문·허용 도구: [tests/fixtures/agent_golden.json](../tests/fixtures/agent_golden.json)

## 4. 통합 스모크 스크립트 (게이트 → 선택 검색 → 채팅)

저장소 루트에서:

```bash
python scripts/api_smoke_chat.py --base-url http://127.0.0.1:8000 --with-search
```

- 질문 목록: [scripts/api_smoke_questions.json](../scripts/api_smoke_questions.json)
- 전체 검색 배치까지 포함: `python scripts/api_smoke_chat.py --run-search-suite` (시간 소요)
- `allowed_tools` 불일치 시 실패: `--soft-tools` 로 경고만

환경 변수: `TEST_API_BASE_URL`, `API_SMOKE_CHAT_DELAY_SEC`, `API_SMOKE_CHAT_TIMEOUT_SEC`

## 5. 운영 정기 점검: 10질의 채팅 배치 (`scripts/qa_10_eval.py`)

고정 10개 질의로 `POST /api/chat` 응답·도구 교차·품질 힌트를 한 번에 점검합니다. **실 LLM 호출**이므로 비용·부하를 고려해 주간·배포 직후 등으로 제한하는 것을 권장합니다.

```bash
set TEST_API_BASE_URL=http://127.0.0.1:8000
python scripts/qa_10_eval.py
```

- JSON: `artifacts/qa10_chat_report.json`
- 운영용 Markdown: `artifacts/qa10_operational_report.md`
- `python scripts/aggregate_quality_metrics.py` 실행 시 위 JSON이 있으면 `artifacts/quality_triage_summary.md` 에 **1.5 Chat QA10** 요약이 포함됩니다.

사내망 전용 API(`192.168.x.x`)는 GitHub Actions 러너에서 도달하지 못할 수 있으므로, **배포 서버 cron**으로 `scripts/cron_qa10_example.sh` 패턴을 쓰는 것이 일반적입니다. GitHub에서 실행할 때는 Actions → **RAG chat QA10** 워크플로(수동)와 공개·라우팅 가능한 베이스 URL을 사용합니다.

CLI: `--base-url`, `--delay`, `--timeout`, `--json-out`, `--md-out`, `--no-md`

환경: `TEST_API_BASE_URL`, `QA10_BASE_URL`, `QA10_DELAY_SEC`

## 6. 주의

- 채팅은 **실 LLM 호출**이므로 비용·부하를 고려합니다.
- 응답은 **SSE**이므로 `response.json()`이 아니라 스트림 전체를 읽어 `data:` 줄을 파싱해야 합니다.

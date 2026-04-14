# 비기능 요구·모니터링 항목 (Agentic RAG)

운영 규모가 커질 때 아래 지표를 수집·알람하는 것을 권장한다.

## 1. 지연 시간

| 구분 | 측정 | 목표(예시) |
|------|------|------------|
| `POST /api/chat` | 클라이언트 시작~`done` SSE 수신 | p95 < 백엔드 타임아웃(ollama 600s / openai 120s 참고) |
| `POST /api/search` | 요청~응답 본문 완료 | p95 < 10~30s(임베딩·DB 부하에 따라 조정) |
| `GET /api/health/detail` | 전체 응답 | < 5s |

에이전트 응답의 `sources.elapsed_ms`(SSE `result` 이벤트)를 로그로 적재하면 서버 측 처리 시간 분포를 잡기 쉽다.

## 2. 동시성·용량

- **동시 채팅 세션 수**: Uvicorn worker 수·단일 프로세스 한계와 Zabbix **asyncpg 풀** 크기를 함께 본다.
- **LLM 동시 요청**: 외부 API rate limit(429) 발생 시 재시도·백오프; 로그에 `[Agent] API 오류 (코드: 429)` 패턴 모니터링.
- **부하 시험**: `POST /api/chat` N건 동시(예: 10~50)로 p95·에러율·풀 고갈 여부를 스테이징에서만 수행.

## 3. 가용성·의존성

| 의존성 | 확인 | 실패 시 |
|--------|------|---------|
| RAG PostgreSQL(pgvector) | `vector_db` in `/api/health/detail` | 검색·세션 DB 실패 |
| Zabbix 읽기 DB | 앱 로그 `ZabbixDB`, 도구 실행 오류율 | 라이브 질의 불가 |
| OpenAI/Ollama | `agent_mesh`, Ollama HTTP | 답변 생성 불가 |

## 4. 품질·비용

- **도구 실패율**: 로그 `[ToolExec] … 실행 실패` 비율.
- **빈 검색**: `vector_search` 결과 0건 비율(임베딩 동기화 지연 신호).
- **토큰/비용**: OpenAI 사용 시 모델별 호출 수·에이전트 `iterations` 분포.

### 4.1 주간 품질 트리아지 (테스트 결과 → 백로그)

1. 스테이징에서 `python sync/test_search_quality.py` 실행 → `artifacts/search_quality_report.json` 생성.
2. **(선택, 채팅 10질의)** `python scripts/qa_10_eval.py --base-url http://<호스트>:8000` 실행 → `artifacts/qa10_chat_report.json`, `artifacts/qa10_operational_report.md` 생성. 사내망 전용 API는 **서버 cron**으로 주기 실행하는 편이 안전하다 (`scripts/cron_qa10_example.sh` 참고). GitHub Actions에서는 Actions → **RAG chat QA10** 워크플로를 수동 실행하고, 공개 URL이 아니면 러너가 대상에 도달하지 못할 수 있다.
3. `python scripts/aggregate_quality_metrics.py` 실행 → `artifacts/quality_triage_summary.md` 에 RAG 검색·**QA10**·(선택) E2E가 한 파일로 요약된다.
4. 서버 로그에서 parse/UI 불일치 grep (표·로그 렌더 깨짐 신호):

   - `query_type=list이지만 details`
   - `log 타입인데 logs 없음`
   - `[Agent] JSON 파싱 실패`

5. (선택) Agent 골든 E2E 결과를 `artifacts/pytest_e2e.json` 등으로 남기면 동일 스크립트에 병합 가능.

## 5. 동기화 신선도

- 스케줄러(`sync/scheduler.py`) 로그와 앱 설정 `RAG_EMBED_SYNC_INTERVAL_HOURS` / `RAG_CHUNK_SYNC_INTERVAL_HOURS` 를 정합성 있게 유지.
- 채팅 컨텍스트에 주입되는 **data freshness** 문구가 사용자에게 노출되는 경우, 운영 문서에 “최대 지연 허용치”를 명시.

## 6. 알람 예시(로그 기반)

- `health/detail` 중 `vector_db` 또는 `agent_mesh` 가 `error`/`degraded` 연속 N회.
- 5xx 비율 급증, `Agent 타임아웃` 메시지 빈도 임계 초과.

---

자동화된 검색 품질 배치는 `sync/test_search_quality.py` 및 GitHub Actions `rag-search-quality.yml`(workflow_dispatch)를 참고한다. 채팅 10질의는 `scripts/qa_10_eval.py` 및 `rag-chat-qa10.yml`(workflow_dispatch)을 참고한다.

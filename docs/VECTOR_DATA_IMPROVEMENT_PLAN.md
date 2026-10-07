# Zabbix RAG 벡터 데이터 진단 및 개선 계획서

- 작성일: 2026-10-07
- 대상: 192.168.6.24 운영 환경 (`rag-pgvector`/rag_db, `postgres-server`/zabbix_70, `rag-scheduler`)
- 근거: 소스 코드(`sync/`, `app/services/`), 사용자 테이블 정의(`QUERY/*.sql`), 운영 DB 실데이터·스케줄러 상태 (전부 읽기 전용으로 조사, 변경 없음)

---

## 0. 요약

| 구분 | 판정 | 핵심 사유 |
|---|---|---|
| 벡터 전환 방식 | **부적정** | 수치 시계열을 시점마다 1문장씩 임베딩해 쌓아 둔다(유사 벡터 수십만 건). 임베딩 모델 접두어를 쓰지 않는다. 요약(청킹) 계층은 **09-06부터 31일째 정지** 상태다. |
| 벡터 테이블 설계 | **부분 적정** | 768차원과 HNSW cosine은 적정하다. 다만 테이블 35개에 메타 컬럼이 중복되고, 저장이 append 방식이라 중복과 부풀림이 생긴다(1.9GB, 1.3GB 테이블). 빈 테이블과 레거시 테이블도 섞여 있다. |
| 원본(사용자 테이블) 수집 | **부분 적정** | 범위는 넓지만 갱신이 멈춘 테이블이 있고, 호스트가 누락되며, 항상 NULL인 컬럼과 단위·의미가 다른 컬럼이 있다. 모든 오류를 예외 처리로 삼켜 실패를 추적할 수 없다. |
| 실데이터 품질 | **미흡** | 중복 텍스트가 대량이다(vmware 88%, connections 30%). None/Unknown/":None"이 문장에 그대로 들어간다. 사라진 호스트가 6개 남아 있다. 벡터 테이블 8개가 비어 있다. |
| 벡터화 커버리지 | **미흡** | 취약점, EOS/SSL, 보안감사, PostgreSQL, Windows 서비스, 디스크 I/O, vCenter 알람·이벤트, 장비 환경(온도·팬·전원), 가용성, 장애 지식 이력이 수집은 되지만 벡터화되지 않는다. |

**한 줄 결론:** "모든 수치 샘플을 벡터로" 만드는 구조를 버리고 **엔티티 프로필 / 이벤트 / 기간 요약**의 세 문서 유형으로 재구성해야 한다. 수치 조회는 지금처럼 SQL 도구에 맡긴다. 그다음 미전환 데이터를 같은 틀로 편입한다.

---

## 1. 현재 구조

```
Zabbix 수집(item 4,594개, 호스트 41개)
  └─ history_* ─(트리거 사본)→ history_main / history_*_t (1시간 보존)
       └─ pg_cron 10개 그룹(1~10분) → 사용자 테이블 약 130개 (svr_*, nw_*, vmware_*, alert_trigger, svr_vul*, eos_* …)
            ├─ m2 프론트엔드(80포트)가 조회
            └─ rag-scheduler (orchestrator, 2~10분)
                 ├─ gen_embeddings_* 15종 → embeddings_* 원시 테이블
                 └─ gen_chunking_* 10종 → embeddings_*_chunked (원시 테이블을 다시 요약)  ※ 09-06부터 정지
                      └─ rag-fastapi: search_historical_data → 23개 테이블을 순차로 벡터 검색, 그 밖의 get_* 도구는 Zabbix SQL을 직접 조회
```

> **주의:** 사용자 테이블과 pg_cron 함수는 **80포트 m2 서비스도 같이 쓴다.** `QUERY/` 쪽을 바꾸면 RAG만이 아니라 m2 화면에도 영향이 간다. 그래서 이 계획은 원본 수정을 "버그 수정·컬럼 추가" 수준으로 제한하고, RAG 전용 가공은 RAG 쪽(뷰 또는 sync 코드)에서 한다.

---

## 2. 진단 상세

### 2.1 벡터 전환 방식

| # | 문제 | 근거 | 영향 |
|---|---|---|---|
| T1 | **청킹 작업 전체 정지(31일)** | `state.json`에 `gen_chunking_alerts`(07-15)와 `gen_chunking_server_logs`(09-06)가 `running=true`인 채로 남아 있다. 09-07 컨테이너 재시작 때 실행 중이던 작업 플래그가 지워지지 않았다. `default` 그룹 상한이 `ORCHESTRATOR_MAX_PARALLEL=2`라서 남은 플래그 2개가 그룹 자리를 모두 차지하고, 그 결과 청킹 작업 10종이 계속 선택되지 않는다(`sync/orchestrator.py:355-361`, `:216-218`). | 일간 장애 브리핑, 30분 성능 요약, 포트·연결 요약, 인벤토리 종합 문서가 9월 6일자에 머물러 있다. "최근 장애 요약", "서버 종합 상태" 같은 질의가 한 달 전 내용으로 답변된다. |
| T2 | 수치 시계열을 샘플마다 임베딩 | cpu, memory, swap, fs, if, process, network_interfaces가 시점마다 1행씩 쌓인다. `server_interfaces` 하나가 190,358행인데 고유 키는 575개다. | 숫자와 시각만 다른 거의 같은 벡터가 top-k를 독점한다. 자원 원인분석은 이미 벡터를 우회해 SQL로 직접 조회한다(`tool_executor.py:1764`). 벡터로서의 가치는 낮고 저장과 임베딩 비용만 크다. |
| T3 | nomic 접두어 미사용 | `search_document:`/`search_query:`를 문서와 질의 어디에도 붙이지 않는다(`app/services/embedding.py:26-32`, 각 gen_*). | nomic-embed-text-v2-moe는 비대칭 검색을 전제로 학습된 모델이라, 접두어가 없으면 검색 정확도가 떨어진다. |
| T4 | 결정적이지 않은 텍스트로 재임베딩 반복 | hosts와 network_devices는 가동시간(초 단위)과 CPU%가 해시에 들어가 2분마다 전량을 재임베딩한다. listen 청크는 확인 시각이 들어가 매번 새 행이 생긴다. alerts 청크는 `set()` 순서가 비결정적이다. | Ollama 부하가 커진다. `server_interfaces`는 평균 284초가 걸리고 600초 타임아웃이 누적 186회 났다. |
| T5 | 길이 초과로 뒷부분 무시 | 모델 컨텍스트는 512토큰인데 텍스트 상한은 8,000~12,000자다. errpt는 저장 텍스트와 임베딩 텍스트도 서로 다르다(1,000자에서 자른다). | 인벤토리(포트 50개), 알람 브리핑 같은 긴 문서는 뒷부분이 검색에 반영되지 않는다. |
| T6 | 실패를 성공으로 기록 | gen_* 스크립트가 `except: print` 후 exit 0으로 끝난다. errpt 작업은 "run ok"로 기록되지만 0행이다. | 품질 저하를 감지할 수 없다. |
| T7 | 형식과 언어가 섞여 있음 | 한국어 파이프 형식, 영문 key=value(fs, vmware), 이모지가 공존한다. 키워드 부스트가 단순 부분 문자열이라 '활성'이 '비활성'에 걸린다. | 같은 의미의 질의가 테이블에 따라 다르게 매칭된다. |
| T8 | 의미 오류 | errpt 청크가 적재 시각 기준이다. logs 청크에 연도가 없다. topn이 `SUM(cpu)`라 15,534%가 나온다. memory 청크가 `100-available`로 계산한다. 루트가 아닌 FS에 '루트 파티션' 문구가 붙는다. swap 12%를 '위험'으로 판정한다. | 사실과 다른 답변이 나온다. |

### 2.2 벡터 테이블 설계

| # | 문제 | 실측 |
|---|---|---|
| D1 | append 저장으로 인한 부피 | server_interfaces 1.9GB(dead 26,813건), server_logs는 **37행인데 1.28GB**(TOAST와 HNSW가 부풀었다), vmware 54,258행 중 47,802행이 같은 텍스트다. |
| D2 | 테이블 과다와 중복 스키마 | 35개 테이블에 hostid, asset, ip, group, os 등 약 12개 메타 컬럼이 중복돼 있다. DDL이 `sync/PV_*.sql`과 `sql_updated/PV_*.sql` 두 벌이고 운영 DB와도 다르다(코드상 ivfflat이지만 운영은 HNSW m=32). |
| D3 | 빈 테이블과 레거시 테이블 | 0행 8개(alerts_with_context, filesystems, server_errpt, …), `items_backup_*` 2개, inventory/processes 레거시. 빈 테이블에 만든 IVFFlat 인덱스는 의미가 없다. |
| D4 | 수명 관리 부재 | 사라진 호스트(10793, 10794, 10963, 11078, 11079, 11082)가 hosts, listen, alert_events에 남아 있다. connections에는 보존 정책이 없다. updated_at 기준 보존이라 touch할 때마다 수명이 연장된다. |
| D5 | 검색 측 | 23개 테이블을 순차로 검색한다. HNSW `ef_search`를 기본값 40으로 두고 WHERE 필터를 함께 쓰는데, pgvector 0.8.1의 `hnsw.iterative_scan`을 쓰지 않아 필터를 걸면 결과가 줄어든다. vmware_chunked는 hostid 컬럼이 없어 검색 SQL이 실패하고 빈 결과를 반환한다. |
| D6 | 임베딩 값 상태 | NULL 0건, 영벡터 0건, norm 약 11(cosine이라 문제없음). **임베딩 자체는 정상이다.** |

### 2.3 원본(사용자 테이블) 수집 적정성

| # | 문제 | 근거 | RAG 영향 |
|---|---|---|---|
| S1 | 갱신 정지 테이블 | svr_if_main(09-02), svr_log_errpt(05-08), nw_uplink(05-08), vmware_vcenter_perf/service(04-10). svr_system_main은 4개 호스트가 09-22에 멈췄다(에이전트 불가). | 오래된 값이 "현재 상태"로 임베딩된다. |
| S2 | 실패 은폐 | 모든 함수가 `EXCEPTION WHEN OTHERS`로 WARNING만 남긴다. cron은 항상 succeeded이고 `job_run_details`는 1시간 뒤 삭제된다. 캐스트 하나가 실패해도 그 함수 전체가 롤백된다. | 원본이 멈춰도 알 수 없다. |
| S3 | 호스트 누락 | 호스트명에 '#'가 들어간 호스트, HP-UX, **호스트 수준 `{$SNMP_COMMUNITY}` 매크로가 없는 NW 장비**(nw_main1이 사실상 INNER JOIN), availability 항목이 없는 서버(proc 계열), 템플릿 정규식과 맞지 않는 호스트(os=ETC). | 해당 장비가 질의에서 "없는 장비"로 취급된다. |
| S4 | 항상 NULL이거나 의미 없는 컬럼 | nw_if.port_status(초기 NULL이라 영구 NULL), nw_uplink.uplink_1/2, svr_log_messages.severity, host_item.agent_status(=host_status 버그), Windows proc_stat의 cpu·mem(0 고정). | 템플릿에 그대로 들어가 "None/Unknown" 문장이 된다. |
| S5 | 단위와 의미 불일치 | svr_mem_main.memory_utilization이 사용률이 아니라 bytes다. svr_cpu 주석은 ms인데 실제는 %다. svr_system_main.memory_utilization = 100 − available(Total이 없으면 100%). vmware_disk_io.iops는 실제로 OIO다. 데이터스토어 이름 `27_INTERNAL_DISK`가 하드코딩돼 있다. | 숫자 해석 오류가 그대로 문장이 된다. |
| S6 | alert_trigger 재발 처리 | 키가 triggerid라 재발할 때 clock·name이 갱신되지 않고 이전 r_clock이 남는다. r_clock은 cron이 감지한 시각이다. | 장애 지속시간과 조치시간 서술이 틀린다. |
| S7 | 이력 깊이 | _std와 integrated_*는 1시간 롤링이다. trends를 쓰지 않는다. | RAG가 쌓은 벡터가 사실상 유일한 이력이 된다. T2의 append 구조가 생긴 배경이다. 장기 이력은 Zabbix trends를 SQL로 직접 조회해야 한다. |
| S8 | 배포 스크립트 결함 | `03_SVR_MAIN.sql:46`에 구문 오류가 있다(`FUNCTION select update_svr_system`). apply.sh 실행 순서상 인덱스와 뷰를 대상 테이블보다 먼저 만든다. | 재구축하면 운영과 다른 상태가 된다. 운영 함수 정의를 먼저 추출해 QUERY와 대조해야 한다. |

### 2.4 미전환 데이터 (수집 중, 벡터 미반영)

| 영역 | 원본 | 규모 | 질의 가치 |
|---|---|---|---|
| 취약점 점검 | svr_vul(268), svr_vul_score(6), svr_vul_check, kisa_vul_mst/detail(131/332), item `comm[/tmp/vul/U*.sh]` | 4개 호스트 | 매우 높음 ("sun1 취약 항목은?", "U-01 조치 방법") |
| EOS/EOL 자산 | eos_summary(133), eos_asset(293), eos_info_m | – | 높음 ("EOS 임박 장비") |
| URL·SSL 인증서 | eos_url(18), eos_url_check(90,738), eos_date | 18개 URL | 높음 ("만료 임박 인증서", "응답 지연 URL") |
| 보안 감사 로그 | comm 약 83개 (journalctl sudo/su/Accepted·Failed password, passwd/shadow/crontab stat, md5sum, wtmp, last reboot) | 7개 호스트 | 높음 ("어제 root 로그인 실패") |
| vCenter 알람·이벤트·헬스 | vmware.alarms.get, health.*, eventlog(log), vmware_vcenter_info / datastore | vCenter 1개 | 높음 |
| VM 디스크·스토리지·네트워크 | vmware.vm.vfs.dev, storage latency/OIO, net.if, guest FS (vmware_disk_io/fs/net) | VM 28개 | 중간 |
| 하이퍼바이저 하드웨어 | vmware.hv.hw.* (모델·시리얼), datastore, multipath | HV 4개 | 중간 |
| PostgreSQL 상태 | pgsql.* 146개 | 1개 | 중간 |
| Windows 서비스 | service.info 121개, perf_counter, wmi | 1개 | 중간 |
| 서버 디스크 I/O | vfs.dev.* (await, util) 88개 | 5개 | 중간 |
| 네트워크 장비 환경 | nw_main1 온도·팬·전원, uptime_64(재기동), NW if errors/discards | 1개 | 중간 |
| 가용성 | agent.ping, icmp*, ping_test_*, url.total_time_ms, 에이전트 available | 전체 | 높음 ("통신 안 되는 장비") |
| 서비스 맵 | asset_service_map(161) | – | 중간 ("DB 포트 쓰는 서버") |
| 프로세스 감시·사용자 | svr_process / _check, proc_stat_user | – | 낮음~중간 |
| **장애 지식·종결 이력** | m2_log_knowledge(6), m2_errpt_knowledge(2), m2_log_close(2,747), m2_report_history(82) | – | **매우 높음** (조치 사례를 근거로 답변 가능) |
| AIX errpt | svr_log_errpt (원본 05-08 정지, 벡터 0행) | – | 원본 복구가 선행돼야 함 |
| 미수집 | Nutanix, Commvault(전부 0행), 웹 시나리오 0 | – | 수집 설정 후 대상 |

---

## 3. 개선 목표 설계

### 3.1 문서 유형 3계층 (핵심 원칙)

| 유형 | 의미 | 단위·저장 | 예시 | 주기 |
|---|---|---|---|---|
| **P. 엔티티 프로필** | "지금 이 대상은 어떤 상태인가" | 엔티티당 1문서, **upsert**, 의미가 바뀔 때만 재임베딩 | 서버·NW·VM·HV·vCenter 프로필, FS·IF 구성, 리슨 포트·서비스 맵, 취약점 점수, EOS, 인증서 | 상태 변화 시 (최대 10분) |
| **E. 이벤트** | "무슨 일이 언제 있었나" | 발생 1건당 1문서, append, 보존 기간 지정 | 알람 발생·해소, 로그 패턴, errpt, 보안 감사, 재기동, vCenter 알람·이벤트, 임계 초과, 장애 조치 이력 | 발생 시 |
| **S. 기간 요약** | "그 기간에 어땠나" | 엔티티×기간 1문서 (시간·일 단위) | 일간 장애 브리핑, 시간별 자원 요약(평균/최대/p95와 이상 구간), 주간 보안 요약 | 1시간 / 1일 |

- **수치 샘플은 벡터로 만들지 않는다.** 현재 cpu, mem, swap, fs, if, process, nw_if 원시 임베딩이 해당한다. 원시 수치는 기존 SQL 도구(get_* 와 `_resource_history_supplements`)로 조회하고, 벡터는 "어디를 봐야 하는지" 찾는 역할(P·E·S)만 맡는다.
- 기간 요약(S)은 RAG 원시 테이블을 다시 집계하지 않는다(현재 방식은 임계 초과 표본만 모여 편향된다). **사용자 _std 테이블이나 Zabbix trends에서 직접 집계한다.**

### 3.2 테이블 구조 (통합안)

```sql
CREATE TABLE rag_doc (
  doc_id        text PRIMARY KEY,          -- '{doc_type}:{entity_key}[:{bucket|event_id}]'
  doc_type      text NOT NULL,             -- host_profile / alert_event / vul_profile / daily_brief …
  layer         char(1) NOT NULL,          -- P / E / S
  entity_type   text,                      -- server / network / vm / hypervisor / vcenter / url / asset
  hostid        bigint,
  asset_name    text, ip inet, group_names text[], os text,
  severity      smallint,                  -- 0~5 공통 척도
  valid_from    timestamptz NOT NULL,      -- 원천 시각(이벤트 발생 시각, 버킷 시작, 프로필 측정 시각)
  valid_to      timestamptz,               -- 해소 시각, 버킷 끝, 프로필 폐기 시각
  is_current    boolean NOT NULL DEFAULT true,
  title         text NOT NULL,             -- 한 줄 요약(답변 인용용)
  content       text NOT NULL,             -- 임베딩 원문(접두어 제외, 1,500자 이내)
  facts         jsonb,                     -- 수치·원천 키(답변 근거와 SQL 재조회용)
  source_table  text NOT NULL, source_key text,
  content_hash  bytea NOT NULL,            -- 의미 필드만으로 계산(시각·가동시간 제외)
  embedding     vector(768) NOT NULL,
  embed_model   text NOT NULL,
  created_at    timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX ON rag_doc USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128);
CREATE INDEX ON rag_doc (doc_type, is_current, valid_from DESC);
CREATE INDEX ON rag_doc (hostid);
-- 검색 세션: SET hnsw.ef_search=100; SET hnsw.iterative_scan=relaxed_order;
-- 키워드 보조: content에 tsvector(simple) 또는 pg_trgm GIN 인덱스 → 하이브리드(RRF)
```

- 단일 테이블에 doc_type 필터를 거는 방식이다. pgvector 0.8의 iterative scan으로 필터를 걸어도 결과가 줄지 않는다. 테이블 23개를 순차 검색하던 것이 1회 질의로 바뀐다.
- 문서가 수만 건을 넘으면 `layer`나 `doc_type` 기준 LIST 파티션 또는 부분 HNSW 인덱스로 분리한다.
- 프로필(P)은 `doc_id`로 upsert하고, 사라진 엔티티는 `is_current=false`와 `valid_to`로 표시한 뒤 보존 기간이 지나면 삭제한다.
- 이벤트(E)와 요약(S)은 `valid_from` 기준으로 보존한다(updated_at 기준 아님). 권장 보존 기간: 이벤트 180일, 시간 요약 30일, 일 요약 1년.

### 3.3 문서 작성 규칙

1. **접두어:** 저장 시 `search_document: ` + content, 질의 시 `search_query: ` + 질문. 모델이 바뀌어도 대응하도록 `embed_model` 컬럼으로 관리한다.
2. **문장 형식:** 한국어 자연문으로 통일한다. 첫 줄은 `[유형] 자산명(IP, 그룹) — 핵심 상태`로 쓰고, 그 뒤에 근거 수치를 단위와 함께 적는다. 이모지와 key=value 나열은 쓰지 않는다.
3. **없는 값은 생략한다.** None, Unknown, N/A, ":None"을 출력하지 않는다. 필수 키(자산명, IP)가 없으면 문서를 만들지 않고 품질 로그에 남긴다.
4. **의미 해시:** 측정 시각, 가동시간 초, 순간 수치는 해시에서 빼고 상태 등급(정상/주의/위험)과 구성만으로 계산한다. 그러면 프로필은 상태가 바뀔 때만 재임베딩된다.
5. **길이:** 1,500자(약 450토큰) 이내로 쓴다. 긴 목록(포트 50개 등)은 요약 문장과 `facts` JSON으로 나눈다.
6. **동의어 보강:** 문서마다 한글과 영문 별칭을 한 줄 덧붙인다(예: `별칭: CPU 사용률, cpu usage, 프로세서 부하`). 기존 `term_normalization` 테이블을 활용한다.
7. **시각:** KST로 연·월·일·시·분을 모두 쓴다. 기간 요약은 기간을 명시한다.

### 3.4 문서 유형 카탈로그 (기존 + 신규)

| doc_type | 층 | 원본 | 대체하는 기존 테이블 |
|---|---|---|---|
| host_profile | P | svr_system_main + cpu/mem/swap/fs/if_main + host_item + interface available | embeddings_hosts, infra_inventory_chunked |
| net_device_profile | P | nw_main1 + nw_if 요약 + uptime_64 + 온도·팬·전원 | embeddings_network_devices |
| fs_profile / nic_profile | P | svr_filesystem_main, svr_if_main (구성과 현재 등급. 가상 NIC cali*·veth*·TAP 제외) | server_filesystems, server_interfaces (원시) |
| listen_profile / service_map | P | svr_listen_ports, asset_service_map, service_port_definition | server_listen_ports(_chunked) |
| connection_profile | P | svr_connections (대상이 비었거나 '사용자 PC → :None'인 행 제외) | server_connections(_chunked) |
| vm_profile / hv_profile / vcenter_profile | P | integrated_vm/hypervisor/vcenter, vmware_disk_io/fs/net, hv hw.* | embeddings_vmware(_chunked) |
| **vul_profile** | P | svr_vul_score + svr_vul(취약 항목만) + kisa_vul_mst/detail | 신규 |
| **vul_item_guide** | P | kisa_vul_mst/detail (항목별 점검 기준과 조치 방법, 정적) | 신규 |
| **eos_profile** | P | eos_summary / v_eos_summary | 신규 |
| **cert_url_profile** | P | eos_url + eos_url_check 최근 7일 요약 + eos_date | 신규 |
| **db_profile** | P | pgsql.* 주요 지표 상태 등급 | 신규 |
| **win_service_profile** | P | service.info (중지된 자동 시작 서비스 위주) | 신규 |
| alert_event | E | alert_trigger + events/problem(발생·해소·재발) + 태그 + ack | embeddings_alert_events |
| log_event | E | svr_log_messages (패턴 템플릿과 발생 횟수 집계) | embeddings_server_logs |
| errpt_event | E | svr_log_errpt (원본 복구 후) | embeddings_server_errpt |
| **security_audit_event** | E | 보안감사 comm 항목 (로그인 실패·성공, sudo/su, 계정·권한 파일 변경, 무결성 변경) | 신규 |
| **reboot_event** | E | uptime_64, system.boottime, last reboot | 신규 |
| **vcenter_event** | E | vmware.alarms.get, eventlog, health.* 상태 전이 | 신규 |
| **availability_event** | E | agent.ping, icmp, 에이전트 available 상태 전이 | 신규 |
| **threshold_event** | E | _std 테이블의 임계 초과·급변 구간(시작~끝으로 묶은 1건) | 원시 cpu/mem/swap 임베딩을 대체 |
| **incident_knowledge** | E | m2_log_knowledge, m2_errpt_knowledge, m2_log_close, m2_report_history | 신규 (조치 사례) |
| hourly_resource_summary | S | svr_*_std / trends: 호스트×시간 평균·최대·p95와 상위 프로세스 | server_performance_chunked, process_topn |
| daily_brief | S | alert_event와 threshold_event의 호스트×일 집계 | alerts_chunked, server_logs_chunked |
| weekly_security_summary | S | security_audit_event와 vul 변화의 주간 집계 | 신규 |

---

## 4. 단계별 실행 계획

### Phase 0 — 긴급 조치 (1~2일, 코드 변경 최소)

| # | 작업 | 내용 | 영향 |
|---|---|---|---|
| 0-1 | **청킹 정지 해소** | `state.json`에 남은 `running` 플래그 2건을 해제한다. 근본 수정으로 orchestrator 기동 시 남은 running 플래그를 초기화하고, `last_start + timeout`이 지난 작업을 자동 해제하는 로직을 넣는다. | rag-scheduler만 영향. 재개 직후 청킹 10종이 한꺼번에 돌아 Ollama 부하가 일시적으로 커진다. |
| 0-2 | 실패 감지 | gen_*가 예외 시 exit 1을 반환하게 하고, 적재 0건이면 경고를 남긴다. orchestrator 실패를 `/health`에 노출한다. | rag-scheduler |
| 0-3 | 부풀림 정리 | server_logs, server_interfaces에 `VACUUM FULL`(또는 pg_repack) + REINDEX를 한다. | rag_db 잠금. 사용이 적은 시간에 수행한다. |
| 0-4 | 검색 파라미터 | 검색 세션에 `hnsw.ef_search=100`, `hnsw.iterative_scan=relaxed_order`를 설정한다. vmware_chunked 검색 SQL 오류(hostid)를 수정한다. | rag-fastapi |
| 0-5 | 잔존 데이터 정리 | Zabbix에 없는 hostid 6개 행을 is_active=false로 표시하거나 삭제한다. connections의 ':None' 행을 정리한다. vmware 중복을 정리한다. | rag_db (백업 후) |

### Phase 1 — 원본 품질 보정 (1주, 사용자 테이블. m2 영향을 검토한 뒤 진행)

1. 운영 함수 정의를 추출(`pg_get_functiondef`)해 `QUERY/`와 대조하고 차이를 정리한다. 03_SVR_MAIN 구문 오류와 apply.sh 순서를 고친다.
2. 정지 테이블 원인을 조사하고 복구한다. 대상은 svr_if_main.updated_at(갱신 누락), svr_log_errpt, nw_uplink, vcenter_perf/service다.
3. 버그를 수정한다: nw_if.port_status 초기값, host_item.agent_status, nw_main1의 SNMP 매크로 INNER JOIN화, svr_system_main의 memory 100% 오표시, alert_trigger 재발 시 clock·r_clock 갱신, 데이터스토어 하드코딩.
4. **실패 가시성:** 함수 안의 `EXCEPTION`에서 `rag_etl_error_log`(신규) 테이블에 기록하도록 한다. 사용자 테이블마다 `last_refreshed_at`(값 변경과 무관하게 매 실행마다 갱신)을 추가한다. RAG 쪽은 이 값으로 "오래된 데이터" 여부를 판정해 문서에 반영한다.
5. 단위를 정리한다. 컬럼명과 주석을 바로잡거나, RAG용 뷰 `rag_v_*`에서 단위를 확정한다(m2 영향을 피하려면 뷰 방식이 우선).

### Phase 2 — 벡터 구조 재설계 (2~3주)

1. `rag_doc` 테이블과 공통 모듈(`sync/rag_doc_writer.py`)을 만든다. 공통 모듈이 템플릿 렌더링, 의미 해시, 접두어, 배치 임베딩(`/api/embed` 배열 입력), upsert, 폐기 처리, 품질 로그를 맡는다.
2. 기존 doc_type을 이관한다: host_profile → alert_event → log_event → net_device_profile → vm/hv_profile → listen/service_map → hourly_resource_summary / daily_brief / threshold_event.
3. 검색을 전환한다. `vector_search.py`가 rag_doc을 단일 질의로 조회하게 하고, 질의 의도에 따른 doc_type 가중치, 키워드 결합(RRF), 최신성 가중치를 넣는다. 기존 23개 테이블 경로는 **병행 운영 2주** 뒤 폐기한다.
4. 원시 수치 임베딩 7종(cpu, mem, swap, fs, if, process, nw_if)을 중단하고 테이블을 삭제한다. 레거시와 빈 테이블 10개를 정리한다.
5. 임베딩 주기는 프로필 10분(해시가 바뀐 것만), 이벤트 2분, 요약 시간 단위·일 단위로 둔다. Ollama 호출 수는 현재보다 90% 이상 줄어들 것으로 예상한다.

### Phase 3 — 미전환 데이터 편입 (3~4주, 우선순위 순)

| 순위 | doc_type | 전제 조건 |
|---|---|---|
| 1 | incident_knowledge, alert_event 보강(재발·ack) | – |
| 2 | vul_profile, vul_item_guide | KISA 조치 문구 정리 |
| 3 | security_audit_event, weekly_security_summary | comm 출력 파서 작성. 개인정보·비밀번호가 문장에 들어가지 않게 마스킹 |
| 4 | availability_event, reboot_event | – |
| 5 | cert_url_profile, eos_profile | – |
| 6 | vcenter_event, vm/hv 디스크·스토리지·네트워크·하드웨어 보강 | vcenter_alarm 테이블이 0행이라 원본 함수 확인 필요 |
| 7 | db_profile, win_service_profile, 서버 디스크 I/O(vfs.dev) | 사용자 테이블이 없으므로 RAG 뷰나 sync에서 history_main을 직접 조회하거나 사용자 테이블을 신설 |
| 8 | net_device 환경(온도·팬·전원) 등급화 | 제조사별 정상 범위 정의 |
| 9 | errpt_event | Phase 1의 errpt 원본 복구 |
| – | Nutanix, Commvault, 웹 시나리오 | Zabbix 수집 설정이 먼저 필요 (현재 0행) |

### Phase 4 — 검증과 운영 체계 (Phase 2부터 상시)

1. **정답셋:** 실제 질의 유형으로 50~100문항을 만든다(장애, 자원, 보안, 자산, 인증서, 조치 사례). 기존 `tests/fixtures/agent_golden.json`과 `sync/test_search_quality.py`를 확장한다.
2. **지표:** 검색 Recall@5와 MRR, 답변 근거 일치율, 최신성(문서 valid_from과 원천 시각의 차이), 중복률, None/Unknown 포함률, 작업 실패율.
3. **품질 게이트:** 이관할 때마다 이전 구조와 지표를 비교해 동일 이상일 때만 전환한다.
4. **대시보드:** doc_type별 건수, 최신 시각, 실패 건수를 `/health` 또는 Grafana에 노출한다.

---

## 5. 영향 범위와 위험

| 항목 | 영향 | 대응 |
|---|---|---|
| rag-fastapi / rag-scheduler 소스 변경 | bind mount와 `--reload` 때문에 **배포 즉시 반영**된다. `/agentic/`과 open-webui가 영향받는다(연동 범위라 감수 가능). | 개발 컨테이너(별도 포트)에서 검증한 뒤 반영한다. 검색 경로는 병행 운영한다. |
| 사용자 테이블·pg_cron 변경 | **80포트 m2 화면에 직접 영향**을 준다. 함수 하나가 실패하면 그 그룹 전체 갱신이 멈춘다. | Phase 1은 함수별로 바꾸고 백업(`pg_get_functiondef`)한다. RAG 전용 가공은 `rag_v_*` 뷰로 분리한다. |
| rag_db 정리(VACUUM FULL, 삭제) | 테이블 잠금이 걸리고 일시적으로 검색 결과가 빈다. | 사용이 적은 시간에 한다. `pg_dump` 백업을 먼저 한다. |
| Ollama 부하 | 청킹 재개와 재임베딩(접두어 도입 시 전량) 때 부하가 몰린다. | 배치 임베딩과 단계적 재생성으로 분산한다. |
| 보안감사 데이터 | 계정명, 원격 IP 같은 민감정보가 벡터 텍스트에 들어간다. | 비밀번호, 해시 원문, 명령 인자를 마스킹한다. 접근 통제를 검토한다. |
| 기존 보안 이슈 (별도) | 서버 `.env` 권한 666 (OpenAI 키 포함), DB 포트 5432/5433 전체 개방, 저장소에 DB 비밀번호가 하드코딩됨. | Phase 0과 함께 정리할 것을 권고한다. |

---

## 부록 A. 운영 실측 요약 (2026-10-07 08:30 UTC)

- **Zabbix:** 활성 호스트 41개(서버 7, VM 28, HV 4, 스위치 1, vCenter 1), 활성 item 4,594개, 트리거 770개, 미해결 문제 36건, 일 평균 문제 발생 약 50건.
- **rag_db:** 벡터 테이블 35개, 768차원, HNSW cosine 위주. 주요 테이블 행수: server_interfaces 190,358 / vmware 54,258 / network_interfaces 15,984 / server_connections 13,088 / server_cpu 10,255 / alert_events 892 / hosts 13.
- **청킹 테이블:** 마지막 갱신 09-06 (alerts_chunked는 07-15).
- **pg_cron:** 10개 그룹, 전부 succeeded로 기록됨(함수 안에서 예외를 삼키므로 이 상태로는 성공 여부를 판단할 수 없음).
- **rag-scheduler:** 최근 72시간 실패는 server_interfaces 타임아웃 5건. 청킹 작업은 이 기간에 0회 실행됐다.

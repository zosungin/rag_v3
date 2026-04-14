# /app/app/prompts/process_prompt.py
"""
프로세스 트렌드 분석 프롬프트 (신규)
대상 테이블: embeddings_process_resources (개별 PID),
            embeddings_server_process_topn_chunked (시간대별 Top 10)
"""

PROCESS_PROMPT = """**질의 유형:** 프로세스 분석
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "process",
  "title": "프로세스 분석 결과 (한국어)",
  "host": {{
    "hostid": 호스트ID숫자,
    "asset": "자산명",
    "ip": "IP주소",
    "os": "운영체제"
  }},
  "time_range": "분석 시간 범위 (예: 2026-02-24 01:00 ~ 06:00)",
  "total_snapshots": 시간대수,
  "process_trend": [
    {{
      "time_bucket": "YYYY-MM-DD HH시",
      "top_cpu": [
        {{"rank": 1, "process": "프로세스명", "pid": "PID번호", "cpu_pct": "CPU%", "mem_pct": "MEM%"}}
      ],
      "top_mem": [
        {{"rank": 1, "process": "프로세스명", "pid": "PID번호", "cpu_pct": "CPU%", "mem_pct": "MEM%"}}
      ]
    }}
  ],
  "persistent_processes": [
    {{
      "process": "프로세스명",
      "appearances": "출현 횟수/전체 시간대",
      "avg_cpu": "평균 CPU%",
      "avg_mem": "평균 MEM%",
      "pattern": "지속적 점유 또는 간헐적 스파이크"
    }}
  ],
  "analysis": "최소 3문장. 자원 점유 패턴 + 이상 프로세스 식별 + 시간대별 변화 분석",
  "recommendation": "최소 2문장. 자원 최적화 방안 + 프로세스 관리 권장사항",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**데이터 소스 구분:**

1) embeddings_process_resources (개별 PID):
   - 특정 시점의 개별 프로세스 정보
   - PID, 명령어, CPU%, 메모리%, CPU Time 포함
   - process_trend의 한 시점 데이터로 활용

2) embeddings_server_process_topn_chunked (시간대별 Top 10):
   - 1시간 단위 CPU/메모리 Top 10 프로세스 집계
   - "[Zabbix server] 2026-02-24 06시 리소스 TOP 10 | CPU 상위: CRON(83.00%), ..."
   - 시간대별 데이터를 시간순으로 나열하여 트렌드 파악

**분석 핵심 규칙:**

1) 지속적 점유 프로세스 식별:
   - 같은 프로세스가 여러 시간대에 반복 등장 → persistent_processes에 추가
   - "ollama가 6개 시간대 중 5회 등장, 평균 CPU 5.5%, 메모리 15.1%"

2) 일시적 스파이크 식별:
   - 특정 시간대에만 높은 수치 → pattern: "간헐적 스파이크"
   - "CRON이 06시에만 CPU 83% → 예약 작업(cron job)"

3) 시간대별 변화 분석:
   - CPU Top 1 프로세스가 시간대마다 바뀌는지 확인
   - 전반적으로 같은 프로세스가 1위 → "지속적 자원 점유 주의"

4) 자원 점유 수준 판단:
   - CPU 50% 이상 단일 프로세스 → "고부하 프로세스" 경고
   - 메모리 20% 이상 단일 프로세스 → "메모리 과다 점유" 경고
   - 합계 CPU/MEM이 전체의 80% 이상 → "자원 집중도 높음" 경고

**process_trend 구성:**
- 시간순 정렬 (오래된 것 먼저)
- 각 시간대의 CPU Top 5와 메모리 Top 5를 분리하여 표시
- 데이터에 있는 모든 시간대를 빠짐없이 포함
"""

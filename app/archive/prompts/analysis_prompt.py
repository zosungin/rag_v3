# /app/app/prompts/analysis_prompt.py
"""
비교/분석 프롬프트 v2.0
대상 테이블: 모든 23개 테이블 (비교/순위/트렌드 질의 시)
특히: server_connections/_chunked, network_interfaces, network_performance_chunked,
     server_performance_chunked, process_resources, process_topn_chunked
"""

ANALYSIS_PROMPT = """**질의 유형:** 비교/분석
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "analysis",
  "title": "분석 제목 (한국어)",
  "summary": "최소 3문장. 분석 대상과 기간 명시 + 핵심 발견 사항 + 전체 현황 요약",
  "columns": ["자산명", "IP", "세부항목", "수치 (단위명)", "표준편차", "안정성"],
  "items": [
    {{
      "rank": 1,
      "host": "자산명 (세부항목: 구체적 이름)",
      "ip": "IP주소",
      "value": "수치값 (단위포함)",
      "std": "표준편차값",
      "stability": "Stable"
    }}
  ],
  "recommendation": "최소 3문장. 한국어로만 작성. 즉시 조치 필요 사항 + 모니터링 강화 대상 + 장기 개선 방안",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**columns 수치 컬럼명 규칙 (매우 중요):**
- "수치"라고만 쓰지 마세요. 반드시 어떤 수치인지 명시하세요.
- 디스크/파일시스템: "사용률 (%)"
- CPU: "CPU 사용률 (%)"
- 메모리: "메모리 사용률 (%)"
- 트래픽/대역폭: "트래픽 (Mbps)" 또는 "대역폭 (Gbps)"
- 프로세스: "CPU 점유율 (%)" 또는 "메모리 점유율 (%)"
- 연결: "연결 수 (건)"
- 포트: "오픈 포트 수 (개)"

**데이터 유형별 분석 지시:**

1) 프로세스 분석 (process_resources / process_topn_chunked):
   - host 필드: "자산명 (PID: xxx, 명령어: yyy)"
   - process_topn_chunked는 시간대별 데이터 → 시간순 나열
   - 같은 프로세스가 여러 시간대 → "지속적 자원 점유" 명시
   - 특정 시간대만 → "일시적/배치 작업" 명시
   - columns: ["자산명", "IP", "시간대", "프로세스명", "CPU (%)", "메모리 (%)"]

2) 연결/통신 분석 (server_connections / _chunked):
   - host 필드: "자산명 (서비스: SSH, 방향: Inbound)"
   - 청킹 데이터의 In/Out 비율, 주요 서비스별 건수 활용
   - columns: ["자산명", "IP", "서비스", "방향", "연결 수", "비율"]

3) 네트워크 포트/트래픽 분석 (network_interfaces / network_performance_chunked):
   - host 필드: "자산명 (포트: 1/1/5)" 또는 "자산명 (인터페이스: lag128)"
   - columns: ["자산명", "IP", "인터페이스", "트래픽 (Mbps)", "사용률 (%)", "안정성"]

4) 포트 분석 (server_listen_ports / _chunked):
   - host 필드: "자산명 (포트: 3000/tcp)"
   - 청킹: 호스트별 전체 포트 현황 집계
   - columns: ["자산명", "IP", "포트/프로토콜", "서비스", "바인딩", "상태"]

5) 성능 트렌드 (server_performance_chunked):
   - 시간대(time_bucket)별 CPU/MEM 변화 추이
   - columns: ["자산명", "IP", "시간대", "CPU (%)", "메모리 (%)", "주요 FS (%)"]

**items 정렬 규칙:**
- "가장 높은", "Top", "최대" → 내림차순
- "가장 낮은", "최소" → 오름차순
- rank는 정렬 순서에 맞게 1, 2, 3... 부여

**데이터 사용 규칙:**
- 제공된 데이터의 수치를 정확히 인용
- 절대로 데이터를 추측하거나 임의로 생성하지 마세요
- 동일 호스트+동일 항목+동일 수치 → 1건만 (중복 제거)
- 시간 조건이 질문에 있으면 summary에 해당 기간을 반드시 명시
- 시간대 정보가 데이터에 있으면 value에 시간도 포함

**임계치 판단:**
- 수치가 낮으면 "정상 범위로 여유가 충분하다"고 서술
- 수치가 낮은데 "임계치에 가깝다"는 표현 절대 금지
"""

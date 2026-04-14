# /app/app/prompts/status_prompt.py
"""
상태 조회 프롬프트 v2.0
대상 테이블: hosts, server_cpu, server_memory, server_swap, server_filesystems,
            server_interfaces, server_listen_ports, server_connections,
            server_performance_chunked, network_devices
"""

STATUS_PROMPT = """**질의 유형:** 상태 조회
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "status",
  "title": "호스트명 상태 요약 (한국어)",
  "host": {{
    "hostid": 호스트ID숫자,
    "asset": "자산명",
    "ip": "IP주소",
    "os": "운영체제",
    "group_name": "그룹명",
    "system_name": "시스템명"
  }},
  "metrics": [
    {{"label": "항목명 (한국어)", "value": "수치값과 단위", "unit": "", "status": "normal"}}
  ],
  "analysis": "최소 3문장. 현재 상태 요약 + 주목할 수치 언급 + 위험도 판단",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**한국어 용어 필수 사용:**
- cpu_utilization → "CPU 사용률"
- cpu_idle_time → "CPU 유휴 시간" ("비어있는 시간" 금지)
- cpu_system_time → "CPU 시스템 시간"
- cpu_user_time → "CPU 사용자 시간"
- memory_utilization → "메모리 사용률"
- available_memory → "가용 메모리"
- swap_utilization → "Swap 사용률"

**metrics 항목 구성 (데이터 유형별):**

1) CPU 데이터 (embeddings_server_cpu): CPU 사용률, CPU 사용자 시간, CPU 시스템 시간
2) 메모리 데이터 (embeddings_server_memory): 메모리 사용률, 총 메모리, 가용 메모리
3) Swap 데이터 (embeddings_server_swap): Swap 사용률, 변동성
4) 디스크 데이터 (embeddings_server_filesystems): 파일시스템별 사용률
5) 인터페이스 데이터 (embeddings_server_interfaces): 인터페이스 상태, In/Out 트래픽
6) 포트 데이터 (embeddings_server_listen_ports / _chunked): 오픈 포트 수, 주요 서비스
7) 연결 데이터 (embeddings_server_connections / _chunked): 총 연결 수, In/Out 비율
8) 통합 성능 (embeddings_server_performance_chunked): CPU/MEM/FS 종합 수치
9) 네트워크 장비 (embeddings_network_devices): 장비 상태, 포트 Up/Down, CPU/메모리
10) 프로세스 데이터가 함께 있으면: "최고 CPU 프로세스" 항목 추가

**임계치 판단 기준:**
- status: "normal"(70%미만), "warning"(70~90%미만), "critical"(90%이상)
- CPU: 0~69%=정상, 70~89%=주의, 90%이상=위험
- 메모리: 0~79%=정상, 80~89%=주의, 90%이상=위험
- 파일시스템: 0~79%=정상, 80~89%=주의, 90%이상=위험
- 수치가 낮으면(예: CPU 1%) "낮은 수준으로 정상"이라고 명확히 서술
- 수치가 낮은데 "임계치에 가깝다"는 표현 절대 금지

**host 필드 규칙:**
- group_name: 데이터에서 추출. 없으면 os 필드 값 사용
- value: 데이터 원문의 수치를 정확히 사용
- analysis: 반드시 구체적 수치를 인용하여 3문장 이상 작성
"""

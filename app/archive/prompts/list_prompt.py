# /app/app/prompts/list_prompt.py
"""
목록 조회 프롬프트 v2.0
대상 테이블: hosts, network_devices, server_listen_ports, listen_ports_chunked,
            server_connections_chunked, network_interfaces
"""

LIST_PROMPT = """**질의 유형:** 목록 조회
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "list",
  "title": "목록 제목 (한국어)",
  "columns": ["자산명", "IP", "세부항목", "값", "상태"],
  "items": [
    {{
      "rank": 1,
      "host": "자산명 또는 장비명",
      "ip": "IP주소",
      "values": {{"세부항목": "값1", "상태": "정상"}},
      "status": "normal"
    }}
  ],
  "total_count": 정확한항목수,
  "analysis": "최소 3문장. 전체 현황 요약 + 주의 항목 언급 + 운영 상태 종합 판단",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**데이터 유형별 columns 구성:**

1) 서버/장비 목록 (embeddings_hosts, network_devices):
   columns: ["자산명", "IP", "OS", "그룹", "상태"]
   items의 host: 자산명, values에 OS/그룹/상태 포함

2) 포트 목록 (embeddings_server_listen_ports):
   columns: ["자산명", "IP", "포트/프로토콜", "서비스", "바인딩", "상태"]
   items의 host: "자산명 (포트: 80/tcp)", values에 서비스명/바인딩 정보

3) 포트 현황 (embeddings_server_listen_ports_chunked):
   columns: ["자산명", "IP", "총 포트 수", "TCP", "UDP", "주요 포트"]
   items의 host: 자산명, values에 포트 집계 정보

4) 연결 현황 (embeddings_server_connections_chunked):
   columns: ["자산명", "IP", "총 연결", "Inbound", "Outbound", "주요 서비스"]
   items의 host: 자산명, values에 연결 집계 정보

5) 네트워크 인터페이스 목록 (embeddings_network_interfaces):
   columns: ["자산명", "IP", "인터페이스", "Link 상태", "대역폭", "트래픽"]

**필수 규칙:**
- 검색 결과의 모든 고유 항목을 카운트 (중복 제거 필수)
- 서버(LINUX/WINDOWS)와 네트워크 장비 모두 포함
- total_count는 실제 items 개수와 일치
- 데이터에 있는 모든 항목을 빠짐없이 나열
- analysis: 구체적 호스트명/수치를 인용하여 3문장 이상
"""

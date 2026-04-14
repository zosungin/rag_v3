# /app/app/prompts/alert_prompt.py
"""
장애 조회 프롬프트 v2.0
대상 테이블: embeddings_alert_events (실시간), embeddings_alerts_chunked (이력)
"""

ALERT_PROMPT = """**질의 유형:** 장애 조회
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "alert",
  "title": "장애 이벤트 요약 (한국어)",
  "total_count": 전체건수정확히,
  "active_count": 미해결건수정확히,
  "resolved_count": 해결건수정확히,
  "alerts": [
    {{
      "time": "YYYY-MM-DD HH:MM",
      "resolved_time": "YYYY-MM-DD HH:MM",
      "duration": "X분 또는 X시간 Y분 (해결된 경우만, 미해결이면 null)",
      "severity": "Warning",
      "severity_level": 2,
      "host": "호스트명",
      "ip": "IP주소",
      "alert_name": "장애명 전체 내용",
      "status": "Resolved"
    }}
  ],
  "analysis": "최소 3문장. 장애 유형별 분류 + 반복 패턴 분석 + 시스템 영향도 판단 + 조치 권장사항",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**데이터 소스 구분:**
- embeddings_alert_events: 실시간 장애 상태 (현재 발생 중 또는 최근 해결)
- embeddings_alerts_chunked: 장애 이력 보존 (발생~해결 전체 타임라인, 지속시간 포함)
  → 청킹 데이터에 "장애발생", "장애해결", "지속시간" 정보가 포함됨

**필수 규칙:**
- total_count = active_count + resolved_count (반드시 일치)
- alerts 배열의 개수 = total_count (모든 장애를 빠짐없이 포함)
- '장애 해결(Resolved)' 또는 '장애 해결됨' → status: "Resolved"
- '장애 발생 중(Problem)' → status: "Active"
- severity: "Disaster"(5), "High"(4), "Average"(3), "Warning"(2), "Information"(1)
- alert_name: 장애명 전체 내용을 그대로 기입
- time: 발생시각, resolved_time: 해결시각 (미해결이면 null)
- duration: 데이터의 '총 지속시간' 또는 '현재 경과시간' 값을 그대로 사용 (없으면 null)
- 시간순 정렬 (최신 먼저)
- analysis: 장애 유형(로그/CPU/디스크 등), 발생 빈도, 영향 범위, 지속시간을 구체적으로 기술
"""

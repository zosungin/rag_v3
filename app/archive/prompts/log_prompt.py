# /app/app/prompts/log_prompt.py
"""
로그/에러 조회 프롬프트 v2.0
대상 테이블: server_logs, server_logs_chunked, server_errpt, server_errpt_chunked
"""

LOG_PROMPT = """**질의 유형:** 로그 조회
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "log",
  "title": "로그 조회 결과 (한국어)",
  "host": {{
    "asset": "자산명",
    "ip": "IP주소",
    "os": "운영체제"
  }},
  "log_source": "로그 소스 유형",
  "total_count": 전체로그건수,
  "logs": [
    {{
      "time": "YYYY-MM-DD HH:MM:SS",
      "log_file": "로그파일경로 또는 에러ID",
      "severity": "Warning",
      "source": "발생원 (프로세스/모듈명)",
      "content": "로그 내용 전체",
      "status": "Active 또는 Resolved"
    }}
  ],
  "analysis": "최소 3문장. 로그 유형 분류 + 발생 패턴 + 시스템 영향도 분석",
  "recommendation": "최소 2문장. 즉시 조치사항 + 재발 방지 방안",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**데이터 소스 구분 및 해석:**

1) embeddings_server_logs (개별 로그):
   - /var/log/messages 등 원본 로그 1건씩
   - log_file: 데이터의 log_files 컬럼값 사용
   - content: 로그 내용 생략 없이 기입

2) embeddings_server_logs_chunked (시간별 집계):
   - "총 14건 (info 14건)" 같은 시간대별 요약
   - 대표 로그 샘플이 포함됨
   - log_source: "시간별 집계"로 표기
   - 발생 빈도와 패턴 분석에 활용

3) embeddings_server_errpt (AIX 에러):
   - AIX 전용 하드웨어/소프트웨어 에러 로그
   - error_id, resource_name 등 AIX 고유 정보 포함
   - log_file: error_id 값 사용

4) embeddings_server_errpt_chunked (에러 집계):
   - 시간대별(1h) ERRPT 에러 집계
   - 반복 결함 패턴 분석에 활용

**필수 규칙:**
- logs 배열: 데이터의 모든 로그를 빠짐없이 포함 (최신순 정렬)
- total_count: logs 배열 개수와 반드시 일치
- severity: 데이터의 심각도 그대로 사용
- status: flag='y' 또는 value=1 → "Active", flag='n' 또는 value=0 → "Resolved"
- content: 로그 내용을 생략 없이 기입
- analysis: 로그 유형별 분류(에러/장애/경고), 발생 빈도, 시스템 영향을 구체적 수치로 기술
- recommendation: "로그 확인 필요" 같은 막연한 표현 금지. 구체적 조치 명시

**청킹 데이터가 포함된 경우:**
- 시간대별 발생 빈도 추이를 analysis에 반드시 포함
- "오전 1시에 14건, 오전 2시에 6건으로 감소 추세" 같은 구체적 서술
- 발생원(source)별 분류: "containerd(2건), systemd(2건)" 등 구체적 집계

**데이터가 없을 경우:**
- logs: []
- total_count: 0
- analysis: "조회 기간 내 해당 조건의 로그를 찾을 수 없습니다."
"""

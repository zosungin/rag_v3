# /app/app/prompts/inventory_prompt.py
"""
자산 인벤토리 프롬프트 (신규)
대상 테이블: embeddings_infra_inventory_chunked
"""

INVENTORY_PROMPT = """**질의 유형:** 자산 인벤토리 조회
**질문:** {query}

**관련 모니터링 데이터:**
{context}

**응답 형식:** 아래 JSON 구조로만 응답하라. 한국어만 사용. JSON 외 텍스트 금지.
{{
  "query_type": "inventory",
  "title": "자산 프로필 (한국어)",
  "host": {{
    "hostid": 호스트ID숫자,
    "asset": "자산명",
    "ip": "IP주소",
    "os": "운영체제",
    "group_name": "그룹명",
    "system_name": "시스템명"
  }},
  "specs": {{
    "cpu_cores": "CPU 코어 수",
    "total_memory": "총 메모리 (GB)",
    "os_version": "OS 상세 버전"
  }},
  "storage": [
    {{
      "mount_point": "/",
      "total": "전체 용량",
      "usage_percent": "사용률 (%)",
      "status": "normal"
    }}
  ],
  "open_ports": {{
    "total": 포트수,
    "tcp": TCP수,
    "udp": UDP수,
    "list": "22/tcp, 80/tcp, 443/tcp, ..."
  }},
  "recent_alerts": [
    {{
      "alert_name": "장애명",
      "count": 발생횟수,
      "severity": "Warning"
    }}
  ],
  "analysis": "최소 3문장. 자산 전반 상태 요약 + 스토리지 여유도 + 최근 장애 패턴 + 보안 관점(오픈 포트)",
  "recommendation": "최소 2문장. 점검 필요 항목 + 개선 권장사항",
  "measured_at": "YYYY-MM-DD HH:MM"
}}

**데이터 해석 규칙:**
- embeddings_infra_inventory_chunked의 inventory_summary는 통합 프로필
- 서버 사양, 스토리지 현황, 오픈 포트, 최근 7일 장애 이력이 모두 포함
- specs: 데이터에서 CPU, 메모리, OS 정보를 추출
- storage: 파일시스템별 사용률 → status는 80% 미만=normal, 80~90%=warning, 90%이상=critical
- open_ports: 포트 목록과 집계 정보
- recent_alerts: 최근 7일 장애 이력에서 추출. 없으면 빈 배열

**분석 필수 포함 사항:**
- 전체 스토리지 중 가장 사용률 높은 파일시스템 언급
- 오픈 포트 중 보안 관점에서 주의할 포트 (불필요한 고위험 포트 여부)
- 최근 7일 장애 이력에서 반복 패턴이 있는지 분석
- 장애가 없으면 "최근 7일간 장애 발생 없음, 안정적 운영 상태" 명시
"""

# /app/app/services/tools.py
"""
Agentic RAG — Tool Definitions (Phase 1)
=========================================
LLM Function Calling용 Tool 명세 10개.
기존 17개 MCP 함수를 의미적으로 통합.

형식: OpenAI Function Calling JSON Schema (Groq 호환)
Gemini는 functionDeclarations 형식으로 변환 필요 → agent.py에서 처리.

Tool 목록:
  1. get_server_status      — 서버 종합 상태 (CPU/MEM/Swap/FS)
  2. get_server_cpu          — CPU 상세 + 상위 프로세스
  3. get_server_memory       — 메모리 상세
  4. get_server_filesystem   — 파일시스템(디스크) 상세
  5. get_network_device      — 네트워크 장비 상태
  6. get_network_interfaces  — 네트워크 포트/인터페이스 (트래픽, Link)
  7. get_alerts              — 장애/알림 이벤트
  8. get_processes           — 프로세스 목록 (CPU/MEM 상위)
  9. get_all_assets          — 전체 자산 목록 (서버+네트워크)
  10. search_historical_data — RAG 벡터 검색 (이력/트렌드/로그)
"""

from typing import List, Dict

# ============================================================
# Tool 정의 (OpenAI Function Calling 형식)
# ============================================================

TOOL_DEFINITIONS: List[Dict] = [
    # ─── 1. 서버 종합 상태 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_status",
            "description": (
                "서버의 현재 종합 상태를 조회합니다. "
                "CPU 사용률, 메모리(GB), Swap 사용률, 최대 파일시스템 사용률, 프로세스 수, 로그인 유저 수를 반환합니다. "
                "특정 서버 1대를 조회하거나, 전체 서버 목록을 조회할 수 있습니다. "
                "사용 예: '서버 상태', 'Zabbix server 상태', '전체 서버 목록', '192.168.6.24 상태'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": (
                            "서버를 식별하는 키워드. "
                            "IP 주소(예: 192.168.6.24), 자산명(예: Zabbix server), "
                            "역할명(예: DB 서버, 웹서버), 또는 부분 이름(예: zabbix). "
                            "전체 서버 조회 시 'all'."
                        )
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 2. CPU 상세 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_cpu",
            "description": (
                "서버의 실시간 CPU 상세 정보를 조회합니다. "
                "CPU 사용률, User/System/Idle 비율, 코어 수를 반환합니다. "
                "선택적으로 CPU 사용량 상위 프로세스도 함께 조회합니다. "
                "CPU 부하·사용률 높음·상위 프로세스 질의에 우선 사용. "
                "사용 예: 'Zabbix server CPU 상태', 'CPU 사용률 높은 서버'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "include_top_processes": {
                        "type": "boolean",
                        "description": "CPU 상위 프로세스 5개를 함께 반환할지 여부. 기본값 false.",
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 3. 메모리 상세 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_memory",
            "description": (
                "서버의 실시간 메모리 사용 현황을 조회합니다. "
                "전체 메모리(GB), 사용량(GB), 가용량(GB), 사용률(%)을 반환합니다. "
                "Swap 정보도 포함됩니다. "
                "사용 예: 'Zabbix server 메모리', '메모리 사용률'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 4. 파일시스템 상세 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_filesystem",
            "description": (
                "서버의 파일시스템(디스크) 사용 현황을 조회합니다. "
                "마운트 포인트별 전체 용량, 사용량, 사용률을 반환합니다. "
                "디스크 공간 부족·여유 용량·inode·마운트별 용량 질의에 사용. "
                "사용 예: '디스크 사용률', '파일시스템 현황', '/data 용량'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 5. 네트워크 장비 상태 ───
    {
        "type": "function",
        "function": {
            "name": "get_network_device",
            "description": (
                "네트워크 장비(스위치, 라우터 등)의 상태를 조회합니다. "
                "ICMP Ping, SNMP 상태, CPU/메모리 사용률, 포트 Up/Down 수, 가동일, 온도를 반환합니다. "
                "모니터링 에이전트가 비활성(agent_status≠0)인 장비는 제외됩니다. "
                "VMware/vCenter/가상머신 CPU 질문에는 사용하지 말 것 — get_vmware 사용. "
                "사용 예: '네트워크 장비 상태', 'Aruba 스위치', '전체 네트워크 장비 목록'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": (
                            "네트워크 장비 식별 키워드. "
                            "자산명(예: test_network_devices), IP, 모델명. "
                            "전체 장비 조회 시 'all'."
                        )
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 6. 네트워크 인터페이스/포트 ───
    {
        "type": "function",
        "function": {
            "name": "get_network_interfaces",
            "description": (
                "네트워크 장비의 포트/인터페이스 상태를 조회합니다. "
                "각 포트의 Link 상태(Up/Down), 트래픽(수신/송신), 속도, 에러 수를 반환합니다. "
                "Link Up/Down 필터링, 트래픽 순 정렬, Top N 조회가 가능합니다. "
                "**현재(최신) 스냅샷 위주**이며, 언제 트래픽이 최대였는지·피크 시각·몇 시에 가장 높았는지 같은 **과거 시간대 피크**는 알 수 없음. "
                "그런 질문에는 반드시 search_historical_data(categories=[\"network\"], …)를 병행하세요. "
                "사용 예: 'Up 포트 목록', '트래픽 Top 5 포트', 'Down 포트', '대역폭 상위 인터페이스'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "네트워크 장비 식별 키워드. 전체 장비는 'all'."
                    },
                    "link_filter": {
                        "type": "string",
                        "enum": ["all", "up", "down"],
                        "description": "Link 상태 필터. 'up'=활성 포트만, 'down'=비활성 포트만, 'all'=전체. 기본값 'all'."
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["name", "traffic", "errors"],
                        "description": "정렬 기준. 'traffic'=트래픽(수신+송신) 내림차순, 'errors'=에러 내림차순, 'name'=이름순. 기본값 'name'."
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "상위 N개만 반환. 0이면 전체 반환. 기본값 0."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 7. 장애/알림 ───
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": (
                "장애(Alert) 이벤트를 조회합니다. "
                "장애명, 심각도(Disaster/High/Average/Warning/Information), 발생시각, 상태(Active/Resolved)를 반환합니다. "
                "현재 활성(미해결) 장애만 또는 전체 이력을 조회할 수 있습니다. "
                "사용 예: '현재 활성 장애', '장애 이력', 'Zabbix server 장애'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "장비 식별 키워드. 전체 장비는 'all'."
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "active", "resolved"],
                        "description": "장애 상태 필터. 'active'=미해결만, 'resolved'=해결만, 'all'=전체. 기본값 'all'."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 반환 건수. 기본값 20."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 8. 프로세스 목록 ───
    {
        "type": "function",
        "function": {
            "name": "get_processes",
            "description": (
                "서버에서 실행 중인 프로세스 목록을 조회합니다. "
                "PID, 사용자, CPU 사용률, 메모리 사용률, 명령어를 반환합니다. "
                "CPU 사용량 내림차순으로 정렬됩니다. "
                "사용 예: 'Zabbix server 프로세스', 'CPU 상위 프로세스'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드."
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "상위 N개 프로세스. 기본값 10."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 9. 전체 자산 목록 ───
    {
        "type": "function",
        "function": {
            "name": "get_all_assets",
            "description": (
                "전체 모니터링 대상(서버+네트워크 장비) 목록을 조회합니다. "
                "각 자산의 이름, IP, OS, CPU 사용률, 상태를 반환합니다. "
                "CPU 임계치 이상인 장비만 필터링하거나, 서버/네트워크 유형별 필터링이 가능합니다. "
                "사용 예: '전체 자산 목록', '모니터링 장비 몇 대', 'CPU 50% 초과 장비', '서버 목록'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": ["all", "server", "network"],
                        "description": "자산 유형 필터. 'server'=서버만, 'network'=네트워크 장비만, 'all'=전체. 기본값 'all'."
                    },
                    "cpu_threshold": {
                        "type": "number",
                        "description": "CPU 사용률(%) 이 값 이상인 장비만 필터링. 0이면 전체. 기본값 0."
                    }
                }
            }
        }
    },

    # ─── 11. 서버 NIC 트래픽 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_interfaces",
            "description": (
                "서버의 NIC(네트워크 인터페이스) 트래픽 현황을 조회합니다. "
                "인터페이스명, 송신/수신 bps, 활성 상태, 속도를 반환합니다. "
                "**현재 스냅샷**이므로 '과거 언제 피크였나'에는 부족함 — 해당 경우 search_historical_data(categories=[\"server\"] 등) 병행. "
                "사용 예: '서버 NIC 트래픽', 'eth0 트래픽', '서버 인터페이스 현황'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "active", "inactive"],
                        "description": "인터페이스 상태 필터. 'active'=활성만, 'inactive'=비활성만, 'all'=전체. 기본값 'all'."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 12. 서버 Listen 포트 목록 ───
    {
        "type": "function",
        "function": {
            "name": "get_listen_ports",
            "description": (
                "서버에서 현재 Listen(대기) 중인 포트 목록을 직접 조회합니다. "
                "포트 번호, 프로토콜(TCP/UDP), 바인드 IP, 서비스 정보를 반환합니다. "
                "사용 예: '서버 오픈 포트', 'Listen 포트 목록', '22번 포트 열려있나', '포트 현황'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "port_number": {
                        "type": "integer",
                        "description": "특정 포트 번호 필터. 미지정 시 전체 반환."
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["all", "tcp", "udp"],
                        "description": "프로토콜 필터. 기본값 'all'."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 13. 서버 외부 연결 세션 ───
    {
        "type": "function",
        "function": {
            "name": "get_connections",
            "description": (
                "서버의 현재 외부/내부 연결 세션(커넥션) 목록을 직접 조회합니다. "
                "출발지/목적지 IP·포트, 프로토콜, 방향(In/Out), 서비스명을 반환합니다. "
                "사용 예: '서버 연결 현황', '외부 연결 세션', '통신 중인 IP 목록', '연결 상태'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["all", "in", "out"],
                        "description": "연결 방향 필터. 'in'=수신, 'out'=송신, 'all'=전체. 기본값 'all'."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 반환 건수. 기본값 50."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 14. 시스템 로그 원문 ───
    {
        "type": "function",
        "function": {
            "name": "get_server_logs",
            "description": (
                "서버의 시스템/애플리케이션 로그 원문을 직접 조회합니다. "
                "Zabbix log[] 모니터링 아이템으로 수집된 모든 로그 파일을 지원합니다: "
                "/var/log/messages, /var/log/syslog, AIX 애플리케이션 로그(/tmp/a 등) 포함. "
                "log_file 파라미터로 특정 로그 파일 경로를 지정할 수 있습니다. "
                "키워드로 로그 내용을 필터링할 수 있습니다. "
                "사용 예: '서버 로그 조회', 'error 로그', '/tmp/a 로그', 'AIX 애플리케이션 로그'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "keyword": {
                        "type": "string",
                        "description": "로그 내용 검색 키워드. 미지정 시 전체 반환."
                    },
                    "log_file": {
                        "type": "string",
                        "description": (
                            "조회할 로그 파일 경로 필터 (부분 일치). "
                            "예: '/tmp/a', '/var/log/messages', '/var/log/syslog'. "
                            "미지정 시 수집된 모든 로그 파일 반환."
                        )
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["all", "error", "warning", "info"],
                        "description": "로그 심각도 필터. 기본값 'all'."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 반환 건수. 기본값 20."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 10-2. get_errpt (AIX ERRPT 로그 원문 직접 조회) ───
    {
        "type": "function",
        "function": {
            "name": "get_errpt",
            "description": (
                "AIX 서버 전용 ERRPT(에러 리포트) 로그를 svr_log_errpt 원본 테이블에서 직접 조회합니다. "
                "AIX 시스템의 하드웨어/소프트웨어 오류, 경보, 복구 이벤트를 확인할 수 있습니다. "
                "Linux/Windows 서버 로그(get_server_logs)와 완전히 분리된 AIX 전용 도구입니다. "
                "사용 예: 'AIX 서버 에러 로그', 'AIX_TEST ERRPT 조회', "
                "'AIX 하드웨어 오류 확인', 'errpt 로그 보여줘', 'AIX 로그'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": (
                            "조회할 AIX 서버 식별 키워드. "
                            "자산명(asset), 자산명칭(asset_name), IP, hostid 모두 가능. "
                            "전체 AIX 서버는 'all'. 예: 'AIX_TEST', '172.202.0.50'"
                        )
                    },
                    "keyword": {
                        "type": "string",
                        "description": (
                            "ERRPT identifier / resource_name / description 키워드 필터. "
                            "예: 'errdemon', 'DISK', 'LOGGING'"
                        )
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["all", "critical", "warning", "info"],
                        "description": (
                            "심각도 필터. "
                            "critical=P(Permanent), warning=T(Temporary)/U(Undetermined), "
                            "info=I(Informational)/D(Diagnosed)/R(Recovered). 기본값 'all'."
                        )
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 반환 건수. 기본값 20."
                    },
                    "days_ago": {
                        "type": "integer",
                        "description": "최근 N일 이내 데이터만 조회. 예: days_ago=7"
                    },
                    "hours_ago": {
                        "type": "integer",
                        "description": "최근 N시간 이내 데이터만 조회. days_ago보다 우선."
                    }
                },
                "required": ["host_query"]
            }
        }
    },

    # ─── 10. RAG 벡터 검색 (이력/트렌드/로그) ───
    {
        "type": "function",
        "function": {
            "name": "search_historical_data",
            "description": (
                "과거 모니터링 데이터를 벡터(의미) 검색합니다. "
                "실시간 조회 도구로 얻을 수 없는 데이터에 사용합니다: "
                "시간대별 트렌드/변화 분석, 표준편차/안정성 분석, 로그 검색, "
                "자산 인벤토리(사양/포트/장애이력 통합 프로필), "
                "프로세스 트렌드(시간대별 Top 10), 과거 장애 이력 분석, 네트워크 성능 이력. "
                "**네트워크 포트/인터페이스 트래픽이 특정 시각·시간대에 최대였는지** 같은 질문은 "
                "categories=[\"network\"], search_query에 포트명·장비명·bps·트렌드·time_bucket 등을 포함해 검색. "
                "사용 예: '지난 24시간 CPU 트렌드', '로그에서 error 검색', "
                "'서버 자산 프로필', '시간대별 프로세스 분석', "
                "'VSX-Secondary 1/1/5 포트 트래픽 피크 시각', '최근 7일 네트워크 인터페이스 트래픽 이력'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {
                        "type": "string",
                        "description": "검색할 내용을 자연어로 기술합니다."
                    },
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["server", "network", "alert", "infra", "log", "vmware"]
                        },
                        "description": (
                            "검색 범위 카테고리. "
                            "server=서버 전체(CPU/MEM/FS/포트/연결/프로세스/로그 포함), "
                            "log=로그 전용(server_logs/server_logs_chunked/errpt), "
                            "network=네트워크 장비, alert=장애 이벤트, "
                            "infra=자산 통합 프로필, "
                            "vmware=VMware 하이퍼바이저/VM 이상징후 이력. "
                            "로그 질의 시 categories=[\"server\"] 또는 [\"log\"] 사용. "
                            "VMware 이력 질의 시 categories=[\"vmware\"] 사용. "
                            "미지정 시 전체 카테고리 검색."
                        )
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "반환할 최대 결과 수. 기본값 10."
                    },
                    "days_ago": {
                        "type": "integer",
                        "description": (
                            "최근 N일 이내 데이터만 검색합니다. "
                            "예: '최근 2일' → days_ago=2, '지난 7일' → days_ago=7. "
                            "기간이 명시된 경우 반드시 설정하세요. 미지정 시 전체 기간 검색."
                        )
                    },
                    "hours_ago": {
                        "type": "integer",
                        "description": (
                            "최근 N시간 이내 데이터만 검색합니다. days_ago보다 우선 적용됩니다. "
                            "예: '최근 6시간' → hours_ago=6, '최근 12시간' → hours_ago=12."
                        )
                    }
                },
                "required": ["search_query"]
            }
        }
    },
    # ─── 14-3. VMware 하이퍼바이저/VM 조회 ───
    {
        "type": "function",
        "function": {
            "name": "get_vmware",
            "description": (
                "VMware 하이퍼바이저(물리장비) 및 VM(가상머신) 상태를 조회합니다. "
                "두 가지 모드를 지원합니다: "
                "realtime=Zabbix DB 직접 조회(현재 CPU/메모리/상태/데이터스토어/스냅샷 등 실시간 지표), "
                "anomaly=RAG DB(embeddings_vmware) 이상징후 이력 조회(CPU급변/메모리급변 등). "
                "동기화/적재가 없으면 0건일 수 있음 — 그 경우 서버는 실시간 VM CPU 상위로 대체할 수 있음. "
                "사용 예: 'VMware 현재 상태', '하이퍼바이저 목록', 'VM 특이사항', "
                "'poweredOff VM', '스냅샷 다수 VM', '데이터스토어 여유', 'Balloon 메모리 발생'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "자산명/IP/hostid 또는 'all'. 기본값 'all'."
                    },
                    "vmware_type": {
                        "type": "string",
                        "enum": ["all", "Hypervisor", "VM"],
                        "description": "조회 대상 분류. 'Hypervisor'=물리장비, 'VM'=가상머신, 'all'=전체. 기본값 'all'."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["realtime", "anomaly"],
                        "description": (
                            "조회 모드. "
                            "'realtime'=현재 실시간 지표 조회(현재상태/목록/전체 질의 시 사용), "
                            "'anomaly'=이상징후 이력 조회(특이사항/이상/급변 질의 시 사용). "
                            "기본값 'realtime'."
                        )
                    },
                    "priority": {
                        "type": "integer",
                        "description": "이상 심각도 필터(anomaly 전용). 0=전체, 1=Warning이상, 2=Critical만. 기본값 0."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 반환 건수. 기본값 20."
                    },
                    "days_ago": {
                        "type": "integer",
                        "description": (
                            "최근 N일 이내 데이터 조회(anomaly 전용). 기본값 1. "
                            "'지난 1주일/7일' 질의는 반드시 7로 설정."
                        )
                    },
                    "keyword": {
                        "type": "string",
                        "description": "anomaly_reason 또는 metric_summary 내 검색어(anomaly 전용). 예: 'CPU급변', 'Balloon', '스냅샷'"
                    }
                },
                "required": []
            }
        }
    },

    # ─── 15. 로그 통계 집계 (integrated_report 직접 조회) ───
    {
        "type": "function",
        "function": {
            "name": "get_log_stats",
            "description": (
                "embeddings_server_logs_chunked 테이블의 integrated_report를 직접 집계하여 "
                "특정 기간 내 로그 발생 통계를 반환합니다. "
                "오늘/특정 기간의 총 로그 건수, 심각도 분포, 발생원(source)별 빈도, "
                "시간대별 분포, 가장 많이 발생한 로그 유형을 알고 싶을 때 사용합니다. "
                "사용 예: '오늘 로그 몇 건?', '어떤 로그가 가장 많이 발생?', "
                "'최근 6시간 로그 통계', '로그 발생 빈도 분석'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "서버 식별 키워드. 전체 서버는 'all'."
                    },
                    "hours_ago": {
                        "type": "integer",
                        "description": (
                            "최근 N시간 이내 데이터만 집계합니다. "
                            "예: '최근 6시간' → hours_ago=6, '오늘' → hours_ago=24. "
                            "기본값 24 (오늘 하루)."
                        )
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "상위 N개 발생원/시간대 반환. 기본값 10."
                    }
                },
                "required": ["host_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_events",
            "description": (
                "멀티 시그널 상관 분석: alerts, 서버 CPU/메모리 상태, 로그를 동시에 조회하여 "
                "시간 축 기반 상관관계를 분석합니다. "
                "장애 원인 분석, 종합 점검, 이상 징후 파악 시 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_query": {
                        "type": "string",
                        "description": "분석 대상 호스트명 또는 'all' (전체). 예: 'Zabbix server', 'all'"
                    },
                    "days_ago": {
                        "type": "integer",
                        "description": "분석 기간 (일 단위). 기본값 1 (오늘). 최대 30."
                    },
                    "signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "수집할 신호 종류: ['alert','cpu','memory','log'] 중 선택. 기본: 전체."
                    }
                },
                "required": []
            }
        }
    },
]


# ============================================================
# Tool 이름 → 정의 매핑 (빠른 조회용)
# ============================================================
TOOL_MAP: Dict[str, Dict] = {
    t["function"]["name"]: t["function"]
    for t in TOOL_DEFINITIONS
}

# Tool 이름 목록
TOOL_NAMES: List[str] = list(TOOL_MAP.keys())


# ============================================================
# Gemini용 변환 (functionDeclarations 형식)
# ============================================================
def to_gemini_tools() -> List[Dict]:
    """
    OpenAI Function Calling 형식 → Gemini functionDeclarations 형식 변환.
    Gemini는 tools: [{"functionDeclarations": [...]}] 형식을 사용.
    """
    declarations = []
    for tool in TOOL_DEFINITIONS:
        func = tool["function"]
        decl = {
            "name": func["name"],
            "description": func["description"],
            "parameters": func["parameters"]
        }
        declarations.append(decl)

    return [{"functionDeclarations": declarations}]


# ============================================================
# Ollama 시뮬레이션용: Tool 목록을 자연어 프롬프트로 변환
# ============================================================
def to_prompt_description() -> str:
    """
    Tool Calling을 지원하지 않는 모델(Ollama)용으로
    Tool 목록을 자연어 프롬프트로 변환.
    """
    lines = ["사용 가능한 도구 목록:", ""]
    for i, tool in enumerate(TOOL_DEFINITIONS, 1):
        func = tool["function"]
        params = func["parameters"].get("properties", {})
        param_descs = []
        for pname, pinfo in params.items():
            required = pname in func["parameters"].get("required", [])
            req_mark = " (필수)" if required else " (선택)"
            param_descs.append(f"    - {pname}: {pinfo.get('description', '')}{req_mark}")

        lines.append(f"{i}. {func['name']}")
        lines.append(f"   설명: {func['description'][:120]}")
        if param_descs:
            lines.append("   파라미터:")
            lines.extend(param_descs)
        lines.append("")

    lines.append("응답 형식:")
    lines.append('```json')
    lines.append('{"tool": "도구이름", "params": {"파라미터명": "값"}}')
    lines.append('```')
    lines.append("도구가 필요 없으면 바로 최종 답변을 작성하세요.")

    return "\n".join(lines)

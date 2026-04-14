# /app/app/db/tables.py
"""
RAG 임베딩/청킹 테이블 메타데이터 정의 (v2.6 — EMBEDDING_TABLES 항목 합계 23개)

구성:
  1. 서버 인프라 임베딩  (Server)  : 11개
  2. 네트워크 장비 임베딩 (Network) :  2개
  3. 장애/이벤트 임베딩  (Alert)   :  1개
  4. 청킹 및 통계 임베딩 (Chunked) :  7개
  5. 자산 통합 / 프로세스 트렌드    :  2개
                               합계 : 23개

[v2.5 변경]
- embeddings_inventory 테이블 삭제 (EMBEDDING_TABLES['inventory'], VECTOR_TABLES['inventory'])
  → gen_embeddings_server_devices.py 스크립트 archive 이동
- server_logs_chunked summary_column: 'log_summary' → 'integrated_report' (DDL 컬럼명 일치)

[v2.4 변경]
- server_cpu, server_memory, server_swap, server_filesystem,
  server_interfaces, process_resources:
  UPSERT 전환으로 PK가 (hostid, source_updated_at) 복합키가 됨에 따라
  'multi_row': True, 'data_retention': '3 days' 플래그 추가.
  → vector_search.py의 hostid 기반 중복 제거 및 top_k 확장 검색이 적용됨.
- server_performance description: 1시간 → 30분 단위로 설명 업데이트.

[v2.3 변경]
- embeddings_network_interfaces: PK 변경 (uid → uid, source_updated_at)
  3일간 포트별 이력 누적 저장, type/stability_level/priority 컬럼 추가
  'data_retention' 필드 추가, 'multi_row' 플래그 추가 (중복 제거 필요 표시)
- embeddings_network_performance_chunked: time_bucket 30분 단위로 변경,
  date_partition/hour_partition GENERATED ALWAYS 컬럼으로 변경, 보관주기 30일

[v2.2 변경]
- server_logs, server_logs_chunked: categories=['log'] 로도 검색 가능하도록
  'categories' 필드 추가 (기본 category='server' 유지, 다중 카테고리 지원)
- server_errpt, server_errpt_chunked 동일 적용
"""

# ============================================================
# EMBEDDING_TABLES — 벡터 검색 대상 (vector_search.py 에서 사용)
# ============================================================
EMBEDDING_TABLES = {

    # ── 1. 서버 인프라 임베딩 (Server Category) ── 11개
    'hosts': {
        'table': 'embeddings_hosts',
        'summary_column': 'host_summary',
        'category': 'server',
        'description': '서버 기본 정보, OS, 가동시간 및 서버상태',
    },
    'server_cpu': {
        'table': 'embeddings_server_cpu',
        'summary_column': 'cpu_summary',
        'category': 'server',
        'description': '실시간 CPU 코어별 사용률, 시스템/유저 부하 및 변동성 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
    },
    'server_memory': {
        'table': 'embeddings_server_memory',
        'summary_column': 'memory_summary',
        'category': 'server',
        'description': '실시간 물리 메모리 사용량, 가용량 및 메모리 사용률 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
    },
    'server_swap': {
        'table': 'embeddings_server_swap',
        'summary_column': 'swap_summary',
        'category': 'server',
        'description': '스왑 메모리 사용률 및 리소스 부족 징후 모니터링 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
    },
    'server_filesystem': {
        'table': 'embeddings_server_filesystems',
        'summary_column': 'fs_summary',
        'category': 'server',
        'description': '마운트된 파일시스템별 용량 현황 및 디스크 사용률 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, fs_name, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
        'sub_key': 'fs_name',        # hostid 외 추가 서브키 (마운트 포인트별 중복 제거)
    },
    'server_interfaces': {
        'table': 'embeddings_server_interfaces',
        'summary_column': 'interface_summary',
        'category': 'server',
        'description': '서버 네트워크 인터페이스(NIC) 트래픽 및 물리 상태 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, interface_name, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
        'sub_key': 'interface_name', # hostid 외 추가 서브키 (인터페이스명별 중복 제거)
    },
    'server_listen_ports': {
        'table': 'embeddings_server_listen_ports',
        'summary_column': 'port_summary',
        'category': 'server',
        'description': '현재 서버에서 리슨(Listen) 중인 포트 및 서비스 정보',
    },
    'server_connections': {
        'table': 'embeddings_server_connections',
        'summary_column': 'connection_summary',
        'category': 'server',
        'description': '외부/내부 IP 간의 실시간 세션 및 통신 방향(In/Out)',
    },
    'process_resources': {
        'table': 'embeddings_process_resources',
        'summary_column': 'process_summary',
        'category': 'server',
        'description': '개별 프로세스(PID)별 리소스 점유율 및 실행 명령어 (3일 누적 이력)',
        'multi_row': True,           # PK(hostid, pid, source_updated_at) — 3일 누적 UPSERT
        'data_retention': '3 days',
        'sub_key': 'pid',            # hostid 외 추가 서브키 (PID별 중복 제거)
    },
    'server_logs': {
        'table': 'embeddings_server_logs',
        'summary_column': 'log_summary',
        'category': 'server',
        'categories': ['server', 'log'],   # 'log' 카테고리로도 검색 가능
        'description': '/var/log/messages 등 시스템 로그 텍스트 데이터',
    },
    'server_errpt': {
        'table': 'embeddings_server_errpt',
        'summary_column': 'errpt_summary',
        'category': 'server',
        'categories': ['server', 'log'],   # 'log' 카테고리로도 검색 가능
        'description': 'AIX 시스템 전용 하드웨어/소프트웨어 에러 로그 정보',
    },

    # ── 2. 네트워크 장비 임베딩 (Network Category) ── 2개
    'network_devices': {
        'table': 'embeddings_network_devices',
        'summary_column': 'device_summary',
        'category': 'network',
        'description': '네트워크 장비(스위치 등) 모델, SNMP 상태, 리소스 요약',
    },
    'network_interfaces': {
        'table': 'embeddings_network_interfaces',
        'summary_column': 'interface_summary',
        'category': 'network',
        'description': 'L2/L3 스위치 개별 포트의 Link 상태, 대역폭 사용률, 안정성 진단 (3일 이력 누적)',
        'multi_row': True,           # PK(uid, source_updated_at) — 동일 포트가 시간대별 여러 행 존재
        'data_retention': '3 days',  # 보관주기 3일
    },

    # ── 3. 장애 및 이벤트 임베딩 (Alert Category) ── 1개
    'alert_events': {
        'table': 'embeddings_alert_events',
        'summary_column': 'alert_summary',
        'category': 'alert',
        'description': '실시간 장애 발생 및 해결 상태 (Upsert 기반 최신 상태)',
    },

    # ── 4. 청킹 및 통계 임베딩 (Chunked Category) ── 7개
    'server_performance': {
        'table': 'embeddings_server_performance_chunked',
        'summary_column': 'performance_summary',
        'category': 'server',
        'description': '30분 단위 서버 리소스(CPU, MEM, FS) 통합 통계',
    },
    'server_logs_chunked': {
        'table': 'embeddings_server_logs_chunked',
        'summary_column': 'integrated_report',   # DDL 실제 컬럼명
        'category': 'server',
        'categories': ['server', 'log'],   # 'log' 카테고리로도 검색 가능
        'description': '시간별 로그 발생 빈도 및 주요 로그 샘플 통합 청크',
    },
    'server_listen_ports_chunked': {
        'table': 'embeddings_server_listen_ports_chunked',
        'summary_column': 'port_summary',
        'category': 'server',
        'description': '서버별 전체 오픈 포트 목록 및 변경 이력 관리',
    },
    'server_connections_chunked': {
        'table': 'embeddings_server_connections_chunked',
        'summary_column': 'connection_summary',
        'category': 'server',
        'description': '자산별 주요 통신 서비스 및 연결 개수 집계 요약',
    },
    'network_performance': {
        'table': 'embeddings_network_performance_chunked',
        'summary_column': 'network_summary',
        'category': 'network',
        'description': '네트워크 장비 가동률 및 주요 트래픽 포트 Top-N 요약 (30분 단위 청킹)',
        'data_retention': '30 days',  # 보관주기 30일
    },
    'alerts_chunked': {
        'table': 'embeddings_alerts_chunked',
        'summary_column': 'alert_summary',
        'category': 'alert',
        'description': '장애 이력 보존(발생~해결) 및 지속 시간 통계 분석용',
    },
    'server_errpt_chunked': {
        'table': 'embeddings_server_errpt_chunked',
        'summary_column': 'errpt_summary',
        'category': 'server',
        'categories': ['server', 'log'],   # 'log' 카테고리로도 검색 가능
        'description': 'AIX 시스템의 에러 로그(ERRPT)를 시간대별(1h)로 집계하여 장애 징후 및 반복 결함 요약',
    },

    # ── 5. 자산 통합 요약 및 프로세스 트렌드 ── 2개
    'infra_inventory': {
        'table': 'embeddings_infra_inventory_chunked',
        'summary_column': 'inventory_summary',
        'category': 'infra',
        'description': '서버 사양, 스토리지 현황, 오픈 포트 목록, 최근 7일간의 장애 이력을 통합한 자산 마스터 프로필',
    },
    'process_topn': {
        'table': 'embeddings_server_process_topn_chunked',
        'summary_column': 'process_summary',
        'category': 'server',
        'description': '시간대별(1h) CPU 및 메모리 사용량 상위 10개 프로세스 정보 및 리소스 점유 트렌드 분석',
    },

    # ── 6. VMware 가상화 인프라 (VMware) ── 2개
    'vmware': {
        'table': 'embeddings_vmware',
        'summary_column': 'metric_summary',
        'category': 'vmware',
        'description': 'VMware 하이퍼바이저/VM 이상징후 원시 임베딩 — CPU 급변, 메모리 급변, 상태 이상, 데이터스토어 여유 부족, Balloon 메모리, 스냅샷 다수 등 건별 감지 레코드',
    },
    'vmware_chunked': {
        'table': 'embeddings_vmware_chunked',
        'summary_column': 'integrated_report',
        'category': 'vmware',
        'description': 'VMware 하이퍼바이저/VM 30분 단위 통합 청킹 — 이상징후 누적 요약, 클러스터/데이터센터 토폴로지, 성능 지표 스냅샷 포함',
    },
}


# ============================================================
# VECTOR_TABLES — 통계/가중치 (main.py, stats.py 등에서 사용)
# ============================================================
VECTOR_TABLES = {

    # ── 1. 서버 인프라 (Server) ── 11개
    "hosts": {
        "name": "embeddings_hosts",
        "weight": 1.0,
        "summary_field": "host_summary",
        "key_fields": ["hostid", "asset", "ip", "os", "is_active"],
    },
    "cpu": {
        "name": "embeddings_server_cpu",
        "weight": 0.9,
        "summary_field": "cpu_summary",
        "key_fields": ["hostid", "cpu_usage", "load_avg"],
    },
    "memory": {
        "name": "embeddings_server_memory",
        "weight": 0.9,
        "summary_field": "memory_summary",
        "key_fields": ["hostid", "mem_usage", "available_mem"],
    },
    "swap": {
        "name": "embeddings_server_swap",
        "weight": 0.7,
        "summary_field": "swap_summary",
        "key_fields": ["hostid", "swap_usage", "volatility"],
    },
    "filesystems": {
        "name": "embeddings_server_filesystems",
        "weight": 0.8,
        "summary_field": "fs_summary",
        "key_fields": ["hostid", "fs_name", "used_percent"],
    },
    "server_interfaces": {
        "name": "embeddings_server_interfaces",
        "weight": 0.7,
        "summary_field": "interface_summary",
        "key_fields": ["hostid", "interface", "traffic_in_out"],
    },
    "listen_ports": {
        "name": "embeddings_server_listen_ports",
        "weight": 0.8,
        "summary_field": "port_summary",
        "key_fields": ["hostid", "port", "protocol", "state"],
    },
    "connections": {
        "name": "embeddings_server_connections",
        "weight": 0.8,
        "summary_field": "connection_summary",
        "key_fields": ["hostid", "service", "source_ip", "dest_ip"],
    },
    "processes": {
        "name": "embeddings_process_resources",
        "weight": 0.8,
        "summary_field": "process_summary",
        "key_fields": ["hostid", "pid", "process_name", "cpu_mem"],
    },
    "server_logs": {
        "name": "embeddings_server_logs",
        "weight": 0.9,
        "summary_field": "log_summary",
        "key_fields": ["hostid", "log_level", "source", "message"],
    },
    "server_errpt": {
        "name": "embeddings_server_errpt",
        "weight": 0.9,
        "summary_field": "errpt_summary",
        "key_fields": ["hostid", "error_id", "resource_name"],
    },

    # ── 2. 네트워크 장비 (Network) ── 2개
    "network_devices": {
        "name": "embeddings_network_devices",
        "weight": 1.0,
        "summary_field": "device_summary",
        "key_fields": ["hostid", "asset_name", "snmp_status", "uptime"],
    },
    "network_interfaces": {
        "name": "embeddings_network_interfaces",
        "weight": 0.9,
        "summary_field": "interface_summary",
        "key_fields": ["hostid", "uid", "link", "type", "stability_level", "priority", "received_rate", "sent_rate"],
    },

    # ── 3. 장애/이벤트 (Alert) ── 1개
    "alerts": {
        "name": "embeddings_alert_events",
        "weight": 1.0,
        "summary_field": "alert_summary",
        "key_fields": ["triggerid", "hostid", "priority", "event_name", "flag"],
    },

    # ── 4. 청킹 및 통계 (Chunked) ── 7개
    "server_performance_chunked": {
        "name": "embeddings_server_performance_chunked",
        "weight": 0.9,
        "summary_field": "performance_summary",
        "key_fields": ["hostid", "time_bucket", "cpu_metrics", "mem_metrics"],
    },
    "server_logs_chunked": {
        "name": "embeddings_server_logs_chunked",
        "weight": 0.8,
        "summary_field": "integrated_report",   # DDL 실제 컬럼명
        "key_fields": ["hostid", "time_bucket", "log_metrics", "log_samples"],
    },
    "listen_ports_chunked": {
        "name": "embeddings_server_listen_ports_chunked",
        "weight": 0.7,
        "summary_field": "port_summary",
        "key_fields": ["hostid", "port_metrics", "port_details"],
    },
    "connections_chunked": {
        "name": "embeddings_server_connections_chunked",
        "weight": 0.7,
        "summary_field": "connection_summary",
        "key_fields": ["asset", "connection_metrics", "last_seen_at"],
    },
    "network_performance_chunked": {
        "name": "embeddings_network_performance_chunked",
        "weight": 0.9,
        "summary_field": "network_summary",
        "key_fields": ["hostid", "time_bucket", "interface_summary", "device_info"],
    },
    "alerts_chunked": {
        "name": "embeddings_alerts_chunked",
        "weight": 1.0,
        "summary_field": "alert_summary",
        "key_fields": ["triggerid", "event_type", "duration_text", "time_bucket"],
    },
    "server_errpt_chunked": {
        "name": "embeddings_server_errpt_chunked",
        "weight": 0.9,
        "summary_field": "errpt_summary",
        "key_fields": ["hostid", "time_bucket", "errpt_metrics", "errpt_samples"],
    },

    # ── 5. 자산 통합 / 프로세스 트렌드 ── 2개
    "infra_inventory": {
        "name": "embeddings_infra_inventory_chunked",
        "weight": 1.0,
        "summary_field": "inventory_summary",
        "key_fields": ["hostid", "asset_name", "ip", "os", "port_list", "alert_history"],
    },
    "process_topn": {
        "name": "embeddings_server_process_topn_chunked",
        "weight": 0.9,
        "summary_field": "process_summary",
        "key_fields": ["hostid", "time_bucket", "top_cpu_procs", "top_mem_procs", "resource_usage"],
    },

    # ── 6. VMware 가상화 인프라 (VMware) ── 2개
    "vmware": {
        "name": "embeddings_vmware",
        "weight": 0.9,
        "summary_field": "metric_summary",
        "key_fields": ["hostid", "vmware_type", "priority", "anomaly_reason", "cluster_name"],
    },
    "vmware_chunked": {
        "name": "embeddings_vmware_chunked",
        "weight": 0.9,
        "summary_field": "integrated_report",
        "key_fields": ["hostid", "vmware_type", "time_bucket", "max_priority", "anomaly_summary"],
    },
}

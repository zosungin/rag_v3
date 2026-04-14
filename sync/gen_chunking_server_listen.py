#!/usr/bin/env python3
import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

import psycopg2
from psycopg2.extras import RealDictCursor, Json
import requests
from datetime import datetime, timezone, timedelta

# 설정 정보

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def main():
    print(f"[{datetime.now()}] 🚀 지능형 통합 청킹 시작 (변경 감지 모드)")
    conn = psycopg2.connect(**RAG_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. 대상 호스트 정보 조회
        cur.execute("SELECT hostid, asset, ip as server_ip, asset_name, system_name, is_active FROM embeddings_hosts WHERE is_active = 1")
        hosts = cur.fetchall()

        for host in hosts:
            hostid = host['hostid']
            
            # 2. 현재 오픈된 'Active' 포트 정보 수집 (서비스 명칭 포함)
            cur.execute("""
                SELECT p.listen_port, p.protocol, COALESCE(s.service_name, 'Unknown') as svc, p.last_seen_at
                FROM embeddings_server_listen_ports p
                LEFT JOIN service_port_definition s ON p.listen_port = s.port AND LOWER(p.protocol) = LOWER(s.protocol)
                WHERE p.hostid = %s AND p.status = 'active'
                ORDER BY p.listen_port ASC
            """, (hostid,))
            active_ports = cur.fetchall()

            if not active_ports: continue

            # 상세 리스트 (50개 확장) 및 통계
            port_details = [f"{p['listen_port']}/{p['protocol']}({p['svc']})" for p in active_ports[:50]]
            port_list_str = ", ".join(port_details)
            tcp_cnt = sum(1 for p in active_ports if p['protocol'].lower() == 'tcp')
            udp_cnt = sum(1 for p in active_ports if p['protocol'].lower() == 'udp')
            KST = timezone(timedelta(hours=9))
            max_ts = max(p['last_seen_at'] for p in active_ports)
            max_ts_kst = max_ts.astimezone(KST) if max_ts.tzinfo is not None else max_ts.replace(tzinfo=timezone.utc).astimezone(KST)

            # RAG 요약문 생성
            summary = (
                f"호스트 ID: {hostid} | 자산명: {host['asset_name'] or host['asset']} | IP: {host['server_ip']} | "
                f"현재 오픈 포트: 총 {len(active_ports)}개 (TCP {tcp_cnt}, UDP {udp_cnt}) | "
                f"상세 리스트: {port_list_str} | 최근 확인: {max_ts_kst.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # 3. 💡 변경 감지: 마지막 기록과 비교
            cur.execute("""
                SELECT port_summary, time_bucket 
                FROM embeddings_server_listen_ports_chunked 
                WHERE hostid = %s 
                ORDER BY time_bucket DESC LIMIT 1
            """, (hostid,))
            last_record = cur.fetchone()

            if not last_record or last_record['port_summary'] != summary:
                # 상태 변경됨 -> 신규 이력 생성
                print(f"   - [{host['asset_name'] or hostid}] 상태 변경됨: 새 이력 생성")
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    cur.execute("""
                        INSERT INTO embeddings_server_listen_ports_chunked 
                        (hostid, asset, server_ip, asset_name, system_name, time_bucket, 
                         port_metrics, port_details, port_summary, embedding, record_count, last_seen_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                    """, (hostid, host['asset'], host['server_ip'], host['asset_name'], host['system_name'],
                          datetime.now(), Json({'tcp': tcp_cnt, 'udp': udp_cnt}), Json(port_details),
                          summary, str(emb), len(active_ports), max_ts))
                    conn.commit()
            else:
                # 상태 동일함 -> 관측 시간만 업데이트 (Snapshot 성격 유지)
                print(f"   - [{host['asset_name'] or hostid}] 상태 동일: 시간만 갱신")
                cur.execute("""
                    UPDATE embeddings_server_listen_ports_chunked 
                    SET last_seen_at = %s, updated_at = now()
                    WHERE hostid = %s AND time_bucket = %s
                """, (max_ts, hostid, last_record['time_bucket']))
                conn.commit()

    finally:
        # 보존 기간 초과 데이터 삭제
        try:
            del_cur = conn.cursor()
            retention_days = CONF['RETENTION_DAYS']
            del_cur.execute(f"DELETE FROM embeddings_server_listen_ports_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
            conn.commit()
            del_cur.close()
            print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")
        except Exception:
            pass
        conn.close()
        print(f"[{datetime.now()}] ✅ 동기화 프로세스 종료")

if __name__ == "__main__":
    main()
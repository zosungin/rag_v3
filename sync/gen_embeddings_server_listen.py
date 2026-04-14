#!/usr/bin/env python3
import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, ZABBIX_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from datetime import datetime, timezone, timedelta

# 1. 설정 정보
RETENTION_DAYS_SOURCE = 3 

def get_service_map():
    """RAG DB에서 서비스 명칭 정의 테이블을 읽어와 사전(Dict) 형태로 반환"""
    conn = psycopg2.connect(**RAG_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT port, protocol, service_name FROM service_port_definition")
        return {(r['port'], r['protocol'].lower()): r['service_name'] for r in cur.fetchall()}
    except Exception:
        return {} # 테이블이 없거나 오류 시 빈 사전 반환
    finally:
        cur.close(); conn.close()

def generate_enhanced_summary(row, svc_name):
    """사용자님의 원본 형식에 서비스 명칭을 결합하여 요약문 생성"""
    is_active_txt = "활성" if row['is_active'] == 1 else "비활성"
    status_txt = "열림(Active)" if row['status'] == 'active' else "닫힘(Inactive)"
    
    # 자산명 우선, 없으면 asset 또는 system_name 사용
    display_name = row['asset_name'] or row['asset'] or row['system_name'] or "Unknown Server"
    
    prefix = f"호스트 ID: {row['hostid']} | 장비 상태: {is_active_txt} | 자산명: {display_name} | IP: {row['server_ip']}"
    # 예: TCP 22(SSH)
    body = f"포트 상태: {status_txt} | 서비스: {row['protocol'].upper()} {row['listen_port']}({svc_name}) | 바인딩: {row['bind_ip']} ({row['bind_type']})"

    KST = timezone(timedelta(hours=9))
    if row['last_seen_at'].tzinfo is not None:
        kst_ts = row['last_seen_at'].astimezone(KST)
    else:
        kst_ts = row['last_seen_at'].replace(tzinfo=timezone.utc).astimezone(KST)

    if row['status'] == 'inactive':
        suffix = f"최종 관측시각(닫힌 시간): {kst_ts.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        suffix = f"관측시각: {kst_ts.strftime('%Y-%m-%d %H:%M:%S')}"

    return f"{prefix} | {body} | {suffix}"

def fetch_ports_data():
    """사용자님의 검증된 원본 쿼리를 그대로 사용 (hostname 오류 해결)"""
    conn = psycopg2.connect(**ZABBIX_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
        WITH host_info AS (
            SELECT hostid, ip, asset, asset_name, os, system_name, group_name,
                   CASE WHEN zabbix_agent = '0' THEN 1 ELSE 0 END AS is_active
            FROM svr_system_main
        )
        SELECT
            p.asset, p.protocol, p.listen_port, p.bind_ip, p.hostid,
            h.ip AS server_ip, h.asset_name, h.os, h.system_name, h.group_name,
            p.bind_type, p.ip_version, p.status,
            to_timestamp(p.first_seen) AT TIME ZONE 'UTC' AS first_seen_at,
            to_timestamp(p.last_seen) AT TIME ZONE 'UTC' AS last_seen_at,
            COALESCE(h.is_active, 0) AS is_active,
            p.updated_at AS source_updated_at
        FROM svr_listen_ports p
        LEFT JOIN host_info h ON p.hostid = h.hostid
        """
        cur.execute(query)
        return cur.fetchall()
    finally:
        cur.close(); conn.close()

def main():
    print(f"[{datetime.now()}] 🔄 리슨 포트 동기화 (Service Mapping 통합)")
    svc_map = get_service_map()
    rows = fetch_ports_data()
    
    r_conn = psycopg2.connect(**RAG_DB)
    r_cur = r_conn.cursor()

    try:
        for i, row in enumerate(rows, 1):
            svc_name = svc_map.get((row['listen_port'], row['protocol'].lower()), "Unknown")
            summary = generate_enhanced_summary(row, svc_name)

            print(f"({i}/{len(rows)}) 처리 중: {row['listen_port']}/{row['protocol']} ({row['status']})...", end="\r")

            res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
            if res.status_code == 200:
                emb = res.json().get('embedding')
                if emb:
                    r_cur.execute("""
                        INSERT INTO embeddings_server_listen_ports (
                            asset, protocol, listen_port, bind_ip, hostid, server_ip,
                            os, system_name, group_name, bind_type, ip_version, status,
                            is_active, first_seen_at, last_seen_at, port_summary, embedding, source_updated_at, updated_at
                        ) VALUES (
                            %(asset)s, %(protocol)s, %(listen_port)s, %(bind_ip)s, %(hostid)s, %(server_ip)s,
                            %(os)s, %(system_name)s, %(group_name)s, %(bind_type)s, %(ip_version)s, %(status)s,
                            %(is_active)s, %(first_seen_at)s, %(last_seen_at)s, %(summary)s, %(embedding)s::vector, %(source_updated_at)s, now()
                        )
                        ON CONFLICT (asset, protocol, listen_port, bind_ip) DO UPDATE SET
                            status = EXCLUDED.status,
                            last_seen_at = EXCLUDED.last_seen_at,
                            port_summary = EXCLUDED.port_summary,
                            embedding = EXCLUDED.embedding,
                            updated_at = now();
                    """, {**row, 'summary': summary, 'embedding': str(emb)})
                    r_conn.commit()

        # 유예 기간 삭제
        r_cur.execute(f"DELETE FROM embeddings_server_listen_ports WHERE status = 'inactive' AND last_seen_at < NOW() - INTERVAL '{RETENTION_DAYS_SOURCE} days'")
        r_conn.commit()

    finally:
        r_conn.close()
        print(f"\n[{datetime.now()}] ✅ 임베딩 동기화가 완료되었습니다.")

if __name__ == "__main__":
    main()

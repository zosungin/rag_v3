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

def get_reference_data():
    """RAG DB에서 자산 정보와 서비스 카테고리 정보를 로드"""
    conn = psycopg2.connect(**RAG_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # IP -> 자산명 매핑
        cur.execute("SELECT ip, asset_name FROM embeddings_hosts")
        host_map = {r['ip']: r['asset_name'] for r in cur.fetchall() if r['ip']}
        
        # 서비스 포트 -> 카테고리 포함 이름 매핑
        svc_map = {}
        try:
            cur.execute("SELECT port, protocol, service_name, port_category FROM service_port_definition")
            svc_map = {(r['port'], r['protocol'].lower()): f"{r['service_name']} ({r['port_category']})" for r in cur.fetchall()}
        except Exception:
            conn.rollback()
        return host_map, svc_map
    finally:
        cur.close(); conn.close()

def generate_intelligent_summary(row, host_map, svc_map):
    """자산명 병기 및 동적 포트 제거가 적용된 지능형 요약문 생성"""
    active_txt = "활성" if row['is_active'] == 1 else "비활성"
    
    # 1. IP 자산명 변환 (Source/Target 모두 적용)
    src_ip = str(row['source_ip'])
    tgt_ip = str(row['target_ip'])
    src_display = f"{src_ip}({host_map[src_ip]})" if src_ip in host_map else src_ip
    tgt_display = f"{tgt_ip}({host_map[tgt_ip]})" if tgt_ip in host_map else tgt_ip
    
    # 2. 서비스 정보 구성
    svc_info = svc_map.get((row['target_port'], row['protocol'].lower()), row['service_name'] or "Unknown")
    
    # 3. 연결 흐름 시각화 (동적 소스 포트는 제거하여 벡터 순도 향상)
    conn_flow = f"{src_display} -> {tgt_display}:{row['target_port']} ({row['protocol']})"
    KST = timezone(timedelta(hours=9))
    ts_str = datetime.fromtimestamp(row['clock'], tz=timezone.utc).astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')
    
    return (
        f"호스트 ID: {row['hostid']} | 상태: {active_txt} | 자산명: {row['asset_name'] or row['asset']} | "
        f"방향: {row['direction']} | 서비스: {svc_info} | "
        f"연결 흐름: {conn_flow} | 측정시각: {ts_str}"
    )

def main():
    print(f"[{datetime.now()}] 🔄 지능형 연결 임베딩 동기화 시작")
    host_map, svc_map = get_reference_data()
    
    conn_z = psycopg2.connect(**ZABBIX_DB); conn_r = psycopg2.connect(**RAG_DB)
    z_cur = conn_z.cursor(cursor_factory=RealDictCursor); r_cur = conn_r.cursor(cursor_factory=RealDictCursor)

    try:
        # 💡 [개정] hostid 오류 해결을 위한 asset 기준 JOIN
        z_cur.execute("""
            SELECT c.*, h.hostid, h.asset_name, h.os, h.system_name, h.group_name,
                   CASE WHEN h.zabbix_agent='0' THEN 1 ELSE 0 END as is_active
            FROM svr_connections c
            INNER JOIN svr_system_main h ON c.asset = h.asset
            WHERE c.flag = 'y'
        """)
        rows = z_cur.fetchall()
        print(f"   - [확인] Zabbix 활성 연결 조회 성공: {len(rows)}건")

        processed = 0
        skipped = 0

        for row in rows:
            current_ts = datetime.fromtimestamp(row['clock'], tz=timezone.utc)
            
            # 💡 [개정] 기존 데이터 스킵 로직 검증
            r_cur.execute("SELECT source_updated_at FROM embeddings_server_connections WHERE uid = %s", (row['uid'],))
            existing = r_cur.fetchone()
            
            if existing and existing['source_updated_at'] == current_ts:
                skipped += 1
                continue

            summary = generate_intelligent_summary(row, host_map, svc_map)
            
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=40)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    r_cur.execute("""
                        INSERT INTO embeddings_server_connections (
                            uid, hostid, asset, asset_ip, protocol, local_ip, local_port, 
                            foreign_ip, foreign_port, source_ip, source_port, target_ip, target_port,
                            service_name, direction, flag, os, system_name, group_name,
                            connection_summary, embedding, source_updated_at, updated_at
                        ) VALUES (
                            %(uid)s, %(hostid)s, %(asset)s, %(asset_ip)s, %(protocol)s, %(local_ip)s, %(local_port)s,
                            %(foreign_ip)s, %(foreign_port)s, %(source_ip)s, %(source_port)s, %(target_ip)s, %(target_port)s,
                            %(service_name)s, %(direction)s, %(flag)s, %(os)s, %(system_name)s, %(group_name)s,
                            %(summary)s, %(embedding)s::vector, %(current_ts)s, now()
                        ) ON CONFLICT (uid) DO UPDATE SET
                            flag = EXCLUDED.flag, os = EXCLUDED.os,
                            system_name = EXCLUDED.system_name, group_name = EXCLUDED.group_name,
                            connection_summary = EXCLUDED.connection_summary,
                            embedding = EXCLUDED.embedding, source_updated_at = EXCLUDED.source_updated_at, updated_at = now()
                    """, {**row, 'summary': summary, 'embedding': str(emb), 'current_ts': current_ts})
                    conn_r.commit()
                    processed += 1
            except Exception as e:
                print(f"   ❌ 개별 처리 오류 ({row['uid']}): {e}")
                conn_r.rollback()

        print(f"   - [결과] 신규/갱신: {processed}건, 스킵: {skipped}건")

    finally:
        conn_z.close(); conn_r.close()
        print(f"[{datetime.now()}] ✅ 동기화 프로세스 종료")

if __name__ == "__main__":
    main()
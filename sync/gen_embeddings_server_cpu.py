#!/usr/bin/env python3
import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, ZABBIX_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

import psycopg2
import requests
import time
from datetime import datetime, timezone, timedelta

# --- 1. 설정 및 전략 변수 ---

CONF = {
    'RETENTION_DAYS': 3,
    'CHANGE_LIMIT': 10.0,  
    'CPU_DANGER': 90.0,
    'STD_WARN': 15.0
}

def get_trend_value(cur, hostid, current_ts):
    """최신값 기준 바로 직전(Previous) 수집 데이터 조회"""
    query = "SELECT cpu_utilization FROM svr_cpu_std WHERE hostid = %s AND updated_at < %s ORDER BY updated_at DESC LIMIT 1"
    cur.execute(query, (hostid, current_ts))
    res = cur.fetchone()
    return float(res[0]) if res else None

def determine_stability(util, std):
    """상태 등급 및 우선순위 판별"""
    if util >= CONF['CPU_DANGER']: return "임계치 초과 (위험)", 4
    if std >= CONF['STD_WARN']: return "불안정 (주의)", 3
    return "정상", 0

def generate_summary(d, stability, priority, trend_diff):
    trend_txt = f"{trend_diff:+.2f}%" if trend_diff is not None else "이력 없음"
    
    # [수정] naive/aware 모두 UTC 기준으로 통일 후 KST 변환 → +00:00 suffix 제거 보장
    KST = timezone(timedelta(hours=9))
    if d['ts'].tzinfo is not None:
        kst_ts = d['ts'].astimezone(KST)
    else:
        kst_ts = d['ts'].replace(tzinfo=timezone.utc).astimezone(KST)
        
    ts_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
    
    return (
        f"[{ts_str}] | 상태진단: {stability} (표준편차 {d['std']:.2f}) | 우선순위: {priority} | "
        f"CPU 사용률 {d['util']}% (Idle: {d['idle']}%, 직전 대비 {trend_txt}, {d['cores']}코어) | "
        f"System: {d['sys']}%, User: {d['user']}% | "
        f"호스트 ID: {d['hostid']} | 자산명: {d['asset_name']} | 시스템: {d['system_name']} | "
        f"그룹: {d['group_name']} | IP: {d['ip']} | OS: {d['os']}"
    )

def upsert_to_rag(conn, d, summary, emb, stability, priority):
    """RAG DB Upsert"""
    cur = conn.cursor()
    
    # [수정] DB의 TIMESTAMPTZ 컬럼에 넣기 위해 UTC 타임존 정보 명시
    ts_aware = d['ts'].replace(tzinfo=timezone.utc) if d['ts'].tzinfo is None else d['ts']

    cur.execute("""
        INSERT INTO embeddings_server_cpu (
            hostid, asset, ip, asset_name, os, system_name, group_name, is_active,
            number_of_cpus, cpu_graph_id, cpu_utilization, cpu_idle_time, cpu_system_time, 
            cpu_user_time, cpu_utilization_std, stability_level, priority, cpu_summary, 
            embedding, source_updated_at, updated_at
        ) VALUES (%(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
                  %(cores)s, %(graph_id)s, %(util)s, %(idle)s, %(sys)s, %(user)s, %(std)s, %(stability)s, %(priority)s, 
                  %(summary)s, %(emb)s::vector, %(ts_val)s, now())
        ON CONFLICT (hostid, source_updated_at) DO UPDATE SET
            stability_level = EXCLUDED.stability_level,
            priority = EXCLUDED.priority,
            cpu_summary = EXCLUDED.cpu_summary,
            embedding = EXCLUDED.embedding,
            updated_at = now();
    """, {**d, 'stability': stability, 'priority': priority, 'summary': summary, 'emb': str(emb), 'ts_val': ts_aware})
    conn.commit()
    cur.close()

def main():
    print(f"[{datetime.now()}] 🔄 CPU 동기화 시작")
    try:
        z_conn = psycopg2.connect(**ZABBIX_DB); r_conn = psycopg2.connect(**RAG_DB)
        z_cur = z_conn.cursor()
        
        query = """
        WITH host_info AS (
            SELECT hostid, ip, asset, asset_name, os, system_name, group_name,
                   CASE WHEN zabbix_agent = '0' THEN 1 ELSE 0 END AS is_active
            FROM svr_system_main
            UNION ALL
            SELECT hostid, ip, asset, asset_name, ios AS os, device_name AS system_name, group_name,
                   snmp_agent AS is_active
            FROM nw_main1
        )
        SELECT s.hostid, s.asset, h.ip, h.asset_name, h.os, h.system_name, h.group_name,
               s.number_of_cpus, s.cpu_utilization, s.cpu_utilization_std, 
               s.updated_at,
               COALESCE(h.is_active, 0) as is_active, s.cpu_idle_time, s.cpu_system_time, 
               s.cpu_user_time, s.flag, s.cpu_graph_id
        FROM svr_cpu_std s
        LEFT JOIN host_info h ON s.hostid = h.hostid
        WHERE s.updated_at >= now() - INTERVAL '1 hour'
        """
        z_cur.execute(query)
        columns = [desc[0] for desc in z_cur.description]
        rows = z_cur.fetchall()
        
        for row in rows:
            d = dict(zip(columns, row))
            d['util'], d['std'], d['ts'], d['cores'] = float(d['cpu_utilization'] or 0), float(d['cpu_utilization_std'] or 0), d['updated_at'], d['number_of_cpus']
            d['sys'], d['user'], d['idle'], d['graph_id'] = float(d['cpu_system_time'] or 0), float(d['cpu_user_time'] or 0), float(d['cpu_idle_time'] or 0), d['cpu_graph_id']

            prev_val = get_trend_value(z_cur, d['hostid'], d['ts'])
            trend_diff = (d['util'] - prev_val) if prev_val is not None else None
            
            if (d['util'] >= CONF['CPU_DANGER'] or d['std'] >= CONF['STD_WARN'] or 
                (trend_diff is not None and abs(trend_diff) >= CONF['CHANGE_LIMIT']) or d['flag'] == 'y'):
                
                stability, priority = determine_stability(d['util'], d['std'])
                summary = generate_summary(d, stability, priority, trend_diff)
                
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    upsert_to_rag(r_conn, d, summary, emb, stability, priority)
            else:
                r_cur = r_conn.cursor()
                # [수정] 업데이트 조건 검색 시에도 UTC 객체로 비교
                ts_aware = d['ts'].replace(tzinfo=timezone.utc) if d['ts'].tzinfo is None else d['ts']
                r_cur.execute("UPDATE embeddings_server_cpu SET updated_at = now() WHERE hostid=%s AND source_updated_at=%s", (d['hostid'], ts_aware))
                r_conn.commit(); r_cur.close()

        r_cur = r_conn.cursor()
        r_cur.execute(f"DELETE FROM embeddings_server_cpu WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} day'")
        r_conn.commit(); r_cur.close()
        
    except Exception as e: print(f"❌ 오류: {e}")
    finally:
        if 'z_conn' in locals(): z_conn.close()
        if 'r_conn' in locals(): r_conn.close()
        print(f"[{datetime.now()}] ✅ CPU 동기화 완료")

if __name__ == "__main__":
    main()

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
    'MEM_DANGER': 90.0,
    'STD_WARN': 10.0
}

def get_trend_util(cur, hostid, current_ts):
    """직전 수집 데이터를 조회하여 사용률(%)을 계산"""
    query = """
        SELECT total_memory, available_memory, available_memory_in 
        FROM svr_mem_std 
        WHERE hostid = %s AND updated_at < %s 
        ORDER BY updated_at DESC LIMIT 1
    """
    cur.execute(query, (hostid, current_ts))
    res = cur.fetchone()
    if not res: return None
    
    total, avail, avail_in = res
    # available_memory_in이 있으면 100에서 빼고, 없으면 바이트로 계산
    if avail_in is not None:
        return round(100 - float(avail_in), 2)
    elif total and float(total) > 0:
        return round(100 - (float(avail) / float(total) * 100), 2)
    return None

def determine_stability(util, std):
    if util >= CONF['MEM_DANGER']: return "임계치 초과 (위험)", 4
    if std >= CONF['STD_WARN']: return "불안정 (주의)", 3
    return "정상", 0

def generate_summary(d, stability, priority, trend_diff):
    """[최종 포맷] 메모리 상세 지표 + 추세 + 호스트 정보 통합"""
    trend_txt = f"{trend_diff:+.2f}%" if trend_diff is not None else "이력 없음"
    
    KST = timezone(timedelta(hours=9))
    if d['ts'].tzinfo is not None:
        kst_ts = d['ts'].astimezone(KST)
    else:
        kst_ts = d['ts'].replace(tzinfo=timezone.utc).astimezone(KST)
    ts_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]

    return (
        f"[{ts_str}] | 상태진단: {stability} (표준편차 {d['std']:.2f}) | 우선순위: {priority} | "
        f"메모리 사용률 {d['util']:.2f}% (Idle: {100-d['util']:.2f}%, 직전 대비 {trend_txt}) | "
        f"Total: {d['total_gb']:.2f}GB, Available: {d['avail_gb']:.2f}GB | "
        f"호스트 ID: {d['hostid']} | 자산명: {d['asset_name']} | 시스템: {d['system_name']} | "
        f"그룹: {d['group_name']} | IP: {d['ip']} | OS: {d['os']}"
    )

def upsert_to_rag(conn, d, summary, emb, stability, priority):
    """RAG DB Upsert (컬럼명 available_memory_std 로 수정 완료)"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO embeddings_server_memory (
            hostid, asset, ip, asset_name, os, system_name, group_name, is_active,
            total_memory, available_memory, memory_utilization, available_memory_in, available_memory_std,
            stability_level, priority, memory_summary, embedding, source_updated_at, updated_at
        ) VALUES (%(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
                  %(total_memory)s, %(available_memory)s, %(util)s, %(avail_in)s, %(std)s, %(stability)s, %(priority)s, 
                  %(summary)s, %(emb)s::vector, %(ts)s, now())
        ON CONFLICT (hostid, source_updated_at) DO UPDATE SET
            stability_level = EXCLUDED.stability_level,
            priority = EXCLUDED.priority,
            memory_summary = EXCLUDED.memory_summary,
            embedding = EXCLUDED.embedding,
            updated_at = now();
    """, {**d, 'stability': stability, 'priority': priority, 'summary': summary, 'emb': str(emb)})
    conn.commit()
    cur.close()

def main():
    print(f"[{datetime.now()}] 🔄 메모리 동기화 시작 (사용률 계산 공식 및 컬럼 수정 적용)")
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
               s.total_memory, s.available_memory, s.available_memory_in, 
               COALESCE(s.available_memory_std, 0) as available_memory_std, 
               s.updated_at, COALESCE(h.is_active, 0) as is_active, s.flag
        FROM svr_mem_std s
        LEFT JOIN host_info h ON s.hostid = h.hostid
        WHERE s.updated_at >= now() - INTERVAL '1 hour'
        """
        z_cur.execute(query)
        columns = [desc[0] for desc in z_cur.description]
        rows = z_cur.fetchall()
        
        for row in rows:
            d = dict(zip(columns, row))
            d['ts'] = d['updated_at']
            d['std'] = float(d['available_memory_std'] or 0)
            
            # --- 메모리 사용률(%) 계산 로직 (수정) ---
            total_b = float(d['total_memory'] or 0)
            avail_b = float(d['available_memory'] or 0)
            avail_in = d['available_memory_in']
            
            # 사용률(util) 계산: 100 - available_memory_in
            if avail_in is not None:
                d['util'] = round(100 - float(avail_in), 2)
                d['avail_in'] = float(avail_in)
            elif total_b > 0:
                avail_pct = (avail_b / total_b) * 100
                d['util'] = round(100 - avail_pct, 2)
                d['avail_in'] = round(avail_pct, 2)
            else:
                d['util'] = 0.0
                d['avail_in'] = 100.0
            
            d['total_gb'] = total_b / (1024**3)
            d['avail_gb'] = avail_b / (1024**3)

            # 1. 직전 값 대비 추세 분석
            prev_util = get_trend_util(z_cur, d['hostid'], d['ts'])
            trend_diff = (d['util'] - prev_util) if prev_util is not None else None
            
            # 2. 필터링 (위험/불안정/급변/최신값)
            if (d['util'] >= CONF['MEM_DANGER'] or d['std'] >= CONF['STD_WARN'] or 
                (trend_diff is not None and abs(trend_diff) >= CONF['CHANGE_LIMIT']) or d['flag'] == 'y'):
                
                stability, priority = determine_stability(d['util'], d['std'])
                summary = generate_summary(d, stability, priority, trend_diff)
                
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    upsert_to_rag(r_conn, d, summary, emb, stability, priority)
            else:
                r_cur = r_conn.cursor(); r_cur.execute("UPDATE embeddings_server_memory SET updated_at = now() WHERE hostid=%s AND source_updated_at=%s", (d['hostid'], d['ts']))
                r_conn.commit(); r_cur.close()

        r_cur = r_conn.cursor(); r_cur.execute(f"DELETE FROM embeddings_server_memory WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} day'")
        r_conn.commit(); r_cur.close()
        
    except Exception as e: print(f"❌ 오류: {e}")
    finally:
        if 'z_conn' in locals(): z_conn.close()
        if 'r_conn' in locals(): r_conn.close()
        print(f"[{datetime.now()}] ✅ 메모리 동기화 완료")

if __name__ == "__main__":
    main()

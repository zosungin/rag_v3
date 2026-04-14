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
    'PROC_CPU_DANGER': 50.0,
    'STD_WARN': 10.0
}

def parse_cpu_time_to_seconds(time_str):
    try:
        if not time_str or ':' not in str(time_str): return 0
        parts = str(time_str).split(':')
        if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2: return int(parts[0]) * 60 + int(parts[1])
        return 0
    except Exception: return 0

def get_trend_util(cur, hostid, pid, command, current_clock):
    query = "SELECT cpu FROM resource_sta WHERE hostid = %s AND pid = %s AND command = %s AND clock < %s ORDER BY clock DESC LIMIT 1"
    cur.execute(query, (str(hostid), pid, command, current_clock))
    res = cur.fetchone()
    return float(res[0]) if res and res[0] is not None else None

def determine_stability(cpu, std, flag, prev_cpu):
    """VARCHAR(20) 제한에 맞춰 진단 문자열 최적화 (공백 포함 20자 이내)"""
    if flag == 'n':
        if prev_cpu and prev_cpu >= 30.0:
            return "종료 (직전 고부하)", 3  # 11자
        return "종료 (정상/유휴)", 1        # 9자
    
    if cpu >= CONF['PROC_CPU_DANGER']: 
        return "자원 고점유 (주의)", 4      # 10자
    if std >= CONF['STD_WARN']: 
        return "자원 불안정 (변동)", 2      # 10자
        
    return "실행 중 (정상)", 0             # 8자

def generate_summary(d, stability, priority, trend_diff):
    trend_txt = f"{trend_diff:+.2f}%" if trend_diff is not None else "이력 없음"
    KST = timezone(timedelta(hours=9))
    kst_ts = datetime.fromtimestamp(d['clock'], tz=timezone.utc).astimezone(KST)
    ts_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
    status_icon = "🔵" if d['flag'] == 'y' else "⚪"
    status_text = "Active" if d['flag'] == 'y' else "Terminated"

    return (
        f"[{ts_str}] | 현황: {status_text} | 진단: {stability} | 우선순위: {priority} | "
        f"프로세스명: {d['command']} (PID: {d['pid']}, 계정: {d['user_name']}) | "
        f"자원지표: CPU {d['cpu']:.1f}% (직전대비 {trend_txt}), 메모리 {d['mem']:.1f}% | "
        f"CPU 누적시간: {d['cpu_time_sec']}초 | 호스트: {d['asset_name']} ({d['ip']})"
    )

def upsert_to_rag(conn, d, summary, emb, stability, priority):
    try:
        cur = conn.cursor()
        source_ts = datetime.fromtimestamp(d['clock'])
        params = {**d, 'stability': stability, 'priority': priority, 'summary': summary, 'emb': str(emb), 'source_ts': source_ts, 'cpu_time_val': d['cpu_time_sec']}
        cur.execute("""
            INSERT INTO embeddings_process_resources (
                hostid, pid, source_updated_at, asset, ip, asset_name, os, system_name, group_name, is_active, 
                user_name, command, cpu, cpu_time, mem, mem_used, resource_std, stability_level, priority, 
                process_summary, embedding, updated_at
            ) VALUES (
                %(hostid)s, %(pid)s, %(source_ts)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s, 
                %(user_name)s, %(command)s, %(cpu)s, %(cpu_time_val)s, %(mem)s, %(mem_used)s, %(resource_std)s, 
                %(stability)s, %(priority)s, %(summary)s, %(emb)s::vector, now()
            )
            ON CONFLICT (hostid, pid, source_updated_at) DO UPDATE SET
                stability_level = EXCLUDED.stability_level, priority = EXCLUDED.priority, process_summary = EXCLUDED.process_summary, 
                embedding = EXCLUDED.embedding, updated_at = now();
        """, params)
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        raise e

def main():
    print(f"[{datetime.now()}] 🔄 프로세스 동기화 시작 (VARCHAR 제한 최적화)")
    try:
        z_conn = psycopg2.connect(**ZABBIX_DB); r_conn = psycopg2.connect(**RAG_DB)
        z_cur = z_conn.cursor()
        current_epoch = int(time.time())
        query = f"""
        WITH host_info AS (SELECT hostid, ip, asset, asset_name, os, system_name, group_name, CASE WHEN zabbix_agent = '0' THEN 1 ELSE 0 END AS is_active FROM svr_system_main)
        SELECT s.hostid, s.asset, h.ip, h.asset_name, h.os, h.system_name, h.group_name, s.pid, s.command, s.cpu, s.mem, s.user_name, s.clock, s.cpu_time, s.mem_used, s.resource_std, s.flag, COALESCE(h.is_active, 0) as is_active
        FROM resource_sta s LEFT JOIN host_info h ON s.hostid::bigint = h.hostid WHERE s.clock >= {current_epoch} - 3600
        """
        z_cur.execute(query)
        columns = [desc[0] for desc in z_cur.description]
        rows = z_cur.fetchall()
        print(f"   - 처리 대상 데이터: {len(rows)}건")
        
        for i, row in enumerate(rows, 1):
            d = dict(zip(columns, row))
            d['cpu'] = float(d['cpu'] or 0); d['mem'] = float(d['mem'] or 0); d['resource_std'] = float(d['resource_std'] or 0)
            d['cpu_time_sec'] = parse_cpu_time_to_seconds(d['cpu_time'])
            prev_cpu = get_trend_util(z_cur, d['hostid'], d['pid'], d['command'], d['clock'])
            trend_diff = (d['cpu'] - prev_cpu) if prev_cpu is not None else None
            stability, priority = determine_stability(d['cpu'], d['resource_std'], d['flag'], prev_cpu)
            
            if (priority >= 2 or d['flag'] == 'y' or (trend_diff is not None and abs(trend_diff) >= CONF['CHANGE_LIMIT'])):
                summary = generate_summary(d, stability, priority, trend_diff)
                print(f"   ({i}/{len(rows)}) 처리: {d['hostid']} - {d['command'][:20]}...", end="\r")
                try:
                    res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                    if res.status_code == 200:
                        emb = res.json().get('embedding')
                        upsert_to_rag(r_conn, d, summary, emb, stability, priority)
                except Exception as e: print(f"\n❌ 처리 중 오류 ({d['hostid']}-{d['pid']}): {e}")
            else:
                source_ts = datetime.fromtimestamp(d['clock'])
                r_cur = r_conn.cursor()
                try:
                    r_cur.execute("UPDATE embeddings_process_resources SET updated_at = now() WHERE hostid=%s AND pid=%s AND source_updated_at=%s", (int(d['hostid']), d['pid'], source_ts))
                    r_conn.commit()
                except: r_conn.rollback()
                finally: r_cur.close()

        r_cur = r_conn.cursor(); r_cur.execute(f"DELETE FROM embeddings_process_resources WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} day'"); r_conn.commit(); r_cur.close()
    except Exception as e: print(f"\n❌ 실행 오류: {e}")
    finally:
        if 'z_conn' in locals(): z_conn.close()
        if 'r_conn' in locals(): r_conn.close()
        print(f"\n[{datetime.now()}] ✅ 프로세스 동기화 완료")

if __name__ == "__main__": main()

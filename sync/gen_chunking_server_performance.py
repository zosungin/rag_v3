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
import time
from datetime import datetime, timezone, timedelta

# --- 1. 설정 및 전략 변수 ---

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def to_float(val, default=None):
    if val is None: return default
    try: return round(float(val), 2)
    except: return default

def format_bps(bps):
    """bps 숫자를 가독성 있는 단위(Gbps/Mbps/Kbps/bps)로 변환"""
    if bps is None:
        return "N/A"
    bps = float(bps)
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.2f} Kbps"
    return f"{bps:.2f} bps"

def generate_analyst_report(m, cpu, mem, swap, fs_all, net, stopped_count, bucket_epoch):
    """리포트 텍스트 생성"""
    KST = timezone(timedelta(hours=9))
    bucket_time = datetime.fromtimestamp(float(bucket_epoch), tz=timezone.utc).astimezone(KST)
    prefix = f"호스트 ID: {m['hostid']} | 자산: {m.get('asset_name') or 'Unknown'} | IP: {m.get('ip')} | OS: {m.get('os')}"
    
    cpu_txt = f"CPU(평균 {cpu['avg']}%/최대 {cpu['max']}%)" if cpu['avg'] is not None else "CPU(N/A)"
    mem_txt = f"메모리(평균 {mem['avg']}%/최대 {mem['max']}%)" if mem['avg'] is not None else "메모리(N/A)"
    
    critical_fs = [f"{f['name']}({f['used']}%)" for f in fs_all if f['used'] >= 80]
    fs_txt = f"위험FS:{', '.join(critical_fs)}" if critical_fs else f"FS정상(최대:{max([f['used'] for f in fs_all]+[0]):.1f}%)"

    return (
        f"[{bucket_time.strftime('%Y-%m-%d %H:%M')}] {prefix} | "
        f"현황: {cpu_txt}, {mem_txt}, 스왑({swap['avg']}%), {fs_txt}. "
        f"네트워크: In {format_bps(net['in_avg'])} / Out {format_bps(net['out_avg'])}. "
        f"의견: {stopped_count}건의 프로세스 종료 이력 확인."
    )

def main():
    print(f"[{datetime.now()}] 🚀 통합 성능 청킹 시작 (JSONB 컬럼 매핑 보완)")
    conn = psycopg2.connect(**RAG_DB)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        epoch_sql = "floor(extract(epoch from source_updated_at)/1800)*1800"

        # 1. 대상 구간 및 메타데이터 추출
        cur.execute(f"""
            WITH raw_data AS (
                SELECT hostid, {epoch_sql} as bucket_epoch, asset, ip, asset_name, os, system_name, group_name 
                FROM embeddings_server_cpu
                WHERE source_updated_at >= now() - INTERVAL '12 hours'
            )
            SELECT hostid, bucket_epoch, 
                   max(asset) as asset, max(ip) as ip, max(asset_name) as asset_name, 
                   max(os) as os, max(system_name) as system_name, max(group_name) as group_name
            FROM raw_data GROUP BY 1, 2 ORDER BY 2 DESC;
        """)
        work_list = cur.fetchall()
        print(f"   - 처리 대상 구간: {len(work_list)}건")

        for i, work in enumerate(work_list, 1):
            hostid, b_epoch = work['hostid'], work['bucket_epoch']
            cond = (hostid, float(b_epoch))

            # [지표 집계]
            cur.execute(f"SELECT avg(cpu_utilization) as a, max(cpu_utilization) as mx FROM embeddings_server_cpu WHERE hostid=%s AND {epoch_sql}=%s", cond)
            r = cur.fetchone(); cpu_m = {'avg': to_float(r['a']), 'max': to_float(r['mx'])}

            cur.execute(f"SELECT avg(100-available_memory_in) as a, max(100-available_memory_in) as mx FROM embeddings_server_memory WHERE hostid=%s AND {epoch_sql}=%s", cond)
            r = cur.fetchone(); mem_m = {'avg': to_float(r['a']), 'max': to_float(r['mx'])}

            cur.execute(f"SELECT avg(used_swap_percent) as a FROM embeddings_server_swap WHERE hostid=%s AND {epoch_sql}=%s", cond)
            r = cur.fetchone(); swap_m = {'avg': to_float(r['a'])}

            cur.execute(f"SELECT fs_name as n, used_percent as u FROM embeddings_server_filesystems WHERE hostid=%s AND {epoch_sql}=%s", cond)
            fs_all = [{'name': f['n'], 'used': to_float(f['u'], 0.0)} for f in cur.fetchall()]

            cur.execute(f"SELECT avg(bits_received) as r, avg(bits_sent) as s FROM embeddings_server_interfaces WHERE hostid=%s AND {epoch_sql}=%s", cond)
            r = cur.fetchone(); net_m = {'in_avg': to_float(r['r']), 'out_avg': to_float(r['s'])}

            cur.execute(f"SELECT command, cpu, mem FROM embeddings_process_resources WHERE hostid=%s AND {epoch_sql}=%s ORDER BY cpu DESC LIMIT 5", cond)
            top_procs = [{'cmd': p['command'], 'cpu': float(p['cpu']), 'mem': float(p['mem'])} for p in cur.fetchall()]

            cur.execute(f"SELECT count(*) FROM embeddings_process_resources WHERE hostid=%s AND {epoch_sql}=%s AND stability_level LIKE '종료%%'", cond)
            stopped_cnt = cur.fetchone()['count']

            # 요약문 생성 및 임베딩
            summary = generate_analyst_report(work, cpu_m, mem_m, swap_m, fs_all, net_m, stopped_cnt, b_epoch)
            
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    
                    # 💡 [핵심] DDL에 정의된 모든 JSONB 컬럼을 INSERT 문에 포함
                    cur.execute("""
                        INSERT INTO embeddings_server_performance_chunked (
                            hostid, asset, ip, asset_name, os, system_name, group_name,
                            time_bucket, cpu_metrics, mem_metrics, swap_metrics, fs_metrics, net_metrics, top_processes,
                            performance_summary, embedding, updated_at
                        ) VALUES (
                            %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
                            to_timestamp(%(b_epoch)s), %(cpu)s, %(mem)s, %(swap)s, %(fs)s, %(net)s, %(top)s,
                            %(summary)s, %(emb)s::vector, now()
                        )
                        ON CONFLICT (hostid, time_bucket) DO UPDATE SET
                            cpu_metrics = EXCLUDED.cpu_metrics, mem_metrics = EXCLUDED.mem_metrics,
                            swap_metrics = EXCLUDED.swap_metrics, fs_metrics = EXCLUDED.fs_metrics,
                            net_metrics = EXCLUDED.net_metrics, top_processes = EXCLUDED.top_processes,
                            performance_summary = EXCLUDED.performance_summary, embedding = EXCLUDED.embedding, updated_at = now();
                    """, {
                        'hostid': hostid, 'asset': work['asset'], 'ip': work['ip'], 'asset_name': work['asset_name'],
                        'os': work['os'], 'system_name': work['system_name'], 'group_name': work['group_name'],
                        'b_epoch': float(b_epoch), 
                        'cpu': Json(cpu_m), 'mem': Json(mem_m), 'swap': Json(swap_m), 
                        'fs': Json(fs_all), 'net': Json(net_m), 'top': Json(top_procs),
                        'summary': summary, 'emb': str(emb)
                    })
                    conn.commit()
            except Exception as e: print(f"\n❌ DB/API 오류: {e}")

        print(f"\n[{datetime.now()}] ✅ 통합 청킹 완료 (JSONB 데이터 포함)")
        # 보존 기간 초과 데이터 삭제
        del_cur = conn.cursor()
        retention_days = CONF['RETENTION_DAYS']
        del_cur.execute(f"DELETE FROM embeddings_server_performance_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
        conn.commit()
        del_cur.close()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")
    finally: conn.close()

if __name__ == "__main__":
    main()
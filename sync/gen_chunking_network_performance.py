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
from decimal import Decimal

# --- 1. 설정 및 전략 변수 ---

TOP_N = 5  # 💡 사용자 요청에 따라 TOP 5로 확장 (필요 시 10으로 변경 가능)

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def to_float(val, default=0.0):
    if val is None: return default
    try: return round(float(val), 2)
    except: return default

def convert_decimal(obj):
    if isinstance(obj, list): return [convert_decimal(i) for i in obj]
    elif isinstance(obj, dict): return {k: convert_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal): return float(obj)
    return obj

def format_mb_s(bytes_val):
    val = to_float(bytes_val)
    if val >= 1024 * 1024 * 1024: return f"{val/(1024*1024*1024):.2f} GB/s"
    if val >= 1024 * 1024: return f"{val/(1024*1024):.2f} MB/s"
    return f"{val/1024:.2f} KB/s" if val >= 1024 else f"{val:.2f} B/s"

def generate_master_report(info, net, top_in, top_out, bucket_epoch):
    """장비 종합 분석 보고서 (사용자 피드백 반영 버전)"""
    KST = timezone(timedelta(hours=9))
    bucket_time = datetime.fromtimestamp(float(bucket_epoch), tz=timezone.utc).astimezone(KST)
    prefix = (f"호스트 ID: {info['hostid']} | 자산명: {info['asset_name'] or info['asset']} | IP: {info['ip']} | "
              f"장비명: {info['device_name']} | 그룹: {info['group_name']}")
    
    # 💡 [피드백 1 반영] Down 포트는 장애가 아니므로 진단 키워드에서 제외
    status_msg = "안정적"
    if net['unstable_cnt'] > 0: 
        status_msg = "주의(트래픽 변동성 높음)"
    elif net['up_ports'] == 0 and net['total_ports'] > 0:
        status_msg = "경고(모든 포트 연결 단절)"

    # 현황은 객관적 수치로만 전달
    port_stat = f"포트 현황: 총 {net['total_ports']}개(사용 중 {net['up_ports']}/미연결 {net['down_ports']})"
    
    # 💡 [피드백 2 반영] TOP N 명시 및 개수 확장
    in_list = [f"{p['interface']}({format_mb_s(p['in_avg'])})" for p in top_in if to_float(p['in_avg']) > 0]
    out_list = [f"{p['interface']}({format_mb_s(p['out_avg'])})" for p in top_out if to_float(p['out_avg']) > 0]
    
    in_str = f"수신 TOP {TOP_N}: {', '.join(in_list)}" if in_list else "수신 트래픽 없음"
    out_str = f"송신 TOP {TOP_N}: {', '.join(out_list)}" if out_list else "송신 트래픽 없음"

    return f"[{bucket_time.strftime('%Y-%m-%d %H:%M')}] {prefix} | 진단: {status_msg}. 현황: {port_stat}. 트래픽: {in_str} | {out_str}"

def main():
    print(f"[{datetime.now()}] 🚀 네트워크 통합 청킹 시작 (TOP {TOP_N} 및 진단 로직 보정)")
    conn = psycopg2.connect(**RAG_DB)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        epoch_sql = "floor(extract(epoch from source_updated_at)/1800)*1800"

        cur.execute(f"""
            SELECT hostid, {epoch_sql} as bucket_epoch, 
                   max(asset) as asset, max(ip) as ip, max(asset_name) as asset_name, 
                   max(system_name) as device_name, max(group_name) as group_name
            FROM embeddings_network_interfaces
            WHERE source_updated_at >= now() - INTERVAL '12 hours'
            GROUP BY 1, 2 ORDER BY 2 DESC;
        """)
        work_list = cur.fetchall()

        for work in work_list:
            hostid, b_epoch = work['hostid'], work['bucket_epoch']
            cond = (hostid, float(b_epoch))

            # 중복 제거 집계
            cur.execute(f"""
                WITH latest_port_status AS (
                    SELECT DISTINCT ON (interface) interface, stability_level
                    FROM embeddings_network_interfaces
                    WHERE hostid = %s AND {epoch_sql} = %s
                    ORDER BY interface, source_updated_at DESC
                )
                SELECT COUNT(*) as total_ports,
                       COUNT(CASE WHEN stability_level LIKE 'Link Up%%' THEN 1 END) as up_ports,
                       COUNT(CASE WHEN stability_level LIKE 'Link Down%%' THEN 1 END) as down_ports,
                       COUNT(CASE WHEN stability_level LIKE '%%불안정%%' THEN 1 END) as unstable_cnt
                FROM latest_port_status
            """, cond)
            net_m = convert_decimal(cur.fetchone())

            # 💡 [수정] TOP_N 상위 트래픽 추출
            cur.execute(f"SELECT interface, avg(received_bits) as in_avg FROM embeddings_network_interfaces WHERE hostid=%s AND {epoch_sql}=%s GROUP BY 1 ORDER BY 2 DESC LIMIT %s", (hostid, float(b_epoch), TOP_N))
            top_in = convert_decimal(cur.fetchall())
            cur.execute(f"SELECT interface, avg(sent_bits) as out_avg FROM embeddings_network_interfaces WHERE hostid=%s AND {epoch_sql}=%s GROUP BY 1 ORDER BY 2 DESC LIMIT %s", (hostid, float(b_epoch), TOP_N))
            top_out = convert_decimal(cur.fetchall())

            summary = generate_master_report(work, net_m, top_in, top_out, b_epoch)
            
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    cur.execute("""
                        INSERT INTO embeddings_network_performance_chunked (
                            hostid, asset, asset_name, device_name, ip, group_name,
                            time_bucket, date_partition, hour_partition, interface_summary, network_summary, embedding, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s)::date, extract(hour from to_timestamp(%s))::smallint, %s, %s, %s::vector, now())
                        ON CONFLICT (hostid, time_bucket) DO UPDATE SET
                            network_summary = EXCLUDED.network_summary,
                            embedding = EXCLUDED.embedding,
                            updated_at = now();
                    """, (hostid, work['asset'], work['asset_name'], work['device_name'], work['ip'], work['group_name'],
                          float(b_epoch), float(b_epoch), float(b_epoch), Json({'stats': net_m}), summary, str(emb)))
                    conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"❌ 오류: {e}")

    finally:
        # 보존 기간 초과 데이터 삭제
        try:
            del_cur = conn.cursor()
            retention_days = CONF['RETENTION_DAYS']
            del_cur.execute(f"DELETE FROM embeddings_network_performance_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
            conn.commit()
            del_cur.close()
            print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")
        except Exception:
            pass
        conn.close()
    print(f"\n[{datetime.now()}] ✅ 최적화 청킹 완료")

if __name__ == "__main__": main()
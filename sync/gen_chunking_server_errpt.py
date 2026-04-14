#!/usr/bin/env python3
"""
Phase B2: AIX ERRPT 로그 통합 청킹 스크립트 (파티션 컬럼 대응 버전)
- 수정내역: 
  1. date_partition, hour_partition 컬럼 데이터 삽입 로직 추가
  2. INSERT 문에 missing columns (os, date_partition, hour_partition) 보강
"""
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

# 1. 데이터베이스 및 API 설정

CONF = {
    'RETENTION_DAYS': 30
}

def generate_errpt_summary(info, metrics, samples, bucket_time):
    """표준화된 ERRPT 통합 요약 생성"""
    active_txt = "활성" if info['is_active'] == 1 else "비활성"
    prefix = f"호스트 ID: {info['hostid']} | 상태: {active_txt} | 자산명: {info['asset_name'] or info['asset']} | IP: {info['ip']}"
    metric_str = f"Critical {metrics.get('critical', 0)}건, Warning {metrics.get('warning', 0)}건"
    
    sample_list = []
    for s in samples[:3]:
        sample_list.append(f"[{s['level']}] {s['resource']}: {s['desc'][:100]}")
    sample_str = " | ".join(sample_list) if sample_list else "내용 없음"
    
    body = (f"그룹: {info['group_name']} | 현황: 총 {info['total_count']}건 ({metric_str}) | 대표 로그: {sample_str}")
    
    KST = timezone(timedelta(hours=9))
    bucket_kst = bucket_time.astimezone(KST) if bucket_time.tzinfo is not None else bucket_time.replace(tzinfo=timezone.utc).astimezone(KST)
    suffix = f"측정시각: {bucket_kst.strftime('%Y-%m-%d %H:00:00')}"
    
    return f"{prefix} | {body} | {suffix}"

def sync_with_host_status(conn):
    """마스터 테이블과 활성 상태 동기화"""
    cur = conn.cursor()
    try:
        print("\n🔄 마스터 장비 상태와 ERRPT 청킹 데이터 동기화 중...")
        query = """
        UPDATE embeddings_server_errpt_chunked t
        SET is_active = h.is_active, updated_at = now()
        FROM embeddings_hosts h
        WHERE t.hostid = h.hostid AND t.is_active != h.is_active;
        """
        cur.execute(query)
        affected = cur.rowcount
        conn.commit()
        if affected > 0: print(f"   ✅ {affected}건 상태 동기화 완료.")
    finally: cur.close()

def main():
    print(f"[{datetime.now()}] 🔄 AIX ERRPT 통합 청킹 시작")
    
    conn = psycopg2.connect(**RAG_DB)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 1. 집계 대상 추출 (os 컬럼 포함 확인)
        cur.execute("""
            SELECT hostid, asset, asset_name, ip, group_name, is_active, 
                   COALESCE(os, 'AIX') as os,
                   date_trunc('hour', created_at) as bucket,
                   count(*) as total_count
            FROM embeddings_server_errpt
            GROUP BY 1,2,3,4,5,6,7,8
            ORDER BY bucket DESC;
        """)
        groups = cur.fetchall()

        total = len(groups)
        for i, group in enumerate(groups, 1):
            hostid, bucket = group['hostid'], group['bucket']
            
            # 파티션 값 계산
            date_part = bucket.date()
            hour_part = bucket.hour

            cur.execute("SELECT record_count FROM embeddings_server_errpt_chunked WHERE hostid = %s AND time_bucket = %s", (hostid, bucket))
            existing = cur.fetchone()
            if existing and int(existing['record_count']) == int(group['total_count']):
                continue

            # 2. 로그 상세 수집 (type 컬럼 사용)
            cur.execute("""
                SELECT type, resource_name as res, description as desc
                FROM embeddings_server_errpt
                WHERE hostid = %s AND date_trunc('hour', created_at) = %s
            """, (hostid, bucket))
            logs = cur.fetchall()

            metrics = {'critical': 0, 'warning': 0, 'info': 0}
            samples = []
            for l in logs:
                raw_type = l['type'].upper() if l['type'] else 'U'
                if raw_type == 'P':
                    lvl_label, key = "Critical", "critical"
                elif raw_type == 'T':
                    lvl_label, key = "Warning", "warning"
                else:
                    lvl_label, key = "Info", "info"
                
                metrics[key] += 1
                if len(samples) < 3:
                    samples.append({'level': lvl_label, 'resource': l['res'], 'desc': l['desc']})

            summary_text = generate_errpt_summary(group, metrics, samples, bucket)
            print(f"({i}/{total}) 처리 중: {group['asset_name']} ({bucket})...", end="\r")

            # 3. 임베딩 생성
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary_text}, timeout=60)
                embedding = res.json().get('embedding') if res.status_code == 200 else None
            except: embedding = None
            
            if embedding:
                cur.execute("""
                    INSERT INTO embeddings_server_errpt_chunked (
                        hostid, asset, ip, asset_name, os, group_name, is_active,
                        time_bucket, date_partition, hour_partition,
                        errpt_metrics, errpt_samples, errpt_summary, embedding, 
                        record_count, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, now())
                    ON CONFLICT (hostid, time_bucket) DO UPDATE SET
                        is_active = EXCLUDED.is_active,
                        errpt_metrics = EXCLUDED.errpt_metrics,
                        errpt_samples = EXCLUDED.errpt_samples,
                        errpt_summary = EXCLUDED.errpt_summary,
                        embedding = EXCLUDED.embedding,
                        record_count = EXCLUDED.record_count,
                        updated_at = now();
                """, (
                    hostid, group['asset'], group['ip'], group['asset_name'], group['os'], group['group_name'], group['is_active'],
                    bucket, date_part, hour_part,
                    Json(metrics), Json(samples), summary_text, embedding, group['total_count']
                ))
                conn.commit()

        sync_with_host_status(conn)
        
        # 4. 보존 기간 초과 데이터 삭제
        del_cur = conn.cursor()
        del_cur.execute(f"DELETE FROM embeddings_server_errpt_chunked WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} days'")
        conn.commit()
        print(f"\n[{datetime.now()}] ✅ AIX ERRPT 통합 청킹 완료.")

    except Exception as e:
        if conn: conn.rollback()
        print(f"\n❌ 오류 발생: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    main()

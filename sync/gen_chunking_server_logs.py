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

# [설정]

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def get_latest_metric(cur, table, hostid, ts_end, col):
    """데이터 부재 시 최신값 보정"""
    cur.execute(
        f"SELECT {col} FROM {table} WHERE hostid=%s AND source_updated_at < %s "
        f"ORDER BY source_updated_at DESC LIMIT 1",
        (hostid, ts_end)
    )
    res = cur.fetchone()
    return float(res[col] or 0) if res else 0

def main():
    print(f"[{datetime.now()}] 🚀 통합 청킹 최종 동기화 실행 (장비 유형 분류 포함)")
    conn = psycopg2.connect(**RAG_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # ✅ [핵심 수정] 타임라인 쿼리에서 device_type 함께 조회
        # embeddings_server_logs.device_type 은 임베딩 동기화 시 이미 분류되어 저장된 값을 사용
        # 동일 hostid 내 여러 device_type 이 혼재할 경우 최빈값(MODE)으로 대표값 선택
        cur.execute("""
            WITH timeline AS (
                SELECT hostid,
                       to_timestamp(floor(extract(epoch from source_updated_at)/1800)*1800) AS bucket
                FROM embeddings_server_logs
                WHERE source_updated_at >= now() - INTERVAL '24 hours'
                UNION
                SELECT hostid,
                       to_timestamp(floor(extract(epoch from source_updated_at)/1800)*1800) AS bucket
                FROM embeddings_server_cpu
                WHERE source_updated_at >= now() - INTERVAL '24 hours'
            )
            SELECT
                t.hostid,
                t.bucket,
                (SELECT asset_name  FROM embeddings_server_logs WHERE hostid = t.hostid LIMIT 1) AS asset_name,
                (SELECT ip          FROM embeddings_server_logs WHERE hostid = t.hostid LIMIT 1) AS ip,
                -- ✅ [추가] hostid 기준 device_type 최빈값 (단일값이면 그대로 반환)
                COALESCE(
                    (
                        SELECT device_type
                        FROM embeddings_server_logs
                        WHERE hostid = t.hostid AND device_type IS NOT NULL
                        GROUP BY device_type
                        ORDER BY COUNT(*) DESC
                        LIMIT 1
                    ),
                    '미분류'
                ) AS device_type
            FROM timeline t
            GROUP BY 1, 2
            ORDER BY 2 DESC;
        """)
        work_list = cur.fetchall()

        for work in work_list:
            hostid   = work['hostid']
            ts       = work['bucket']
            ts_end   = work['bucket'] + timedelta(minutes=30)
            # ✅ [추가] device_type 참조
            dev_type = work['device_type']

            # 1. 성능 데이터 (보정 적용)
            cur.execute(
                "SELECT AVG(cpu_utilization) as cpu FROM embeddings_server_cpu "
                "WHERE hostid=%s AND source_updated_at >= %s AND source_updated_at < %s",
                (hostid, ts, ts_end)
            )
            cpu_val  = float(cur.fetchone()['cpu'] or 0)
            mem_util = get_latest_metric(cur, 'embeddings_server_memory',     hostid, ts_end, 'memory_utilization')
            mem_free = get_latest_metric(cur, 'embeddings_server_memory',     hostid, ts_end, 'available_memory_in')
            fs_val   = get_latest_metric(cur, 'embeddings_server_filesystems', hostid, ts_end, 'used_percent')

            # 2. 로그 데이터
            cur.execute("""
                SELECT SUM(occurrence_count) as cnt,
                       string_agg(DISTINCT log_level,  ', ') as levels,
                       string_agg(DISTINCT log_source, ', ') as srcs,
                       (SELECT logs FROM embeddings_server_logs
                        WHERE hostid=%s AND source_updated_at >= %s AND source_updated_at < %s
                        ORDER BY occurrence_count DESC LIMIT 1) as top_log
                FROM embeddings_server_logs
                WHERE hostid=%s AND source_updated_at >= %s AND source_updated_at < %s
            """, (hostid, ts, ts_end, hostid, ts, ts_end))
            log_stats = cur.fetchone()
            log_msg   = f": \"{log_stats['top_log'][:50]}...\"" if log_stats['top_log'] else ""

            # 3. 이벤트 및 부하 프로세스
            cur.execute(
                "SELECT string_agg(DISTINCT name, ' | ') as alerts "
                "FROM embeddings_alert_events "
                "WHERE hostid=%s AND to_timestamp(event_clock) >= %s "
                "  AND to_timestamp(event_clock) < %s AND value = 1",
                (hostid, ts, ts_end)
            )
            alerts = cur.fetchone()['alerts'] or '정상'

            cur.execute(
                "SELECT command, cpu FROM embeddings_process_resources "
                "WHERE hostid=%s AND source_updated_at >= %s AND source_updated_at < %s "
                "ORDER BY cpu DESC LIMIT 1",
                (hostid, ts, ts_end)
            )
            p_res    = cur.fetchone()
            top_proc = f"{p_res['command'][:30]}(CPU {p_res['cpu']:.1f}%)" if p_res else "N/A"

            # 4. 통합 리포트 (장비 유형 포함)
            KST    = timezone(timedelta(hours=9))
            ts_kst = ts.astimezone(KST) if ts.tzinfo is not None \
                     else ts.replace(tzinfo=timezone.utc).astimezone(KST)

            # ✅ [추가] 리포트 헤더에 장비 유형 명시
            report = (
                f"[{ts_kst.strftime('%m-%d %H:%M')}] {work['asset_name']}({work['ip']}) "
                f"[장비 유형: {dev_type}] 통합 진단: "
                f"성능(CPU {cpu_val:.1f}%, Mem {mem_util:.1f}%(여유 {mem_free:.1f}%), FS {fs_val:.1f}%) | "
                f"로그: {log_stats['levels'] or '정상'} {log_stats['cnt'] or 0}건 발생"
                f"(원인: {log_stats['srcs']}){log_msg} | "
                f"이벤트: {alerts} | 부하 프로세스: {top_proc}"
            )

            # 5. DB 저장
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": report}, timeout=40)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    # ✅ [추가] device_type 컬럼 INSERT / UPDATE
                    cur.execute("""
                        INSERT INTO embeddings_server_logs_chunked
                            (hostid, time_bucket, device_type,
                             perf_stats, log_stats, event_stats, process_stats,
                             integrated_report, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                        ON CONFLICT (hostid, time_bucket) DO UPDATE SET
                            device_type       = EXCLUDED.device_type,
                            integrated_report = EXCLUDED.integrated_report,
                            embedding         = EXCLUDED.embedding,
                            updated_at        = now();
                    """, (
                        hostid, ts, dev_type,
                        Json({'cpu': cpu_val, 'mem': mem_util, 'fs': fs_val}),
                        Json(dict(log_stats)),
                        Json({'alerts': alerts}),
                        Json({'top': top_proc}),
                        report, str(emb)
                    ))
                    conn.commit()
            except Exception as e:
                print(f"❌ 오류: {e}")
                conn.rollback()

    finally:
        try:
            del_cur = conn.cursor()
            retention_days = CONF['RETENTION_DAYS']
            del_cur.execute(
                f"DELETE FROM embeddings_server_logs_chunked "
                f"WHERE updated_at < now() - INTERVAL '{retention_days} days'"
            )
            conn.commit()
            del_cur.close()
            print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")
        except Exception:
            pass
        conn.close()
        print(f"[{datetime.now()}] ✅ 청킹 동기화 완료")

if __name__ == "__main__":
    main()

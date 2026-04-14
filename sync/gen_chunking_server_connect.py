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
from collections import Counter

# 1. 설정 정보

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def get_host_map(conn):
    """IP -> 자산명 매핑 딕셔너리 생성"""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT ip, asset_name FROM embeddings_hosts")
        return {r['ip']: r['asset_name'] for r in cur.fetchall() if r['ip']}

def main():
    print(f"[{datetime.now()}] 🚀 지능형 연결 청킹 시작 (JSON 데이터 보정 및 디버그 모드)")
    conn = psycopg2.connect(**RAG_DB)
    host_map = get_host_map(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 모든 활성 서버 조회
        cur.execute("SELECT hostid, asset, ip as asset_ip, asset_name FROM embeddings_hosts WHERE is_active = 1")
        hosts = cur.fetchall()
        print(f"   - 대상 호스트 수: {len(hosts)}대")

        for host in hosts:
            asset = host['asset']
            
            # 💡 [검증] 원본 임베딩 테이블에서 해당 자산의 활성 연결 조회
            cur.execute("""
                SELECT direction, service_name, foreign_ip, source_updated_at 
                FROM embeddings_server_connections 
                WHERE asset = %s AND flag = 'y'
            """, (asset,))
            conns = cur.fetchall()

            if not conns:
                # 데이터가 없는 경우 다음 호스트로 이동
                continue

            # 통계 및 상세 분석
            total = len(conns)
            inbound_cnt = sum(1 for c in conns if c['direction'] == 'INBOUND')
            outbound_cnt = total - inbound_cnt
            KST = timezone(timedelta(hours=9))
            max_ts = max(c['source_updated_at'] for c in conns)
            max_ts_kst = max_ts.astimezone(KST) if max_ts.tzinfo is not None else max_ts.replace(tzinfo=timezone.utc).astimezone(KST)
            
            # Top IP 분석 (자산명 병기)
            top_ips_raw = Counter(f"{host_map.get(str(c['foreign_ip']), 'Unknown')}({c['foreign_ip']})" for c in conns).most_common(10)
            ip_str = ", ".join([f"{name}:{count}건" for name, count in top_ips_raw[:5]])

            # Top 서비스 분석
            top_svcs_raw = Counter(c['service_name'] for c in conns).most_common(10)
            svc_str = ", ".join([f"{name}:{count}건" for name, count in top_svcs_raw[:3]])

            # 요약문 생성
            summary = (
                f"호스트 ID: {host['hostid']} | 자산명: {host['asset_name']} | IP: {host['asset_ip']} | "
                f"연결 현황: 총 {total}건 (Inbound {inbound_cnt}, Outbound {outbound_cnt}) | "
                f"주요 서비스: {svc_str} | 주요 접속지: {ip_str} | 최근 확인: {max_ts_kst.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # 💡 변경 감지: 마지막 기록과 비교
            cur.execute("""
                SELECT id, connection_summary, time_bucket 
                FROM embeddings_server_connections_chunked 
                WHERE asset = %s 
                ORDER BY time_bucket DESC LIMIT 1
            """, (asset,))
            last = cur.fetchone()

            # 시간 정보를 제외한 핵심 문구 비교
            is_changed = True
            if last:
                old_core = last['connection_summary'].split('| 최근 확인')[0].strip()
                new_core = summary.split('| 최근 확인')[0].strip()
                if old_core == new_core:
                    is_changed = False

            if is_changed:
                print(f"   - [{host['asset_name']}] 신규 데이터 생성 중... (연결 {total}건)")
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    
                    # 💡 JSON 데이터 구성
                    metrics = {'total': total, 'inbound': inbound_cnt, 'outbound': outbound_cnt}
                    details = {
                        'top_ips': [{'name': name, 'count': count} for name, count in top_ips_raw],
                        'top_services': [{'name': name, 'count': count} for name, count in top_svcs_raw]
                    }

                    # 💡 INSERT 수행 (connection_details 필드 확실히 포함)
                    cur.execute("""
                        INSERT INTO embeddings_server_connections_chunked 
                        (hostid, asset, server_ip, asset_name, time_bucket, 
                         connection_metrics, connection_details, connection_summary, 
                         embedding, record_count, last_seen_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, now())
                    """, (host['hostid'], asset, host['asset_ip'], host['asset_name'], datetime.now(), 
                          Json(metrics), Json(details), summary, str(emb), total, max_ts))
                    conn.commit()
            else:
                # 변화가 없으면 관측 시각만 업데이트
                cur.execute("""
                    UPDATE embeddings_server_connections_chunked 
                    SET last_seen_at = %s, updated_at = now() 
                    WHERE id = %s
                """, (max_ts, last['id']))
                conn.commit()

    except Exception as e:
        print(f"   ❌ 오류 발생: {e}")
        conn.rollback()
    finally:
        # 보존 기간 초과 데이터 삭제
        try:
            del_cur = conn.cursor()
            retention_days = CONF['RETENTION_DAYS']
            del_cur.execute(f"DELETE FROM embeddings_server_connections_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
            conn.commit()
            del_cur.close()
            print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")
        except Exception:
            pass
        conn.close()
        print(f"[{datetime.now()}] ✅ 지능형 연결 청킹 완료")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Phase B2: 서버 프로세스 TOP-N 청킹 스크립트 (Decimal 직렬화 오류 해결 버전)
- 수정 내역:
  1. SQL 쿼리 수정: SUM(cpu)::float, SUM(mem)::float로 명시적 형변환 추가 (JSON 직렬화 에러 방지)
  2. 프로세스명 추출 로직 유지 및 안정성 강화
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
import hashlib
from datetime import datetime, timezone, timedelta

# ⚙️ 설정

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def get_embedding(text):
    try:
        res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": text}, timeout=60)
        return res.json().get('embedding') if res.status_code == 200 else None
    except: return None

def sync_process_trend():
    print(f"[{datetime.now()}] 🔄 프로세스 합산 및 트렌드 청킹 시작...")
    conn = psycopg2.connect(**RAG_DB)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. 대상 시간대와 호스트 식별
        cur.execute("""
            SELECT DISTINCT hostid, date_trunc('hour', source_updated_at) as hb 
            FROM embeddings_process_resources 
            WHERE source_updated_at > now() - interval '24 hours'
        """)
        targets = cur.fetchall()
        print(f"   - 분석 대상 시간 버킷: {len(targets)}건")

        for t in targets:
            hostid = t['hostid']
            hb = t['hb']

            # 2. 💡 [핵심수정] SUM(cpu)::float 로 형변환하여 Decimal JSON 에러 해결
            # CPU TOP 5
            cur.execute("""
                SELECT 
                    replace(split_part(split_part(command, ' ', 1), '/', -1), ':', '') as process_name, 
                    SUM(cpu)::float as total_cpu, 
                    SUM(mem)::float as total_mem
                FROM embeddings_process_resources
                WHERE hostid = %s AND date_trunc('hour', source_updated_at) = %s
                GROUP BY 1
                ORDER BY total_cpu DESC LIMIT 5
            """, (hostid, hb))
            cpu_rows = cur.fetchall()

            # MEM TOP 5
            cur.execute("""
                SELECT 
                    replace(split_part(split_part(command, ' ', 1), '/', -1), ':', '') as process_name, 
                    SUM(cpu)::float as total_cpu, 
                    SUM(mem)::float as total_mem
                FROM embeddings_process_resources
                WHERE hostid = %s AND date_trunc('hour', source_updated_at) = %s
                GROUP BY 1
                ORDER BY total_mem DESC LIMIT 5
            """, (hostid, hb))
            mem_rows = cur.fetchall()

            if not cpu_rows: continue

            # 3. 호스트 정보 가져오기
            cur.execute("SELECT asset_name, ip FROM embeddings_hosts WHERE hostid = %s", (hostid,))
            h_info = cur.fetchone()
            asset_name = h_info['asset_name'] if h_info else "Unknown"
            ip = h_info['ip'] if h_info else "0.0.0.0"

            # 4. 요약문 조립
            cpu_str = ", ".join([f"{r['process_name']}({r['total_cpu']:.1f}%)" for r in cpu_rows])
            mem_str = ", ".join([f"{r['process_name']}({r['total_mem']:.1f}%)" for r in mem_rows])
            
            KST = timezone(timedelta(hours=9))
            hb_kst = hb.astimezone(KST) if hb.tzinfo is not None else hb.replace(tzinfo=timezone.utc).astimezone(KST)
            time_str = hb_kst.strftime('%Y-%m-%d %H시')
            summary = (f"[프로세스 TOP 5] {asset_name} ({ip}) | {time_str} | "
                       f"CPU 합산 상위: {cpu_str} | 메모리 합산 상위: {mem_str}")

            # 5. 해시 기반 변경 감지
            current_hash = hashlib.sha256(summary.encode('utf-8')).hexdigest()
            cur.execute("""
                SELECT summary_hash FROM embeddings_server_process_topn_chunked 
                WHERE hostid = %s AND time_bucket = %s
            """, (hostid, hb))
            existing = cur.fetchone()

            if existing and existing['summary_hash'] == current_hash:
                continue

            # 6. 임베딩 생성 및 저장
            print(f"   - [{asset_name}] {time_str} 데이터 처리 중...")
            emb = get_embedding(summary)
            if emb:
                cur.execute("""
                    INSERT INTO embeddings_server_process_topn_chunked (
                        hostid, asset_name, ip, time_bucket, 
                        top_cpu_processes, top_mem_processes, 
                        process_summary, summary_hash, embedding, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
                    ON CONFLICT (hostid, time_bucket) DO UPDATE SET
                        process_summary = EXCLUDED.process_summary,
                        summary_hash = EXCLUDED.summary_hash,
                        embedding = EXCLUDED.embedding,
                        top_cpu_processes = EXCLUDED.top_cpu_processes,
                        top_mem_processes = EXCLUDED.top_mem_processes,
                        updated_at = now();
                """, (hostid, asset_name, ip, hb, Json(cpu_rows), Json(mem_rows), summary, current_hash, str(emb)))
                conn.commit()

        print(f"✅ [{datetime.now()}] 프로세스 트렌드 동기화 완료.")

        # 보존 기간 초과 데이터 삭제
        retention_days = CONF['RETENTION_DAYS']
        cur.execute(f"DELETE FROM embeddings_server_process_topn_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
        conn.commit()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    sync_process_trend()
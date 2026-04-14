#!/usr/bin/env python3
"""
Phase B2: 자산 통합 인벤토리 청킹 스크립트 (포트 정보 및 장애 이력 정밀 정제 버전)
- 수정내역: 
  1. 포트 데이터 정제 강화: "현재 오픈 포트" 또는 "오픈 포트 현황" 키워드 대응
  2. 장애 이력 가독성 개선: 중복되는 문구 정리
  3. 해시 기반 스킵 로직 유지
"""
import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import hashlib
import re
from datetime import datetime

# ⚙️ DB 및 API 설정

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

def get_embedding(text):
    try:
        res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": text}, timeout=60)
        return res.json().get('embedding') if res.status_code == 200 else None
    except: return None

def clean_port_summary(raw_port_str):
    """포트 요약문에서 중복 메타데이터를 제거하고 서비스 리스트만 추출"""
    if not raw_port_str:
        return "정보 없음"
    # "현재 오픈 포트:" 또는 "오픈 포트 현황:" 이후의 문자열만 추출
    match = re.search(r'(?:현재 오픈 포트|오픈 포트 현황):\s*(.*)', raw_port_str)
    if match:
        # 최근 확인 시각 이전까지만 잘라냄
        port_info = match.group(1).split(' | 최근 확인')[0]
        return port_info
    return raw_port_str

def sync_inventory():
    print(f"[{datetime.now()}] 🔄 자산 통합 인벤토리(B2) 최종 정제 시작...")
    conn = psycopg2.connect(**RAG_DB)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        query = """
        SELECT 
            h.hostid, h.asset_name, h.ip, h.os,
            COALESCE('CPU ' || c.number_of_cpus || 'Core', 'CPU 정보 없음') as specs_val,
            
            (SELECT string_agg(fs_name || ':' || max_used || '%', ', ') 
             FROM (
                 SELECT fs_name, MAX(used_percent) as max_used 
                 FROM embeddings_server_filesystems 
                 WHERE hostid = h.hostid 
                 GROUP BY fs_name
                 ORDER BY fs_name
             ) sub) as storage_val,
             
            (SELECT port_summary 
             FROM embeddings_server_listen_ports_chunked 
             WHERE hostid = h.hostid 
             ORDER BY updated_at DESC LIMIT 1) as port_val,
            
            (SELECT string_agg(clean_name || '(' || count || '회)', ', ') 
             FROM (
                 SELECT 
                    REGEXP_REPLACE(name, '/var/log/messages 로그파일 - ', '로그:') as clean_name, 
                    count(*) as count 
                 FROM embeddings_alert_events 
                 WHERE hostid = h.hostid AND event_clock > extract(epoch from now() - interval '7 days')
                 GROUP BY clean_name
             ) t) as alerts_val
             
        FROM embeddings_hosts h
        LEFT JOIN embeddings_server_cpu c ON h.hostid = c.hostid
        WHERE h.is_active = 1
        """
        cur.execute(query)
        rows = cur.fetchall()

        processed = 0
        skipped = 0

        for r in rows:
            storage = r['storage_val'] if r['storage_val'] else "정보 없음"
            ports = clean_port_summary(r['port_val'])
            alerts = r['alerts_val'] if r['alerts_val'] else "발생 안함"
            os_val = r['os'] if r['os'] else "Unknown"
            
            # 💡 [정제된 요약문 조립]
            summary = (f"[서버 종합 진단] 자산명: {r['asset_name']} ({r['ip']}) | 상태: 활성 | OS: {os_val} | "
                       f"사양: {r['specs_val']} | 스토리지: {storage} | 오픈 포트: {ports} | "
                       f"최근 7일 장애: {alerts}")

            # 해시 기반 변경 감지
            current_hash = hashlib.sha256(summary.encode('utf-8')).hexdigest()
            cur.execute("SELECT summary_hash FROM embeddings_infra_inventory_chunked WHERE hostid = %s", (r['hostid'],))
            existing = cur.fetchone()
            
            if existing and existing['summary_hash'] == current_hash:
                skipped += 1
                cur.execute("UPDATE embeddings_infra_inventory_chunked SET updated_at = now() WHERE hostid = %s", (r['hostid'],))
                conn.commit()
                continue

            print(f" - [{r['asset_name']}] 상태 변경 감지. 임베딩 업데이트...")
            emb = get_embedding(summary)
            
            if emb:
                cur.execute("""
                    INSERT INTO embeddings_infra_inventory_chunked (
                        hostid, asset_name, ip, specs, storage_summary, 
                        port_summary, recent_alerts, inventory_summary, 
                        summary_hash, embedding, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
                    ON CONFLICT (hostid) DO UPDATE SET
                        inventory_summary = EXCLUDED.inventory_summary,
                        summary_hash = EXCLUDED.summary_hash,
                        embedding = EXCLUDED.embedding,
                        updated_at = now();
                """, (r['hostid'], r['asset_name'], r['ip'], r['specs_val'], 
                      storage, ports, alerts, summary, current_hash, emb))
                conn.commit()
                processed += 1

        print(f"✅ 동기화 완료 (갱신: {processed}건, 스킵: {skipped}건)")

        # 보존 기간 초과 데이터 삭제
        retention_days = CONF['RETENTION_DAYS']
        cur.execute(f"DELETE FROM embeddings_infra_inventory_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
        conn.commit()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    sync_inventory()

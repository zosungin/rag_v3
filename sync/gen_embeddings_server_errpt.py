#!/usr/bin/env python3
"""
Phase B1: AIX ERRPT 로그 임베딩 및 동기화 (최종 통합본)
- 수정내역:
  1. 요약문 형식: '호스트 ID | 상태 | 자산명 | IP | 등급 | 분류 | 리소스 | 내용 | 측정시각(맨 뒤)'
  2. 원본 컬럼명 대응: e.errpt_timestamp 대신 e.timestamp(원본명) 사용
  3. Skip 로직: source_updated_at 및 is_active 변동 없을 시 임베딩 생략
  4. 데이터 무결성: (uid, source_updated_at) 복합 PK 기반 UPSERT로 중복 방지
"""
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

# 1. 데이터베이스 및 API 설정

CONF = {
    'RETENTION_DAYS': 30  # ERRPT 로그 보존 기간 (일)
}

# AIX ERRPT 코드 매핑
TYPE_SEVERITY = {'P': 'critical', 'T': 'warning', 'I': 'info', 'U': 'warning', 'D': 'info', 'R': 'info'}
CLASS_CATEGORY = {'H': 'hardware', 'S': 'software', 'O': 'performance', 'U': 'info'}

def get_embedding(text):
    """Ollama API를 통한 임베딩 생성"""
    try:
        res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": text[:1000]}, timeout=600)
        return res.json().get('embedding') if res.status_code == 200 else None
    except Exception as e:
        print(f"      ❌ 임베딩 API 오류: {e}")
        return None

def generate_errpt_summary(row, severity, category):
    """
    요청된 표준 형식으로 요약문 생성
    row index: 0:uid, 1:source_updated_at, 2:hostid, 3:asset, 4:ip, 5:asset_name, 6:os, 7:sys_name, 8:group_name,
               9:is_active, 10:identifier, 11:timestamp(원본), 12:type, 13:class, 14:resource_name, 15:description
    """
    active_txt = "활성" if row[9] == 1 else "비활성"
    
    # Prefix: 호스트 식별 정보 표준화
    prefix = f"호스트 ID: {row[2]} | 상태: {active_txt} | 자산명: {row[5] or row[3]} | IP: {row[4]}"
    
    # Body: ERRPT 상세 정보
    body = (
        f"등급: {severity.upper()} | 분류: {category.upper()} | 리소스: {row[14]} | "
        f"ID: {row[10]} | 내용: {row[15]}"
    )
    
    # row[1] = to_timestamp(clock)::timestamp → naive, UTC 기준으로 KST 변환
    KST = timezone(timedelta(hours=9))
    if row[1].tzinfo is not None:
        kst_ts = row[1].astimezone(KST)
    else:
        kst_ts = row[1].replace(tzinfo=timezone.utc).astimezone(KST)
    timestamp_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S')
    
    return f"{prefix} | {body} | 측정시각: {timestamp_str}"

def get_existing_info(conn, uid, ts):
    """RAG DB에서 기존 기록 확인 (Skip 로직용)"""
    cur = conn.cursor()
    cur.execute(
        "SELECT is_active FROM embeddings_server_errpt WHERE uid = %s AND source_updated_at = %s", 
        (uid, ts)
    )
    return cur.fetchone()

def fetch_errpt_data():
    """Zabbix DB에서 AIX ERRPT 데이터 조회"""
    conn = psycopg2.connect(**ZABBIX_DB)
    cur = conn.cursor()
    try:
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
        SELECT 
            e.uid, to_timestamp(e.clock)::timestamp as source_updated_at,
            e.hostid, h.asset, h.ip, h.asset_name, h.os, h.system_name, h.group_name,
            COALESCE(h.is_active, 0) as is_active,
            e.identifier, e.timestamp, e.type, e.class, e.resource_name, e.description, e.flag
        FROM svr_log_errpt e
        LEFT JOIN host_info h ON e.hostid = h.hostid
        WHERE e.flag IN ('y', 'n')
        """
        cur.execute(query)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

def upsert_errpt_to_rag(conn, row, summary, sev, cat, embedding):
    """RAG DB에 ERRPT 정보 UPSERT"""
    cur = conn.cursor()
    try:
        query = """
        INSERT INTO embeddings_server_errpt (
            uid, source_updated_at, hostid, asset, ip, asset_name, os, system_name, group_name,
            is_active, identifier, errpt_timestamp, type, class, resource_name, description, flag,
            severity_level, error_category, errpt_summary, embedding, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s::vector, now()
        )
        ON CONFLICT (uid, source_updated_at) DO UPDATE SET
            is_active = EXCLUDED.is_active,
            errpt_summary = EXCLUDED.errpt_summary,
            embedding = EXCLUDED.embedding,
            updated_at = now();
        """
        cur.execute(query, (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
            row[9], row[10], row[11], row[12], row[13], row[14], row[15], row[16],
            sev, cat, summary, str(embedding)
        ))
        conn.commit()
    finally:
        cur.close()

def sync_with_host_status(conn, table_name):
    """embeddings_hosts의 상태를 현재 테이블에 강제 동기화"""
    cur = conn.cursor()
    try:
        print(f"\n🔄 {table_name} 상태 동기화 중 (Master: embeddings_hosts)... ")
        query = f"""
        UPDATE {table_name} t
        SET is_active = h.is_active, updated_at = now()
        FROM embeddings_hosts h
        WHERE t.hostid = h.hostid AND t.is_active != h.is_active;
        """
        cur.execute(query)
        affected = cur.rowcount
        conn.commit()
        if affected > 0:
            print(f"✅ {affected}건의 상태를 마스터 정보와 일치시켰습니다.")
    finally:
        cur.close()
        
def main():
    print(f"[{datetime.now()}] 🔄 AIX ERRPT 동기화 시작 (최종 통합 최적화 모드)")
    
    rows = fetch_errpt_data()
    r_conn = psycopg2.connect(**RAG_DB)
    
    try:
        total = len(rows)
        success_count = 0
        skip_count = 0
        
        for i, row in enumerate(rows, 1):
            uid, ts, is_active = row[0], row[1], row[9]
            
            # [Skip Logic] 기존 데이터와 시각/상태 대조
            existing = get_existing_info(r_conn, uid, ts)
            if existing and existing[0] == is_active:
                skip_count += 1
                continue
            
            print(f"({i}/{total}) 처리 중: {uid[:12]}...", end="\r")
            
            # 등급 및 분류 매핑
            sev = TYPE_SEVERITY.get(row[12], 'info')
            cat = CLASS_CATEGORY.get(row[13], 'info')
            
            # 요약문 생성 (측정시각 맨 뒤 배치)
            summary = generate_errpt_summary(row, sev, cat)
            
            # 임베딩 생성 및 저장
            emb = get_embedding(summary)
            if emb:
                upsert_errpt_to_rag(r_conn, row, summary, sev, cat, emb)
                success_count += 1
                
        print(f"\n[{datetime.now()}] ✅ 동기화 완료 (전체: {total}, 신규/갱신: {success_count}, 스킵: {skip_count})")
        
        # 💡 추가: 마스터 상태와 강제 동기화
        sync_with_host_status(r_conn, 'embeddings_server_errpt')

        # 보존 기간 초과 데이터 삭제
        cur = r_conn.cursor()
        cur.execute(
            f"DELETE FROM embeddings_server_errpt "
            f"WHERE source_updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} days'"
        )
        r_conn.commit()
        cur.close()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    finally:
        r_conn.close()

if __name__ == "__main__":
    main()
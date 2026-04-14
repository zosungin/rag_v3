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
    'CHANGE_LIMIT': 10.0,   # 10%p 이상 변동 시 임베딩
    'FS_DANGER': 95.0,      # 파일시스템 위험 임계치
    'STD_WARN': 5.0         # 표준편차 주의 기준
}

def get_trend_util(cur, hostid, fs_name, current_ts):
    """동일 호스트의 동일 마운트 지점(fs_name) 기준 직전 수집 데이터의 사용률 조회"""
    query = """
        SELECT space_utilization FROM svr_filesystem_std 
        WHERE hostid = %s AND fs_name = %s AND updated_at < %s 
        ORDER BY updated_at DESC LIMIT 1
    """
    cur.execute(query, (hostid, fs_name, current_ts))
    res = cur.fetchone()
    return float(res[0]) if res and res[0] is not None else None

def determine_stability(util, std):
    """파일시스템 수치와 표준편차를 기반으로 상태 등급 및 우선순위 판별"""
    if util >= CONF['FS_DANGER']: return "임계치 초과 (위험)", 4
    if std >= CONF['STD_WARN']: return "사용량 급변 (주의)", 2
    return "정상", 0

def generate_summary(d, stability, priority, trend_diff):
    """[최종 포맷] 파일시스템 상세 지표 + 마운트별 추세 + 호스트 정보 통합"""
    trend_txt = f"{trend_diff:+.2f}%" if trend_diff is not None else "이력 없음"
    
    # 테이블 정의서 기준 NUMERIC(12,1) 단위를 GB로 변환 (GB 단위로 가정)
    KST = timezone(timedelta(hours=9))
    if d['ts'].tzinfo is not None:
        kst_ts = d['ts'].astimezone(KST)
    else:
        kst_ts = d['ts'].replace(tzinfo=timezone.utc).astimezone(KST)
    ts_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]

    return (
        f"[{ts_str}] | 상태진단: {stability} (표준편차 {d['std']:.2f}) | 우선순위: {priority} | "
        f"파일시스템 [{d['fs_name']}] 사용률 {d['util']:.2f}% (직전 대비 {trend_txt}) | "
        f"전체 용량: {d['total_space']:.1f}GB, 사용 중: {d['used_space']:.1f}GB | "
        f"호스트 ID: {d['hostid']} | 자산명: {d['asset_name']} | 시스템: {d['system_name']} | "
        f"그룹: {d['group_name']} | IP: {d['ip']} | OS: {d['os']}"
    )

def upsert_to_rag(conn, d, summary, emb, stability, priority):
    """RAG DB Upsert (제공된 스키마에 맞춘 파라미터 매핑)"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO embeddings_server_filesystems (
            hostid, asset, ip, asset_name, os, system_name, group_name, is_active,
            fs_name, total_size, used_size, used_percent, used_percent_std,
            stability_level, priority, fs_summary, embedding, source_updated_at, updated_at
        ) VALUES (%(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
                  %(fs_name)s, %(total_space)s, %(used_space)s, %(util)s, %(std)s, %(stability)s, %(priority)s, 
                  %(summary)s, %(emb)s::vector, %(ts)s, now())
        ON CONFLICT (hostid, fs_name, source_updated_at) DO UPDATE SET
            stability_level = EXCLUDED.stability_level,
            priority = EXCLUDED.priority,
            fs_summary = EXCLUDED.fs_summary,
            embedding = EXCLUDED.embedding,
            updated_at = now();
    """, {**d, 'stability': stability, 'priority': priority, 'summary': summary, 'emb': str(emb)})
    conn.commit()
    cur.close()

def main():
    print(f"[{datetime.now()}] 🔄 파일시스템 동기화 시작 (확정 스키마 반영)")
    try:
        z_conn = psycopg2.connect(**ZABBIX_DB); r_conn = psycopg2.connect(**RAG_DB)
        z_cur = z_conn.cursor()
        
        # JOIN 쿼리 (제공해주신 svr_filesystem_std 컬럼명으로 수정)
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
               s.fs_name, s.total_space, s.used_space, s.space_utilization as used_percent, 
               COALESCE(s.space_utilization_std, 0) as used_percent_std, 
               s.updated_at, COALESCE(h.is_active, 0) as is_active, s.flag
        FROM svr_filesystem_std s
        LEFT JOIN host_info h ON s.hostid = h.hostid
        WHERE s.updated_at >= now() - INTERVAL '1 hour'
        """
        z_cur.execute(query)
        columns = [desc[0] for desc in z_cur.description]
        rows = z_cur.fetchall()
        print(f"   - 처리 대상 데이터: {len(rows)}건")
        
        for i, row in enumerate(rows, 1):
            d = dict(zip(columns, row))
            d['ts'] = d['updated_at']
            d['util'] = float(d['used_percent'] or 0)
            d['std'] = float(d['used_percent_std'] or 0)
            d['total_space'] = float(d['total_space'] or 0)
            d['used_space'] = float(d['used_space'] or 0)

            # 1. 마운트별 직전 추세 분석
            prev_util = get_trend_util(z_cur, d['hostid'], d['fs_name'], d['ts'])
            trend_diff = (d['util'] - prev_util) if prev_util is not None else None
            
            # 2. 필터링 (위험/주의/급변/최신값)
            if (d['util'] >= CONF['FS_DANGER'] or d['std'] >= CONF['STD_WARN'] or 
                (trend_diff is not None and abs(trend_diff) >= CONF['CHANGE_LIMIT']) or d['flag'] == 'y'):
                
                stability, priority = determine_stability(d['util'], d['std'])
                summary = generate_summary(d, stability, priority, trend_diff)
                
                print(f"   ({i}/{len(rows)}) 처리: {d['hostid']} - {d['fs_name']} {d['util']:.2f}%", end="\r")
                try:
                    res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                    if res.status_code == 200:
                        emb = res.json().get('embedding')
                        upsert_to_rag(r_conn, d, summary, emb, stability, priority)
                except Exception as e:
                    print(f"\n❌ API/DB 오류 ({d['hostid']}): {e}")
            else:
                r_cur = r_conn.cursor()
                r_cur.execute("""
                    UPDATE embeddings_server_filesystems SET updated_at = now() 
                    WHERE hostid=%s AND fs_name=%s AND source_updated_at=%s
                """, (d['hostid'], d['fs_name'], d['ts']))
                r_conn.commit(); r_cur.close()

        r_cur = r_conn.cursor(); r_cur.execute(f"DELETE FROM embeddings_server_filesystems WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} day'")
        r_conn.commit(); r_cur.close()
        
    except Exception as e: print(f"\n❌ 실행 오류: {e}")
    finally:
        if 'z_conn' in locals(): z_conn.close()
        if 'r_conn' in locals(): r_conn.close()
        print(f"\n[{datetime.now()}] ✅ 파일시스템 동기화 완료")

if __name__ == "__main__":
    main()

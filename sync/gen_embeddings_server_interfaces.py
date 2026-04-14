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
    'CHANGE_LIMIT': 10.0,   # 10%p 이상 트래픽 변동 시 임베딩
    'STD_WARN': 1000000.0   # 표준편차 주의 기준 (예: 1Mbps 이상 요동칠 때)
}

def format_bps(bps):
    """bps 단위를 Mbps/Kbps로 변환하여 가독성 향상"""
    bps = float(bps or 0)
    if bps >= 1000000: return f"{bps/1000000:.2f} Mbps"
    if bps >= 1000: return f"{bps/1000:.2f} Kbps"
    return f"{bps:.2f} bps"

def get_trend_values(cur, hostid, if_name, current_ts):
    """동일 인터페이스의 직전 수집 트래픽 데이터 조회"""
    query = """
        SELECT bits_received, bits_sent FROM svr_if_std 
        WHERE hostid = %s AND interface_name = %s AND updated_at < %s 
        ORDER BY updated_at DESC LIMIT 1
    """
    cur.execute(query, (hostid, if_name, current_ts))
    res = cur.fetchone()
    return (float(res[0] or 0), float(res[1] or 0)) if res else (None, None)

def determine_stability(std_in, std_out):
    """트래픽 변동성(표준편차) 기반 상태 판별"""
    if std_in >= CONF['STD_WARN'] or std_out >= CONF['STD_WARN']:
        return "트래픽 급변 (주의)", 2
    return "정상", 0

def generate_summary(d, stability, priority, trend_in, trend_out):
    """[최종 포맷] 표준편차에도 단위를 적용하여 분석 품질 향상"""
    txt_in = f"{trend_in:+.2f}%" if trend_in is not None else "이력 없음"
    txt_out = f"{trend_out:+.2f}%" if trend_out is not None else "이력 없음"
    status_txt = "Up" if d['op_status'] == 1 else "Down"

    # 💡 표준편차(Std) 값에 단위 변환 적용
    formatted_std_in = format_bps(d['std_in'])
    formatted_std_out = format_bps(d['std_out'])

    KST = timezone(timedelta(hours=9))
    if d['ts'].tzinfo is not None:
        kst_ts = d['ts'].astimezone(KST)
    else:
        kst_ts = d['ts'].replace(tzinfo=timezone.utc).astimezone(KST)
    ts_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]

    return (
        f"[{ts_str}] | 상태진단: {stability} (In_Std: {formatted_std_in}, Out_Std: {formatted_std_out}) | "
        f"우선순위: {priority} | 인터페이스 [{d['if_name']}] 상태: {status_txt} (Speed: {format_bps(d['speed'])}) | "
        f"Inbound: {format_bps(d['in_bps'])} (직전 대비 {txt_in}), "
        f"Outbound: {format_bps(d['out_bps'])} (직전 대비 {txt_out}) | "
        f"호스트 ID: {d['hostid']} | 자산명: {d['asset_name']} | 시스템: {d['system_name']} | "
        f"그룹: {d['group_name']} | IP: {d['ip']} | OS: {d['os']}"
    )

def upsert_to_rag(conn, d, summary, emb, stability, priority):
    """RAG DB Upsert (embeddings_server_interfaces 테이블 적용)"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO embeddings_server_interfaces (
            hostid, asset, ip, asset_name, os, system_name, group_name, is_active,
            interface_name, bits_received, bits_sent, bits_received_std, bits_sent_std,
            stability_level, priority, interface_summary, embedding, source_updated_at, updated_at
        ) VALUES (%(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s, %(is_active)s,
                  %(if_name)s, %(in_bps)s, %(out_bps)s, %(std_in)s, %(std_out)s, %(stability)s, %(priority)s, 
                  %(summary)s, %(emb)s::vector, %(ts)s, now())
        ON CONFLICT (hostid, interface_name, source_updated_at) DO UPDATE SET
            stability_level = EXCLUDED.stability_level,
            priority = EXCLUDED.priority,
            interface_summary = EXCLUDED.interface_summary,
            embedding = EXCLUDED.embedding,
            updated_at = now();
    """, {**d, 'stability': stability, 'priority': priority, 'summary': summary, 'emb': str(emb)})
    conn.commit()
    cur.close()

def main():
    print(f"[{datetime.now()}] 🔄 네트워크 인터페이스 동기화 시작 (svr_if_std 스키마 반영)")
    try:
        z_conn = psycopg2.connect(**ZABBIX_DB); r_conn = psycopg2.connect(**RAG_DB)
        z_cur = z_conn.cursor()
        
        # JOIN 쿼리 (시스템/그룹명 통합 및 확정 테이블명 svr_if_std 사용)
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
               s.interface_name as if_name, s.bits_received as in_bps, s.bits_sent as out_bps, 
               s.bits_received_std as std_in, s.bits_sent_std as std_out,
               s.operation_status as op_status, s.speed,
               s.updated_at, COALESCE(h.is_active, 0) as is_active, s.flag
        FROM svr_if_std s
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
            d['in_bps'], d['out_bps'] = float(d['in_bps'] or 0), float(d['out_bps'] or 0)
            d['std_in'], d['std_out'] = float(d['std_in'] or 0), float(d['std_out'] or 0)
            d['speed'] = float(d['speed'] or 0)

            # 1. In/Out 각각 직전 값 대비 추세 분석
            prev_in, prev_out = get_trend_values(z_cur, d['hostid'], d['if_name'], d['ts'])
            
            # 변동 퍼센트 계산 (직전 수치 기준)
            trend_in = ((d['in_bps'] - prev_in) / prev_in * 100) if prev_in and prev_in > 0 else None
            trend_out = ((d['out_bps'] - prev_out) / prev_out * 100) if prev_out and prev_out > 0 else None
            
            # 2. 필터링 (In/Out 10% 변동 OR 주의 상황 OR 최신 snapshot)
            if ( (trend_in is not None and abs(trend_in) >= CONF['CHANGE_LIMIT']) or 
                 (trend_out is not None and abs(trend_out) >= CONF['CHANGE_LIMIT']) or
                 d['std_in'] >= CONF['STD_WARN'] or d['std_out'] >= CONF['STD_WARN'] or d['flag'] == 'y'):
                
                stability, priority = determine_stability(d['std_in'], d['std_out'])
                summary = generate_summary(d, stability, priority, trend_in, trend_out)
                
                print(f"   ({i}/{len(rows)}) 처리: {d['hostid']} - {d['if_name']} {format_bps(d['in_bps'])}", end="\r")
                try:
                    res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                    if res.status_code == 200:
                        emb = res.json().get('embedding')
                        upsert_to_rag(r_conn, d, summary, emb, stability, priority)
                except Exception as e:
                    print(f"\n❌ API/DB 오류 ({d['hostid']}): {e}")
            else:
                # 변화가 작으면 시간만 갱신
                r_cur = r_conn.cursor()
                r_cur.execute("""
                    UPDATE embeddings_server_interfaces SET updated_at = now() 
                    WHERE hostid=%s AND interface_name=%s AND source_updated_at=%s
                """, (d['hostid'], d['if_name'], d['ts']))
                r_conn.commit(); r_cur.close()

        r_cur = r_conn.cursor(); r_cur.execute(f"DELETE FROM embeddings_server_interfaces WHERE updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} day'")
        r_conn.commit(); r_cur.close()
        
    except Exception as e: print(f"\n❌ 실행 오류: {e}")
    finally:
        if 'z_conn' in locals(): z_conn.close()
        if 'r_conn' in locals(): r_conn.close()
        print(f"\n[{datetime.now()}] ✅ 인터페이스 동기화 완료")

if __name__ == "__main__":
    main()

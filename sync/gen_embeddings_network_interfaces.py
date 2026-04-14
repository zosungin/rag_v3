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
    'MBPS_THRESHOLD': 10.0  # 10MB/s 이상의 표준편차 발생 시 주의
}

def format_bytes_per_sec(bytes_val):
    """Bytes/sec -> MB/s 변환 (실제 트래픽 수치용)"""
    val = float(bytes_val or 0)
    if val >= 1024 * 1024 * 1024: return f"{val/(1024*1024*1024):.2f} GB/s"
    if val >= 1024 * 1024: return f"{val/(1024*1024):.2f} MB/s"
    if val >= 1024: return f"{val/1024:.2f} KB/s"
    return f"{val:.2f} B/s"

def format_bits_per_sec(bits_val):
    """bps -> Gbps/Mbps 변환 (인터페이스 대역폭용)"""
    val = float(bits_val or 0)
    if val >= 1000000000: return f"{val/1000000000:.2f} Gbps"
    if val >= 1000000: return f"{val/1000000:.2f} Mbps"
    return f"{val:.2f} bps"

def get_interface_type_name(type_id):
    mapping = {1: "other", 6: "ethernet", 161: "LAG/Bonding", 135: "L2 VLAN"}
    try: return mapping.get(int(type_id), f"type-{type_id}")
    except: return "N/A"

def determine_stability(link, in_bytes, out_bytes, in_std, out_std):
    """물리 상태 및 부하 판별"""
    is_up = (float(link or 0) >= 1.0 or float(in_bytes or 0) > 100 or float(out_bytes or 0) > 100)
    if is_up:
        if (float(in_std or 0) / (1024*1024)) >= CONF['MBPS_THRESHOLD']:
            return "Link Up (불안정)", 2
        return "Link Up (정상)", 0
    # std_in == 0 AND std_out == 0 → 트래픽 변동 없음 = 비사용 포트
    if float(in_std or 0) == 0.0 and float(out_std or 0) == 0.0:
        return "Link Down (비사용 포트)", 3
    return "Link Down (단절)", 4

def generate_summary(d, stability, priority):
    KST = timezone(timedelta(hours=9))
    if d['ts'].tzinfo is not None:
        kst_ts = d['ts'].astimezone(KST)
    else:
        kst_ts = d['ts'].replace(tzinfo=timezone.utc).astimezone(KST)
    dt_str = kst_ts.strftime('%Y-%m-%d %H:%M:%S')
    type_name = get_interface_type_name(d['type'])
    
    # 💡 [보완] system_name, group_name을 포함한 식별 정보 구성
    prefix = (f"호스트 ID: {d['hostid']} | 자산명: {d['asset_name'] or d['asset']} | IP: {d['ip']} | "
              f"시스템: {d['system_name']} | 그룹: {d['group_name']}")
    
    # 물리상태 표현 결정
    if 'Link Up' in stability:
        phys_state = '연결됨'
    elif '비사용 포트' in stability:
        phys_state = '단절 (비사용 포트)'
    else:
        phys_state = '단절'

    body = (
        f"진단: {stability} | 인터페이스: {d['if_name']} ({d['alias'] or 'No Alias'}, {type_name}) | "
        f"물리상태: {phys_state} | 대역폭: {format_bits_per_sec(d['speed'])} | "
        f"트래픽(In/Out): {format_bytes_per_sec(d['in_bytes'])} / {format_bytes_per_sec(d['out_bytes'])} | "
        f"변동성(std): In {format_bytes_per_sec(d['in_std'])}, Out {format_bytes_per_sec(d['out_std'])}"
    )
    return f"[현재스냅샷] [{dt_str}] {prefix} | {body}"

def main():
    print(f"[{datetime.now()}] 🚀 네트워크 인터페이스 동기화 (메타데이터 NULL 해결 및 단위 보정)")
    try:
        z_conn = psycopg2.connect(**ZABBIX_DB); r_conn = psycopg2.connect(**RAG_DB)
        z_cur = z_conn.cursor(); r_cur = r_conn.cursor()
        
        # 💡 [핵심 수정] h.device_name as system_name 및 h.group_name 추가 선택
        query = """
            SELECT s.uid, s.hostid, h.asset, h.ip, h.asset_name, h.ios as os, 
                   h.device_name as system_name, h.group_name,
                   s.interface as if_name, s.port_alias_name as alias, s.link, s.type, s.port_speed as speed,
                   s.received as in_bytes, s.sent as out_bytes,
                   s.received_rate_std as in_std, s.sent_rate_std as out_std, s.updated_at as ts, s.flag,
                   CAST(COALESCE(h.snmp_agent, '0') AS INTEGER) as is_active
            FROM nw_if_std s JOIN nw_main1 h ON s.hostid = h.hostid
            WHERE s.flag = 'y'
        """
        z_cur.execute(query)
        columns = [desc[0] for desc in z_cur.description]
        rows = z_cur.fetchall()
        print(f"   - 처리 대상: {len(rows)}건")
        
        for row in rows:
            d = dict(zip(columns, row))
            stability, priority = determine_stability(d['link'], d['in_bytes'], d['out_bytes'], d['in_std'], d['out_std'])
            summary = generate_summary(d, stability, priority)

            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=60)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    # 💡 [핵심 수정] INSERT 컬럼에 system_name, group_name 추가 및 ON CONFLICT 업데이트
                    r_cur.execute("""
                        INSERT INTO embeddings_network_interfaces (
                            uid, hostid, asset, ip, asset_name, os, system_name, group_name,
                            interface, port_alias, link, port_status, is_active, port_speed, type,
                            received_bits, sent_bits, received_rate, sent_rate, 
                            received_rate_std, sent_rate_std, stability_level, priority,
                            interface_summary, embedding, source_updated_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, now())
                        ON CONFLICT (uid, source_updated_at) DO UPDATE SET
                            system_name = EXCLUDED.system_name,
                            group_name = EXCLUDED.group_name,
                            stability_level = EXCLUDED.stability_level,
                            interface_summary = EXCLUDED.interface_summary,
                            embedding = EXCLUDED.embedding,
                            updated_at = now();
                    """, (d['uid'], d['hostid'], d['asset'], d['ip'], d['asset_name'], d['os'], d['system_name'], d['group_name'],
                          d['if_name'], d['alias'], d['link'], stability, d['is_active'], d['speed'], get_interface_type_name(d['type']),
                          d['in_bytes'], d['out_bytes'], 0, 0,
                          d['in_std'], d['out_std'], stability, priority,
                          summary, str(emb), d['ts']))
                    r_conn.commit()
            except Exception as e: print(f"\n❌ 처리 오류 ({d['uid']}): {e}")

        r_cur.execute(f"DELETE FROM embeddings_network_interfaces WHERE source_updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} days'")
        r_conn.commit()
        
    finally:
        z_conn.close(); r_conn.close()
        print(f"\n[{datetime.now()}] ✅ 동기화 완료")

if __name__ == "__main__": main()

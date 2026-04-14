#!/usr/bin/env python3
"""
Phase B1 Step 3.2: 네트워크 장비 정보 임베딩 및 동기화 (실제 DDL 매핑 완료)
- 주요 개선 사항:
  1. 원본 nw_main1 테이블의 실제 컬럼명 반영 (device_cpu_usage, update_clock 등)
  2. Unix 타임스탬프(update_clock)를 datetime 포맷으로 자동 변환
  3. [네트워크] 특화 데이터 추가: 인터페이스 가동률, ICMP 응답속도, 패킷 에러 정보
  4. 해시 기반 변경 감지로 API 호출 최적화
"""
import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, ZABBIX_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import hashlib
from datetime import datetime, timezone, timedelta

# ==========================================
# ⚙️ 1. 설정 부분
# ==========================================

def format_uptime(uptime_val):
    """Uptime이 초(Seconds) 단위일 경우 가독성 있게 변환"""
    try:
        seconds = int(float(uptime_val))
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        return f"{days}일 {hours}시간 {minutes}분"
    except (ValueError, TypeError):
        return str(uptime_val) if uptime_val else "알 수 없음"

# ==========================================
# 🚀 2. 메인 동기화 프로세스
# ==========================================
def main():
    print(f"[{datetime.now()}] 🚀 지능형 네트워크 장비 임베딩 동기화 시작")
    conn_z = psycopg2.connect(**ZABBIX_DB)
    conn_r = psycopg2.connect(**RAG_DB)
    
    try:
        z_cur = conn_z.cursor(cursor_factory=RealDictCursor)
        r_cur = conn_r.cursor(cursor_factory=RealDictCursor)
        
        # 💡 실제 nw_main1 DDL에 맞춘 정확한 SELECT 쿼리
        z_cur.execute("""
            SELECT 
                hostid, group_name, agent_status, communication_name, template_name,
                asset, asset_name, device_name, ip, ios, snmp_agent, uptime,
                total_interface, up_interface, icmp_ping, icmp_response_time,
                total_outbound_packets_with_errors, total_inbound_packets_with_errors,
                system_description, device_cpu_usage, device_mem_usage, update_clock
            FROM nw_main1
        """)
        rows = z_cur.fetchall()
        print(f"   - 처리 대상: {len(rows)}대의 네트워크 장비")
        
        processed, skipped = 0, 0
        
        for row in rows:
            # 1. 자산명 중복 표기 방지
            asset = row.get('asset', '')
            asset_name = row.get('asset_name', '')
            display_name = f"{asset_name}({asset})" if asset and asset_name and asset != asset_name else (asset_name or asset or "Unknown")
            
            # 2. 상태 및 타임스탬프 처리
            # agent_status: '0'=active, '1'=inactive
            is_active = 1 if row.get('agent_status') == '0' else 0
            status_text = '활성(Up)' if is_active == 1 else '비활성(Down)'
            
            # update_clock (Unix Timestamp) 변환
            KST = timezone(timedelta(hours=9))
            dt_measure = datetime.fromtimestamp(row['update_clock'], tz=timezone.utc).astimezone(KST).strftime('%Y-%m-%d %H:%M') if row.get('update_clock') else '알 수 없음'
            source_dt = datetime.fromtimestamp(row['update_clock'], tz=timezone.utc) if row.get('update_clock') else datetime.now(tz=timezone.utc)
            
            # ICMP 상태
            icmp_text = '정상(응답)' if row.get('icmp_ping') == 1 else '장애(미응답)'
            icmp_ms = f" {row['icmp_response_time']}ms" if row.get('icmp_response_time') is not None else ""
            
            # 3. 💡 동적 요약문 조립 (값이 있는 항목만 추가하여 토큰 절약)
            parts = [
                f"[네트워크] 자산명: {display_name} ({row.get('ip', 'Unknown')})",
                f"장비명: {row.get('device_name', 'Unknown')}",
                f"상태: {status_text}",
                f"그룹: {row.get('group_name', 'Unknown')}",
                f"OS: {row.get('ios', 'Unknown')}"
            ]
            
            # 리소스 정보 (실제 컬럼 매핑)
            cpu = row.get('device_cpu_usage')
            mem = row.get('device_mem_usage')
            if cpu is not None or mem is not None:
                parts.append(f"리소스: CPU {cpu or 0}%, 메모리 {mem or 0}%")
                
            # 인터페이스 포트 정보
            if row.get('total_interface') is not None and row.get('up_interface') is not None:
                parts.append(f"인터페이스: {row['up_interface']}/{row['total_interface']} 포트 가동")
                
            # ICMP 정보
            parts.append(f"ICMP: {icmp_text}{icmp_ms}")
            
            # 기타 정보
            parts.append(f"가동시간: {format_uptime(row.get('uptime'))}")
            parts.append(f"측정: {dt_measure}")
            parts.append(f"호스트 ID: {row['hostid']}")
            
            summary = " | ".join(parts)
            
            # 4. 해시 기반 변경 감지
            current_hash = hashlib.sha256(summary.encode('utf-8')).hexdigest()
            r_cur.execute("SELECT summary_hash FROM embeddings_network_devices WHERE hostid = %s", (row['hostid'],))
            existing = r_cur.fetchone()
            
            if existing and existing['summary_hash'] == current_hash:
                skipped += 1
                r_cur.execute("UPDATE embeddings_network_devices SET source_updated_at = %s, updated_at = now() WHERE hostid = %s", (source_dt, row['hostid']))
                conn_r.commit()
                continue
                
            # 5. 임베딩 벡터 생성 및 DB 저장 (Upsert)
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=40)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    
                    r_cur.execute("""
                        INSERT INTO embeddings_network_devices (
                            hostid, group_name, asset, asset_name, device_name, ip,
                            is_active, agent_status, snmp_agent, communication_name, template_name, ios,
                            system_description, uptime, icmp_ping,
                            device_summary, summary_hash, embedding, source_updated_at, updated_at
                        ) VALUES (
                            %(hostid)s, %(group_name)s, %(asset)s, %(asset_name)s, %(device_name)s, %(ip)s,
                            %(is_active)s, %(agent_status)s, %(snmp_agent)s, %(communication_name)s, %(template_name)s, %(ios)s,
                            %(system_description)s, %(uptime)s, %(icmp_ping_str)s,
                            %(summary)s, %(summary_hash)s, %(embedding)s::vector, %(source_updated_at)s, now()
                        ) ON CONFLICT (hostid) DO UPDATE SET
                            group_name = EXCLUDED.group_name, asset = EXCLUDED.asset, 
                            asset_name = EXCLUDED.asset_name, device_name = EXCLUDED.device_name, ip = EXCLUDED.ip,
                            is_active = EXCLUDED.is_active, agent_status = EXCLUDED.agent_status, snmp_agent = EXCLUDED.snmp_agent,
                            communication_name = EXCLUDED.communication_name, template_name = EXCLUDED.template_name,
                            ios = EXCLUDED.ios, system_description = EXCLUDED.system_description,
                            uptime = EXCLUDED.uptime, icmp_ping = EXCLUDED.icmp_ping,
                            device_summary = EXCLUDED.device_summary, summary_hash = EXCLUDED.summary_hash,
                            embedding = EXCLUDED.embedding, source_updated_at = EXCLUDED.source_updated_at,
                            updated_at = now();
                    """, {
                        'hostid': row['hostid'],
                        'group_name': row.get('group_name'),
                        'asset': row.get('asset'),
                        'asset_name': row.get('asset_name'),
                        'device_name': row.get('device_name'),
                        'ip': row.get('ip'),
                        'is_active': is_active,
                        'agent_status': str(row.get('agent_status')),
                        'snmp_agent': str(row.get('snmp_agent')) if row.get('snmp_agent') is not None else None,
                        'communication_name': row.get('communication_name'),
                        'template_name': row.get('template_name'),
                        'ios': row.get('ios'),
                        'system_description': row.get('system_description'),
                        'uptime': format_uptime(row.get('uptime')),
                        'icmp_ping_str': str(row.get('icmp_ping')) if row.get('icmp_ping') is not None else None,
                        'summary': summary,
                        'summary_hash': current_hash,
                        'embedding': str(emb),
                        'source_updated_at': source_dt
                    })
                    conn_r.commit()
                    processed += 1
            except Exception as e:
                print(f"   ❌ DB 처리 오류 (HostID: {row['hostid']}): {e}")
                conn_r.rollback()
                
        print(f"   - [결과] 신규/상태변경 갱신: {processed}건, 스킵(유지): {skipped}건")
        
    except Exception as e:
        print(f"   ❌ 전체 프로세스 실패: {e}")
    finally:
        conn_z.close()
        conn_r.close()
        print(f"[{datetime.now()}] ✅ 지능형 네트워크 장비 임베딩 동기화 완료")

if __name__ == "__main__":
    main()
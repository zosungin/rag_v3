#!/usr/bin/env python3
"""
Phase B1 Step 1.2: 서버 정보 임베딩 및 동기화 (메모리 사용률 정확도 개선)
- 수정내역:
  1. svr_mem_main 테이블 LEFT JOIN 추가
  2. 메모리 사용률 계산식 적용: 100 - svr_mem_main.available_memory_in
  3. 계산된 값을 소수점 1자리 퍼센트(%)로 변환하여 요약문에 반영
"""

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import hashlib
from datetime import datetime, timezone, timedelta

# ==========================================
# ⚙️ 1. 설정 부분
# ==========================================
ZABBIX_DB = {'host': '172.202.0.10', 'port': 5432, 'database': 'zabbix_70', 'user': 'zabbix', 'password': 'zabbix123!@#'}
RAG_DB = {'host': '172.202.0.20', 'port': 5432, 'database': 'rag_db', 'user': 'rag_user', 'password': 'rag_password'}
OLLAMA_API_URL = "http://172.202.0.21:11434/api/embeddings"
OLLAMA_MODEL = "nomic-embed-text-v2-moe"

def format_uptime(uptime_val):
    """Uptime이 초(Seconds) 단위일 경우 가독성 있게 변환"""
    try:
        seconds = int(float(uptime_val))
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{days}일 {hours}시간 {minutes}분 {secs}초"
    except (ValueError, TypeError):
        return str(uptime_val) if uptime_val else "알 수 없음"

# ==========================================
# 🚀 2. 메인 동기화 프로세스
# ==========================================
def main():
    print(f"[{datetime.now()}] 🚀 지능형 서버(Hosts) 임베딩 동기화 시작")
    conn_z = psycopg2.connect(**ZABBIX_DB)
    conn_r = psycopg2.connect(**RAG_DB)
    
    try:
        z_cur = conn_z.cursor(cursor_factory=RealDictCursor)
        r_cur = conn_r.cursor(cursor_factory=RealDictCursor)
        
        # 💡 [핵심] svr_mem_main JOIN 및 available_memory_in 컬럼 추가
        z_cur.execute("""
            SELECT 
                s.hostid, s.group_name, s.ip, s.asset, s.asset_name, s.os, s.system_name, 
                s.number_of_processes, s.system_local_time, s.system_description, 
                s.system_uptime, s.logined_user, s.zabbix_agent, 
                s.cpu_utilization, s.max_fs, s.comp, s.noncomp, s.updated_at,
                m.available_memory_in,
                CASE WHEN s.zabbix_agent='0' THEN 1 ELSE 0 END as is_active
            FROM svr_system_main s
            LEFT JOIN svr_mem_main m ON s.hostid = m.hostid
        """)
        rows = z_cur.fetchall()
        print(f"   - 처리 대상: {len(rows)}대의 서버")
        
        processed, skipped = 0, 0
        
        for row in rows:
            # 1. 자산명 중복 표기 방지
            asset = row.get('asset', '')
            asset_name = row.get('asset_name', '')
            display_name = f"{asset_name}({asset})" if asset and asset_name and asset != asset_name else (asset_name or asset or "Unknown")
            
            # 2. 리소스 및 상태 변수 정리
            cpu = f"{row.get('cpu_utilization') or 0}%"
            disk = f"{row.get('max_fs') or 0}%"
            
            # 💡 [핵심] 메모리 사용률 계산 (100 - 가용량)
            avail_mem = row.get('available_memory_in')
            if avail_mem is not None:
                try:
                    used_mem_pct = 100.0 - float(avail_mem)
                    mem = f"{used_mem_pct:.1f}%"
                except (ValueError, TypeError):
                    mem = "확인 불가"
            else:
                mem = "확인 불가"
            
            status = '활성(Up)' if row['is_active'] == 1 else '비활성(Down)'
            KST = timezone(timedelta(hours=9))
            if row.get('updated_at'):
                upd = row['updated_at']
                kst_upd = upd.astimezone(KST) if upd.tzinfo is not None else upd.replace(tzinfo=timezone.utc).astimezone(KST)
                dt_measure = kst_upd.strftime('%Y-%m-%d %H:%M')
            else:
                dt_measure = '알 수 없음'
            
            # 3. 요약문 조립
            summary = (
                f"[서버] 자산명: {display_name} ({row['ip']}) | 상태: {status} | 그룹: {row.get('group_name', 'Unknown')} | "
                f"OS: {row.get('os', 'Unknown')} | 리소스: CPU {cpu}, 메모리 {mem}, 디스크(최대) {disk} | "
                f"프로세스 수: {row.get('number_of_processes') or 0} | 로그인: {row.get('logined_user') or 0}명 | "
                f"시스템명: {row.get('system_name', 'Unknown')} | 가동시간: {format_uptime(row.get('system_uptime'))} | "
                f"상세: {row.get('system_description', '없음')} | 측정: {dt_measure} | 호스트 ID: {row['hostid']}"
            )
            
            # 4. 해시 기반 변경 감지
            current_hash = hashlib.sha256(summary.encode('utf-8')).hexdigest()
            r_cur.execute("SELECT summary_hash FROM embeddings_hosts WHERE hostid = %s", (row['hostid'],))
            existing = r_cur.fetchone()
            
            if existing and existing['summary_hash'] == current_hash:
                skipped += 1
                r_cur.execute("UPDATE embeddings_hosts SET source_updated_at = %s, updated_at = now() WHERE hostid = %s", (row['updated_at'], row['hostid']))
                conn_r.commit()
                continue
                
            # 5. 임베딩 벡터 생성 및 DB 저장 (Upsert)
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=40)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    
                    r_cur.execute("""
                        INSERT INTO embeddings_hosts (
                            hostid, host_name, ip, asset, asset_name, os, system_name, group_name,
                            is_active, zabbix_agent, system_description, system_uptime, system_local_time,
                            number_of_processes, logined_user, comp, noncomp,
                            host_summary, summary_hash, embedding, source_updated_at, updated_at
                        ) VALUES (
                            %(hostid)s, %(system_name)s, %(ip)s, %(asset)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
                            %(is_active)s, %(zabbix_agent)s, %(system_description)s, %(system_uptime)s, %(system_local_time)s,
                            %(number_of_processes)s, %(logined_user)s, %(comp)s, %(noncomp)s,
                            %(summary)s, %(summary_hash)s, %(embedding)s::vector, %(source_updated_at)s, now()
                        ) ON CONFLICT (hostid) DO UPDATE SET
                            host_name = EXCLUDED.host_name, ip = EXCLUDED.ip, asset = EXCLUDED.asset, 
                            asset_name = EXCLUDED.asset_name, os = EXCLUDED.os, system_name = EXCLUDED.system_name, 
                            group_name = EXCLUDED.group_name, is_active = EXCLUDED.is_active, 
                            zabbix_agent = EXCLUDED.zabbix_agent, system_description = EXCLUDED.system_description, 
                            system_uptime = EXCLUDED.system_uptime, system_local_time = EXCLUDED.system_local_time,
                            number_of_processes = EXCLUDED.number_of_processes, logined_user = EXCLUDED.logined_user,
                            comp = EXCLUDED.comp, noncomp = EXCLUDED.noncomp,
                            host_summary = EXCLUDED.host_summary, summary_hash = EXCLUDED.summary_hash, 
                            embedding = EXCLUDED.embedding, source_updated_at = EXCLUDED.source_updated_at, 
                            updated_at = now();
                    """, {
                        **row,
                        'summary': summary,
                        'summary_hash': current_hash,
                        'embedding': str(emb),
                        'source_updated_at': row['updated_at']
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
        print(f"[{datetime.now()}] ✅ 지능형 서버(Hosts) 임베딩 동기화 완료")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Phase B1: 장애 이벤트 정보 벡터화 (정밀 분기 및 태그 포함)
- 변경 사항:
  1. Zabbix trigger_tag 테이블 조인 추가하여 tags 수집 및 DB 적재
  2. 이벤트 성격(상태형 장애 vs 단발성 알림)에 따른 요약문 분기 처리
     - 단발성 알림: 로그 키워드 감지, 설정 변경 알림 등 (조치 시간/해결 시간 제외)
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
from datetime import datetime, timezone, timedelta

# ⚙️ 설정 부분

CONF = {
    'RETENTION_DAYS': 90  # 알림 이벤트 보존 기간 (일)
}

PRIORITY_MAP = {0: 'Information', 1: 'Warning', 2: 'Average', 3: 'High', 4: 'Disaster', 5: 'Disaster'}

def format_duration(minutes):
    if minutes is None or minutes < 0: return "정보 없음"
    if minutes == 0: return "1분 미만"
    days = minutes // 1440
    hours = (minutes % 1440) // 60
    mins = minutes % 60
    res = []
    if days > 0: res.append(f"{days}일")
    if hours > 0: res.append(f"{hours}시간")
    if mins > 0: res.append(f"{mins}분")
    return " ".join(res)

def generate_intelligent_summary(row):
    """이벤트 유형별 맞춤형 자연어 문맥 요약문 생성"""
    p_text = PRIORITY_MAP.get(row['priority'], 'Unknown')
    asset_name = row['asset_name'] or row['asset'] or 'Unknown'
    ip = row['ip'] or 'Unknown'
    group = row['group_name'] or 'Unknown'
    item_key = row.get('item_key') or "확인 불가"
    tags = row.get('tags') or "태그 없음"
    
    KST = timezone(timedelta(hours=9))
    dt_event = datetime.fromtimestamp(row['event_clock'], tz=timezone.utc).astimezone(KST).strftime('%Y-%m-%d %H:%M') if row['event_clock'] else "알 수 없음"
    event_name = row['name']
    
    # 💡 [핵심 개선] 단발성 알림 이벤트 판별 (로그 키워드 감지, 감시 목록 수정)
    is_log_keyword = "로그파일 -" in event_name and "검색됨" in event_name
    is_list_modified = "LOG" in event_name and "감시 목록이 수정됨" in event_name
    
    if is_log_keyword or is_list_modified:
        # 단발성 알림용 포맷 (조치/해결 시간 정보 제외)
        event_category = "로그 감지" if is_log_keyword else "설정 알림"
        summary = (
            f"[{p_text}/{event_category}] {asset_name} ({ip}) 장비에서 '{event_name}' 이벤트가 발생했습니다. (단발성 알림) | "
            f"관련 아이템 키: {item_key} | 태그: {tags} | 그룹: {group} | 감지시각: {dt_event} | 호스트 ID: {row['hostid']}"
        )
    else:
        # 일반적인 장애 (지속형 - Problem / Resolved) 포맷
        if row['event_type'] == 'resolved':
            duration_text = format_duration(row.get('duration_minutes', 0))
            dt_resolved = datetime.fromtimestamp(row['resolved_clock'], tz=timezone.utc).astimezone(KST).strftime('%Y-%m-%d %H:%M') if row.get('resolved_clock') else "알 수 없음"
            
            summary = (
                f"[{p_text}/장애 해결] {asset_name} ({ip}) 장비에서 발생한 '{event_name}' 이벤트가 {duration_text} 만에 조치되었습니다. | "
                f"관련 아이템 키: {item_key} | 태그: {tags} | 그룹: {group} | 발생: {dt_event} | 해결: {dt_resolved} | 호스트 ID: {row['hostid']}"
            )
        else:
            summary = (
                f"[{p_text}/장애 발생] {asset_name} ({ip}) 장비에서 '{event_name}' 이벤트가 발생하여 현재 지속 중입니다. | "
                f"관련 아이템 키: {item_key} | 태그: {tags} | 그룹: {group} | 발생시각: {dt_event} | 호스트 ID: {row['hostid']}"
            )
            
    return summary

def main():
    print(f"[{datetime.now()}] 🚀 지능형 장애 임베딩 동기화 시작")
    
    conn_z = psycopg2.connect(**ZABBIX_DB); conn_r = psycopg2.connect(**RAG_DB)
    z_cur = conn_z.cursor(cursor_factory=RealDictCursor); r_cur = conn_r.cursor()

    try:
        # 💡 [쿼리 개선] trigger_tag 테이블 JOIN 추가로 tags 정보 추출
        z_cur.execute("""
            WITH item_keys AS (
                SELECT f.triggerid, string_agg(i.key_, ', ') as item_key
                FROM functions f
                JOIN items i ON f.itemid = i.itemid
                GROUP BY f.triggerid
            ),
            trigger_tags AS (
                SELECT triggerid, string_agg(tag || COALESCE(':' || value, ''), ', ') as tags
                FROM trigger_tag
                GROUP BY triggerid
            )
            SELECT 
                a.triggerid, a.hostid, a.asset, a.name, a.value, a.priority, a.flag,
                a.clock AS event_clock, 
                a.r_clock AS resolved_clock, 
                a.duration AS duration_minutes,
                CASE 
                    WHEN a.value = 0 THEN 'resolved'
                    WHEN a.value = 1 AND a.flag = 'n' THEN 'resolved'
                    ELSE 'problem' 
                END AS event_type,
                a.created_at AS source_updated_at,
                h.ip, h.asset_name, h.os, h.system_name, h.group_name,
                ik.item_key,
                tt.tags
            FROM alert_trigger a
            LEFT JOIN svr_system_main h ON a.hostid = h.hostid
            LEFT JOIN item_keys ik ON a.triggerid = ik.triggerid
            LEFT JOIN trigger_tags tt ON a.triggerid = tt.triggerid
            WHERE a.flag IN ('y', 'yt', 'n')
        """)
        rows = z_cur.fetchall()
        print(f"   - 처리 대상: {len(rows)}건")

        processed, skipped = 0, 0

        for row in rows:
            r_cur.execute("SELECT source_updated_at FROM embeddings_alert_events WHERE triggerid = %s", (row['triggerid'],))
            existing = r_cur.fetchone()
            
            current_ts = datetime.fromtimestamp(row['source_updated_at']) if isinstance(row['source_updated_at'], int) else row['source_updated_at']
            if existing and existing[0] == current_ts:
                skipped += 1
                continue

            # 분기 처리된 요약문 생성
            summary = generate_intelligent_summary(row)
            
            try:
                res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary}, timeout=40)
                if res.status_code == 200:
                    emb = res.json().get('embedding')
                    
                    # 💡 tags 파라미터 맵핑 추가
                    r_cur.execute("""
                        INSERT INTO embeddings_alert_events (
                            triggerid, hostid, asset, name, ip, asset_name, os, system_name, group_name,
                            value, flag, priority, tags, item_key, event_type, event_clock, resolved_clock, 
                            duration_minutes, alert_summary, embedding, source_updated_at, updated_at
                        ) VALUES (
                            %(triggerid)s, %(hostid)s, %(asset)s, %(name)s, %(ip)s, %(asset_name)s, %(os)s, %(system_name)s, %(group_name)s,
                            %(value)s, %(flag)s, %(priority)s, %(tags)s, %(item_key)s, %(event_type)s, %(event_clock)s, %(resolved_clock)s,
                            %(duration_minutes)s, %(summary)s, %(embedding)s::vector, %(current_ts)s, now()
                        ) ON CONFLICT (triggerid) DO UPDATE SET
                            value = EXCLUDED.value, flag = EXCLUDED.flag, priority = EXCLUDED.priority,
                            tags = EXCLUDED.tags, item_key = EXCLUDED.item_key, event_type = EXCLUDED.event_type,
                            resolved_clock = EXCLUDED.resolved_clock, duration_minutes = EXCLUDED.duration_minutes,
                            alert_summary = EXCLUDED.alert_summary, embedding = EXCLUDED.embedding,
                            source_updated_at = EXCLUDED.source_updated_at, updated_at = now();
                    """, {**row, 'summary': summary, 'embedding': str(emb), 'current_ts': current_ts, 'tags': row.get('tags') or '태그 없음'})
                    conn_r.commit()
                    processed += 1
            except Exception as e:
                print(f"   ❌ 개별 처리 오류 (Trigger: {row['triggerid']}): {e}")
                conn_r.rollback()

        print(f"   - [결과] 신규/갱신: {processed}건, 스킵: {skipped}건")

        # 보존 기간 초과 데이터 삭제
        r_cur.execute(
            f"DELETE FROM embeddings_alert_events "
            f"WHERE source_updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} days'"
        )
        conn_r.commit()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    except Exception as e:
        print(f"   ❌ DB 조회/처리 실패: {e}")
    finally:
        conn_z.close(); conn_r.close()
        print(f"[{datetime.now()}] ✅ 지능형 장애 임베딩 완료")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Phase B2: 일간 장애 요약(Daily Summary) 청킹 스크립트 (최종 고도화 버전)
- 주요 특징:
  1. 호스트별/일자별 1건의 브리핑으로 데이터 압축 (토큰 절약 및 거시적 통찰력 제공)
  2. 단발성(로그)과 지속형(시스템) 장애 분리 통계화
  3. [추가] 일일 최고 심각도(Max Priority) 및 최장 조치 소요시간(Max Duration) 분석 반영
  4. 미해결 장애 잔여 건수 확인 및 경고 문구 추가
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
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ==========================================
# ⚙️ 1. DB 및 API 설정
# ==========================================

CONF = {
    'RETENTION_DAYS': 30  # 청킹 데이터 보존 기간 (일)
}

PRIORITY_MAP = {0: 'Information', 1: 'Warning', 2: 'Average', 3: 'High', 4: 'Disaster', 5: 'Disaster'}

def format_duration(minutes):
    """분을 가독성 있는 텍스트로 변환"""
    if minutes is None or minutes <= 0: return "측정 불가(또는 즉시)"
    if minutes == 0: return "1분 미만"
    
    days = minutes // 1440
    hours = (minutes % 1440) // 60
    mins = minutes % 60
    res = []
    if days > 0: res.append(f"{days}일")
    if hours > 0: res.append(f"{hours}시간")
    if mins > 0: res.append(f"{mins}분")
    return " ".join(res)

# ==========================================
# 🧠 2. 일간 브리핑 텍스트 생성 로직
# ==========================================
def generate_daily_briefing(date_str, host_info, daily_alerts):
    """수집된 하루 치 알람들을 바탕으로 지능형 일간 보고서(자연어) 생성"""
    total = len(daily_alerts)
    asset_name = host_info['asset_name'] or host_info['asset'] or 'Unknown'
    ip = host_info['ip'] or 'Unknown'
    group = host_info['group_name'] or 'Unknown'

    log_alerts = []
    system_alerts = []
    unresolved_count = 0
    
    # 💡 [핵심] 최고 심각도 및 최장 지속 시간 추적 변수
    max_priority = -1
    max_duration_mins = 0

    # 이벤트 성격 분류 및 통계 계산
    for a in daily_alerts:
        event_name = a['name']
        is_log = "로그파일 -" in event_name or "LOG" in event_name
        
        # 당일 최고 심각도 갱신
        if a['priority'] > max_priority:
            max_priority = a['priority']
            
        if is_log:
            log_alerts.append(a)
        else:
            system_alerts.append(a)
            # 미해결 장애 카운트
            if a['event_type'] != 'resolved':
                unresolved_count += 1
            # 최장 조치 소요시간 갱신 (지속형 장애 대상)
            if a.get('duration_minutes') and a['duration_minutes'] > max_duration_mins:
                max_duration_mins = a['duration_minutes']

    # 진단 정보 텍스트화
    p_text = PRIORITY_MAP.get(max_priority, 'Unknown')
    duration_text = format_duration(max_duration_mins)

    # 💡 브리핑 텍스트 조립 시작
    summary_lines = [
        f"[일간 장애 브리핑] {date_str} | {asset_name} ({ip}, 그룹:{group}) 서버의 장애 요약입니다. 하루 동안 총 {total}건의 이벤트가 발생했습니다."
    ]

    # 로그/단발성 알람 요약
    if log_alerts:
        names = list(set(a['name'] for a in log_alerts))
        sample_names = ", ".join(names[:2]) + (" 등" if len(names) > 2 else "")
        summary_lines.append(f"단발성 알림(로그 감지 및 설정): {len(log_alerts)}건 발생 ({sample_names}).")

    # 시스템/지속형 장애 요약 (최고 심각도, 최장 조치시간 포함)
    if system_alerts:
        names = list(set(a['name'] for a in system_alerts))
        sample_names = ", ".join(names[:2]) + (" 등" if len(names) > 2 else "")
        summary_lines.append(f"시스템 장애(부하 등): {len(system_alerts)}건 발생 [당일 최고 심각도: {p_text} | 최장 조치 소요시간: {duration_text}] ({sample_names}).")

    # 현재 상태 결론 도출
    if unresolved_count > 0:
        summary_lines.append(f"⚠️ 주의: 아직 조치되지 않고 진행 중인 시스템 장애가 {unresolved_count}건 남아 있습니다.")
    else:
        summary_lines.append(f"✅ 현재 모든 장애 및 알림이 조치 또는 확인 완료되어 정상 상태입니다.")

    final_summary = " ".join(summary_lines)
    
    # JSONB 컬럼에 저장할 구조화된 메트릭 데이터
    metrics = {
        "total": total,
        "log_alerts": len(log_alerts),
        "system_alerts": len(system_alerts),
        "unresolved": unresolved_count,
        "max_priority": p_text,
        "max_duration_mins": max_duration_mins
    }
    
    return final_summary, metrics

# ==========================================
# 🚀 3. 메인 실행 프로세스
# ==========================================
def main():
    print(f"[{datetime.now()}] 🚀 지능형 일간 장애 브리핑(B2) 동기화 시작")
    conn = psycopg2.connect(**RAG_DB)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. B1(임베딩된 개별 이벤트) 전체 조회 (시간 오름차순)
        cur.execute("SELECT * FROM embeddings_alert_events ORDER BY event_clock ASC")
        alerts = cur.fetchall()

        # 2. 호스트별, 날짜별 데이터 그룹핑 (Dictionary 구조 활용)
        # 구조: grouped_data[hostid][date_string] = [alert1, alert2, ...]
        grouped_data = defaultdict(lambda: defaultdict(list))
        for a in alerts:
            KST = timezone(timedelta(hours=9))
            dt = datetime.fromtimestamp(a['event_clock'], tz=timezone.utc).astimezone(KST)
            date_str = dt.strftime('%Y-%m-%d')
            grouped_data[a['hostid']][date_str].append(a)

        processed = 0

        # 3. 그룹핑된 데이터를 바탕으로 일간 브리핑 생성 및 적재
        for hostid, dates in grouped_data.items():
            for date_str, daily_alerts in dates.items():
                host_info = daily_alerts[0] # 해당 그룹의 기준 메타정보 추출
                
                # 지능형 요약문 및 통계 메트릭 생성
                summary_text, metrics = generate_daily_briefing(date_str, host_info, daily_alerts)
                
                try:
                    # Ollama API를 통한 벡터 임베딩 추출
                    res = requests.post(OLLAMA_API_URL, json={"model": OLLAMA_MODEL, "prompt": summary_text}, timeout=40)
                    if res.status_code == 200:
                        embedding = res.json().get('embedding')
                        
                        # B2 청킹 테이블에 Upsert (같은 날짜, 같은 서버면 덮어쓰기하여 최신 요약 유지)
                        cur.execute("""
                            INSERT INTO embeddings_alerts_chunked (
                                hostid, asset, ip, asset_name, os, group_name,
                                target_date, total_alerts, alert_metrics, alert_summary, embedding, updated_at
                            ) VALUES (
                                %(hostid)s, %(asset)s, %(ip)s, %(asset_name)s, %(os)s, %(group_name)s,
                                %(target_date)s, %(total_alerts)s, %(alert_metrics)s, %(alert_summary)s, %(embedding)s::vector, now()
                            )
                            ON CONFLICT (hostid, target_date) DO UPDATE SET
                                total_alerts = EXCLUDED.total_alerts,
                                alert_metrics = EXCLUDED.alert_metrics,
                                alert_summary = EXCLUDED.alert_summary,
                                embedding = EXCLUDED.embedding,
                                updated_at = now();
                        """, {
                            'hostid': hostid,
                            'asset': host_info['asset'],
                            'ip': host_info['ip'],
                            'asset_name': host_info['asset_name'],
                            'os': host_info['os'],
                            'group_name': host_info['group_name'],
                            'target_date': date_str,
                            'total_alerts': metrics['total'],
                            'alert_metrics': Json(metrics),
                            'alert_summary': summary_text,
                            'embedding': str(embedding)
                        })
                        conn.commit()
                        processed += 1
                except Exception as e:
                    print(f"   ❌ 임베딩/DB 오류 (Host: {hostid}, Date: {date_str}): {e}")
                    conn.rollback()

        print(f"   - [결과] 총 {processed}일 치의 서버별 일간 브리핑 생성 및 적재 완료")

        # 보존 기간 초과 데이터 삭제
        retention_days = CONF['RETENTION_DAYS']
        cur.execute(f"DELETE FROM embeddings_alerts_chunked WHERE updated_at < now() - INTERVAL '{retention_days} days'")
        conn.commit()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    except Exception as e:
        print(f"   ❌ 전체 프로세스 실패: {e}")
    finally:
        conn.close()
        print(f"[{datetime.now()}] ✅ 지능형 일간 장애 브리핑(B2) 종료")

if __name__ == "__main__":
    main()
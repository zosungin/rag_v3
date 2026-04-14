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
import re
from datetime import datetime, timezone, timedelta
from psycopg2.extras import RealDictCursor

# 1. 설정 정보

CONF = {
    'RETENTION_DAYS': 30,
    'MAX_PROMPT_CHARS': 900,   # 최종 안전망 (섹션별 예산 합계 ~780자 기준, 초과 시에만 작동)
    'OLLAMA_NUM_CTX': 2048,    # Ollama context window 명시적 확장 (기본값 512 토큰)
}

# 2. 유틸리티 함수
def truncate_text(text, max_len):
    """지정한 길이만큼 텍스트를 자르고 생략 표시 추가"""
    if not text: return ""
    return (text[:max_len] + '..[Truncated]') if len(text) > max_len else text

def extract_template(log_text):
    """로그 내 가변 데이터를 마스킹하여 뼈대 추출"""
    tmp = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\d.+-:]+', '{ISO_TIME}', log_text)
    tmp = re.sub(r'[a-zA-Z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}', '{TIME}', tmp)
    tmp = re.sub(r'\[\d+\]', '[{PID}]', tmp)
    tmp = re.sub(r'id=[a-f0-9]{16,}', 'id={ID}', tmp)
    return truncate_text(tmp, 500)

def parse_log_details(log_text, severity):
    """로그 발생원과 레벨 파싱"""
    parts = log_text.split(None, 4)
    source = "System"
    if len(parts) >= 5:
        source_match = re.search(r'^([a-zA-Z0-9\-_.]+)', parts[4])
        if source_match: source = source_match.group(1)
    level = str(severity or "").upper()
    if not level or level == "NONE":
        lower_log = log_text.lower()
        if any(kw in lower_log for kw in ["warning", "warn"]): level = "WARNING"
        elif any(kw in lower_log for kw in ["error", "fail", "critical"]): level = "ERROR"
        else: level = "INFO"
    return source, level

def sanitize_prompt(text: str) -> str:
    """JSON 직렬화 오류를 유발하는 문자 제거
    - NULL 바이트 (\\x00)      : Ollama JSON 파싱 실패 → HTTP 500
    - ASCII 제어문자 (\\x01-\\x08, \\x0b-\\x1f) : 탭(\\t)/개행(\\n) 은 유지
    - 유효하지 않은 UTF-8 문자 : encode/decode 로 정제
    """
    # 1) 인코딩 오류 문자 제거
    text = text.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
    # 2) NULL 바이트 및 불필요한 제어문자 제거 (탭·LF·CR 은 허용)
    allowed_ctrl = {'\t', '\n', '\r'}
    text = ''.join(c if (c >= ' ' or c in allowed_ctrl) else ' ' for c in text)
    return text

def send_embedding(prompt: str, uid: str):
    """Ollama 임베딩 요청. 텍스트 정제 → 길이 절단 → 전송."""
    # ✅ 텍스트 정제 (NULL 바이트·제어문자 제거)
    prompt = sanitize_prompt(prompt)

    max_chars = CONF['MAX_PROMPT_CHARS']
    if len(prompt) > max_chars:
        print(f"   [경고] 프롬프트 길이 초과 ({len(prompt)}자 → {max_chars}자 절단): {uid[:30]}")
        prompt = prompt[:max_chars] + '..[Truncated]'

    res = requests.post(
        OLLAMA_API_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "options": {"num_ctx": CONF['OLLAMA_NUM_CTX']}},
        timeout=60
    )
    if res.status_code != 200:
        # ✅ 응답 본문까지 출력해 원인 파악
        print(f"   [API 에러] {uid[:30]} HTTP {res.status_code} | 프롬프트길이: {len(prompt)}자")
        try:
            print(f"   [API 응답] {res.json()}")
        except Exception:
            print(f"   [API 응답] {res.text[:300]}")
        return None
    return res.json().get('embedding')


def fetch_log_context(z_cur, itemid, clock):
    """전후 문맥을 각 1건씩 추출 (섹션별 예산 제한)"""
    z_cur.execute("""
        SELECT logs FROM svr_log_messages 
        WHERE itemid = %s AND clock <= %s AND LENGTH(logs) > 15 
        ORDER BY clock DESC, created_at DESC LIMIT 1
    """, (itemid, clock))
    row_b = z_cur.fetchone()
    before = truncate_text(row_b['logs'], 150) if row_b else ""  # 150자 예산

    z_cur.execute("""
        SELECT logs FROM svr_log_messages 
        WHERE itemid = %s AND clock > %s AND LENGTH(logs) > 15
        ORDER BY clock ASC, created_at ASC LIMIT 1
    """, (itemid, clock))
    row_a = z_cur.fetchone()
    after = truncate_text(row_a['logs'], 80) if row_a else ""    # 80자 예산

    return before, after

def generate_log_summary(d, template, count, before, after):
    """본문 1,500자 제한 및 요약문 구성"""
    KST = timezone(timedelta(hours=9))
    kst_ts = d['source_updated_at'].replace(tzinfo=timezone.utc).astimezone(KST) \
             if d['source_updated_at'].tzinfo is None \
             else d['source_updated_at'].astimezone(KST)
    event_time = kst_ts.strftime('%Y-%m-%d %H:%M:%S')
    main_log = truncate_text(d['logs'], 400)   # 400자 예산 (한국어 혼합 ~2토큰/자 기준 800토큰 이내)

    # ✅ [추가] device_type을 prefix에 포함
    prefix = (
        f"발생 시각: {event_time} | 호스트 ID: {d['hostid']} | IP: {d['ip']} | "
        f"시스템: {d['system_name']} | 장비 유형: {d['device_type']}"
    )
    status  = f"진단: {d['log_level']} 로그 검출 (발생원: {d['log_source']})"
    details = f"패턴: {template}"

    summary = f"{prefix} | {status} | {details}\n[에러 본문]\n{main_log}"
    if before: summary += f"\n[직전 상황]\n{before}"
    if after:  summary += f"\n[직후 상황]\n{after}"

    return summary

def main():
    print(f"[{datetime.now()}] 🚀 서버 로그 동기화 시작 (장비 유형 분류 포함)")
    conn_z = psycopg2.connect(**ZABBIX_DB); conn_r = psycopg2.connect(**RAG_DB)
    z_cur = conn_z.cursor(cursor_factory=RealDictCursor)
    r_cur = conn_r.cursor(cursor_factory=RealDictCursor)

    try:
        # ✅ [핵심 수정] svr_system_main + nw_main1 동시 LEFT JOIN → device_type CASE 분기
        # - svr_system_main 매칭  → '서버'
        # - nw_main1 매칭         → '네트워크 장비'
        # - 양쪽 모두 미매칭      → '미분류'
        z_cur.execute("""
            SELECT
                l.*,
                COALESCE(s.asset_name,   n.asset,       l.hostname)          AS asset_name,
                COALESCE(s.ip,           n.ip,           'N/A')               AS ip,
                COALESCE(s.os,           'UNKNOWN')                           AS os,
                COALESCE(s.system_name,  n.device_name,  l.hostname)          AS system_name,
                COALESCE(s.group_name,   n.group_name,   'Managed Servers')   AS group_name,
                CASE
                    WHEN s.hostid IS NOT NULL THEN '서버'
                    WHEN n.hostid IS NOT NULL THEN '네트워크 장비'
                    ELSE '미분류'
                END AS device_type
            FROM svr_log_messages l
            LEFT JOIN svr_system_main s ON l.hostid = s.hostid
            LEFT JOIN nw_main1        n ON l.hostid = n.hostid
            WHERE l.flag = 'y'
        """)
        rows = z_cur.fetchall()
        print(f"   - 검출된 후보: {len(rows)}건")

        p_count = 0; s_count = 0

        for row in rows:
            uid = row['uid']

            # RAG DB 중복 체크
            r_cur.execute("SELECT 1 FROM embeddings_server_logs WHERE uid = %s LIMIT 1", (uid,))
            if r_cur.fetchone():
                s_count += 1
                continue

            # 데이터 가공
            template = extract_template(row['logs'])
            l_source, l_level = parse_log_details(row['logs'], row['severity'])
            ts = row['created_at'] if row['created_at'].tzinfo is not None \
                 else row['created_at'].replace(tzinfo=timezone.utc)
            row.update({'log_source': l_source, 'log_level': l_level, 'source_updated_at': ts})

            before, after = fetch_log_context(z_cur, row['itemid'], row['clock'])
            summary = generate_log_summary(row, template, 1, before, after)

            try:
                emb = send_embedding(summary, uid)
                if emb is not None:
                    # ✅ [추가] device_type 컬럼 INSERT
                    r_cur.execute("""
                        INSERT INTO embeddings_server_logs (
                            uid, hostid, hostname, ip, asset_name, os,
                            system_name, group_name, device_type,
                            itemid, log_files, log_level, log_source, logs, log_template,
                            occurrence_count, first_seen_at, last_seen_at,
                            context_before, context_after,
                            log_summary, embedding, source_updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            1, %s, %s,
                            %s, %s,
                            %s, %s::vector, %s
                        )
                    """, (
                        uid, row['hostid'], row['hostname'], row['ip'], row['asset_name'], row['os'],
                        row['system_name'], row['group_name'], row['device_type'],
                        row['itemid'], row['log_files'], l_level, l_source, row['logs'], template,
                        ts, ts,
                        before, after,
                        summary, str(emb), ts
                    ))
                    conn_r.commit()
                    p_count += 1
                else:
                    pass  # send_embedding 내부에서 이미 에러 출력
            except Exception as e:
                print(f"   [예외 발생] {e}")
                conn_r.rollback()

        print(f"   - 결과: 신규 {p_count}건 저장 / 중복 {s_count}건 건너뜀")

        # 보존 기간 초과 데이터 삭제
        r_cur.execute(
            f"DELETE FROM embeddings_server_logs "
            f"WHERE source_updated_at < now() - INTERVAL '{CONF['RETENTION_DAYS']} days'"
        )
        conn_r.commit()
        print(f"   - 보존 기간({CONF['RETENTION_DAYS']}일) 초과 데이터 삭제 완료")

    finally:
        conn_z.close(); conn_r.close()
        print(f"[{datetime.now()}] ✅ 프로세스 종료")

if __name__ == "__main__":
    main()
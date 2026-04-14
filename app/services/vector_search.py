# /app/app/services/vector_search.py
"""
벡터 검색 함수 구현 v3.9
- v3.2: 테이블별 균형 보장
- v3.3: 키워드 부스팅 — 질의 핵심 키워드가 summary에 포함된 결과 우선
- v3.4: embeddings_inventory 부스팅 추가 (→ v3.9에서 대체)
- v3.9: embeddings_inventory 테이블 삭제에 따라 embeddings_hosts 부스팅으로 대체
- v3.5: 다중 카테고리(categories 필드) 지원 — 'log' 카테고리로 server_logs 테이블 검색 가능
- v3.6: multi_row 테이블(3일 누적) 지원 — uid별 최신 행만 검색하여 중복 방지
       안정성 키워드 부스팅 추가 (stability_level 기반)
- v3.7: uid 없는 multi_row 테이블(서버 임베딩) 지원
       — hostid + sub_key(fs_name/interface_name/pid) 조합 기반 최신 행 중복 제거
       — uid 컬럼 없이 PK(hostid, [sub_key,] source_updated_at)인 테이블에 적용
- v3.8: balance_results 개선
       — 단일 카테고리 요청 시 balancing 우회 (순수 유사도 순 반환)
       — max_total = max(top_k*2, active_table_count*2) 공식으로 truncation 방지
       — min_per_table을 active_table_count 기반으로 동적 산정
"""

import re
from typing import List, Dict, Optional
import logging
import asyncpg
from app.db.connection import get_pool
from app.db.tables import EMBEDDING_TABLES
from app.services.embedding import generate_embedding

logger = logging.getLogger(__name__)

_column_cache = {}


async def get_table_columns(conn: asyncpg.Connection, table_name: str) -> List[str]:
    if table_name in _column_cache:
        return _column_cache[table_name]
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = $1
    """
    rows = await conn.fetch(query, table_name)
    columns = [row['column_name'] for row in rows]
    _column_cache[table_name] = columns
    return columns


def get_os_expression(table_columns: List[str]) -> str:
    if 'os' in table_columns: return 'os'
    elif 'ios' in table_columns: return 'ios'
    elif 'device_info' in table_columns: return "device_info->>'ios'"
    else: return "NULL"


def get_ip_expression(table_columns: List[str]) -> str:
    if 'ip' in table_columns: return 'ip'
    elif 'asset_ip' in table_columns: return 'asset_ip'
    elif 'server_ip' in table_columns: return 'server_ip'
    else: return "NULL"


def get_asset_expression(table_columns: List[str]) -> str:
    if 'asset' in table_columns: return 'asset'
    elif 'asset_name' in table_columns: return 'asset_name'
    else: return "NULL"


def get_group_expression(table_columns: List[str]) -> str:
    if 'group_name' in table_columns: return 'group_name'
    else: return "NULL"


def get_time_column(table_columns: List[str]) -> Optional[str]:
    if 'source_updated_at' in table_columns: return 'source_updated_at'
    elif 'time_bucket' in table_columns: return 'time_bucket'
    elif 'last_seen_at' in table_columns: return 'last_seen_at'
    elif 'created_at' in table_columns: return 'created_at'
    return None


# ============================================================
# 단일 테이블 검색
# ============================================================
async def search_single_table(
    conn: asyncpg.Connection,
    table_info: Dict,
    query_embedding: List[float],
    top_k: int = 5,
    filters: Optional[Dict] = None
) -> List[Dict]:
    table_name = table_info['table']
    summary_column = table_info['summary_column']
    category = table_info['category']
    is_multi_row = table_info.get('multi_row', False)

    embedding_str = '[' + ','.join(map(str, query_embedding)) + ']'
    table_columns = await get_table_columns(conn, table_name)

    os_expr = get_os_expression(table_columns)
    ip_expr = get_ip_expression(table_columns)
    asset_expr = get_asset_expression(table_columns)
    group_expr = get_group_expression(table_columns)

    has_asset_name = 'asset_name' in table_columns
    asset_name_expr = 'asset_name' if has_asset_name else asset_expr

    where_clauses = []
    params = []
    param_idx = 2

    if filters:
        if 'hostid' in filters and 'hostid' in table_columns:
            where_clauses.append(f"hostid = ${param_idx}")
            params.append(filters['hostid'])
            param_idx += 1

        time_col = get_time_column(table_columns)
        if time_col:
            if 'start_time' in filters:
                where_clauses.append(f"{time_col} >= ${param_idx}")
                params.append(filters['start_time'])
                param_idx += 1
            if 'end_time' in filters:
                where_clauses.append(f"{time_col} <= ${param_idx}")
                params.append(filters['end_time'])
                param_idx += 1

    where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # v3.7: multi_row 테이블 분류
    #   - uid 있음 (네트워크 인터페이스): uid 기반 최신 행 중복 제거
    #   - uid 없음 (서버 임베딩): hostid + sub_key 조합 기반 최신 행 중복 제거
    has_uid = 'uid' in table_columns
    has_source_ts = 'source_updated_at' in table_columns
    sub_key = table_info.get('sub_key')       # fs_name / interface_name / pid 등
    needs_dedup = is_multi_row and has_source_ts and (has_uid or 'hostid' in table_columns)

    if needs_dedup:
        # 3일 누적 데이터이므로 동일 엔티티가 여러 행 → top_k의 5배로 검색
        fetch_limit = top_k * 5
    else:
        fetch_limit = top_k

    limit_param = f"${param_idx}"
    params.append(fetch_limit)

    # SELECT에 포함할 추가 컬럼 결정 (중복 제거 키용 + 메타 필드)
    extra_cols = ''
    if needs_dedup:
        if has_uid:
            extra_cols += 'uid,'
        if sub_key and sub_key in table_columns:
            extra_cols += f'{sub_key},'
        extra_cols += 'source_updated_at,'

    # v3.7: priority, stability_level 컬럼이 있으면 SELECT에 포함 (LLM 중요도 판단용)
    meta_cols = ''
    if 'priority' in table_columns:
        meta_cols += 'priority,'
    if 'stability_level' in table_columns:
        meta_cols += 'stability_level,'
    # source_updated_at이 extra_cols에 없는데 컬럼이 있으면 추가
    if 'source_updated_at' in table_columns and 'source_updated_at,' not in extra_cols:
        meta_cols += 'source_updated_at,'

    query = f"""
        SELECT
            '{table_name}' as source_table,
            '{category}' as category,
            {summary_column} as summary,
            hostid,
            {ip_expr} as ip,
            {asset_expr} as asset,
            {asset_name_expr} as asset_name,
            {os_expr} as os,
            {group_expr} as group_name,
            {extra_cols}
            {meta_cols}
            embedding <=> $1::vector as distance,
            1 - (embedding <=> $1::vector) as similarity
        FROM {table_name}
        {where_clause}
        ORDER BY embedding <=> $1::vector
        LIMIT {limit_param}
    """

    try:
        rows = await conn.fetch(query, embedding_str, *params)
        results = []

        if needs_dedup:
            # ── 엔티티별 최신 행만 유지 (벡터 유사도 순으로 이미 정렬됨) ──
            # 복합 키: uid(있으면) + sub_key(있으면) + hostid
            seen_keys = {}  # composite_key → (source_updated_at, row)
            for row in rows:
                updated_at = row.get('source_updated_at')
                # 중복 제거 복합 키 생성
                if has_uid:
                    ent_key = row.get('uid')
                else:
                    # uid 없음 → hostid + sub_key 조합
                    hostid_val = row.get('hostid')
                    sub_val = row.get(sub_key) if sub_key else None
                    ent_key = (hostid_val, sub_val) if sub_val is not None else hostid_val

                if ent_key in seen_keys:
                    # 동일 엔티티: 더 최신인 행만 유지
                    if updated_at and updated_at > seen_keys[ent_key][0]:
                        seen_keys[ent_key] = (updated_at, row)
                else:
                    seen_keys[ent_key] = (updated_at, row)

            # 유사도 순으로 재정렬 후 top_k 제한
            deduped_rows = [v[1] for v in seen_keys.values()]
            deduped_rows.sort(key=lambda r: r['distance'])
            target_rows = deduped_rows[:top_k]
        else:
            target_rows = rows

        for row in target_rows:
            result_item = {
                'source_table': row['source_table'],
                'category': row['category'],
                'summary': row['summary'],
                'hostid': row.get('hostid'),
                'ip': row.get('ip'),
                'asset': row.get('asset'),
                'asset_name': row.get('asset_name'),
                'os': row.get('os'),
                'group_name': row.get('group_name'),
                'distance': float(row['distance']),
                'similarity': float(row['similarity']),
            }
            # v3.7: 메타 필드 전달 (priority, stability_level, source_updated_at)
            # — 임베딩 테이블에 해당 컬럼이 있을 경우 row에 포함되어 반환됨
            for meta_col in ('priority', 'stability_level', 'source_updated_at'):
                if meta_col in row.keys() and row[meta_col] is not None:
                    result_item[meta_col] = row[meta_col]
            results.append(result_item)
        return results
    except Exception as e:
        logger.error(f"테이블 {table_name} 검색 실패: {e}")
        return []


# ============================================================
# v3.3: 키워드 부스팅 — 질의 핵심 키워드가 summary에 있으면 우선
# ============================================================
BOOST_KEYWORD_MAP = {
    # 질의 키워드 → summary에서 찾을 키워드 (대소문자 무시)
    'up': ['Link Up', 'link up', 'UP', '활성', 'Active', 'active'],
    'down': ['Link Down', 'link down', 'DOWN', '비활성', 'Inactive'],
    'link up': ['Link Up', 'link up'],
    'link down': ['Link Down', 'link down'],
    '활성': ['활성', 'Active', 'Link Up', 'UP'],
    '비활성': ['비활성', 'Inactive', 'Link Down', 'DOWN'],
    '장애': ['장애', 'Problem', 'Active', 'Disaster', 'High', 'Warning'],
    '해결': ['해결', 'Resolved'],
    '에러': ['error', 'Error', 'ERROR', '에러'],
    '경고': ['warning', 'Warning', '경고', '경고(포트'],
    # v3.4: 자산/장비 관련 부스팅
    '자산': ['자산번호', '자산명', '장비구분', 'SERVER', 'NETWORK'],
    '장비': ['장비구분', 'SERVER', 'NETWORK', '자산명', '활성'],
    '서버': ['SERVER', '서버', 'LINUX', 'linux', 'Windows'],
    '네트워크': ['NETWORK', '네트워크', 'Aruba', 'Switch', 'switch'],
    '인벤토리': ['자산번호', '장비구분', 'SERVER', 'NETWORK', '호스트명', '운영체제'],
    # v3.6: 안정성 진단 관련 부스팅
    '안정': ['안정적', '안정', 'Stable', '변동성 정상'],
    '불안정': ['불안정', 'Unstable', '변동성 높음'],
    # 답변 품질: 리소스·메트릭 질의 → 해당 임베딩 summary 와 정합
    'cpu': ['cpu', 'CPU', 'processor', '사용률', 'utilization'],
    'memory': ['memory', 'Memory', '메모리', 'RAM', 'swap', 'Swap'],
    'disk': ['filesystem', 'disk', '디스크', '용량', 'fs', '마운트', 'inode'],
    'alert': ['alert', '알림', '트리거', '장애', 'Problem', 'event'],
}

# 질의에서 부스팅할 키워드 추출에 사용할 패턴
BOOST_QUERY_PATTERNS = [
    (r'\bup\b', 'up'),
    (r'\bdown\b', 'down'),
    (r'link\s*up', 'link up'),
    (r'link\s*down', 'link down'),
    (r'활성', '활성'),
    (r'비활성', '비활성'),
    (r'장애', '장애'),
    (r'해결', '해결'),
    (r'에러|error', '에러'),
    (r'경고|warning', '경고'),
    # v3.4: 자산/장비 부스팅 패턴
    (r'자산', '자산'),
    (r'장비', '장비'),
    (r'인벤토리|inventory', '인벤토리'),
    # v3.6: 안정성 부스팅 패턴
    (r'안정|stable', '안정'),
    (r'불안정|unstable', '불안정'),
    (r'\bcpu\b|프로세서|사용률.*cpu|cpu.*사용', 'cpu'),
    (r'memory|메모리|ram|스왑|swap', 'memory'),
    (r'disk|디스크|파일시스템|filesystem|용량|마운트|inode|여유', 'disk'),
    (r'알림|장애|트리거|이벤트|alert|problem', 'alert'),
]

BOOST_SCORE = 0.08  # 부스팅 보너스 (유사도에 더함)


def _get_boosted_tables(query_lower: str) -> set:
    """
    질의 키워드에 따라 top_k를 증가시킬 테이블 결정.
    "포트 목록"이면 interfaces 테이블에서 30건 가져와야 16개 UP 포트 모두 포함 가능.
    """
    boosted = set()

    # 네트워크 포트/인터페이스 질의
    if any(kw in query_lower for kw in ['포트', 'port', '인터페이스', 'interface', 'link', 'up', 'down']):
        boosted.add('embeddings_network_interfaces')

    # 서버 포트 질의
    if any(kw in query_lower for kw in ['listen', '리슨', '서비스 포트', '오픈 포트', 'open port']):
        boosted.add('embeddings_server_listen_ports')
        boosted.add('embeddings_server_listen_ports_chunked')

    # 장애 전체 목록
    if any(kw in query_lower for kw in ['장애', 'alert', '이벤트']):
        boosted.add('embeddings_alert_events')
        boosted.add('embeddings_alerts_chunked')

    # 프로세스 목록
    if any(kw in query_lower for kw in ['프로세스', 'process', 'pid']):
        boosted.add('embeddings_process_resources')
        boosted.add('embeddings_server_process_topn_chunked')

    # CPU / 메모리 메트릭 (검색 품질·RAG 근거)
    if any(kw in query_lower for kw in ['cpu', '프로세서', '사용률']):
        boosted.add('embeddings_server_cpu')
        boosted.add('embeddings_server_performance_chunked')

    if any(kw in query_lower for kw in ['memory', '메모리', 'ram', 'swap', '스왑']):
        boosted.add('embeddings_server_memory')
        boosted.add('embeddings_server_swap')
        boosted.add('embeddings_server_performance_chunked')

    if any(kw in query_lower for kw in ['disk', '디스크', 'filesystem', '파일시스템', '용량', 'inode', '여유']):
        boosted.add('embeddings_server_filesystems')
        boosted.add('embeddings_server_performance_chunked')

    # 로그
    if any(kw in query_lower for kw in ['로그', 'log', 'errpt']):
        boosted.add('embeddings_server_logs')
        boosted.add('embeddings_server_logs_chunked')

    # 연결
    if any(kw in query_lower for kw in ['연결', 'connection']):
        boosted.add('embeddings_server_connections')
        boosted.add('embeddings_server_connections_chunked')

    # v3.9: 자산/장비/인벤토리 질의 → embeddings_hosts 부스팅 (embeddings_inventory 테이블 삭제됨)
    if any(kw in query_lower for kw in [
        '자산', '장비', '인벤토리', 'inventory', '호스트',
        '서버 목록', '장비 목록', '서버 정보', '장비 정보',
        '몇 대', '몇대', '인프라', '구성'
    ]):
        boosted.add('embeddings_hosts')
        boosted.add('embeddings_infra_inventory_chunked')

    return boosted


def apply_keyword_boost(results: List[Dict], query: str) -> List[Dict]:
    """질의 키워드가 summary에 포함된 결과에 유사도 보너스 부여"""
    query_lower = query.lower()

    # 질의에서 부스팅 키워드 추출
    boost_targets = []
    for pattern, key in BOOST_QUERY_PATTERNS:
        if re.search(pattern, query_lower):
            targets = BOOST_KEYWORD_MAP.get(key, [])
            boost_targets.extend(targets)

    if not boost_targets:
        return results

    logger.info(f"[Boost] 키워드 부스팅 적용: {boost_targets}")

    for r in results:
        summary = r.get('summary', '') or ''
        # 비사용 포트는 장애 부스팅 대상에서 제외
        if '비사용 포트' in summary:
            continue
        matched = any(kw in summary for kw in boost_targets)
        if matched:
            r['similarity'] = min(1.0, r['similarity'] + BOOST_SCORE)
            r['_boosted'] = True

    # 재정렬
    results.sort(key=lambda x: x['similarity'], reverse=True)
    boosted_count = sum(1 for r in results if r.get('_boosted'))
    logger.info(f"[Boost] {boosted_count}건 부스팅됨 / 전체 {len(results)}건")

    return results


# ============================================================
# 테이블별 균형 보장 (v3.8 개선)
# ============================================================
def balance_results(
    all_results: List[Dict],
    max_total: int,
    single_category: bool = False
) -> List[Dict]:
    """
    v3.8 개선:
    - single_category=True 이면 balancing 우회 → 순수 유사도 순 정렬만 적용
    - max_total이 active_table_count 기반으로 동적 산정되어 truncation 최소화
    - min_per_table을 active_table_count 기반으로 동적 산정
    """
    if len(all_results) <= max_total:
        return sorted(all_results, key=lambda x: x['similarity'], reverse=True)

    # ── v3.8: 단일 카테고리면 유사도 순만 적용 (balancing 불필요)
    if single_category:
        return sorted(all_results, key=lambda x: x['similarity'], reverse=True)[:max_total]

    table_groups = {}
    for r in all_results:
        tbl = r['source_table']
        if tbl not in table_groups:
            table_groups[tbl] = []
        table_groups[tbl].append(r)

    for tbl in table_groups:
        table_groups[tbl].sort(key=lambda x: x['similarity'], reverse=True)

    num_tables = len(table_groups)
    if num_tables == 0:
        return []

    # ── v3.8: min_per_table을 active_table_count 기반으로 동적 산정
    # 최소 보장 건수: max_total을 테이블 수 + 1로 나누되 최소 1건 보장
    min_per_table = max(1, max_total // (num_tables + 1))
    max_per_table = max(3, int(max_total * 0.5))

    selected = []
    used_keys = set()

    for tbl, items in table_groups.items():
        if len(selected) >= max_total:
            break
        count = 0
        for item in items:
            if count >= min_per_table:
                break
            if len(selected) >= max_total:
                break
            key = id(item)
            if key not in used_keys:
                selected.append(item)
                used_keys.add(key)
                count += 1

    remaining = max_total - len(selected)
    if remaining > 0:
        all_sorted = sorted(all_results, key=lambda x: x['similarity'], reverse=True)
        table_counts = {}
        for s in selected:
            tbl = s['source_table']
            table_counts[tbl] = table_counts.get(tbl, 0) + 1

        for item in all_sorted:
            if remaining <= 0:
                break
            key = id(item)
            if key in used_keys:
                continue
            tbl = item['source_table']
            if table_counts.get(tbl, 0) >= max_per_table:
                continue
            selected.append(item)
            used_keys.add(key)
            table_counts[tbl] = table_counts.get(tbl, 0) + 1
            remaining -= 1

    selected.sort(key=lambda x: x['similarity'], reverse=True)
    return selected


# ============================================================
# 통합 벡터 검색 (v3.9: embeddings_hosts 부스팅)
# ============================================================
async def vector_search(
    query_text: str,
    top_k: int = 5,
    categories: Optional[List[str]] = None,
    filters: Optional[Dict] = None
) -> List[Dict]:
    logger.info(f"벡터 검색 시작: query={query_text[:50]}, top_k={top_k}, categories={categories}")

    try:
        query_embedding = await generate_embedding(query_text)
        pool = await get_pool()

        async with pool.acquire() as conn:
            all_results = []
            query_lower = query_text.lower()

            # v3.3: 질의에 따라 특정 테이블의 top_k를 증가
            boosted_tables = _get_boosted_tables(query_lower)

            for table_key, table_info in EMBEDDING_TABLES.items():
                # v3.5: 다중 카테고리 지원
                # tables.py에 'categories' 필드가 있으면 그것을 사용,
                # 없으면 기본 'category' 단일 값을 리스트로 변환
                if categories:
                    table_cats = table_info.get('categories') or [table_info['category']]
                    if not any(c in categories for c in table_cats):
                        continue

                # 부스팅 대상 테이블은 top_k를 3배로
                table_name = table_info['table']
                effective_top_k = top_k
                if table_name in boosted_tables:
                    effective_top_k = max(top_k * 3, 30)
                    logger.info(f"[TableBoost] {table_name} top_k={effective_top_k}")

                results = await search_single_table(
                    conn=conn,
                    table_info=table_info,
                    query_embedding=query_embedding,
                    top_k=effective_top_k,
                    filters=filters
                )
                all_results.extend(results)

            # v3.3: 키워드 부스팅 적용 (균형 보장 전)
            all_results = apply_keyword_boost(all_results, query_text)

            # v3.8: 활성 테이블 수 카운트
            active_table_count = len(set(r['source_table'] for r in all_results)) if all_results else 1

            # v3.8: single_category 판정 — categories 인자가 단 1개이면 balancing 우회
            is_single_cat = bool(categories and len(categories) == 1)

            # v3.8: max_results = max(top_k*2, active_table_count*2) — truncation 최소화
            max_results = max(top_k * 2, active_table_count * 2)

            logger.info(
                f"[Balance] active_tables={active_table_count}, single_cat={is_single_cat}, "
                f"max_results={max_results} (top_k={top_k})"
            )

            final_results = balance_results(all_results, max_results, single_category=is_single_cat)

            # 테이블별 통계 로깅
            table_stats = {}
            for r in final_results:
                tbl = r['source_table']
                table_stats[tbl] = table_stats.get(tbl, 0) + 1
            logger.info(f"검색 완료: {len(final_results)}개 결과, 테이블별: {table_stats}")

            return final_results

    except Exception as e:
        logger.error(f"벡터 검색 실패: {e}")
        raise


async def search_server_resources(query_text: str, top_k: int = 5) -> List[Dict]:
    return await vector_search(query_text, top_k=top_k, categories=['server'])

async def search_network_resources(query_text: str, top_k: int = 5) -> List[Dict]:
    return await vector_search(query_text, top_k=top_k, categories=['network'])

async def search_alerts(query_text: str, top_k: int = 5) -> List[Dict]:
    return await vector_search(query_text, top_k=top_k, categories=['alert'])

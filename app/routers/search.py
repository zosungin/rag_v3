# /app/app/routers/search.py
"""
벡터 검색 API 엔드포인트
POST /api/search - 벡터 유사도 검색만 수행
"""

from fastapi import APIRouter, HTTPException
import logging

from app.models.request import SearchRequest
from app.services.vector_search import vector_search
from app.db.tables import VECTOR_TABLES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/search")
async def unified_search(request: SearchRequest):
    """
    통합 검색 API
    """
    try:
        # 카테고리 매핑
        categories = request.categories
        if not categories and request.tables:
            category_map = {
                'server': [
                    'hosts', 'cpu', 'memory', 'swap', 'server_interfaces', 'filesystems', 'processes',
                    'listen_ports', 'connections', 'server_logs', 'server_errpt',
                    'server_performance_chunked', 'server_logs_chunked', 'listen_ports_chunked',
                    'connections_chunked', 'server_errpt_chunked', 'process_topn'
                ],
                'network': [
                    'network_devices', 'network_interfaces',
                    'network_performance_chunked'
                ],
                'alert': [
                    'alerts', 'alerts_chunked'
                ],
                'infra': [
                    'infra_inventory'
                ]
            }
            
            categories = []
            for table in request.tables:
                for cat, tables in category_map.items():
                    if table in tables and cat not in categories:
                        categories.append(cat)
        
        # 벡터 검색
        results = await vector_search(
            query_text=request.query,
            top_k=request.top_k,
            categories=categories
        )
        
        # 결과 포맷팅
        formatted_results = []
        for result in results:
            formatted_results.append({
                "table": result['source_table'].replace('embeddings_', '').replace('_chunked', ''),
                "table_name": result['source_table'],
                "category": result['category'],
                "score": result['similarity'],
                "data": {
                    'hostid': result.get('hostid'),
                    'asset': result.get('asset'),
                    'asset_name': result.get('asset_name'),
                    'ip': result.get('ip'),
                    'os': result.get('os'),
                    'group_name': result.get('group_name'),
                    'summary': result['summary']
                }
            })
        
        return {
            "query": request.query,
            "results": formatted_results,
            "total_count": len(formatted_results)
        }
        
    except Exception as e:
        logger.error(f"Search API 오류: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

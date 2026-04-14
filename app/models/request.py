# /app/app/models/request.py
"""
API 요청 모델 정의
"""

from pydantic import BaseModel
from typing import List, Optional


class SearchRequest(BaseModel):
    """벡터 검색 요청"""
    query: str
    top_k: Optional[int] = 5
    time_filter: Optional[str] = None       # "1h", "24h", "7d"
    priority_filter: Optional[List[int]] = None  # [0,1,2,3]
    tables: Optional[List[str]] = None      # 검색할 테이블 선택
    categories: Optional[List[str]] = None  # ['server', 'network', 'alert']


class ChatRequest(BaseModel):
    """RAG 채팅 요청"""
    query: str
    top_k: Optional[int] = 5
    categories: Optional[List[str]] = None

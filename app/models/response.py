# /app/app/models/response.py
"""
5개 유형별 응답 모델 정의
- status: 상태 조회 (카드형)
- list: 목록 조회 (테이블형)
- alert: 장애 조회 (타임라인형)
- analysis: 비교/분석 (요약+테이블형)
- general: 일반 질문 (텍스트형)
"""

from pydantic import BaseModel
from typing import List, Optional, Dict, Any


# ============================================================
# 공통 응답 구조
# ============================================================
class HostInfo(BaseModel):
    """호스트 기본 정보"""
    hostid: Optional[int] = None
    asset: Optional[str] = None
    ip: Optional[str] = None
    os: Optional[str] = None
    group_name: Optional[str] = None
    system_name: Optional[str] = None


class MetricItem(BaseModel):
    """수치 항목"""
    label: str
    value: str
    unit: Optional[str] = None
    status: Optional[str] = None  # "normal", "warning", "critical"


# ============================================================
# 유형 1: 상태 조회 (카드형)
# ============================================================
class StatusResponse(BaseModel):
    query_type: str = "status"
    title: str
    host: HostInfo
    metrics: List[MetricItem]
    analysis: str
    measured_at: Optional[str] = None


# ============================================================
# 유형 2: 목록 조회 (테이블형)
# ============================================================
class ListItem(BaseModel):
    """목록 행"""
    rank: Optional[int] = None
    host: Optional[str] = None
    ip: Optional[str] = None
    values: Dict[str, str] = {}
    status: Optional[str] = None


class ListResponse(BaseModel):
    query_type: str = "list"
    title: str
    columns: List[str]
    items: List[ListItem]
    total_count: int
    analysis: str
    measured_at: Optional[str] = None


# ============================================================
# 유형 3: 장애 조회 (타임라인형)
# ============================================================
class AlertItem(BaseModel):
    """장애 이벤트"""
    time: str
    severity: str       # "Disaster", "High", "Average", "Warning", "Information"
    severity_level: int  # 5,4,3,2,1
    host: str
    ip: Optional[str] = None
    alert_name: str
    status: str          # "Active", "Resolved"


class AlertResponse(BaseModel):
    query_type: str = "alert"
    title: str
    total_count: int
    active_count: int
    resolved_count: int
    alerts: List[AlertItem]
    analysis: str
    measured_at: Optional[str] = None


# ============================================================
# 유형 4: 비교/분석 (요약+테이블형)
# ============================================================
class AnalysisItem(BaseModel):
    """분석 항목"""
    rank: Optional[int] = None
    host: str
    ip: Optional[str] = None
    value: str
    std: Optional[str] = None
    stability: Optional[str] = None


class AnalysisResponse(BaseModel):
    query_type: str = "analysis"
    title: str
    summary: str
    columns: List[str]
    items: List[AnalysisItem]
    recommendation: str
    measured_at: Optional[str] = None


# ============================================================
# 유형 5: 일반 질문 (텍스트형)
# ============================================================
class GeneralResponse(BaseModel):
    query_type: str = "general"
    title: str
    answer: str
    note: Optional[str] = None


# ============================================================
# 통합 응답 (프론트엔드 전달용)
# ============================================================
class RAGResponse(BaseModel):
    """RAG 최종 응답"""
    query: str
    query_type: str
    data: Dict[str, Any]
    sources: Optional[List[Dict[str, Any]]] = None
    raw_answer: Optional[str] = None

# /app/app/config.py
"""
전체 설정 집중 관리 (v26 — 루프/최종 모델 분리 / 로컬 Ollama Qwen 복원)
- Tool 호출 루프: gpt-4.1-mini (빠름, Function Calling 전용)
- 최종 답변 생성: gpt-5.4 (고품질 추론·JSON 합성)
- UI 모델 선택: gpt-5.4 (OpenAI) / qwen2.5:7b (로컬 Ollama) 2종
- DB·API 키·URL은 환경 변수로만 주입 (기본값은 로컬/단일 서버 가정).
- 표준 배포 경로: 192.168.6.24:/app/rag/fastapi  → APP_ROOT 기본값과 동일.

환경 변수 예시는 저장소 루트의 env.example 참고.
"""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

# 프로젝트 루트의 .env (있으면 로드 — python-dotenv 선택 의존성)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.is_file():
    try:
        from dotenv import load_dotenv

        # Docker Compose often injects KEY= (empty string). With override=False,
        # python-dotenv skips those keys and mounted .env never applies.
        for _k in (
            "OPENAI_API_KEY",
            "DATABASE_URL",
            "ZABBIX_DB_PASSWORD",
            "OLLAMA_URL",
        ):
            if os.environ.get(_k, "").strip() == "":
                os.environ.pop(_k, None)
        load_dotenv(_env_path)
    except ImportError:
        pass

# ============================================================
# 경로 (Docker/베어메탈 공통)
# ============================================================
APP_ROOT = os.path.abspath(os.environ.get("APP_ROOT", "/app/rag/fastapi"))

# RAG 임베딩/청킹 동기화 주기 (문서·세션 힌트용, 스케줄러 설정과 맞출 것)
RAG_EMBED_SYNC_INTERVAL_HOURS = int(os.environ.get("RAG_EMBED_SYNC_INTERVAL_HOURS", "2"))
RAG_CHUNK_SYNC_INTERVAL_HOURS = int(os.environ.get("RAG_CHUNK_SYNC_INTERVAL_HOURS", "1"))


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    return v


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _rag_db_from_database_url(url: str) -> dict | None:
    """Docker Compose 등에서 쓰는 DATABASE_URL → RAG_DB dict."""
    p = urlparse(url.strip())
    if p.scheme not in ("postgresql", "postgres", "postgresql+asyncpg"):
        return None
    db = (p.path or "").lstrip("/").split("?")[0] or "postgres"
    return {
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 5432,
        "database": db,
        "user": unquote(p.username or ""),
        "password": unquote(p.password or "") if p.password else "",
    }


# ============================================================
# 데이터베이스 설정
# ============================================================
_db_url = os.environ.get("DATABASE_URL", "").strip()
_rag_from_url = _rag_db_from_database_url(_db_url) if _db_url else None

if _rag_from_url:
    RAG_DB = _rag_from_url
    # 구 이미지의 잘못된 서비스명 보정 (Docker DNS: pgvector → rag-pgvector)
    if _in_docker() and RAG_DB["host"] == "pgvector":
        RAG_DB = {**RAG_DB, "host": "rag-pgvector"}
else:
    RAG_DB = {
        "host": _env("RAG_DB_HOST", "127.0.0.1"),
        "port": _env_int("RAG_DB_PORT", 5432),
        "database": _env("RAG_DB_NAME", "rag_db"),
        "user": _env("RAG_DB_USER", "rag_user"),
        "password": _env("RAG_DB_PASSWORD", ""),
    }

# Docker(zabbix-net): 호스트 기본값 postgres-server / DB zabbix_70 (환경 변수로 덮어쓰기)
_zabbix_host_d = "postgres-server" if _in_docker() else "127.0.0.1"
_zabbix_name_d = "zabbix_70" if _in_docker() else "zabbix"

ZABBIX_DB = {
    "host": _env("ZABBIX_DB_HOST", _zabbix_host_d),
    "port": _env_int("ZABBIX_DB_PORT", 5432),
    "database": _env("ZABBIX_DB_NAME", _zabbix_name_d),
    "user": _env("ZABBIX_DB_USER", "zabbix"),
    "password": _env("ZABBIX_DB_PASSWORD", ""),
}

RAG_DB_CONFIG = {
    "host": RAG_DB["host"],
    "port": RAG_DB["port"],
    "database": RAG_DB["database"],
    "user": RAG_DB["user"],
    "password": RAG_DB["password"],
}

# ============================================================
# Ollama 설정
# ============================================================
OLLAMA_URL = _env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_API_URL = OLLAMA_URL
OLLAMA_EMBED_URL = f"{OLLAMA_URL}/api/embeddings"
OLLAMA_GENERATE_URL = f"{OLLAMA_URL}/api/generate"
OLLAMA_TAGS_URL = f"{OLLAMA_URL}/api/tags"

# ============================================================
# 임베딩 모델
# ============================================================
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "nomic-embed-text-v2-moe")
VECTOR_DIMENSION = _env_int("VECTOR_DIMENSION", 768)

# ============================================================
# 하위 호환 더미 변수 (구버전 코드 ImportError 방지용)
# ============================================================
GEMINI_API_KEY = ""
GEMINI_API_URL = ""
GEMINI_MODEL = ""
GEMINI_TIMEOUT = 0.0
GROQ_API_KEY = ""
GROQ_API_URL = ""

# ============================================================
# OpenAI API 설정 (OPENAI_API_KEY 필수)
# ============================================================
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
OPENAI_API_URL = _env("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")

# ============================================================
# LLM 백엔드 선택 (기본값)
# - UI/일반 생성용 기본 모델: LLM_MODEL
# - 에이전트(Tool calling) 기본: AGENT_MODEL (아래 블록)
# ============================================================
LLM_BACKEND = _env("LLM_BACKEND", "openai")
LLM_MODEL = _env("LLM_MODEL", "gpt-5.4")

# ============================================================
# 선택 가능한 모델 목록
# ┌─ UI 노출 (사용자 선택 가능)
# │  gpt-5.4      : OpenAI 최신 추론 모델 — 최종 답변 생성 기본
# │  qwen2.5:7b   : 로컬 Ollama — 인터넷 차단 환경·비용 절감용 (고품질)
# │  qwen2.5:3b   : 로컬 Ollama — 경량 모델, 빠른 응답 (3B 파라미터)
# └─ UI 비노출 (내부 전용)
#    gpt-4.1-mini : Agentic Tool 루프 전용 (빠른 Function Calling)
# ============================================================
AVAILABLE_MODELS = {
    # ── UI 노출: OpenAI GPT-5.4 (최종 답변·고품질 추론) ──
    "gpt-5.4": {
        "backend": "openai",
        "display_name": "GPT-5.4",
        "description": "OpenAI GPT-5.4 — 최종 답변 생성·고품질 추론",
        "max_tokens": 16384,
        "timeout": 180.0,
        "obs_max_chars": 15000,
        "obs_max_items": 25,
        "top_k": 10,
        "use_max_completion_tokens": True,
        "no_temperature": True,
    },
    # ── UI 노출: 로컬 Ollama Qwen 2.5 7B (고품질) ──
    "qwen2.5:7b": {
        "backend": "ollama",
        "display_name": "Qwen 2.5 7B (로컬)",
        "description": "로컬 Ollama — 인터넷 차단 환경·비용 절감 (응답 느림)",
        "temperature": 0.3,
        "max_tokens": 4096,
        "num_ctx": 8192,
        "timeout": 600.0,
        "force_json": False,
        "obs_max_chars": 2000,
        "obs_max_items": 5,
        "top_k": 5,
    },
    # ── UI 노출: 로컬 Ollama Qwen 2.5 3B (경량·빠름) ──
    "qwen2.5:3b": {
        "backend": "ollama",
        "display_name": "Qwen 2.5 3B (로컬·경량)",
        "description": "로컬 Ollama 경량 모델 — 빠른 응답, 인터넷 차단 환경 (3B)",
        "temperature": 0.3,
        "max_tokens": 2048,
        "num_ctx": 4096,
        "timeout": 300.0,
        "force_json": False,
        "obs_max_chars": 1500,
        "obs_max_items": 5,
        "top_k": 5,
    },
    # ── UI 비노출: Agentic Tool 루프 전용 (Function Calling, 저비용·고속) ──
    "gpt-4.1-mini": {
        "backend": "openai",
        "display_name": "gpt-4.1-mini",
        "description": "Agentic 루프 전용 — 도구 선택·인자 추출 (UI 비노출)",
        "temperature": 0.2,
        "max_tokens": 16384,
        "timeout": 60.0,
        "obs_max_chars": 12000,
        "obs_max_items": 20,
        "top_k": 10,
    },
}

# UI 드롭다운 노출 순서 (gpt-4.1-mini 는 내부 전용이므로 제외)
UI_MODEL_ORDER = (
    "gpt-5.4",
    "qwen2.5:7b",
    "qwen2.5:3b",
)

def get_model_config(model_id: str = None) -> dict:
    if not model_id:
        model_id = LLM_MODEL
    return AVAILABLE_MODELS.get(model_id, AVAILABLE_MODELS["gpt-5.4"])




_profile = get_model_config(LLM_MODEL)
LLM_TEMPERATURE = _profile.get("temperature", 0.3)
LLM_MAX_TOKENS = _profile.get("max_tokens", 16384)
LLM_NUM_CTX = _profile.get("num_ctx", 8192)
LLM_TIMEOUT = _profile.get("timeout", 180.0)
LLM_FORCE_JSON = _profile.get("force_json", False)

# ============================================================
# 질의 유형 정의 (8개)
# ============================================================
QUERY_TYPES = {
    "status": "상태 조회",
    "list": "목록 조회",
    "alert": "장애 조회",
    "analysis": "비교/분석",
    "general": "일반 질문",
    "log": "로그/에러 조회",
    "process": "프로세스 분석",
}

DATA_SOURCES = {
    "mcp": "실시간 조회 (Zabbix DB)",
    "rag": "RAG 벡터 검색",
    "hybrid": "실시간 + RAG 혼합",
}

# ============================================================
# Agent 설정
# ============================================================
# AGENT_MODEL    : UI에서 선택된 모델 (기본값, 하위 호환)
AGENT_MODEL = _env("AGENT_MODEL", "gpt-5.4")

# AGENT_LOOP_MODEL  : Tool 호출 루프 전용 모델 (빠른 Function Calling)
#   - 환경 변수 AGENT_LOOP_MODEL 로 덮어쓰기 가능
#   - 기본: gpt-4.1-mini  (GPT-5.4 대비 ~5배 빠름, Tool 선택 품질 동등)
AGENT_LOOP_MODEL = _env("AGENT_LOOP_MODEL", "gpt-4.1-mini")

# AGENT_FINAL_MODEL : 최종 답변 JSON 합성 모델 (고품질 추론)
#   - 환경 변수 AGENT_FINAL_MODEL 로 덮어쓰기 가능
#   - 기본: gpt-5.4
AGENT_FINAL_MODEL = _env("AGENT_FINAL_MODEL", "gpt-5.4")

AGENT_MAX_ITERATIONS = _env_int("AGENT_MAX_ITERATIONS", 5)
AGENT_MAX_ITERATIONS_BY_BACKEND = {
    "ollama": _env_int("AGENT_MAX_ITERATIONS_OLLAMA", 5),
    "openai": _env_int("AGENT_MAX_ITERATIONS_OPENAI", 8),
}

# Tool Calling을 지원하는 모델 집합
TOOL_CALLING_MODELS = {
    "gpt-5.4",
    "gpt-4.1-mini",
    "qwen2.5:7b",   # Ollama Qwen은 자체 tool-call 포맷 사용
    "qwen2.5:3b",   # Ollama Qwen 2.5 3B — 경량 로컬 모델
}

AGENT_ENABLED = _env("AGENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")

HOST_RESOLVE_CONFIG = {
    "min_partial_length": _env_int("HOST_RESOLVE_MIN_PARTIAL_LENGTH", 2),
    "max_ambiguous_candidates": _env_int("HOST_RESOLVE_MAX_AMBIGUOUS", 10),
}

# 스케줄러·로그 경로 (sync/scheduler.py에서 사용 가능)
SYNC_LOG_DIR = os.path.abspath(_env("SYNC_LOG_DIR", os.path.join(APP_ROOT, "sync")))

# /app/app/services/embedding.py
"""
Ollama 임베딩 생성 함수
"""

import httpx
from typing import List
import logging
from app.config import OLLAMA_URL, EMBEDDING_MODEL, VECTOR_DIMENSION

logger = logging.getLogger(__name__)


async def generate_embedding(text: str) -> List[float]:
    """
    Ollama를 사용하여 텍스트를 임베딩 벡터로 변환
    
    Args:
        text: 임베딩할 텍스트
        
    Returns:
        768차원 임베딩 벡터
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={
                    "model": EMBEDDING_MODEL,
                    "prompt": text
                }
            )
            response.raise_for_status()
            result = response.json()
            embedding = result.get('embedding', [])
            
            if len(embedding) != VECTOR_DIMENSION:
                raise ValueError(f"Expected {VECTOR_DIMENSION} dimensions, got {len(embedding)}")
            
            return embedding
            
    except Exception as e:
        logger.error(f"임베딩 생성 실패: {e}")
        raise

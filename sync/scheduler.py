#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zabbix 임베딩 자동 동기화 스케줄러
- 실행: 프로젝트 루트에서  PYTHONPATH=.  python -m sync.scheduler
  (배포: cd /app/rag/fastapi && PYTHONPATH=/app/rag/fastapi python -m sync.scheduler)
- 로그: $SYNC_LOG_DIR/scheduler.log (기본: APP_ROOT/sync/scheduler.log)

변경 이력:
  [v2.1] sync.sync_embeddings 연동, 경로·로그를 app.config 와 정합
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.config import SYNC_LOG_DIR, RAG_EMBED_SYNC_INTERVAL_HOURS, RAG_CHUNK_SYNC_INTERVAL_HOURS
from sync.sync_embeddings import sync_embeddings
import logging
from datetime import datetime

# ==================== 설정 ====================

os.makedirs(SYNC_LOG_DIR, exist_ok=True)

SYNC_INTERVAL_HOURS = RAG_EMBED_SYNC_INTERVAL_HOURS
CHUNK_INTERVAL_HOURS = RAG_CHUNK_SYNC_INTERVAL_HOURS
LOG_FILE = os.path.join(SYNC_LOG_DIR, "scheduler.log")

# ==================== 로깅 설정 ====================

log_format = '%(asctime)s - %(levelname)s - %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'

logger = logging.getLogger('scheduler')
logger.setLevel(logging.INFO)
logger.handlers.clear()

file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(log_format, date_format))

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(log_format, date_format))

logger.addHandler(file_handler)
logger.addHandler(console_handler)


# ==================== 스케줄 작업 함수 ====================

def scheduled_sync_job():
    """임베딩 동기화 작업 (2시간마다)"""
    logger.info("=" * 70)
    logger.info(f"[SyncEmb] 스케줄 작업 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)
    try:
        sync_embeddings()
        logger.info("[SyncEmb] 완료")
    except KeyboardInterrupt:
        logger.warning("[SyncEmb] 사용자 중단")
        raise
    except Exception as e:
        logger.error(f"[SyncEmb] 실패: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logger.info("=" * 70)


def _run_chunking_script(module_name: str, func_name: str, label: str):
    """chunking 스크립트의 특정 함수를 임포트하여 실행"""
    try:
        import importlib
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)
        logger.info(f"[Chunk/{label}] 시작")
        func()
        logger.info(f"[Chunk/{label}] 완료")
    except Exception as e:
        logger.error(f"[Chunk/{label}] 실패: {e}")
        import traceback
        logger.error(traceback.format_exc())


def scheduled_chunk_alerts():
    _run_chunking_script("sync.gen_chunking_alerts", "main", "alerts")

def scheduled_chunk_network_perf():
    _run_chunking_script("sync.gen_chunking_network_performance", "main", "network_perf")

def scheduled_chunk_server_connect():
    _run_chunking_script("sync.gen_chunking_server_connect", "main", "server_connect")

def scheduled_chunk_server_errpt():
    _run_chunking_script("sync.gen_chunking_server_errpt", "main", "server_errpt")

def scheduled_chunk_server_inventory():
    _run_chunking_script("sync.gen_chunking_server_inventory", "sync_inventory", "server_inventory")

def scheduled_chunk_server_listen():
    _run_chunking_script("sync.gen_chunking_server_listen", "main", "server_listen")

def scheduled_chunk_server_logs():
    _run_chunking_script("sync.gen_chunking_server_logs", "main", "server_logs")

def scheduled_chunk_server_performance():
    _run_chunking_script("sync.gen_chunking_server_performance", "main", "server_perf")

def scheduled_chunk_server_process_topn():
    _run_chunking_script("sync.gen_chunking_server_process_topn", "sync_process_trend", "process_topn")


# ==================== 메인 실행 ====================

def main():
    """스케줄러 메인 함수"""
    logger.info("=" * 70)
    logger.info("Zabbix 임베딩 동기화 스케줄러 시작 v2.1")
    logger.info("=" * 70)
    logger.info(f"임베딩 동기화 주기: {SYNC_INTERVAL_HOURS}시간마다")
    logger.info(f"청킹 작업 주기    : {CHUNK_INTERVAL_HOURS}시간마다 (9개 스크립트)")
    logger.info(f"로그 파일         : {LOG_FILE}")
    logger.info(f"시작 시간         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    scheduler = BlockingScheduler(timezone='Asia/Seoul')

    # ── 임베딩 동기화 (2시간마다) ──
    scheduler.add_job(
        func=scheduled_sync_job,
        trigger=IntervalTrigger(hours=SYNC_INTERVAL_HOURS),
        id='sync_embeddings_job',
        name='Zabbix 임베딩 동기화',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600
    )

    # ── chunking 스크립트 9개 (1시간마다) ──
    chunking_jobs = [
        ('chunk_alerts',           scheduled_chunk_alerts,          '장애 청킹'),
        ('chunk_network_perf',     scheduled_chunk_network_perf,    '네트워크 성능 청킹'),
        ('chunk_server_connect',   scheduled_chunk_server_connect,  '서버 연결 청킹'),
        ('chunk_server_errpt',     scheduled_chunk_server_errpt,    '서버 ERRPT 청킹'),
        ('chunk_server_inventory', scheduled_chunk_server_inventory,'서버 인벤토리 청킹'),
        ('chunk_server_listen',    scheduled_chunk_server_listen,   '서버 Listen 포트 청킹'),
        ('chunk_server_logs',      scheduled_chunk_server_logs,     '서버 로그 청킹'),
        ('chunk_server_perf',      scheduled_chunk_server_performance,'서버 성능 청킹'),
        ('chunk_process_topn',     scheduled_chunk_server_process_topn,'프로세스 Top-N 청킹'),
    ]

    for job_id, func, name in chunking_jobs:
        scheduler.add_job(
            func=func,
            trigger=IntervalTrigger(hours=CHUNK_INTERVAL_HOURS),
            id=job_id,
            name=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300
        )

    logger.info(f"임베딩 동기화 스케줄 등록: {SYNC_INTERVAL_HOURS}시간마다")
    logger.info(f"청킹 스크립트 스케줄 등록: {len(chunking_jobs)}개 × {CHUNK_INTERVAL_HOURS}시간마다")
    logger.info("")

    try:
        # 시작 시 즉시 1회 실행
        logger.info("초기 동기화 즉시 실행...")
        scheduled_sync_job()

        logger.info("초기 청킹 작업 즉시 실행...")
        for _, func, name in chunking_jobs:
            logger.info(f"  >> {name}")
            func()

        logger.info("")
        logger.info("스케줄러 대기 모드 진입...")
        logger.info("=" * 70)
        scheduler.start()

    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료 요청 수신")
        scheduler.shutdown()
        logger.info("스케줄러 정상 종료")
    except Exception as e:
        logger.error(f"치명적 오류: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


# ==================== 진입점 ====================

if __name__ == "__main__":
    main()

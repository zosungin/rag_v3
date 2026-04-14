"""
10개 Embeddings 테이블 → 3개 청킹 테이블 동기화 스크립트

목적:
1. embeddings_server_performance_hourly 생성 (6개 서버 테이블 통합)
2. embeddings_network_performance_hourly 생성 (2개 네트워크 테이블 통합)
3. embeddings_alerts_with_context 생성 (1개 Alert + 컨텍스트)

작성일: 2026-02-14
실행 환경: /app/rag/fastapi (PYTHONPATH=프로젝트 루트)
"""

import os
import sys

_ROOT = os.path.abspath(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncio
import asyncpg
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import logging
from collections import defaultdict

from app.config import RAG_DB

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# 유틸리티 함수
# ============================================================

def normalize_timestamp(value, is_unix: bool = False) -> Optional[datetime]:
    """
    시간 필드를 TIMESTAMP로 정규화
    
    Args:
        value: 시간 값 (int, datetime, str)
        is_unix: Unix timestamp 여부
    
    Returns:
        datetime 객체 또는 None
    """
    if value is None:
        return None
    
    try:
        if is_unix:
            # Unix timestamp → datetime
            if isinstance(value, int):
                return datetime.fromtimestamp(value, tz=timezone.utc)
            elif isinstance(value, str):
                return datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            # TIMESTAMP → datetime
            if isinstance(value, datetime):
                return value
            elif isinstance(value, str):
                return datetime.fromisoformat(value)
    except Exception as e:
        logger.error(f"시간 정규화 실패: {value}, 에러: {e}")
        return None
    
    return None


def create_time_bucket(dt: datetime, bucket_minutes: int = 60) -> str:
    """
    시간을 버킷으로 변환
    
    Args:
        dt: datetime 객체
        bucket_minutes: 버킷 간격 (분)
    
    Returns:
        time_bucket 문자열 (예: '2026-02-14-08')
    """
    if bucket_minutes == 60:
        return dt.strftime('%Y-%m-%d-%H')
    elif bucket_minutes == 30:
        bucket_minute = (dt.minute // 30) * 30
        return f"{dt.strftime('%Y-%m-%d-%H')}-{bucket_minute:02d}"
    elif bucket_minutes == 10:
        bucket_minute = (dt.minute // 10) * 10
        return f"{dt.strftime('%Y-%m-%d-%H')}-{bucket_minute:02d}"
    else:
        return dt.strftime('%Y-%m-%d-%H')


def safe_float(value, default=0.0) -> float:
    """안전한 float 변환"""
    try:
        return float(value) if value is not None else default
    except:
        return default


def safe_int(value, default=0) -> int:
    """안전한 int 변환"""
    try:
        return int(value) if value is not None else default
    except:
        return default


# ============================================================
# 1. 서버 Performance 동기화
# ============================================================

class ServerPerformanceSynchronizer:
    """서버 Performance 데이터 동기화"""
    
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn
    
    async def sync_all(self):
        """모든 서버 데이터를 시간 단위로 통합"""
        logger.info("=" * 80)
        logger.info("🔄 서버 Performance 동기화 시작")
        logger.info("=" * 80)
        
        # 1. 모든 호스트 목록 조회
        hosts = await self._get_all_hosts()
        logger.info(f"📋 총 호스트 수: {len(hosts)}")
        
        # 2. 각 호스트별로 시간 버킷별 데이터 수집
        for host in hosts:
            await self._sync_host_performance(host)
        
        logger.info("✅ 서버 Performance 동기화 완료")
    
    async def _get_all_hosts(self) -> List[Dict]:
        """모든 호스트 목록 조회"""
        query = """
        SELECT DISTINCT 
            hostid, asset, ip, asset_name, os, system_name, group_name
        FROM embeddings_hosts
        WHERE hostid IS NOT NULL
        ORDER BY hostid
        """
        rows = await self.conn.fetch(query)
        return [dict(row) for row in rows]
    
    async def _sync_host_performance(self, host: Dict):
        """특정 호스트의 시간 버킷별 Performance 데이터 통합"""
        hostid = host['hostid']
        logger.info(f"  📍 호스트 {hostid} ({host['asset']}) 처리 중...")
        
        # 1. 시간 범위 조회 (최근 24시간)
        time_buckets = await self._get_time_buckets_for_host(hostid)
        
        if not time_buckets:
            logger.info(f"    ⚠️  호스트 {hostid}: 최근 24시간 데이터 없음")
            return
        
        logger.info(f"    📅 시간 버킷 수: {len(time_buckets)}")
        
        # 2. 각 시간 버킷별로 데이터 수집 및 통합
        for time_bucket in time_buckets:
            await self._process_time_bucket(host, time_bucket)
    
    async def _get_time_buckets_for_host(self, hostid: int) -> List[str]:
        """호스트의 최근 24시간 시간 버킷 목록 조회"""
        # CPU 테이블을 기준으로 시간 버킷 생성
        query = """
        SELECT DISTINCT 
            to_char(source_updated_at, 'YYYY-MM-DD-HH') as time_bucket
        FROM embeddings_server_cpu
        WHERE hostid = $1
        AND source_updated_at >= now() - interval '24 hours'
        ORDER BY time_bucket DESC
        """
        rows = await self.conn.fetch(query, hostid)
        return [row['time_bucket'] for row in rows]
    
    async def _process_time_bucket(self, host: Dict, time_bucket: str):
        """특정 시간 버킷의 모든 메트릭 수집 및 통합"""
        hostid = host['hostid']
        
        # 시간 버킷 파싱
        try:
            event_time = datetime.strptime(time_bucket, '%Y-%m-%d-%H')
            date_partition = event_time.date()
            hour_partition = event_time.hour
        except Exception as e:
            logger.error(f"    ❌ 시간 버킷 파싱 실패: {time_bucket}, 에러: {e}")
            return
        
        # 시간 범위 설정 (해당 시간의 시작~끝)
        start_time = event_time
        end_time = event_time + timedelta(hours=1)
        
        # 각 메트릭 수집
        cpu_data = await self._get_cpu_metrics(hostid, start_time, end_time)
        memory_data = await self._get_memory_metrics(hostid, start_time, end_time)
        swap_data = await self._get_swap_metrics(hostid, start_time, end_time)
        filesystem_data = await self._get_filesystem_metrics(hostid, start_time, end_time)
        interface_data = await self._get_server_interface_metrics(hostid, start_time, end_time)
        process_data = await self._get_process_metrics(hostid, start_time, end_time)
        
        # 데이터 품질 평가
        data_count = sum([
            1 if cpu_data else 0,
            1 if memory_data else 0,
            1 if swap_data else 0,
            1 if filesystem_data else 0,
            1 if interface_data else 0,
            1 if process_data else 0
        ])
        
        if data_count == 0:
            logger.warning(f"    ⚠️  {time_bucket}: 데이터 없음")
            return
        
        if data_count == 6:
            data_quality = 'complete'
        elif data_count >= 3:
            data_quality = 'partial'
        else:
            data_quality = 'incomplete'
        
        # 통합 요약 텍스트 생성
        performance_summary = self._build_performance_summary(
            host, time_bucket, 
            cpu_data, memory_data, swap_data, 
            filesystem_data, interface_data, process_data
        )
        
        # 소스 레코드 ID 추적
        source_record_ids = {
            'cpu_record': cpu_data.get('record_id') if cpu_data else None,
            'memory_record': memory_data.get('record_id') if memory_data else None,
            'swap_record': swap_data.get('record_id') if swap_data else None,
            'filesystem_records': [fs.get('record_id') for fs in filesystem_data] if filesystem_data else [],
            'interface_records': [iface.get('record_id') for iface in interface_data] if interface_data else [],
            'process_records': [proc.get('record_id') for proc in process_data[:10]] if process_data else []
        }
        
        # 청킹 테이블에 삽입 (UPSERT)
        await self._upsert_performance_record(
            host=host,
            event_time=event_time,
            time_bucket=time_bucket,
            date_partition=date_partition,
            hour_partition=hour_partition,
            cpu_metrics=cpu_data,
            memory_metrics=memory_data,
            swap_metrics=swap_data,
            filesystem_metrics=filesystem_data,
            interface_metrics=interface_data,
            top_processes=process_data[:10] if process_data else [],  # Top 10만
            performance_summary=performance_summary,
            source_record_ids=source_record_ids,
            record_count=data_count,
            data_quality=data_quality
        )
        
        logger.info(f"    ✅ {time_bucket}: {data_quality} ({data_count}/6 메트릭)")
    
    async def _get_cpu_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> Optional[Dict]:
        """CPU 메트릭 조회"""
        query = """
        SELECT *
        FROM embeddings_server_cpu
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY source_updated_at DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_time, end_time)
        
        if not row:
            return None
        
        return {
            'number_of_cpus': row['number_of_cpus'],
            'cpu_utilization': safe_float(row['cpu_utilization']),
            'cpu_idle_time': safe_float(row['cpu_idle_time']),
            'cpu_system_time': safe_float(row['cpu_system_time']),
            'cpu_user_time': safe_float(row['cpu_user_time']),
            'source_table': 'embeddings_server_cpu',
            'record_id': f"{hostid}_{row['source_updated_at'].isoformat()}"
        }
    
    async def _get_memory_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> Optional[Dict]:
        """Memory 메트릭 조회"""
        query = """
        SELECT *
        FROM embeddings_server_memory
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY source_updated_at DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_time, end_time)
        
        if not row:
            return None
        
        return {
            'total_memory': safe_int(row['total_memory']),
            'available_memory': safe_int(row['available_memory']),
            'memory_utilization': safe_int(row['memory_utilization']),
            'available_memory_in': safe_float(row['available_memory_in']),
            'available_memory_std': safe_float(row['available_memory_std']),
            'source_table': 'embeddings_server_memory',
            'record_id': f"{hostid}_{row['source_updated_at'].isoformat()}"
        }
    
    async def _get_swap_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> Optional[Dict]:
        """Swap 메트릭 조회"""
        query = """
        SELECT *
        FROM embeddings_server_swap
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY source_updated_at DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_time, end_time)
        
        if not row:
            return None
        
        return {
            'total_swap_space': safe_int(row['total_swap_space']),
            'free_swap_space': safe_int(row['free_swap_space']),
            'free_swap_space_pct': safe_float(row['free_swap_space_pct']),
            'used_swap_percent': safe_float(row['used_swap_percent']),
            'used_swap_percent_std': safe_float(row['used_swap_percent_std']),
            'source_table': 'embeddings_server_swap',
            'record_id': f"{hostid}_{row['source_updated_at'].isoformat()}"
        }
    
    async def _get_filesystem_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Filesystem 메트릭 조회 (여러 파일시스템)"""
        query = """
        SELECT *
        FROM embeddings_server_filesystem
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY fs_name, source_updated_at DESC
        """
        rows = await self.conn.fetch(query, hostid, start_time, end_time)
        
        # 각 파일시스템별로 최신 데이터만
        fs_dict = {}
        for row in rows:
            fs_name = row['fs_name']
            if fs_name not in fs_dict:
                fs_dict[fs_name] = {
                    'fs_name': fs_name,
                    'total_space': safe_int(row['total_space']),
                    'used_space': safe_int(row['used_space']),
                    'space_utilization': safe_float(row['space_utilization']),
                    'space_utilization_std': safe_float(row['space_utilization_std']),
                    'source_table': 'embeddings_server_filesystem',
                    'record_id': f"{hostid}_{fs_name}_{row['source_updated_at'].isoformat()}"
                }
        
        return list(fs_dict.values())
    
    async def _get_server_interface_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Server Interface 메트릭 조회"""
        query = """
        SELECT *
        FROM embeddings_server_interfaces
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY interface_name, source_updated_at DESC
        """
        rows = await self.conn.fetch(query, hostid, start_time, end_time)
        
        # 각 인터페이스별로 최신 데이터만
        iface_dict = {}
        for row in rows:
            iface_name = row['interface_name']
            if iface_name not in iface_dict:
                iface_dict[iface_name] = {
                    'interface_name': iface_name,
                    'operation_status': row['operation_status'],
                    'speed': safe_int(row['speed']),
                    'bits_sent': safe_int(row['bits_sent']),
                    'bits_received': safe_int(row['bits_received']),
                    'bits_sent_std': safe_float(row['bits_sent_std']),
                    'bits_received_std': safe_float(row['bits_received_std']),
                    'source_table': 'embeddings_server_interfaces',
                    'record_id': f"{hostid}_{iface_name}_{row['source_updated_at'].isoformat()}"
                }
        
        return list(iface_dict.values())
    
    async def _get_process_metrics(self, hostid: int, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Process 메트릭 조회 (CPU 사용률 높은 순)"""
        # clock은 Unix timestamp
        start_unix = int(start_time.timestamp())
        end_unix = int(end_time.timestamp())
        
        query = """
        SELECT *
        FROM embeddings_process_resources
        WHERE hostid = $1
        AND clock >= $2
        AND clock < $3
        ORDER BY cpu DESC, mem DESC
        LIMIT 20
        """
        rows = await self.conn.fetch(query, hostid, start_unix, end_unix)
        
        processes = []
        for row in rows:
            processes.append({
                'pid': safe_int(row['pid']),
                'user_name': row['user_name'],
                'command': row['command'],
                'cpu': safe_float(row['cpu']),
                'mem': safe_float(row['mem']),
                'cpu_time': row['cpu_time'],
                'mem_used': safe_int(row['mem_used']),
                'resource_std': safe_float(row['resource_std']),
                'source_table': 'embeddings_process_resources',
                'record_id': f"{hostid}_{row['pid']}_{row['clock']}"
            })
        
        return processes
    
    def _build_performance_summary(
        self, host: Dict, time_bucket: str,
        cpu_data: Optional[Dict], memory_data: Optional[Dict], swap_data: Optional[Dict],
        filesystem_data: List[Dict], interface_data: List[Dict], process_data: List[Dict]
    ) -> str:
        """통합 Performance 요약 텍스트 생성"""
        
        lines = []
        
        # 호스트 정보
        lines.append(f"호스트 ID: {host['hostid']} | 자산: {host['asset']} | IP: {host['ip']}")
        lines.append(f"자산명: {host['asset_name']} | OS: {host['os']} | 시스템명: {host['system_name']} | 그룹: {host['group_name']}")
        lines.append(f"시간: {time_bucket}:00 ~ {time_bucket}:59")
        lines.append("")
        
        # CPU
        if cpu_data:
            lines.append(f"[CPU]")
            lines.append(f"  코어 수: {cpu_data['number_of_cpus']}")
            lines.append(f"  사용률: {cpu_data['cpu_utilization']:.2f}%, Idle: {cpu_data['cpu_idle_time']:.2f}%")
            lines.append(f"  System: {cpu_data['cpu_system_time']:.2f}%, User: {cpu_data['cpu_user_time']:.2f}%")
            lines.append("")
        
        # Memory
        if memory_data:
            total_gb = memory_data['total_memory'] / (1024**3)
            available_gb = memory_data['available_memory'] / (1024**3)
            used_gb = memory_data['memory_utilization'] / (1024**3)
            
            lines.append(f"[메모리]")
            lines.append(f"  총 용량: {total_gb:.2f} GB")
            lines.append(f"  사용 가능: {available_gb:.2f} GB ({memory_data['available_memory_in']:.2f}%)")
            lines.append(f"  사용 중: {used_gb:.2f} GB")
            lines.append(f"  표준편차: {memory_data['available_memory_std']:.2f}%")
            lines.append("")
        
        # Swap
        if swap_data:
            total_gb = swap_data['total_swap_space'] / (1024**3)
            free_gb = swap_data['free_swap_space'] / (1024**3)
            
            lines.append(f"[Swap]")
            lines.append(f"  총 용량: {total_gb:.2f} GB")
            lines.append(f"  여유 공간: {free_gb:.2f} GB ({swap_data['free_swap_space_pct']:.2f}%)")
            lines.append(f"  사용률: {swap_data['used_swap_percent']:.2f}%")
            lines.append(f"  표준편차: {swap_data['used_swap_percent_std']:.2f}%")
            lines.append("")
        
        # Filesystem
        if filesystem_data:
            lines.append(f"[파일시스템]")
            for fs in filesystem_data:
                total_gb = fs['total_space'] / 1024  # MB → GB
                used_gb = fs['used_space'] / 1024
                lines.append(f"  {fs['fs_name']}: 총 {total_gb:.2f} GB, 사용 {used_gb:.2f} GB ({fs['space_utilization']:.2f}%)")
            lines.append("")
        
        # Server Interfaces
        if interface_data:
            lines.append(f"[네트워크 인터페이스]")
            for iface in interface_data:
                speed_mbps = iface['speed'] / (1024**2) if iface['speed'] else 0
                sent_mbps = iface['bits_sent'] / (1024**2)
                recv_mbps = iface['bits_received'] / (1024**2)
                lines.append(f"  {iface['interface_name']}: {iface['operation_status']}, {speed_mbps:.0f} Mbps")
                lines.append(f"    송신: {sent_mbps:.2f} Mbps, 수신: {recv_mbps:.2f} Mbps")
            lines.append("")
        
        # Top Processes
        if process_data:
            lines.append(f"[주요 프로세스 (Top 10)]")
            for i, proc in enumerate(process_data[:10], 1):
                mem_mb = proc['mem_used'] / (1024**2) if proc['mem_used'] else 0
                cmd = proc['command'][:50] + '...' if len(proc['command']) > 50 else proc['command']
                lines.append(f"  {i}. PID {proc['pid']} ({proc['user_name']}): {cmd}")
                lines.append(f"     CPU: {proc['cpu']:.2f}%, MEM: {proc['mem']:.2f}% ({mem_mb:.0f} MB)")
        
        return '\n'.join(lines)
    
    async def _upsert_performance_record(
        self, host: Dict, event_time: datetime, time_bucket: str,
        date_partition, hour_partition: int,
        cpu_metrics: Optional[Dict], memory_metrics: Optional[Dict], swap_metrics: Optional[Dict],
        filesystem_metrics: List[Dict], interface_metrics: List[Dict], top_processes: List[Dict],
        performance_summary: str, source_record_ids: Dict,
        record_count: int, data_quality: str
    ):
        """Performance 레코드 삽입 또는 업데이트"""
        
        query = """
        INSERT INTO embeddings_server_performance_hourly (
            hostid, asset, ip, asset_name, os, system_name, group_name,
            event_time, time_bucket, date_partition, hour_partition,
            cpu_metrics, memory_metrics, swap_metrics,
            filesystem_metrics, interface_metrics, top_processes,
            performance_summary, source_record_ids,
            record_count, data_quality
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            $8, $9, $10, $11,
            $12, $13, $14,
            $15, $16, $17,
            $18, $19,
            $20, $21
        )
        ON CONFLICT (hostid, time_bucket)
        DO UPDATE SET
            cpu_metrics = EXCLUDED.cpu_metrics,
            memory_metrics = EXCLUDED.memory_metrics,
            swap_metrics = EXCLUDED.swap_metrics,
            filesystem_metrics = EXCLUDED.filesystem_metrics,
            interface_metrics = EXCLUDED.interface_metrics,
            top_processes = EXCLUDED.top_processes,
            performance_summary = EXCLUDED.performance_summary,
            source_record_ids = EXCLUDED.source_record_ids,
            record_count = EXCLUDED.record_count,
            data_quality = EXCLUDED.data_quality,
            updated_at = now()
        """
        
        await self.conn.execute(
            query,
            host['hostid'], host['asset'], host['ip'], host['asset_name'],
            host['os'], host['system_name'], host['group_name'],
            event_time, time_bucket, date_partition, hour_partition,
            json.dumps(cpu_metrics) if cpu_metrics else None,
            json.dumps(memory_metrics) if memory_metrics else None,
            json.dumps(swap_metrics) if swap_metrics else None,
            json.dumps(filesystem_metrics) if filesystem_metrics else None,
            json.dumps(interface_metrics) if interface_metrics else None,
            json.dumps(top_processes) if top_processes else None,
            performance_summary,
            json.dumps(source_record_ids),
            record_count, data_quality
        )


# ============================================================
# 2. 네트워크 Performance 동기화
# ============================================================

class NetworkPerformanceSynchronizer:
    """네트워크 Performance 데이터 동기화"""
    
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn
    
    async def sync_all(self):
        """모든 네트워크 장비 데이터를 시간 단위로 통합"""
        logger.info("=" * 80)
        logger.info("🔄 네트워크 Performance 동기화 시작")
        logger.info("=" * 80)
        
        # 1. 모든 네트워크 장비 목록 조회
        devices = await self._get_all_network_devices()
        logger.info(f"📋 총 네트워크 장비 수: {len(devices)}")
        
        # 2. 각 장비별로 시간 버킷별 데이터 수집
        for device in devices:
            await self._sync_device_performance(device)
        
        logger.info("✅ 네트워크 Performance 동기화 완료")
    
    async def _get_all_network_devices(self) -> List[Dict]:
        """모든 네트워크 장비 목록 조회"""
        query = """
        SELECT DISTINCT 
            hostid, asset, asset_name, device_name, ip, group_name
        FROM embeddings_network_devices
        WHERE hostid IS NOT NULL
        ORDER BY hostid
        """
        rows = await self.conn.fetch(query)
        return [dict(row) for row in rows]
    
    async def _sync_device_performance(self, device: Dict):
        """특정 네트워크 장비의 시간 버킷별 Performance 데이터 통합"""
        hostid = device['hostid']
        logger.info(f"  📍 장비 {hostid} ({device['device_name']}) 처리 중...")
        
        # 1. 시간 범위 조회 (최근 24시간)
        time_buckets = await self._get_time_buckets_for_device(hostid)
        
        if not time_buckets:
            logger.info(f"    ⚠️  장비 {hostid}: 최근 24시간 데이터 없음")
            return
        
        logger.info(f"    📅 시간 버킷 수: {len(time_buckets)}")
        
        # 2. 각 시간 버킷별로 데이터 수집 및 통합
        for time_bucket in time_buckets:
            await self._process_device_time_bucket(device, time_bucket)
    
    async def _get_time_buckets_for_device(self, hostid: int) -> List[str]:
        """네트워크 장비의 최근 24시간 시간 버킷 목록 조회 (30분 단위)"""
        # source_updated_at가 INTEGER (Unix timestamp)인 경우 처리
        # v2.3: 1시간 → 30분 단위 버킷으로 변경
        query = """
        SELECT DISTINCT 
            to_char(
                date_trunc('hour', to_timestamp(source_updated_at))
                + (EXTRACT(MINUTE FROM to_timestamp(source_updated_at))::int / 30) * interval '30 minutes',
                'YYYY-MM-DD-HH24-MI'
            ) as time_bucket
        FROM embeddings_network_devices
        WHERE hostid = $1
        AND to_timestamp(source_updated_at) >= now() - interval '24 hours'
        ORDER BY time_bucket DESC
        """
        rows = await self.conn.fetch(query, hostid)
        return [row['time_bucket'] for row in rows]
    
    async def _process_device_time_bucket(self, device: Dict, time_bucket: str):
        """특정 시간 버킷(30분 단위)의 네트워크 메트릭 수집 및 통합"""
        hostid = device['hostid']
        
        # 시간 버킷 파싱 (v2.3: 30분 단위 — 'YYYY-MM-DD-HH-MM' 형식)
        try:
            event_time = datetime.strptime(time_bucket, '%Y-%m-%d-%H-%M')
        except Exception as e:
            logger.error(f"    ❌ 시간 버킷 파싱 실패: {time_bucket}, 에러: {e}")
            return
        
        # 시간 범위 설정 (v2.3: 30분 간격)
        start_time = event_time
        end_time = event_time + timedelta(minutes=30)
        start_unix = int(start_time.timestamp())
        end_unix = int(end_time.timestamp())
        
        # 장비 정보 조회
        device_info = await self._get_device_info(hostid, start_unix, end_unix)
        
        # 인터페이스 메트릭 조회
        interface_metrics = await self._get_interface_metrics(hostid, start_unix, end_unix)
        
        # 데이터 품질 평가
        if not device_info and not interface_metrics:
            logger.warning(f"    ⚠️  {time_bucket}: 데이터 없음")
            return
        
        data_quality = 'complete' if device_info and interface_metrics else 'partial'
        
        # 인터페이스 통계 요약
        interface_summary = self._calculate_interface_summary(interface_metrics)
        
        # 통합 요약 텍스트 생성
        network_summary = self._build_network_summary(
            device, time_bucket, device_info, interface_metrics, interface_summary
        )
        
        # 소스 레코드 ID 추적
        source_record_ids = {
            'device_record': device_info.get('record_id') if device_info else None,
            'interface_records': [iface.get('record_id') for iface in interface_metrics] if interface_metrics else []
        }
        
        # 청킹 테이블에 삽입 (UPSERT)
        await self._upsert_network_record(
            device=device,
            event_time=event_time,
            time_bucket=time_bucket,
            device_info=device_info,
            interface_metrics=interface_metrics,
            interface_summary=interface_summary,
            network_summary=network_summary,
            source_record_ids=source_record_ids,
            record_count=len(interface_metrics) + (1 if device_info else 0),
            data_quality=data_quality
        )
        
        logger.info(f"    ✅ {time_bucket}: {data_quality} (인터페이스 {len(interface_metrics)}개)")
    
    async def _get_device_info(self, hostid: int, start_unix: int, end_unix: int) -> Optional[Dict]:
        """네트워크 장비 정보 조회"""
        query = """
        SELECT *
        FROM embeddings_network_devices
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY source_updated_at DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_unix, end_unix)
        
        if not row:
            return None
        
        return {
            'agent_status': row['agent_status'],
            'communication_name': row['communication_name'],
            'template_name': row['template_name'],
            'ios': row['ios'],
            'snmp_agent': row['snmp_agent'],
            'system_description': row['system_description'],
            'uptime': row['uptime'],
            'total_interface': row['total_interface'],
            'icmp_ping': row['icmp_ping'],
            'icmp_response_time': safe_float(row['icmp_response_time']),
            'source_table': 'embeddings_network_devices',
            'record_id': f"{hostid}_{row['source_updated_at']}"
        }
    
    async def _get_interface_metrics(self, hostid: int, start_unix: int, end_unix: int) -> List[Dict]:
        """네트워크 인터페이스 메트릭 조회
        v2.3: embeddings_network_interfaces의 source_updated_at이 TIMESTAMP 타입으로 변경됨
        """
        start_ts = datetime.fromtimestamp(start_unix)
        end_ts = datetime.fromtimestamp(end_unix)
        query = """
        SELECT *
        FROM embeddings_network_interfaces
        WHERE hostid = $1
        AND source_updated_at >= $2
        AND source_updated_at < $3
        ORDER BY interface, source_updated_at DESC
        """
        rows = await self.conn.fetch(query, hostid, start_ts, end_ts)
        
        # 각 인터페이스별로 최신 데이터만
        iface_dict = {}
        for row in rows:
            uid = row['uid']
            if uid not in iface_dict:
                iface_dict[uid] = {
                    'uid': uid,
                    'interface': row['interface'],
                    'port_alias': row['port_alias'],
                    'type': row['type'],
                    'port_status': row['port_status'],
                    'port_speed': safe_int(row['port_speed']),
                    'link': safe_float(row['link']),
                    'received': safe_int(row['received']),
                    'received_rate': safe_float(row['received_rate']),
                    'sent': safe_int(row['sent']),
                    'sent_rate': safe_float(row['sent_rate']),
                    'received_rate_std': safe_float(row['received_rate_std']),
                    'sent_rate_std': safe_float(row['sent_rate_std']),
                    'out_error_value': safe_int(row['out_error_value']),
                    'in_error_value': safe_int(row['in_error_value']),
                    'source_table': 'embeddings_network_interfaces',
                    'record_id': f"{uid}_{row['source_updated_at']}"
                }
        
        return list(iface_dict.values())
    
    def _calculate_interface_summary(self, interface_metrics: List[Dict]) -> Dict:
        """인터페이스 통계 요약 계산"""
        if not interface_metrics:
            return {
                'total_interfaces': 0,
                'active_interfaces': 0,
                'down_interfaces': 0,
                'error_interfaces': 0,
                'high_utilization_interfaces': 0,
                'total_traffic_in_gbps': 0.0,
                'total_traffic_out_gbps': 0.0
            }
        
        total = len(interface_metrics)
        active = sum(1 for iface in interface_metrics if iface['port_status'] == '1')
        down = total - active
        error = sum(1 for iface in interface_metrics 
                   if iface['out_error_value'] > 0 or iface['in_error_value'] > 0)
        high_util = sum(1 for iface in interface_metrics if iface['link'] > 80)
        
        total_in = sum(iface['received_rate'] for iface in interface_metrics) / (1024**3)
        total_out = sum(iface['sent_rate'] for iface in interface_metrics) / (1024**3)
        
        return {
            'total_interfaces': total,
            'active_interfaces': active,
            'down_interfaces': down,
            'error_interfaces': error,
            'high_utilization_interfaces': high_util,
            'total_traffic_in_gbps': round(total_in, 2),
            'total_traffic_out_gbps': round(total_out, 2)
        }
    
    def _build_network_summary(
        self, device: Dict, time_bucket: str,
        device_info: Optional[Dict], interface_metrics: List[Dict],
        interface_summary: Dict
    ) -> str:
        """통합 Network 요약 텍스트 생성"""
        
        lines = []
        
        # 장비 정보
        lines.append(f"호스트 ID: {device['hostid']} | 자산: {device['asset']} | IP: {device['ip']}")
        lines.append(f"장비명: {device['device_name']} | 그룹: {device['group_name']}")
        # v2.3: 30분 단위 버킷 — 시간 범위 표시 조정
        try:
            bucket_dt = datetime.strptime(time_bucket, '%Y-%m-%d-%H-%M')
            bucket_end = bucket_dt + timedelta(minutes=30)
            lines.append(f"시간: {bucket_dt.strftime('%Y-%m-%d %H:%M')} ~ {bucket_end.strftime('%H:%M')}")
        except Exception:
            lines.append(f"시간: {time_bucket}")
        lines.append("")
        
        # 장비 상태
        if device_info:
            lines.append(f"[장비 상태]")
            lines.append(f"  Agent: {device_info['agent_status']}, SNMP: {device_info['snmp_agent']}")
            lines.append(f"  통신: {device_info['communication_name']}, 템플릿: {device_info['template_name']}")
            lines.append(f"  IOS: {device_info['ios']}")
            lines.append(f"  Uptime: {device_info['uptime']}")
            lines.append(f"  ICMP: {device_info['icmp_ping']}, 응답시간: {device_info['icmp_response_time']:.2f} ms")
            lines.append(f"  총 인터페이스: {device_info['total_interface']}")
            lines.append("")
        
        # 인터페이스 통계
        if interface_summary['total_interfaces'] > 0:
            lines.append(f"[인터페이스 통계]")
            lines.append(f"  총 {interface_summary['total_interfaces']}개 (활성: {interface_summary['active_interfaces']}, Down: {interface_summary['down_interfaces']})")
            lines.append(f"  에러 발생: {interface_summary['error_interfaces']}개")
            lines.append(f"  고사용률 (>80%): {interface_summary['high_utilization_interfaces']}개")
            lines.append(f"  총 트래픽: 수신 {interface_summary['total_traffic_in_gbps']:.2f} Gbps, 송신 {interface_summary['total_traffic_out_gbps']:.2f} Gbps")
            lines.append("")
        
        # 주요 인터페이스 (사용률 높은 순)
        if interface_metrics:
            high_util_interfaces = sorted(interface_metrics, key=lambda x: x['link'], reverse=True)[:5]
            
            if high_util_interfaces:
                lines.append(f"[주요 인터페이스 (사용률 상위 5개)]")
                for iface in high_util_interfaces:
                    recv_mbps = iface['received_rate'] / (1024**2)
                    sent_mbps = iface['sent_rate'] / (1024**2)
                    lines.append(f"  {iface['interface']} ({iface['port_alias']})")
                    lines.append(f"    상태: {iface['port_status']}, 사용률: {iface['link']:.2f}%")
                    lines.append(f"    수신: {recv_mbps:.2f} Mbps, 송신: {sent_mbps:.2f} Mbps")
                    if iface['out_error_value'] > 0 or iface['in_error_value'] > 0:
                        lines.append(f"    에러: 송신 {iface['out_error_value']}, 수신 {iface['in_error_value']}")
        
        return '\n'.join(lines)
    
    async def _upsert_network_record(
        self, device: Dict, event_time: datetime, time_bucket: str,
        device_info: Optional[Dict], interface_metrics: List[Dict],
        interface_summary: Dict, network_summary: str,
        source_record_ids: Dict, record_count: int, data_quality: str
    ):
        """Network Performance 레코드 삽입 또는 업데이트
        v2.3: date_partition, hour_partition 제거 (DDL에서 GENERATED ALWAYS 컬럼으로 자동 계산)
              time_bucket을 TIMESTAMP로 전달 (30분 단위)
        """
        
        query = """
        INSERT INTO embeddings_network_performance_chunked (
            hostid, asset, asset_name, device_name, ip, group_name,
            time_bucket,
            device_info, interface_summary,
            network_summary,
            record_count
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7,
            $8, $9,
            $10,
            $11
        )
        ON CONFLICT (hostid, time_bucket)
        DO UPDATE SET
            device_info = EXCLUDED.device_info,
            interface_summary = EXCLUDED.interface_summary,
            network_summary = EXCLUDED.network_summary,
            record_count = EXCLUDED.record_count,
            updated_at = now()
        """
        
        await self.conn.execute(
            query,
            device['hostid'], device['asset'], device['asset_name'],
            device['device_name'], device['ip'], device['group_name'],
            event_time,
            json.dumps(device_info) if device_info else None,
            json.dumps(interface_summary),
            network_summary,
            record_count
        )


# ============================================================
# 3. Alert + Context 동기화
# ============================================================

class AlertContextSynchronizer:
    """Alert + Context 데이터 동기화"""
    
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn
    
    async def sync_all(self):
        """모든 Alert 데이터에 컨텍스트 추가"""
        logger.info("=" * 80)
        logger.info("🔄 Alert + Context 동기화 시작")
        logger.info("=" * 80)
        
        # 1. 최근 24시간 Alert 조회
        alerts = await self._get_recent_alerts()
        logger.info(f"📋 총 Alert 수: {len(alerts)}")
        
        # 2. 각 Alert별로 컨텍스트 수집
        for alert in alerts:
            await self._sync_alert_with_context(alert)
        
        logger.info("✅ Alert + Context 동기화 완료")
    
    async def _get_recent_alerts(self) -> List[Dict]:
        """최근 24시간 Alert 조회"""
        query = """
        SELECT *
        FROM embeddings_alert_events
        WHERE to_timestamp(clock) >= now() - interval '24 hours'
        ORDER BY clock DESC
        """
        rows = await self.conn.fetch(query)
        return [dict(row) for row in rows]
    
    async def _sync_alert_with_context(self, alert: Dict):
        """Alert에 컨텍스트 추가"""
        triggerid = alert['triggerid']
        hostid = alert['hostid']
        clock = alert['clock']
        
        # Alert 발생 시간 정규화
        event_time = normalize_timestamp(clock, is_unix=True)
        if not event_time:
            logger.error(f"  ❌ Alert {triggerid}: 시간 정규화 실패")
            return
        
        time_bucket = create_time_bucket(event_time, 60)
        date_partition = event_time.date()
        hour_partition = event_time.hour
        
        logger.info(f"  📍 Alert {triggerid} ({alert['name']}) 처리 중...")
        
        # Priority 텍스트 변환
        priority_map = {
            0: 'Not classified',
            1: 'Information',
            2: 'Warning',
            3: 'Average',
            4: 'High',
            5: 'Disaster'
        }
        priority_text = priority_map.get(alert['priority'], 'Unknown')
        
        # 컨텍스트 시간 범위 (±5분)
        context_start = event_time - timedelta(minutes=5)
        context_end = event_time + timedelta(minutes=5)
        
        # 서버 컨텍스트 조회
        server_context = await self._get_server_context(hostid, context_start, context_end)
        
        # 네트워크 컨텍스트 조회
        network_context = await self._get_network_context(hostid, context_start, context_end)
        
        # 연관 Alert 조회
        related_alerts = await self._get_related_alerts(hostid, event_time, triggerid)
        
        # 컨텍스트 품질 평가
        has_server_context = server_context is not None
        has_network_context = network_context is not None
        has_related_alerts = len(related_alerts) > 0
        
        if has_server_context and has_network_context:
            context_quality = 'complete'
        elif has_server_context or has_network_context:
            context_quality = 'partial'
        else:
            context_quality = 'none'
        
        # 통합 요약 텍스트 생성
        context_summary = self._build_context_summary(
            alert, priority_text, event_time,
            server_context, network_context, related_alerts
        )
        
        # 소스 레코드 ID 추적
        source_record_ids = {
            'alert_record': str(triggerid),
            'server_perf_record': f"{hostid}_{time_bucket}" if has_server_context else None,
            'network_perf_record': f"{hostid}_{time_bucket}" if has_network_context else None,
            'related_alert_ids': [ra['triggerid'] for ra in related_alerts]
        }
        
        # 청킹 테이블에 삽입 (UPSERT)
        await self._upsert_alert_record(
            alert=alert,
            priority_text=priority_text,
            event_time=event_time,
            time_bucket=time_bucket,
            date_partition=date_partition,
            hour_partition=hour_partition,
            server_context=server_context,
            network_context=network_context,
            related_alerts=related_alerts,
            context_summary=context_summary,
            source_record_ids=source_record_ids,
            context_quality=context_quality,
            has_server_context=has_server_context,
            has_network_context=has_network_context,
            has_related_alerts=has_related_alerts
        )
        
        logger.info(f"    ✅ Alert {triggerid}: {context_quality} (서버: {has_server_context}, 네트워크: {has_network_context}, 연관: {len(related_alerts)})")
    
    async def _get_server_context(self, hostid: int, start_time: datetime, end_time: datetime) -> Optional[Dict]:
        """서버 컨텍스트 조회 (청킹 테이블에서)"""
        # 청킹 테이블에서 조회
        query = """
        SELECT 
            cpu_metrics, memory_metrics, swap_metrics,
            filesystem_metrics, top_processes
        FROM embeddings_server_performance_hourly
        WHERE hostid = $1
        AND event_time >= $2
        AND event_time <= $3
        ORDER BY event_time DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_time, end_time)
        
        if not row:
            return None
        
        context = {
            'context_time_range': f"{start_time.isoformat()} ~ {end_time.isoformat()}"
        }
        
        # CPU
        if row['cpu_metrics']:
            cpu = json.loads(row['cpu_metrics'])
            context['cpu'] = {
                'utilization': cpu.get('cpu_utilization'),
                'idle_time': cpu.get('cpu_idle_time'),
                'stability': cpu.get('stability_level')
            }
        
        # Memory
        if row['memory_metrics']:
            mem = json.loads(row['memory_metrics'])
            context['memory'] = {
                'available_percent': mem.get('available_memory_in'),
                'utilization': 100 - mem.get('available_memory_in', 0)
            }
        
        # Swap
        if row['swap_metrics']:
            swap = json.loads(row['swap_metrics'])
            context['swap'] = {
                'used_percent': swap.get('used_swap_percent')
            }
        
        # Filesystem
        if row['filesystem_metrics']:
            fs_list = json.loads(row['filesystem_metrics'])
            context['filesystem'] = [
                {'fs_name': fs['fs_name'], 'utilization': fs['space_utilization']}
                for fs in fs_list
            ]
        
        # Top Processes
        if row['top_processes']:
            proc_list = json.loads(row['top_processes'])
            context['top_processes'] = [
                {'command': proc['command'], 'cpu': proc['cpu'], 'mem': proc['mem']}
                for proc in proc_list[:3]  # Top 3만
            ]
        
        return context
    
    async def _get_network_context(self, hostid: int, start_time: datetime, end_time: datetime) -> Optional[Dict]:
        """네트워크 컨텍스트 조회 (청킹 테이블에서)"""
        query = """
        SELECT 
            device_info, interface_metrics, interface_summary
        FROM embeddings_network_performance_hourly
        WHERE hostid = $1
        AND event_time >= $2
        AND event_time <= $3
        ORDER BY event_time DESC
        LIMIT 1
        """
        row = await self.conn.fetchrow(query, hostid, start_time, end_time)
        
        if not row:
            return None
        
        context = {
            'context_time_range': f"{start_time.isoformat()} ~ {end_time.isoformat()}"
        }
        
        # Device Info
        if row['device_info']:
            device = json.loads(row['device_info'])
            context['device_status'] = device.get('agent_status')
            context['icmp_response_time'] = device.get('icmp_response_time')
        
        # High Utilization Interfaces
        if row['interface_metrics']:
            ifaces = json.loads(row['interface_metrics'])
            # 사용률 > 80% 또는 에러 > 0인 인터페이스만
            problem_ifaces = [
                {
                    'interface': iface['interface'],
                    'utilization': iface['link'],
                    'errors': iface['out_error_value'] + iface['in_error_value']
                }
                for iface in ifaces
                if iface['link'] > 80 or iface['out_error_value'] > 0 or iface['in_error_value'] > 0
            ]
            if problem_ifaces:
                context['interfaces'] = problem_ifaces
        
        return context
    
    async def _get_related_alerts(self, hostid: int, event_time: datetime, current_triggerid: int) -> List[Dict]:
        """연관 Alert 조회 (±10분)"""
        start_time = event_time - timedelta(minutes=10)
        end_time = event_time + timedelta(minutes=10)
        
        start_unix = int(start_time.timestamp())
        end_unix = int(end_time.timestamp())
        
        query = """
        SELECT triggerid, name, priority, clock
        FROM embeddings_alert_events
        WHERE hostid = $1
        AND clock >= $2
        AND clock <= $3
        AND triggerid != $4
        ORDER BY clock
        LIMIT 10
        """
        rows = await self.conn.fetch(query, hostid, start_unix, end_unix, current_triggerid)
        
        related = []
        event_unix = int(event_time.timestamp())
        
        for row in rows:
            time_diff = row['clock'] - event_unix
            related.append({
                'triggerid': row['triggerid'],
                'alert_name': row['name'],
                'priority': row['priority'],
                'time_diff_seconds': time_diff
            })
        
        return related
    
    def _build_context_summary(
        self, alert: Dict, priority_text: str, event_time: datetime,
        server_context: Optional[Dict], network_context: Optional[Dict],
        related_alerts: List[Dict]
    ) -> str:
        """통합 컨텍스트 요약 텍스트 생성"""
        
        lines = []
        
        # Alert 기본 정보
        lines.append(f"Alert ID: {alert['triggerid']} | 호스트 ID: {alert['hostid']} | 자산: {alert['asset']} | IP: {alert['ip']}")
        lines.append(f"발생 시간: {event_time.isoformat()} | 심각도: {priority_text} ({alert['priority']})")
        lines.append(f"Alert 내용: {alert['name']}")
        lines.append(f"상태: {'발생중' if alert['flag'] == 'y' else '해결됨'}")
        lines.append("")
        
        # 서버 컨텍스트
        if server_context:
            lines.append(f"[발생 시점 서버 상태 ({server_context['context_time_range']})]")
            
            if 'cpu' in server_context:
                cpu = server_context['cpu']
                lines.append(f"  CPU: 사용률 {cpu['utilization']:.2f}%, Idle {cpu['idle_time']:.2f}%, {cpu['stability']}")
            
            if 'memory' in server_context:
                mem = server_context['memory']
                lines.append(f"  메모리: 사용 가능 {mem['available_percent']:.2f}%, 사용률 {mem['utilization']:.2f}%")
            
            if 'swap' in server_context:
                lines.append(f"  Swap: 사용률 {server_context['swap']['used_percent']:.2f}%")
            
            if 'filesystem' in server_context:
                fs_strs = [f"{fs['fs_name']} {fs['utilization']:.2f}%" for fs in server_context['filesystem']]
                lines.append(f"  파일시스템: {', '.join(fs_strs)}")
            
            if 'top_processes' in server_context:
                lines.append(f"  주요 프로세스:")
                for proc in server_context['top_processes']:
                    cmd = proc['command'][:30] + '...' if len(proc['command']) > 30 else proc['command']
                    lines.append(f"    - {cmd} (CPU {proc['cpu']:.2f}%, MEM {proc['mem']:.2f}%)")
            
            lines.append("")
        
        # 네트워크 컨텍스트
        if network_context:
            lines.append(f"[발생 시점 네트워크 상태 ({network_context['context_time_range']})]")
            
            if 'device_status' in network_context:
                lines.append(f"  장비 상태: {network_context['device_status']}")
            
            if 'icmp_response_time' in network_context:
                lines.append(f"  ICMP 응답: {network_context['icmp_response_time']:.2f} ms")
            
            if 'interfaces' in network_context:
                lines.append(f"  문제 인터페이스:")
                for iface in network_context['interfaces']:
                    lines.append(f"    - {iface['interface']}: 사용률 {iface['utilization']:.2f}%, 에러 {iface['errors']}건")
            
            lines.append("")
        
        # 연관 Alert
        if related_alerts:
            lines.append(f"[연관 Alert ({len(related_alerts)}건)]")
            for ra in related_alerts:
                time_str = f"{ra['time_diff_seconds']//60}분 {'전' if ra['time_diff_seconds'] < 0 else '후'}"
                lines.append(f"  - {time_str}: {ra['alert_name']} (심각도 {ra['priority']})")
        
        return '\n'.join(lines)
    
    async def _upsert_alert_record(
        self, alert: Dict, priority_text: str,
        event_time: datetime, time_bucket: str,
        date_partition, hour_partition: int,
        server_context: Optional[Dict], network_context: Optional[Dict],
        related_alerts: List[Dict], context_summary: str,
        source_record_ids: Dict, context_quality: str,
        has_server_context: bool, has_network_context: bool, has_related_alerts: bool
    ):
        """Alert + Context 레코드 삽입 또는 업데이트"""
        
        query = """
        INSERT INTO embeddings_alerts_with_context (
            triggerid, hostid, asset, ip, asset_name, os, system_name, group_name,
            alert_name, value, flag, priority, priority_text,
            event_time, time_bucket, date_partition, hour_partition,
            alert_summary, server_context, network_context, related_alerts,
            context_summary, source_record_ids, context_quality,
            has_server_context, has_network_context, has_related_alerts,
            source_created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            $9, $10, $11, $12, $13,
            $14, $15, $16, $17,
            $18, $19, $20, $21,
            $22, $23, $24,
            $25, $26, $27,
            $28
        )
        ON CONFLICT (triggerid)
        DO UPDATE SET
            server_context = EXCLUDED.server_context,
            network_context = EXCLUDED.network_context,
            related_alerts = EXCLUDED.related_alerts,
            context_summary = EXCLUDED.context_summary,
            source_record_ids = EXCLUDED.source_record_ids,
            context_quality = EXCLUDED.context_quality,
            has_server_context = EXCLUDED.has_server_context,
            has_network_context = EXCLUDED.has_network_context,
            has_related_alerts = EXCLUDED.has_related_alerts,
            updated_at = now()
        """
        
        await self.conn.execute(
            query,
            alert['triggerid'], alert['hostid'], alert['asset'], alert['ip'],
            alert['asset_name'], alert['os'], alert['system_name'], alert['group_name'],
            alert['name'], alert['value'], alert['flag'], alert['priority'], priority_text,
            event_time, time_bucket, date_partition, hour_partition,
            alert['alert_summary'],
            json.dumps(server_context) if server_context else None,
            json.dumps(network_context) if network_context else None,
            json.dumps(related_alerts) if related_alerts else None,
            context_summary,
            json.dumps(source_record_ids),
            context_quality,
            has_server_context, has_network_context, has_related_alerts,
            alert.get('source_created_at')
        )


# ============================================================
# 메인 동기화 실행
# ============================================================

async def main():
    """메인 동기화 프로세스"""
    logger.info("=" * 80)
    logger.info("🚀 Embeddings → 청킹 테이블 동기화 시작")
    logger.info("=" * 80)
    
    # DB 연결
    conn = await asyncpg.connect(**RAG_DB)
    logger.info("✅ RAG DB 연결 성공")
    
    try:
        # 1. 서버 Performance 동기화
        server_sync = ServerPerformanceSynchronizer(conn)
        await server_sync.sync_all()
        
        # 2. 네트워크 Performance 동기화
        network_sync = NetworkPerformanceSynchronizer(conn)
        await network_sync.sync_all()
        
        # 3. Alert + Context 동기화
        alert_sync = AlertContextSynchronizer(conn)
        await alert_sync.sync_all()
        
        logger.info("=" * 80)
        logger.info("✅ 모든 동기화 완료!")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error(f"❌ 동기화 중 에러 발생: {e}", exc_info=True)
        raise
    
    finally:
        await conn.close()
        logger.info("🔌 DB 연결 종료")


if __name__ == '__main__':
    asyncio.run(main())


"""운영 레포트 API·집계 단위 테스트."""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.operational_report import (
    SCHEMA_VERSION,
    build_operational_report_payload,
)


class _FakeConn:
    """Zabbix 쿼리 문자열에 따라 최소 응답."""

    async def fetchrow(self, query: str, *args):
        q = query.lower()
        if "svr_system_main" in q:
            return {"c": 3}
        if "nw_main1" in q:
            return {"c": 2}
        if "vmware_hypervisor_info" in q:
            return {"c": 1}
        if "vmware_info" in q and "count" in q:
            return {"c": 4}
        return {"c": 0}

    async def fetch(self, query: str, *args):
        q = query.lower()
        if "group by priority" in q:
            return [{"priority": 4, "cnt": 2}, {"priority": 2, "cnt": 5}]
        if "to_timestamp(clock)" in q:
            return [{"d": __import__("datetime").date(2026, 4, 1), "cnt": 3}]
        if "group by 1" in q and "order by cnt desc" in q:
            return [{"asset": "HOST_A", "cnt": 7}]
        if "order by clock desc" in q:
            return [
                {
                    "asset": "H1",
                    "hostid": 1,
                    "priority": 3,
                    "name": "Test trigger",
                    "clock": 1712000000,
                    "value": 1,
                    "flag": "y",
                }
            ]
        return []


class _FakePool:
    def acquire(self):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=_FakeConn())
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


def test_build_operational_report_payload_mocked():
    async def _fake_zabbix():
        return _FakePool()

    t0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 7, tzinfo=timezone.utc)

    async def _run():
        with patch(
            "app.services.operational_report.get_zabbix_pool",
            _fake_zabbix,
        ), patch(
            "app.services.operational_report.collect_embedding_period_stats",
            AsyncMock(return_value=([], [])),
        ):
            return await build_operational_report_payload(t0, t1)

    payload, _warnings = asyncio.run(_run())

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["kpis"]["servers"] == 3
    assert payload["kpis"]["network"] == 2
    assert payload["kpis"]["alerts_in_period"] == 7
    assert len(payload["charts"]["alerts_by_priority"]["labels"]) == 2
    assert payload["tables"]["top_alert_assets"][0]["asset"] == "HOST_A"
    assert "executive_summary" in payload
    assert "벡터 데이터 기반" in payload["executive_summary"]
    assert "관리 대상 자산" in payload["executive_summary"]
    assert "vector_period_kpis" in payload
    assert payload["vector_period_kpis"]["rows_in_period_total"] == 0


def test_parse_iso_invalid_raises():
    from app.routers.reports import _parse_iso

    with pytest.raises(ValueError):
        _parse_iso("not-an-iso-datetime")

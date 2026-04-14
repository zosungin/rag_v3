"""pytest 공통 설정 및 마커 정의."""

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: 실 API·DB·LLM 등 외부 의존 (기본 CI에서는 제외)",
    )


def pytest_collection_modifyitems(config, items):
    skip_int = pytest.mark.skip(reason="integration — RUN_INTEGRATION_TESTS=1 로 활성화")
    if not config.getoption("--run-integration", default=False):
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_int)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="integration 마크된 테스트 실행",
    )

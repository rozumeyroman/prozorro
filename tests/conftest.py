import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_prozorro import FakeProzorro, demo_tenders  # noqa: E402

from prozorro_mcp.filter import TenderFilter  # noqa: E402
from prozorro_mcp.settings import DEFAULT_FILTER_CONFIG, KYIV_TZ  # noqa: E402

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=KYIV_TZ)


@pytest.fixture
def tender_filter() -> TenderFilter:
    return TenderFilter.from_file(DEFAULT_FILTER_CONFIG)


@pytest.fixture
def tenders():
    return demo_tenders(NOW)


@pytest.fixture
def fake(tenders) -> FakeProzorro:
    return FakeProzorro(tenders)

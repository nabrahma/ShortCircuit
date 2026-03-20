import os
import sys

# Standardize Environment for Tests
os.environ.setdefault('FYERS_CLIENT_ID', 'dummy')
os.environ.setdefault('FYERS_SECRET_ID', 'dummy')
os.environ.setdefault('FYERS_REDIRECT_URI', 'http://localhost/')
os.environ.setdefault('FYERS_ACCESS_TOKEN', 'token')


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from unittest.mock import AsyncMock

@pytest.fixture(autouse=True)
def mock_fyers_get_funds(monkeypatch):
    """
    Provide robust mock for GET /funds to protect CapitalManager.sync(broker)
    from causing real API calls during tests.
    """
    from fyers_broker_interface import FyersBrokerInterface
    monkeypatch.setattr(
        FyersBrokerInterface,
        "get_funds",
        AsyncMock(return_value={"s": "ok", "fund_limit": [{"id": 2, "equityAmount": 1800.0}]})
    )

"""
Safety guards for every test in the repository: the engine's suite under tests/
and plug-in tests under strategies/.

They live at the root, not in tests/conftest.py, because a conftest only governs
the directory it sits in. Under tests/ they would leave strategies/tests/
unguarded, and that is where a test written by a strategy author, holding real
credentials in `.env`, is most likely to reach a live broker by accident.

1. **Tests never touch the network.** Enforced structurally by `no_network`, not
   by convention. A test that reaches a live API fails loudly instead of quietly
   passing, or quietly placing an order.
2. **Tests never see real credentials.** `no_real_credentials` strips them from
   the environment.

Integration tests opt out of both with `@pytest.mark.integration`, because they
legitimately talk to a local PostgreSQL instance.
"""
from __future__ import annotations

import socket
import sys

import pytest

# Windows: socket.socketpair() is implemented in Python on top of
# socket.socket.connect to 127.0.0.1, and every asyncio event loop creates one.
# The no_network guard below patches connect, so without this every async test
# fails on Windows while passing on Linux. Rebuilt here with the connect captured
# before the guard exists — loopback self-pairs only; all other connects stay
# blocked.
if sys.platform == "win32":
    _unguarded_connect = socket.socket.connect

    def _loopback_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        listener = socket.socket(family, type, proto)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            client = socket.socket(family, type, proto)
            _unguarded_connect(client, listener.getsockname())
            server, _ = listener.accept()
        finally:
            listener.close()
        return server, client

    socket.socketpair = _loopback_socketpair


CREDENTIAL_VARS = (
    "FYERS_CLIENT_ID", "FYERS_SECRET_ID", "FYERS_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DB_PASS", "DB_PASSWORD",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Block outbound sockets in every test not marked `integration`."""
    if "integration" in request.keywords:
        return

    def guard(*args, **kwargs):
        raise RuntimeError(
            "network access attempted in a unit test — "
            "mock the broker or mark the test @pytest.mark.integration"
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch, request):
    """
    Ensure tests never pick up the developer's real `.env` values.

    Without this, a test asserting "config loads" would pass on the author's
    machine using live broker credentials and fail everywhere else.
    """
    if "integration" in request.keywords:
        return
    for key in CREDENTIAL_VARS:
        monkeypatch.delenv(key, raising=False)

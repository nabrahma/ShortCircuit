"""
The premise behind the aiohttp entries in .trivyignore: no aiohttp code runs.

fyers_apiv3 pins aiohttp==3.9.3 exactly, so the advisories against it cannot be
cleared by an upgrade. They are suppressed because the code they describe never
executes in this process: the SDK imports aiohttp at module load but opens a
ClientSession only on its is_async=True path, and every FyersModel here is
synchronous. If any of that changes, these tests fail and the suppressions have
to be re-assessed rather than silently going stale.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import types

import pytest
import requests

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_DIRS = [ROOT / "src", ROOT / "strategies"]


def _python_files():
    for base in SOURCE_DIRS:
        if base.is_dir():
            yield from (p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _trees():
    for path in _python_files():
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def test_every_fyers_model_is_synchronous():
    offenders = []
    constructed = 0
    for path, tree in _trees():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_name(node) == "FyersModel"):
                continue
            constructed += 1
            for kw in node.keywords:
                if kw.arg is None:
                    offenders.append(f"{path}:{node.lineno} passes **kwargs; is_async cannot be verified")
                elif kw.arg == "is_async" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    offenders.append(f"{path}:{node.lineno} is_async={ast.unparse(kw.value)}")
            if len(node.args) >= 1:
                # is_async is FyersModel's first positional parameter.
                offenders.append(f"{path}:{node.lineno} passes positional arguments")
    assert constructed, "no FyersModel construction found; the scan is looking in the wrong place"
    assert not offenders, "\n".join(offenders)


def test_nothing_imports_aiohttp_directly():
    offenders = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "aiohttp" or n.startswith("aiohttp.") for n in names):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, "aiohttp imported directly: " + ", ".join(offenders)


def test_sync_sdk_path_never_opens_an_aiohttp_session(monkeypatch, tmp_path):
    """The vendor half of the premise: a sync FyersModel's calls go through requests."""
    aiohttp = pytest.importorskip("aiohttp")
    from fyers_apiv3 import fyersModel

    def forbidden(*args, **kwargs):
        raise AssertionError("aiohttp.ClientSession opened by the synchronous SDK path")

    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)

    payload = {"s": "ok", "code": 200, "data": {}}
    sent = []

    class _Response:
        status_code = 200

        def json(self):
            return payload

        def raise_for_status(self):
            return None

    def verb(name):
        def send(*args, **kwargs):
            sent.append(name)
            return _Response()
        return send

    fake = types.SimpleNamespace(
        HTTPError=requests.HTTPError,
        **{name: verb(name) for name in ("get", "post", "patch", "delete", "put")},
    )
    monkeypatch.setattr(fyersModel, "requests", fake)

    model = fyersModel.FyersModel(client_id="TEST-100", token="t", log_path=str(tmp_path))
    try:
        assert model.get_profile() == payload
        assert model.orderbook() == payload
        assert model.positions() == payload
        assert model.place_order(data={"symbol": "NSE:TESTSYM-EQ"}) == payload
        assert model.cancel_order(data={"id": "1"}) == payload
    finally:
        for name in ("FyersAPI", "FyersAPIRequest"):
            for handler in list(logging.getLogger(name).handlers):
                if str(tmp_path) in str(getattr(handler, "baseFilename", "")):
                    handler.close()
                    logging.getLogger(name).removeHandler(handler)

    assert sent == ["get", "get", "get", "post", "delete"]

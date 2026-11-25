"""CI client resilience: transient failures while waiting must not fail the deploy."""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "shipgate_gate", Path(__file__).parent.parent / "scripts" / "shipgate_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _status(status, approvals=0):
    return Resp(json.dumps({"id": "r1", "status": status, "approvals": approvals, "required_approvals": 1}).encode())


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SHIPGATE_URL", "https://shipgate.example")
    monkeypatch.setenv("SHIPGATE_API_TOKEN", "t")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(gate.time, "sleep", lambda s: None)
    monkeypatch.setattr(gate.signal, "signal", lambda *a: None)
    return tmp_path / "out"


def _run(monkeypatch, responses, *extra):
    it = iter(responses)

    def fake_urlopen(req, timeout):
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        ["gate", "request", "--service", "s", "--environment", "staging", "--version", "v", *extra],
    )
    return gate.main()


def test_survives_timeout_and_restart_while_waiting(monkeypatch, env):
    responses = [
        _status("pending"),                      # POST create
        TimeoutError("The read operation timed out"),  # machine stopping
        urllib.error.URLError("connection refused"),
        ConnectionResetError(),                  # 3 failed attempts -> Unavailable, keep waiting
        _status("pending"),                      # back up
        _status("approved", 1),
    ]
    assert _run(monkeypatch, responses) == 0
    assert "status=approved" in env.read_text()


def test_fails_closed_after_long_outage(monkeypatch, env):
    monkeypatch.setattr(gate.time, "monotonic", iter(range(0, 10_000, 100)).__next__)
    responses = [_status("pending")] + [TimeoutError()] * 100
    assert _run(monkeypatch, responses, "--max-outage-seconds", "300") == 1
    assert "status=unreachable" in env.read_text()


def test_rejected_blocks(monkeypatch):
    assert _run(monkeypatch, [_status("pending"), _status("rejected")]) == 1


def test_client_error_not_retried(monkeypatch):
    err = urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b"bad token"))
    with pytest.raises(SystemExit, match="401"):
        _run(monkeypatch, [err])

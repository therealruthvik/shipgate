from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shipgate.api import create_api
from shipgate.models import DecisionType

AUTH = {"Authorization": "Bearer test-token"}
BODY = {
    "service": "payments-api",
    "environment": "production",
    "version": "v2.0.0",
    "requested_by": "octocat",
    "requester_slack_id": "UREQ",
    "run_url": "https://github.com/acme/payments/actions/runs/1",
    "commit_sha": "abc123def456789",
}


@pytest.fixture
def client(service):
    return TestClient(create_api(service, "test-token"))


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-token"}])
def test_auth_required(client, headers):
    assert client.post("/approvals", json=BODY, headers=headers).status_code == 401


def test_full_flow(client, service):
    r = client.post("/approvals", json=BODY, headers=AUTH)
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "pending" and data["required_approvals"] == 2

    rid = data["id"]
    service.decide(rid, "UALICE", DecisionType.APPROVE)
    service.decide(rid, "UBOB", DecisionType.APPROVE)

    got = client.get(f"/approvals/{rid}", headers=AUTH).json()
    assert got["status"] == "approved"
    assert [d["approver_id"] for d in got["decisions"]] == ["UALICE", "UBOB"]

    ev = client.post(f"/approvals/{rid}/events", json={"event": "succeeded"}, headers=AUTH)
    assert ev.json()["deploy_status"] == "succeeded"


def test_validation(client):
    bad = {**BODY, "run_url": "javascript:alert(1)"}
    assert client.post("/approvals", json=bad, headers=AUTH).status_code == 422
    bad = {**BODY, "requester_slack_id": "<!channel>"}
    assert client.post("/approvals", json=bad, headers=AUTH).status_code == 422


def test_not_found(client):
    assert client.get("/approvals/nope", headers=AUTH).status_code == 404
    assert client.post("/approvals/nope/cancel", headers=AUTH).status_code == 404


def test_bad_event(client):
    rid = client.post("/approvals", json=BODY, headers=AUTH).json()["id"]
    r = client.post(f"/approvals/{rid}/events", json={"event": "boom"}, headers=AUTH)
    assert r.status_code == 422


def test_cancel(client):
    rid = client.post("/approvals", json=BODY, headers=AUTH).json()["id"]
    assert client.post(f"/approvals/{rid}/cancel", headers=AUTH).json()["status"] == "cancelled"

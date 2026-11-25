from __future__ import annotations

import pytest

from shipgate import slack_ui
from shipgate.config import _normalize_db_url, _parse_required_approvals
from shipgate.models import DecisionType


def _action_ids(blocks):
    return [
        el["action_id"] for b in blocks if b["type"] == "actions" for el in b["elements"]
    ]


def test_pending_request_has_buttons(make_request):
    req = make_request()
    blocks = slack_ui.request_blocks(req)
    assert _action_ids(blocks) == [slack_ui.APPROVE_ACTION, slack_ui.REJECT_ACTION]
    assert all(len(b.get("fields", [])) <= 10 for b in blocks)


def test_final_request_has_no_buttons(make_request, service):
    req = make_request(environment="staging")
    req = service.decide(req.id, "UALICE", DecisionType.APPROVE)
    blocks = slack_ui.request_blocks(req)
    assert _action_ids(blocks) == []
    assert "Approved" in str(blocks)


def test_reject_modal_roundtrips_metadata():
    view = slack_ui.reject_modal("rid", "C1", "123.456")
    assert view["private_metadata"].split("|") == ["rid", "C1", "123.456"]


def test_history_blocks_empty_and_full(make_request, service):
    assert "No deploy requests" in str(slack_ui.history_blocks([]))
    make_request()
    assert "payments-api" in str(slack_ui.history_blocks(service.history()))


def test_parse_required_approvals():
    assert _parse_required_approvals("Production=2, staging=1") == {"production": 2, "staging": 1}
    with pytest.raises(ValueError):
        _parse_required_approvals("production")
    with pytest.raises(ValueError):
        _parse_required_approvals("production=0")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_db_url(raw, expected):
    assert _normalize_db_url(raw) == expected


def test_usergroup_policy(make_request, service):
    from shipgate.policy import ApproverPolicy, PolicyError

    service.policy = ApproverPolicy(approver_source=lambda: frozenset({"UGROUP"}))
    req = make_request(environment="staging")
    with pytest.raises(PolicyError):
        service.decide(req.id, "UALICE", DecisionType.APPROVE)
    assert service.decide(req.id, "UGROUP", DecisionType.APPROVE).status.value == "approved"

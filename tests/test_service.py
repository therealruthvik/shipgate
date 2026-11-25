from __future__ import annotations

from datetime import timedelta

import pytest

from shipgate.models import DecisionType, Status, as_utc
from shipgate.policy import PolicyError

APPROVE, REJECT = DecisionType.APPROVE, DecisionType.REJECT


def test_create_posts_to_slack_and_stores_message_ref(make_request, notifier):
    req = make_request()
    assert req.status is Status.PENDING
    assert req.required_approvals == 2
    assert notifier.posted == [req.id]
    assert req.slack_channel == "C123" and req.slack_ts


def test_staging_needs_one_approval(make_request, service):
    req = make_request(environment="staging")
    req = service.decide(req.id, "UALICE", APPROVE)
    assert req.status is Status.APPROVED


def test_production_needs_two_distinct_approvals(make_request, service, notifier):
    req = make_request()
    req = service.decide(req.id, "UALICE", APPROVE)
    assert req.status is Status.PENDING
    assert any("1/2 approvals" in t for _, t in notifier.thread)
    req = service.decide(req.id, "UBOB", APPROVE)
    assert req.status is Status.APPROVED
    assert req.resolved_at is not None
    # Requester DM'd about the outcome.
    assert any(uid == "UREQ" and "Approved" in t for uid, t in notifier.dms)


def test_same_user_cannot_approve_twice(make_request, service):
    req = make_request()
    service.decide(req.id, "UALICE", APPROVE)
    with pytest.raises(PolicyError, match="already responded"):
        service.decide(req.id, "UALICE", APPROVE)
    assert service.get(req.id).status is Status.PENDING


def test_requester_cannot_self_approve(make_request, service):
    req = make_request(requester_slack_id="UREQ")
    with pytest.raises(PolicyError, match="your own"):
        service.decide(req.id, "UREQ", APPROVE)


def test_non_approver_blocked(make_request, service):
    req = make_request()
    with pytest.raises(PolicyError, match="not on the approvers list"):
        service.decide(req.id, "UMALLORY", APPROVE)


def test_single_rejection_blocks_even_after_approval(make_request, service):
    req = make_request()
    service.decide(req.id, "UALICE", APPROVE)
    req = service.decide(req.id, "UBOB", REJECT, reason="Error budget exhausted")
    assert req.status is Status.REJECTED
    assert req.rejections[0].reason == "Error budget exhausted"


def test_cannot_decide_on_final_request(make_request, service):
    req = make_request(environment="staging")
    service.decide(req.id, "UALICE", APPROVE)
    with pytest.raises(PolicyError, match="already approved"):
        service.decide(req.id, "UBOB", REJECT)


def test_decide_after_expiry_marks_expired(make_request, service):
    req = make_request(timeout_minutes=5)
    later = as_utc(req.created_at) + timedelta(minutes=6)
    with pytest.raises(PolicyError, match="expired"):
        service.decide(req.id, "UALICE", APPROVE, now=later)
    assert service.get(req.id).status is Status.EXPIRED


def test_sweep_reminds_at_halfway_then_expires(make_request, service, notifier):
    req = make_request(timeout_minutes=10)
    t0 = as_utc(req.created_at)

    assert service.sweep(now=t0 + timedelta(minutes=2)) == {"expired": 0, "reminded": 0}
    assert service.sweep(now=t0 + timedelta(minutes=5)) == {"expired": 0, "reminded": 1}
    # Reminder only once.
    assert service.sweep(now=t0 + timedelta(minutes=7)) == {"expired": 0, "reminded": 0}
    assert any("Still waiting" in t for _, t in notifier.thread)

    assert service.sweep(now=t0 + timedelta(minutes=10)) == {"expired": 1, "reminded": 0}
    assert service.get(req.id).status is Status.EXPIRED
    assert any("Expired" in t for _, t in notifier.dms)


def test_cancel(make_request, service):
    req = make_request()
    assert service.cancel(req.id).status is Status.CANCELLED
    # Idempotent.
    assert service.cancel(req.id).status is Status.CANCELLED


def test_deploy_events_post_to_thread_and_dm(make_request, service, notifier):
    req = make_request(environment="staging")
    service.decide(req.id, "UALICE", APPROVE)
    service.record_deploy_event(req.id, "started")
    req = service.record_deploy_event(req.id, "succeeded", "All health checks green")
    assert req.deploy_status == "succeeded"
    assert any("succeeded" in t and "health checks" in t for _, t in notifier.thread)
    assert any(uid == "UREQ" and "succeeded" in t for uid, t in notifier.dms)


def test_invalid_deploy_event(make_request, service):
    req = make_request()
    with pytest.raises(ValueError):
        service.record_deploy_event(req.id, "exploded")


def test_slack_outage_does_not_break_workflow(make_request, service, notifier):
    notifier.fail = True
    req = make_request(environment="staging")
    assert req.slack_ts is None
    req = service.decide(req.id, "UALICE", APPROVE)
    assert req.status is Status.APPROVED


def test_history_newest_first(make_request, service):
    a = make_request(service_name="a")
    b = make_request(service_name="b")
    ids = [r.id for r in service.history(10)]
    assert set(ids) == {a.id, b.id}
    assert len(service.history(1)) == 1

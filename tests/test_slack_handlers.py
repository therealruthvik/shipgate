"""Dispatch real Slack payload shapes through Bolt with a mocked WebClient."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient

from shipgate import slack_ui
from shipgate.models import Status
from shipgate.slack_app import register_handlers


class Calls:
    def __init__(self, monkeypatch):
        # Bolt builds a fresh WebClient per request, so patch the class.
        for name in ("chat_postEphemeral", "views_open", "chat_postMessage"):
            mock = MagicMock()
            setattr(self, name, mock)
            monkeypatch.setattr(WebClient, name, lambda _self, *a, _m=mock, **kw: _m(*a, **kw))


@pytest.fixture
def bolt(service, monkeypatch):
    app = App(
        signing_secret="unused",
        authorize=lambda **_: AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_user_id="UBOT"
        ),
        request_verification_enabled=False,
        process_before_response=True,  # run handlers synchronously for assertions
    )
    app.calls = Calls(monkeypatch)
    register_handlers(app, service)
    return app


def _block_action(action_id, request_id, user_id):
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user_id, "username": user_id.lower()},
        "channel": {"id": "C123"},
        "message": {"ts": "1700000000.000001"},
        "trigger_id": "trig",
        "actions": [{"action_id": action_id, "block_id": "shipgate_decision", "value": request_id, "type": "button"}],
    }
    return BoltRequest(
        body=urlencode({"payload": json.dumps(payload)}),
        headers={"content-type": ["application/x-www-form-urlencoded"]},
    )


def _view_submission(request_id, user_id, reason):
    payload = {
        "type": "view_submission",
        "team": {"id": "T1"},
        "user": {"id": user_id, "username": user_id.lower()},
        "view": {
            "type": "modal",
            "id": "V1",
            "callback_id": slack_ui.REJECT_MODAL,
            "private_metadata": f"{request_id}|C123|1700000000.000001",
            "state": {"values": {"reason": {"value": {"type": "plain_text_input", "value": reason}}}},
        },
    }
    return BoltRequest(
        body=urlencode({"payload": json.dumps(payload)}),
        headers={"content-type": ["application/x-www-form-urlencoded"]},
    )


def test_approve_button(bolt, make_request, service):
    req = make_request(environment="staging")
    resp = bolt.dispatch(_block_action(slack_ui.APPROVE_ACTION, req.id, "UALICE"))
    assert resp.status == 200
    assert service.get(req.id).status is Status.APPROVED


def test_unauthorised_approve_gets_ephemeral_warning(bolt, make_request, service):
    req = make_request(environment="staging")
    bolt.dispatch(_block_action(slack_ui.APPROVE_ACTION, req.id, "UMALLORY"))
    assert service.get(req.id).status is Status.PENDING
    kwargs = bolt.calls.chat_postEphemeral.call_args.kwargs
    assert kwargs["user"] == "UMALLORY" and "approvers list" in kwargs["text"]


def test_reject_button_opens_modal_then_submission_rejects(bolt, make_request, service):
    req = make_request()
    bolt.dispatch(_block_action(slack_ui.REJECT_ACTION, req.id, "UBOB"))
    view = bolt.calls.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == slack_ui.REJECT_MODAL

    resp = bolt.dispatch(_view_submission(req.id, "UBOB", "Missing migration"))
    assert resp.status == 200
    got = service.get(req.id)
    assert got.status is Status.REJECTED and got.rejections[0].reason == "Missing migration"


def test_self_reject_shows_modal_error(bolt, make_request):
    req = make_request(requester_slack_id="UREQ")
    resp = bolt.dispatch(_view_submission(req.id, "UREQ", "oops"))
    body = json.loads(resp.body)
    assert body["response_action"] == "errors" and "your own" in body["errors"]["reason"]


def test_reject_button_blocked_for_non_approver(bolt, make_request):
    req = make_request()
    bolt.dispatch(_block_action(slack_ui.REJECT_ACTION, req.id, "UMALLORY"))
    bolt.calls.views_open.assert_not_called()
    bolt.calls.chat_postEphemeral.assert_called_once()

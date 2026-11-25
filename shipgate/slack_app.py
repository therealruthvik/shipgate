"""Slack integration: notifier (outbound) and Bolt handlers (inbound)."""

from __future__ import annotations

import logging
import threading
import time

from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import slack_ui
from .models import ApprovalRequest, DecisionType
from .policy import PolicyError
from .service import ApprovalService, NotFound

log = logging.getLogger(__name__)

# The pipeline-run link would otherwise expand into a large GitHub preview card.
NO_UNFURL = {"unfurl_links": False, "unfurl_media": False}


class SlackNotifier:
    def __init__(self, client: WebClient, channel_id: str) -> None:
        self.client = client
        self.channel_id = channel_id

    def post_request(self, request: ApprovalRequest) -> tuple[str, str] | None:
        resp = self.client.chat_postMessage(
            channel=self.channel_id,
            text=slack_ui.summary_text(request),
            blocks=slack_ui.request_blocks(request),
            **NO_UNFURL,
        )
        return resp["channel"], resp["ts"]

    def update_request(self, request: ApprovalRequest) -> None:
        if not (request.slack_channel and request.slack_ts):
            return
        self.client.chat_update(
            channel=request.slack_channel,
            ts=request.slack_ts,
            text=slack_ui.summary_text(request),
            blocks=slack_ui.request_blocks(request),
        )

    def post_in_thread(self, request: ApprovalRequest, text: str) -> None:
        if not (request.slack_channel and request.slack_ts):
            return
        self.client.chat_postMessage(
            channel=request.slack_channel, thread_ts=request.slack_ts, text=text, **NO_UNFURL
        )

    def notify_user(self, slack_user_id: str, text: str) -> None:
        # chat.postMessage to a user ID opens/uses the bot DM.
        self.client.chat_postMessage(channel=slack_user_id, text=text, **NO_UNFURL)


class UserGroupApprovers:
    """Members of a Slack user group, cached briefly to avoid rate limits."""

    def __init__(self, client: WebClient, usergroup_id: str, ttl_seconds: int = 300) -> None:
        self.client = client
        self.usergroup_id = usergroup_id
        self.ttl = ttl_seconds
        self._cache: frozenset[str] = frozenset()
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> frozenset[str]:
        with self._lock:
            if time.monotonic() - self._fetched_at > self.ttl:
                try:
                    resp = self.client.usergroups_users_list(usergroup=self.usergroup_id)
                    self._cache = frozenset(resp["users"])
                    self._fetched_at = time.monotonic()
                except SlackApiError:
                    # Fail closed: keep last known list (empty on first failure).
                    log.exception("Could not fetch user group %s", self.usergroup_id)
            return self._cache


def register_handlers(app: App, service: ApprovalService) -> None:
    def _ephemeral(client, body, text: str) -> None:
        client.chat_postEphemeral(
            channel=body["channel"]["id"], user=body["user"]["id"], text=text
        )

    @app.action(slack_ui.APPROVE_ACTION)
    def on_approve(ack, body, client):
        ack()
        user = body["user"]
        request_id = body["actions"][0]["value"]
        try:
            service.decide(
                request_id,
                user_id=user["id"],
                user_name=user.get("username") or user.get("name"),
                decision=DecisionType.APPROVE,
            )
        except (PolicyError, NotFound) as exc:
            _ephemeral(client, body, f":warning: {exc if isinstance(exc, PolicyError) else 'Request not found.'}")

    @app.action(slack_ui.REJECT_ACTION)
    def on_reject(ack, body, client):
        ack()
        request_id = body["actions"][0]["value"]
        # Check permission up front so unauthorised users don't fill in a modal for nothing.
        try:
            req = service.get(request_id)
            service.policy.check_can_decide(req, body["user"]["id"])
        except (PolicyError, NotFound) as exc:
            _ephemeral(client, body, f":warning: {exc if isinstance(exc, PolicyError) else 'Request not found.'}")
            return
        client.views_open(
            trigger_id=body["trigger_id"],
            view=slack_ui.reject_modal(request_id, body["channel"]["id"], body["message"]["ts"]),
        )

    @app.view(slack_ui.REJECT_MODAL)
    def on_reject_submit(ack, body, view, client):
        request_id, channel_id, _ts = view["private_metadata"].split("|")
        reason = view["state"]["values"]["reason"]["value"]["value"].strip()
        user = body["user"]
        # Fast DB-only check so errors show inline in the modal; Slack needs ack() within 3s.
        try:
            service.policy.check_can_decide(service.get(request_id), user["id"])
        except (PolicyError, NotFound) as exc:
            msg = str(exc) if isinstance(exc, PolicyError) else "Request not found."
            ack(response_action="errors", errors={"reason": msg})
            return
        ack()
        try:
            service.decide(
                request_id,
                user_id=user["id"],
                user_name=user.get("username") or user.get("name"),
                decision=DecisionType.REJECT,
                reason=reason,
            )
        except PolicyError as exc:  # lost a race (e.g. approved/expired meanwhile)
            client.chat_postEphemeral(channel=channel_id, user=user["id"], text=f":warning: {exc}")

    @app.command("/shipgate")
    def on_command(ack, command, respond):
        ack()
        args = (command.get("text") or "").split()
        sub = args[0] if args else "help"
        if sub == "history":
            limit = 10
            if len(args) > 1 and args[1].isdigit():
                limit = max(1, min(int(args[1]), 25))
            respond(response_type="ephemeral", blocks=slack_ui.history_blocks(service.history(limit)))
        elif sub == "status" and len(args) > 1:
            matches = [r for r in service.history(100) if r.id.startswith(args[1])]
            if not matches:
                respond(response_type="ephemeral", text=f"No request with ID starting `{args[1]}`.")
            else:
                respond(response_type="ephemeral", blocks=slack_ui.request_blocks(matches[0]))
        else:
            respond(
                response_type="ephemeral",
                text=(
                    "*ShipGate* — deploy approvals\n"
                    "• `/shipgate history [n]` — recent deploy requests\n"
                    "• `/shipgate status <id>` — show one request"
                ),
            )

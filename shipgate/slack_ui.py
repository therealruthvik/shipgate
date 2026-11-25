"""Block Kit builders. Pure functions: request in, Slack JSON out."""

from __future__ import annotations

from .models import ApprovalRequest, Status, as_utc

APPROVE_ACTION = "shipgate_approve"
REJECT_ACTION = "shipgate_reject"
REJECT_MODAL = "shipgate_reject_modal"

STATUS_BADGE = {
    Status.PENDING: ":large_yellow_circle: Pending approval",
    Status.APPROVED: ":large_green_circle: Approved",
    Status.REJECTED: ":red_circle: Rejected",
    Status.EXPIRED: ":white_circle: Expired",
    Status.CANCELLED: ":white_circle: Cancelled",
}

ENV_EMOJI = {"production": ":rotating_light:", "prod": ":rotating_light:", "staging": ":test_tube:"}


def summary_text(req: ApprovalRequest) -> str:
    """Plain-text fallback for notifications."""
    return f"Deploy approval: {req.service} {req.version} → {req.environment} ({req.status.value})"


def request_blocks(req: ApprovalRequest) -> list[dict]:
    env_icon = ENV_EMOJI.get(req.environment, ":package:")
    fields = [
        {"type": "mrkdwn", "text": f"*Service*\n`{req.service}`"},
        {"type": "mrkdwn", "text": f"*Environment*\n{env_icon} {req.environment}"},
        {"type": "mrkdwn", "text": f"*Version*\n`{req.version}`"},
        {"type": "mrkdwn", "text": f"*Requested by*\n{_requester(req)}"},
    ]
    if req.commit_sha:
        fields.append({"type": "mrkdwn", "text": f"*Commit*\n`{req.commit_sha[:12]}`"})
    fields.append(
        {"type": "mrkdwn", "text": f"*Approvals*\n{len(req.approvals)}/{req.required_approvals}"}
    )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Deploy request: {req.service} → {req.environment}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{STATUS_BADGE[req.status]}*"}},
        {"type": "section", "fields": fields[:10]},
    ]
    if req.description:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Notes*\n{req.description[:2900]}"}}
        )

    if req.decisions:
        lines = []
        for d in req.decisions:
            icon = ":white_check_mark:" if d.decision.value == "approve" else ":x:"
            line = f"{icon} <@{d.approver_id}>"
            if d.reason:
                line += f" — _{d.reason[:200]}_"
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if req.status is Status.PENDING:
        blocks.append(
            {
                "type": "actions",
                "block_id": "shipgate_decision",
                "elements": [
                    {
                        "type": "button",
                        "action_id": APPROVE_ACTION,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": req.id,
                    },
                    {
                        "type": "button",
                        "action_id": REJECT_ACTION,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "value": req.id,
                    },
                ],
            }
        )

    context = [f"Expires <!date^{int(as_utc(req.expires_at).timestamp())}^{{time}}|soon>"]
    if req.status is not Status.PENDING:
        context = []
    if req.deploy_status:
        context.append(f"Deploy: *{req.deploy_status}*")
    if req.run_url:
        context.append(f"<{req.run_url}|View pipeline run>")
    context.append(f"ID `{req.id[:8]}`")
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]}
    )
    return blocks


def reject_modal(request_id: str, channel_id: str, message_ts: str) -> dict:
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL,
        # Carry context through to the submission handler.
        "private_metadata": f"{request_id}|{channel_id}|{message_ts}",
        "title": {"type": "plain_text", "text": "Reject deploy"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "Why are you rejecting this deploy?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "min_length": 3,
                    "max_length": 500,
                },
            }
        ],
    }


def history_blocks(requests: list[ApprovalRequest]) -> list[dict]:
    if not requests:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No deploy requests yet."}}]
    lines = []
    for r in requests:
        approvers = ", ".join(f"<@{d.approver_id}>" for d in r.approvals) or "—"
        lines.append(
            f"{STATUS_BADGE[r.status].split(' ')[0]} `{r.service}` `{r.version}` → *{r.environment}* "
            f"· by {_requester(r)} · approvers: {approvers} "
            f"· <!date^{int(as_utc(r.created_at).timestamp())}^{{date_short}} {{time}}|{r.created_at:%Y-%m-%d}>"
        )
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Recent deploy requests"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
    ]


def _requester(req: ApprovalRequest) -> str:
    return f"<@{req.requester_slack_id}>" if req.requester_slack_id else req.requested_by

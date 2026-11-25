"""Pure approval-policy rules. No I/O, so easy to test and reason about."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import ApprovalRequest, DecisionType, Status


class PolicyError(Exception):
    """Raised when a user may not take an action. Message is shown to the user."""


@dataclass(frozen=True)
class ApproverPolicy:
    approver_ids: frozenset[str] = frozenset()
    allow_self_approval: bool = False
    # Optional dynamic source (e.g. Slack user group members). Union with approver_ids.
    approver_source: Callable[[], frozenset[str]] | None = None

    def allowed_approvers(self) -> frozenset[str]:
        extra = self.approver_source() if self.approver_source else frozenset()
        return self.approver_ids | extra

    def check_can_decide(self, request: ApprovalRequest, user_id: str) -> None:
        if request.status.is_final:
            raise PolicyError(f"This request is already {request.status.value}.")
        restricted = bool(self.approver_ids) or self.approver_source is not None
        if restricted and user_id not in self.allowed_approvers():
            raise PolicyError("You are not on the approvers list for deploys.")
        if (
            not self.allow_self_approval
            and request.requester_slack_id
            and request.requester_slack_id == user_id
        ):
            raise PolicyError("You can't approve or reject your own deploy request.")
        if any(d.approver_id == user_id for d in request.decisions):
            raise PolicyError("You've already responded to this request.")


def evaluate_status(request: ApprovalRequest) -> Status:
    """Status implied by decisions so far. Any rejection wins; else count approvals."""
    if any(d.decision is DecisionType.REJECT for d in request.decisions):
        return Status.REJECTED
    if len(request.approvals) >= request.required_approvals:
        return Status.APPROVED
    return Status.PENDING

"""Core workflow: create requests, record decisions, expire, notify.

Slack is hidden behind the Notifier protocol so the workflow is testable
without a network connection.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload, sessionmaker

from .config import Settings
from .models import ApprovalRequest, Decision, DecisionType, Status, as_utc, utcnow
from .policy import ApproverPolicy, PolicyError, evaluate_status

log = logging.getLogger(__name__)

DEPLOY_EVENTS = {"started", "succeeded", "failed"}


class Notifier(Protocol):
    def post_request(self, request: ApprovalRequest) -> tuple[str, str] | None: ...
    def update_request(self, request: ApprovalRequest) -> None: ...
    def post_in_thread(self, request: ApprovalRequest, text: str) -> None: ...
    def notify_user(self, slack_user_id: str, text: str) -> None: ...


class NotFound(Exception):
    pass


@dataclass
class NewRequest:
    service: str
    environment: str
    version: str
    requested_by: str
    requester_slack_id: str | None = None
    description: str | None = None
    run_url: str | None = None
    commit_sha: str | None = None
    timeout_minutes: int | None = None


class ApprovalService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        notifier: Notifier,
        policy: ApproverPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = session_factory
        self.notifier = notifier
        self.policy = policy or ApproverPolicy(
            approver_ids=settings.approver_ids,
            allow_self_approval=settings.allow_self_approval,
        )
        # Serialises state transitions within this process (single-instance deployment).
        self._lock = threading.Lock()

    # ---------- queries ----------

    def _load(self, session, request_id: str) -> ApprovalRequest:
        req = session.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .options(selectinload(ApprovalRequest.decisions))
        )
        if req is None:
            raise NotFound(request_id)
        return req

    def get(self, request_id: str) -> ApprovalRequest:
        with self.sessions() as session:
            return self._load(session, request_id)

    def history(self, limit: int = 10) -> list[ApprovalRequest]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(ApprovalRequest)
                    .options(selectinload(ApprovalRequest.decisions))
                    .order_by(ApprovalRequest.created_at.desc())
                    .limit(limit)
                )
            )

    # ---------- commands ----------

    def create_request(self, data: NewRequest, now: datetime | None = None) -> ApprovalRequest:
        now = now or utcnow()
        timeout = data.timeout_minutes or self.settings.default_timeout_minutes
        with self.sessions() as session:
            req = ApprovalRequest(
                service=data.service,
                environment=data.environment.lower(),
                version=data.version,
                requested_by=data.requested_by,
                requester_slack_id=data.requester_slack_id,
                description=data.description,
                run_url=data.run_url,
                commit_sha=data.commit_sha,
                required_approvals=self.settings.approvals_needed(data.environment),
                status=Status.PENDING,
                created_at=now,
                expires_at=now + timedelta(minutes=timeout),
            )
            session.add(req)
            session.commit()
            request_id = req.id

        posted = self._safe(self.notifier.post_request, self.get(request_id))
        if posted:
            channel, ts = posted
            with self.sessions() as session:
                req = self._load(session, request_id)
                req.slack_channel, req.slack_ts = channel, ts
                session.commit()
        log.info("Created approval request %s for %s@%s", request_id, data.service, data.environment)
        return self.get(request_id)

    def decide(
        self,
        request_id: str,
        user_id: str,
        decision: DecisionType,
        user_name: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Record an approve/reject. Raises PolicyError if not allowed."""
        now = now or utcnow()
        with self._lock, self.sessions() as session:
            req = self._load(session, request_id)
            if req.status is Status.PENDING and as_utc(req.expires_at) <= now:
                self._finalize(req, Status.EXPIRED, now)
                session.commit()
                expired = True
            else:
                expired = False
                self.policy.check_can_decide(req, user_id)
                req.decisions.append(
                    Decision(
                        approver_id=user_id,
                        approver_name=user_name,
                        decision=decision,
                        reason=reason,
                        created_at=now,
                    )
                )
                new_status = evaluate_status(req)
                if new_status.is_final:
                    self._finalize(req, new_status, now)
                try:
                    session.commit()
                except IntegrityError as exc:
                    session.rollback()
                    raise PolicyError("You've already responded to this request.") from exc

        req = self.get(request_id)
        if expired:
            self._announce_final(req)
            raise PolicyError("This request expired before your response was recorded.")

        verb = "approved" if decision is DecisionType.APPROVE else "rejected"
        who = f"<@{user_id}>"
        msg = f"{who} {verb}" + (f": _{reason}_" if reason else "")
        if req.status is Status.PENDING:
            msg += f" ({len(req.approvals)}/{req.required_approvals} approvals)"
        self._safe(self.notifier.update_request, req)
        self._safe(self.notifier.post_in_thread, req, msg)
        if req.status.is_final:
            self._announce_final(req)
        return req

    def cancel(self, request_id: str, now: datetime | None = None) -> ApprovalRequest:
        now = now or utcnow()
        with self._lock, self.sessions() as session:
            req = self._load(session, request_id)
            if req.status.is_final:
                return req
            self._finalize(req, Status.CANCELLED, now)
            session.commit()
        req = self.get(request_id)
        self._safe(self.notifier.update_request, req)
        self._safe(self.notifier.post_in_thread, req, "Request cancelled by the pipeline.")
        return req

    def record_deploy_event(
        self, request_id: str, event: str, details: str | None = None
    ) -> ApprovalRequest:
        if event not in DEPLOY_EVENTS:
            raise ValueError(f"event must be one of {sorted(DEPLOY_EVENTS)}")
        with self.sessions() as session:
            req = self._load(session, request_id)
            req.deploy_status = event
            session.commit()
        req = self.get(request_id)
        icon = {"started": ":rocket:", "succeeded": ":white_check_mark:", "failed": ":x:"}[event]
        text = f"{icon} Deploy *{event}* for `{req.service}` `{req.version}` → *{req.environment}*"
        if details:
            text += f"\n{details}"
        self._safe(self.notifier.update_request, req)
        self._safe(self.notifier.post_in_thread, req, text)
        if req.requester_slack_id and event in {"succeeded", "failed"}:
            self._safe(self.notifier.notify_user, req.requester_slack_id, text)
        return req

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Expire overdue requests and send halfway reminders. Run periodically."""
        now = now or utcnow()
        expired_ids: list[str] = []
        remind_ids: list[str] = []
        with self._lock, self.sessions() as session:
            pending = session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == Status.PENDING)
                .options(selectinload(ApprovalRequest.decisions))
            ).all()
            for req in pending:
                created, expires = as_utc(req.created_at), as_utc(req.expires_at)
                if expires <= now:
                    self._finalize(req, Status.EXPIRED, now)
                    expired_ids.append(req.id)
                elif not req.reminder_sent and now >= created + (expires - created) / 2:
                    req.reminder_sent = True
                    remind_ids.append(req.id)
            session.commit()

        for rid in expired_ids:
            req = self.get(rid)
            self._safe(self.notifier.update_request, req)
            self._announce_final(req)
        for rid in remind_ids:
            req = self.get(rid)
            mins = max(1, int((as_utc(req.expires_at) - now).total_seconds() // 60))
            self._safe(
                self.notifier.post_in_thread,
                req,
                f":alarm_clock: Still waiting on approval — expires in ~{mins} min "
                f"({len(req.approvals)}/{req.required_approvals} approvals).",
            )
        return {"expired": len(expired_ids), "reminded": len(remind_ids)}

    # ---------- helpers ----------

    @staticmethod
    def _finalize(req: ApprovalRequest, status: Status, now: datetime) -> None:
        req.status = status
        req.resolved_at = now

    def _announce_final(self, req: ApprovalRequest) -> None:
        text = {
            Status.APPROVED: f":white_check_mark: *Approved* — pipeline may deploy `{req.service}` `{req.version}` to *{req.environment}*.",
            Status.REJECTED: f":no_entry: *Rejected* — deploy of `{req.service}` `{req.version}` to *{req.environment}* blocked.",
            Status.EXPIRED: f":hourglass: *Expired* — no decision in time; deploy of `{req.service}` to *{req.environment}* blocked.",
            Status.CANCELLED: f"Cancelled deploy of `{req.service}` to *{req.environment}*.",
        }.get(req.status)
        if not text:
            return
        self._safe(self.notifier.post_in_thread, req, text)
        if req.requester_slack_id:
            self._safe(self.notifier.notify_user, req.requester_slack_id, text)

    @staticmethod
    def _safe(fn, *args):
        """Slack outages must never corrupt or block the approval state machine."""
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("Notifier call %s failed", getattr(fn, "__name__", fn))
            return None

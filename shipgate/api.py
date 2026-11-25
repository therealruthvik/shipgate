"""HTTP API used by CI pipelines."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .models import ApprovalRequest, as_utc
from .service import ApprovalService, NewRequest, NotFound


class CreateApproval(BaseModel):
    service: str = Field(min_length=1, max_length=200)
    environment: str = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)
    requester_slack_id: str | None = Field(default=None, pattern=r"^[UW][A-Z0-9]+$")
    description: str | None = Field(default=None, max_length=3000)
    run_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    commit_sha: str | None = Field(default=None, max_length=64)
    timeout_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


class DeployEvent(BaseModel):
    event: Literal["started", "succeeded", "failed"]
    details: str | None = Field(default=None, max_length=2000)


class DecisionOut(BaseModel):
    approver_id: str
    approver_name: str | None
    decision: str
    reason: str | None
    created_at: datetime


class ApprovalOut(BaseModel):
    id: str
    service: str
    environment: str
    version: str
    requested_by: str
    status: str
    required_approvals: int
    approvals: int
    deploy_status: str | None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    decisions: list[DecisionOut]

    @classmethod
    def from_model(cls, r: ApprovalRequest) -> "ApprovalOut":
        return cls(
            id=r.id,
            service=r.service,
            environment=r.environment,
            version=r.version,
            requested_by=r.requested_by,
            status=r.status.value,
            required_approvals=r.required_approvals,
            approvals=len(r.approvals),
            deploy_status=r.deploy_status,
            created_at=as_utc(r.created_at),
            expires_at=as_utc(r.expires_at),
            resolved_at=as_utc(r.resolved_at) if r.resolved_at else None,
            decisions=[
                DecisionOut(
                    approver_id=d.approver_id,
                    approver_name=d.approver_name,
                    decision=d.decision.value,
                    reason=d.reason,
                    created_at=as_utc(d.created_at),
                )
                for d in r.decisions
            ],
        )


def create_api(service: ApprovalService, api_token: str) -> FastAPI:
    if not api_token:
        raise ValueError("api_token is required")
    app = FastAPI(title="ShipGate", version="0.1.0")

    def require_token(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), api_token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing bearer token")

    def load(request_id: str) -> ApprovalRequest:
        try:
            return service.get(request_id)
        except NotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval request not found") from None

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post(
        "/approvals",
        status_code=status.HTTP_201_CREATED,
        response_model=ApprovalOut,
        dependencies=[Depends(require_token)],
    )
    def create(body: CreateApproval) -> ApprovalOut:
        return ApprovalOut.from_model(service.create_request(NewRequest(**body.model_dump())))

    @app.get("/approvals/{request_id}", response_model=ApprovalOut, dependencies=[Depends(require_token)])
    def get(request_id: str) -> ApprovalOut:
        return ApprovalOut.from_model(load(request_id))

    @app.post(
        "/approvals/{request_id}/cancel", response_model=ApprovalOut, dependencies=[Depends(require_token)]
    )
    def cancel(request_id: str) -> ApprovalOut:
        load(request_id)
        return ApprovalOut.from_model(service.cancel(request_id))

    @app.post(
        "/approvals/{request_id}/events", response_model=ApprovalOut, dependencies=[Depends(require_token)]
    )
    def deploy_event(request_id: str, body: DeployEvent) -> ApprovalOut:
        load(request_id)
        return ApprovalOut.from_model(service.record_deploy_event(request_id, body.event, body.details))

    return app

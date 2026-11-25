from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shipgate.config import Settings
from shipgate.models import Base
from shipgate.service import ApprovalService, NewRequest


class FakeNotifier:
    """Records outbound Slack calls instead of sending them."""

    def __init__(self) -> None:
        self.posted: list = []
        self.updated: list = []
        self.thread: list[tuple[str, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.fail = False

    def post_request(self, request):
        if self.fail:
            raise RuntimeError("slack down")
        self.posted.append(request.id)
        return ("C123", f"1700000000.{len(self.posted):06d}")

    def update_request(self, request):
        if self.fail:
            raise RuntimeError("slack down")
        self.updated.append((request.id, request.status))

    def post_in_thread(self, request, text):
        if self.fail:
            raise RuntimeError("slack down")
        self.thread.append((request.id, text))

    def notify_user(self, user_id, text):
        if self.fail:
            raise RuntimeError("slack down")
        self.dms.append((user_id, text))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_token="test-token",
        database_url="sqlite://",
        approver_ids=frozenset({"UALICE", "UBOB", "UCAROL", "UREQ"}),
        required_approvals={"production": 2, "staging": 1},
        default_timeout_minutes=30,
    )


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture(params=["sqlite", "postgres"])
def service(request, settings, notifier):
    if request.param == "sqlite":
        # In-memory SQLite shared across threads/sessions.
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    else:
        url = os.environ.get("SHIPGATE_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("SHIPGATE_TEST_POSTGRES_URL not set")
        engine = create_engine(url)
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield ApprovalService(settings, sessionmaker(engine, expire_on_commit=False), notifier)
    engine.dispose()


@pytest.fixture
def make_request(service):
    def _make(environment="production", requester_slack_id="UREQ", **kw):
        return service.create_request(
            NewRequest(
                service=kw.pop("service_name", "payments-api"),
                environment=environment,
                version=kw.pop("version", "v1.4.2"),
                requested_by="octocat",
                requester_slack_id=requester_slack_id,
                **kw,
            )
        )

    return _make

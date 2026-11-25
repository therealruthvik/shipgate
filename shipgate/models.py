"""Database models and session setup."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Status(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self is not Status.PENDING


class DecisionType(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class Base(DeclarativeBase):
    pass


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    service: Mapped[str] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(200))
    requester_slack_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_approvals: Mapped[int] = mapped_column(Integer)
    status: Mapped[Status] = mapped_column(Enum(Status), default=Status.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_sent: Mapped[bool] = mapped_column(default=False)
    deploy_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slack_channel: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)

    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="Decision.created_at"
    )

    @property
    def approvals(self) -> list["Decision"]:
        return [d for d in self.decisions if d.decision is DecisionType.APPROVE]

    @property
    def rejections(self) -> list["Decision"]:
        return [d for d in self.decisions if d.decision is DecisionType.REJECT]


class Decision(Base):
    __tablename__ = "decisions"
    # One decision per approver per request: blocks double-click / double-count races.
    __table_args__ = (UniqueConstraint("request_id", "approver_id", name="uq_one_decision_per_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"))
    approver_id: Mapped[str] = mapped_column(String(50))
    approver_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision: Mapped[DecisionType] = mapped_column(Enum(DecisionType))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    request: Mapped[ApprovalRequest] = relationship(back_populates="decisions")


def make_session_factory(database_url: str) -> sessionmaker:
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        # Bolt handlers, API and sweeper run on different threads.
        connect_args["check_same_thread"] = False
    elif "+psycopg" in database_url:
        # Safe behind transaction-mode poolers (Neon, PgBouncer): no server-side prepared statements.
        connect_args["prepare_threshold"] = None
    # pool_pre_ping: serverless Postgres (Neon) drops idle connections; reconnect transparently.
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)

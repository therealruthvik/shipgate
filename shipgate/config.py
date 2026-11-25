"""Settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_required_approvals(raw: str) -> dict[str, int]:
    """Parse "production=2,staging=1" into {"production": 2, "staging": 1}."""
    result: dict[str, int] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        env, _, count = part.partition("=")
        if not count:
            raise ValueError(f"Invalid SHIPGATE_REQUIRED_APPROVALS entry: {part!r}")
        n = int(count)
        if n < 1:
            raise ValueError(f"Required approvals must be >= 1, got {n} for {env!r}")
        result[env.strip().lower()] = n
    return result


def _normalize_db_url(url: str) -> str:
    """Accept postgres://... / postgresql://... (Render, Fly, Heroku style) and use psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _parse_list(raw: str) -> frozenset[str]:
    return frozenset(filter(None, (p.strip() for p in raw.split(","))))


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    channel_id: str = ""
    api_token: str = ""
    database_url: str = "sqlite:///shipgate.db"
    # Slack user IDs allowed to approve. Empty + no usergroup = anyone in channel.
    approver_ids: frozenset[str] = frozenset()
    # Optional Slack user group ID (e.g. S0123ABC) whose members can approve.
    approver_usergroup: str = ""
    required_approvals: dict[str, int] = field(default_factory=dict)
    default_required_approvals: int = 1
    default_timeout_minutes: int = 30
    allow_self_approval: bool = False
    sweep_interval_seconds: int = 30
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    def approvals_needed(self, environment: str) -> int:
        return self.required_approvals.get(environment.lower(), self.default_required_approvals)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=env.get("SLACK_APP_TOKEN", ""),
            channel_id=env.get("SHIPGATE_CHANNEL_ID", ""),
            api_token=env.get("SHIPGATE_API_TOKEN", ""),
            database_url=_normalize_db_url(
                env.get("DATABASE_URL") or env.get("SHIPGATE_DATABASE_URL", "sqlite:///shipgate.db")
            ),
            approver_ids=_parse_list(env.get("SHIPGATE_APPROVER_IDS", "")),
            approver_usergroup=env.get("SHIPGATE_APPROVER_USERGROUP", ""),
            required_approvals=_parse_required_approvals(
                env.get("SHIPGATE_REQUIRED_APPROVALS", "production=2,staging=1")
            ),
            default_required_approvals=int(env.get("SHIPGATE_DEFAULT_REQUIRED_APPROVALS", "1")),
            default_timeout_minutes=int(env.get("SHIPGATE_TIMEOUT_MINUTES", "30")),
            allow_self_approval=env.get("SHIPGATE_ALLOW_SELF_APPROVAL", "false").lower() == "true",
            sweep_interval_seconds=int(env.get("SHIPGATE_SWEEP_INTERVAL_SECONDS", "30")),
            api_host=env.get("SHIPGATE_API_HOST", "0.0.0.0"),
            api_port=int(env.get("PORT", env.get("SHIPGATE_API_PORT", "8080"))),
        )

    def validate_for_runtime(self) -> None:
        missing = [
            name
            for name, value in [
                ("SLACK_BOT_TOKEN", self.slack_bot_token),
                ("SLACK_APP_TOKEN", self.slack_app_token),
                ("SHIPGATE_CHANNEL_ID", self.channel_id),
                ("SHIPGATE_API_TOKEN", self.api_token),
            ]
            if not value
        ]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

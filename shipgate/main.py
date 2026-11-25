"""Entrypoint: Slack Socket Mode + HTTP API + expiry sweeper in one process."""

from __future__ import annotations

import logging
import threading

import uvicorn
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .api import create_api
from .config import Settings
from .models import make_session_factory
from .policy import ApproverPolicy
from .service import ApprovalService
from .slack_app import SlackNotifier, UserGroupApprovers, register_handlers

log = logging.getLogger("shipgate")


def build(settings: Settings):
    bolt = App(token=settings.slack_bot_token)
    notifier = SlackNotifier(bolt.client, settings.channel_id)
    policy = ApproverPolicy(
        approver_ids=settings.approver_ids,
        allow_self_approval=settings.allow_self_approval,
        approver_source=(
            UserGroupApprovers(bolt.client, settings.approver_usergroup)
            if settings.approver_usergroup
            else None
        ),
    )
    service = ApprovalService(
        settings, make_session_factory(settings.database_url), notifier, policy
    )
    register_handlers(bolt, service)
    return bolt, service, create_api(service, settings.api_token)


def run_sweeper(service: ApprovalService, interval: int, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            result = service.sweep()
            if any(result.values()):
                log.info("Sweep: %s", result)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Sweep failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    settings.validate_for_runtime()
    if not settings.approver_ids and not settings.approver_usergroup:
        log.warning("No approvers configured: anyone in the channel can approve deploys.")

    bolt, service, api = build(settings)

    stop = threading.Event()
    threading.Thread(
        target=run_sweeper, args=(service, settings.sweep_interval_seconds, stop), daemon=True
    ).start()

    socket = SocketModeHandler(bolt, settings.slack_app_token)
    socket.connect()  # non-blocking; runs in background threads
    log.info("Slack Socket Mode connected; API on %s:%s", settings.api_host, settings.api_port)
    try:
        uvicorn.run(api, host=settings.api_host, port=settings.api_port, log_level="info")
    finally:
        stop.set()
        socket.close()


if __name__ == "__main__":
    main()

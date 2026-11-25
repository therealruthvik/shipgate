#!/usr/bin/env python3
"""CI client for ShipGate. Standard library only, so it runs on any runner.

Usage:
  shipgate_gate.py request --service S --environment E --version V [--timeout-minutes N]
      Creates an approval request and blocks until approved (exit 0),
      or rejected / expired / cancelled (exit 1).
  shipgate_gate.py event --id ID --event started|succeeded|failed [--details TEXT]

Env: SHIPGATE_URL, SHIPGATE_API_TOKEN. GitHub context (GITHUB_*) is picked up automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

FINAL = {"approved", "rejected", "expired", "cancelled"}


def api(method: str, path: str, body: dict | None = None, retries: int = 3) -> dict:
    base = os.environ["SHIPGATE_URL"].rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['SHIPGATE_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # 4xx = our request is wrong; retrying won't help.
            if exc.code < 500:
                sys.exit(f"ShipGate API error {exc.code}: {exc.read().decode(errors='replace')}")
            if attempt == retries:
                raise Unavailable(f"HTTP {exc.code}") from exc
        except OSError as exc:  # URLError, TimeoutError, ConnectionResetError, ...
            if attempt == retries:
                raise Unavailable(str(getattr(exc, "reason", exc))) from exc
        time.sleep(2 * attempt)
    raise AssertionError("unreachable")


class Unavailable(Exception):
    """ShipGate could not be reached after retries (network error, timeout, 5xx)."""


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{name}={value}\n")


def cmd_request(args: argparse.Namespace) -> int:
    gh = os.environ
    run_url = None
    if gh.get("GITHUB_SERVER_URL") and gh.get("GITHUB_REPOSITORY") and gh.get("GITHUB_RUN_ID"):
        run_url = f"{gh['GITHUB_SERVER_URL']}/{gh['GITHUB_REPOSITORY']}/actions/runs/{gh['GITHUB_RUN_ID']}"
    body = {
        "service": args.service,
        "environment": args.environment,
        "version": args.version,
        "requested_by": args.requested_by or gh.get("GITHUB_ACTOR", "unknown"),
        "requester_slack_id": args.requester_slack_id or None,
        "description": args.description or None,
        "run_url": run_url,
        "commit_sha": gh.get("GITHUB_SHA"),
        "timeout_minutes": args.timeout_minutes,
    }
    created = api("POST", "/approvals", {k: v for k, v in body.items() if v is not None})
    rid = created["id"]
    set_output("request-id", rid)
    print(f"Approval request {rid} created; waiting for approval in Slack...", flush=True)

    # If the workflow run is cancelled, withdraw the request so nobody approves a dead run.
    def on_term(signum, _frame):
        print("Cancelled; withdrawing approval request.", flush=True)
        try:
            api("POST", f"/approvals/{rid}/cancel", retries=1)
        except Unavailable:
            print("Could not withdraw request; it will expire on its own.", flush=True)
        sys.exit(130)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    # Ride out short ShipGate outages (restarts, deploys, network blips) while waiting;
    # fail closed (block the deploy) if it stays unreachable too long.
    status, last, down_since = created["status"], None, None
    while status not in FINAL:
        time.sleep(args.poll_seconds)
        try:
            current = api("GET", f"/approvals/{rid}")
        except Unavailable as exc:
            down_since = down_since or time.monotonic()
            down_for = time.monotonic() - down_since
            print(f"  ShipGate unreachable ({exc}); retrying ({int(down_for)}s)", flush=True)
            if down_for > args.max_outage_seconds:
                print("ShipGate unreachable for too long; blocking deploy.", file=sys.stderr)
                set_output("status", "unreachable")
                return 1
            continue
        down_since = None
        status = current["status"]
        progress = f"{current['approvals']}/{current['required_approvals']}"
        if progress != last:
            print(f"  status={status} approvals={progress}", flush=True)
            last = progress

    set_output("status", status)
    if status == "approved":
        print("Approved. Proceeding with deploy.")
        return 0
    print(f"Deploy blocked: request {status}.", file=sys.stderr)
    return 1


def cmd_event(args: argparse.Namespace) -> int:
    body = {"event": args.event}
    if args.details:
        body["details"] = args.details
    api("POST", f"/approvals/{args.id}/events", body)
    print(f"Reported deploy {args.event} for {args.id}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("request")
    r.add_argument("--service", required=True)
    r.add_argument("--environment", required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--requested-by")
    r.add_argument("--requester-slack-id")
    r.add_argument("--description")
    r.add_argument("--timeout-minutes", type=int)
    r.add_argument("--poll-seconds", type=int, default=10)
    r.add_argument("--max-outage-seconds", type=int, default=300)
    r.set_defaults(fn=cmd_request)

    e = sub.add_parser("event")
    e.add_argument("--id", required=True)
    e.add_argument("--event", required=True, choices=["started", "succeeded", "failed"])
    e.add_argument("--details")
    e.set_defaults(fn=cmd_event)

    args = p.parse_args()
    try:
        return args.fn(args)
    except Unavailable as exc:
        print(f"ShipGate unreachable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

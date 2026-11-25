# ShipGate

**Slack-based deploy approvals for CI/CD pipelines.** A pipeline pauses before deploying, ShipGate posts an approval request to Slack, approvers click **Approve** / **Reject**, and the pipeline continues or stops. Every decision is stored for audit.

```
GitHub Actions ──POST /approvals──► ShipGate (FastAPI + Slack Bolt, Socket Mode)
      ▲                                  │ posts Block Kit message w/ buttons
      │ polls GET /approvals/{id}        ▼
      └──── approved / rejected ◄── #deploys  [Approve] [Reject → reason modal]
                                         │
                       Postgres ◄────────┘  audit log: who, what, when, why
```

## Features

- **Multi-approver policy per environment**: e.g. `production=2,staging=1`. Any single rejection blocks.
- **Guardrails**: an approver allow-list (user IDs and/or a Slack **user group**), no self-approval, one decision per person (enforced by a DB unique constraint, so it's race-safe).
- **Reject with reason**: a modal collects the reason, which is shown in the thread and in the audit log.
- **Timeouts and reminders**: a reminder goes to the thread halfway through, and the request auto-expires, which fails the pipeline.
- **Deploy lifecycle notifications**: `started` / `succeeded` / `failed` are posted to the same thread, and the requester gets a DM.
- **Pipeline cancellation**: if the workflow run is cancelled, the request is withdrawn.
- **Slack outages fail safe**: notification errors are logged and never corrupt approval state.
- **`/shipgate history`**, **`/shipgate status <id>`**: audit trail inside Slack.
- **Reusable GitHub Action** (`uses: <owner>/shipgate@v1`) with a zero-dependency client.

## Quick start

### 1. Create the Slack app
1. https://api.slack.com/apps → **Create New App** → **From a manifest** → paste `slack-manifest.yml`.
2. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`).
3. **Basic Information → App-Level Tokens** → generate one with scope `connections:write` (`xapp-…`).
4. Create a `#deploys` channel, run `/invite @ShipGate`, and copy the channel ID.

### 2. Run it
```bash
cp .env.example .env          # fill in tokens, channel ID, approvers
docker compose up --build     # ShipGate + Postgres on :8080
```
Or without Docker:
```bash
uv venv && uv pip install -e ".[dev]"
set -a; source .env; set +a
.venv/bin/shipgate
```

> If a value contains `&` (e.g. a Neon URL with `?sslmode=require&channel_binding=require`), wrap it in single quotes before `source`-ing `.env`.

### Deploy to Fly.io
ShipGate keeps a Slack websocket open and runs the expiry sweeper in-process, so it runs as **one always-on machine** (`fly.toml` disables auto-stop).
```bash
fly apps create shipgate-<suffix>          # then set `app` in fly.toml
fly secrets set SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... SHIPGATE_CHANNEL_ID=... \
  SHIPGATE_API_TOKEN=... SHIPGATE_APPROVER_IDS=... DATABASE_URL='postgresql://...'   # e.g. Neon
fly deploy --ha=false
curl https://shipgate-<suffix>.fly.dev/healthz
```
Only one ShipGate instance should be connected to Slack at a time. Stop local copies after deploying.

### 3. Try it without CI
```bash
python3 scripts/shipgate_gate.py request --service demo-api --environment production --version v1.2.3
# (SHIPGATE_URL=http://localhost:8080 and SHIPGATE_API_TOKEN must be set)
```
Approve in Slack and watch the command exit 0.

### 4. Wire up GitHub Actions
GitHub runners must be able to reach ShipGate over HTTPS. Deploy it (Render / Fly.io / any VM with Docker), or for a demo expose your laptop with `cloudflared tunnel --url http://localhost:8080` or `ngrok http 8080`.

Repo **secrets**: `SHIPGATE_URL`, `SHIPGATE_API_TOKEN`.
Optional repo **variable** `SLACK_ID_MAP`: `{"github-login": "U0123ABC"}` enables requester DMs and the self-approval check.

```yaml
- id: gate
  uses: <owner>/shipgate@v1
  with:
    shipgate-url: ${{ secrets.SHIPGATE_URL }}
    api-token: ${{ secrets.SHIPGATE_API_TOKEN }}
    service: payments-api
    environment: production
    version: ${{ github.sha }}
```
See `.github/workflows/deploy-demo.yml` for the full build → approve → deploy → report flow.

## API

All endpoints except `/healthz` require `Authorization: Bearer $SHIPGATE_API_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/approvals` | Create a request (`service`, `environment`, `version`, `requested_by`, optional `requester_slack_id`, `description`, `run_url`, `commit_sha`, `timeout_minutes`) |
| `GET` | `/approvals/{id}` | Status + decisions |
| `POST` | `/approvals/{id}/cancel` | Withdraw a pending request |
| `POST` | `/approvals/{id}/events` | `{"event": "started"\|"succeeded"\|"failed", "details": "..."}` |
| `GET` | `/healthz` | Liveness |

Interactive docs at `/docs`.

## Configuration

| Variable | Default | |
|---|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | required | Slack credentials |
| `SHIPGATE_CHANNEL_ID` | required | Channel for requests |
| `SHIPGATE_API_TOKEN` | required | CI bearer token |
| `DATABASE_URL` | `sqlite:///shipgate.db` | `postgresql://…` supported |
| `SHIPGATE_APPROVER_IDS` | empty | Comma-separated Slack user IDs |
| `SHIPGATE_APPROVER_USERGROUP` | empty | Slack user group ID |
| `SHIPGATE_REQUIRED_APPROVALS` | `production=2,staging=1` | Per-environment approvals needed |
| `SHIPGATE_DEFAULT_REQUIRED_APPROVALS` | `1` | For unlisted environments |
| `SHIPGATE_TIMEOUT_MINUTES` | `30` | Default expiry |
| `SHIPGATE_ALLOW_SELF_APPROVAL` | `false` | |

## Design notes

- **`service.py`** holds the state machine (`pending → approved | rejected | expired | cancelled`). Slack sits behind a `Notifier` protocol, so the workflow is fully testable offline.
- **`policy.py`** is pure functions: who may decide, and what status the decisions imply.
- **Socket Mode** means no public endpoint is needed for Slack interactivity; only the CI API needs to be reachable.
- State transitions are serialised in-process and backed by a DB unique constraint. Run one replica; scaling out would need row locks (`SELECT … FOR UPDATE`) and a leader for the sweeper.

## Tests

```bash
.venv/bin/pytest -q                                   # SQLite
SHIPGATE_TEST_POSTGRES_URL=postgresql+psycopg://user@localhost/shipgate_test .venv/bin/pytest -q   # + Postgres
```
The tests cover policy rules, expiry and reminders, the API with auth and validation, the Block Kit output, and Bolt handlers driven with real Slack payload shapes.

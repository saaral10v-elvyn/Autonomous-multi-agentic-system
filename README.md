# Forge

Forge is a SaaS where AI agents take a user's idea to a live deployed web app with configurable human-in-the-loop. This repository currently implements M1 through the M3 Clarifier/Planner workflow: account access, encrypted provider credentials, run events, clarification questions, and plan review.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 20 and pnpm
- Docker Compose

## Start infrastructure

Copy `.env.example` to `.env`, then run:

```sh
docker compose -f infra/docker-compose.yml up -d
```

Before starting the API, generate a 32-byte AES-256 master key and paste the output into `MASTER_KEY` in `.env`:

```sh
python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Keep this key outside version control. It encrypts provider credentials and signs session cookies; losing it makes stored provider keys unreadable.

## API

```sh
cd apps/api
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

M1 authentication routes are under `/api/v1`; mutating requests must have the configured `WEB_ORIGIN` as their `Origin` header. Browser requests from the web app set this automatically.
Session cookies contain a signed per-user version; logout increments the stored version so previously issued cookies are rejected on subsequent requests.

Health check: `http://localhost:8000/healthz`.

## Worker

```sh
cd apps/api
uv run arq app.worker.WorkerSettings
```

The worker includes the M3 `advance_run` job, which advances a run through clarification and planning until it needs user input or plan approval. `noop_job` remains available as a queue smoke job.

## M3 idea-to-plan flow and smoke test

Create a run with an idea, autonomy level, and budget. The Clarifier asks a structured batch of questions; after answers arrive, the Planner creates a brief, tech stack, milestones, and dependency graph. Users can edit and approve the plan. Autopilot records plan auto-approval and moves to `EXECUTING`. Each stage is persisted as run events. M3 ends at `EXECUTING`; building, deploying, and reporting are later milestones.

The local `scripts/smoke_m3.py` smoke test launches the API, Arq worker, and web app, then checks the full clarification and plan approval flow, autopilot approval, missing-provider-key failure, and worker restart during planning. It requires Python dependencies, web dependencies, a root `.env` with `MASTER_KEY`, and an empty isolated PostgreSQL database plus Redis. In PowerShell, use separate ports so the M3 smoke stack does not collide with another project:

```powershell
$env:FORGE_SMOKE_POSTGRES_PORT = "15432"
$env:FORGE_SMOKE_REDIS_PORT = "16379"
$env:DATABASE_URL = "postgresql+asyncpg://forge:forge@127.0.0.1:15432/forge"
$env:REDIS_URL = "redis://127.0.0.1:16379/0"
$env:FORGE_SMOKE_API_PORT = "18000"
$env:FORGE_SMOKE_WEB_PORT = "13000"
$env:WEB_ORIGIN = "http://localhost:13000"
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:18000"
docker run --rm -d --name forge-m3-postgres -e POSTGRES_DB=forge -e POSTGRES_USER=forge -e POSTGRES_PASSWORD=forge -p 15432:5432 postgres:16
docker run --rm -d --name forge-m3-redis -p 16379:6379 redis:7
uv run --directory apps/api alembic upgrade head
uv run --directory apps/api python ..\..\scripts\smoke_m3.py
docker stop forge-m3-postgres forge-m3-redis
```

The smoke script enables `FAKE_PROVIDER_ENABLED` only in the child processes it launches; it is off by default. `Settings` rejects the flag when `APP_ENV=production`, and the development fake key resolver returns no credential in production or while the flag is off. The fake provider returns deterministic Clarifier and Planner responses and makes no external LLM calls.

## API tests, lint, types, and migrations

```sh
cd apps/api
uv run pytest
uv run ruff check .
uv run mypy app
uv run alembic upgrade head
```

When adding SQLAlchemy models, generate and apply a migration with:

```sh
uv run alembic revision --autogenerate -m "msg" && uv run alembic upgrade head
```

## Web

```sh
cd apps/web
pnpm install
pnpm dev
```

The page checks the API at `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`).

```sh
pnpm test
pnpm lint
pnpm typecheck
```

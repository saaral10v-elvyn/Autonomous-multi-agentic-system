# Forge

Forge is a SaaS where AI agents take a user's idea to a live deployed web app with configurable human-in-the-loop. This repository currently contains the M0 scaffold.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 20 and pnpm
- Docker Compose

## Start infrastructure

Copy `.env.example` to `.env`, then run:

```sh
docker compose -f infra/docker-compose.yml up -d
```

## API

```sh
cd apps/api
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

Health check: `http://localhost:8000/healthz`.

## Worker

```sh
cd apps/api
uv run arq app.worker.WorkerSettings
```

The worker includes `noop_job` as a queue smoke job.

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

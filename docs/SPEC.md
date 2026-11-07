# Forge — Product & Architecture Spec (v1 MVP)

> "Forge" is a placeholder codename. Rename freely.

## 1. Vision

A SaaS where a user shares an idea and a team of dynamically spawned AI agents takes it end to end: clarifying questions, plan, build, test, deploy a live web app, and deliver a final report. The user stays in the loop at a level they configure.

## 2. MVP Scope

### In scope (v1)
- Flagship flow: idea -> clarifying questions -> plan -> build -> test -> deploy live web app -> final report.
- Research happens as a sub-agent inside the flow (market/tech/library research that informs plan and build).
- User-configurable autonomy levels (3 presets).
- Live human interaction during a run: answer questions, approve/reject actions, edit plan, interrupt, redirect, message a specific sub-agent, pause/resume/cancel.
- Dynamically spawned sub-agents per task.
- Multi-provider LLMs via BYOK (Claude, OpenAI, Gemini) behind a provider abstraction.
- Deploy target: user's own free-tier hosting account (Vercel first, then Netlify/Cloudflare Pages/Render). Code lives in the user's GitHub.
- Per-run Docker sandboxes for executing code and tests.
- Encrypted credential storage and per-user data isolation from day one.
- Cost/usage tracking per run with a user-set budget cap.

### Out of scope (v2+)
- Standalone research mode and standalone workflow-automation mode (Zapier/n8n-style).
- Pay-as-you-go credits and subscription billing (design tables now, build later).
- SOC 2 / GDPR programs.
- Mobile apps, team/org workspaces, marketplace of agents.
- Hosting user apps on our own infrastructure.
- MCP-based integration marketplace (plan the interface now, implement later).

## 3. User Flow

1. User signs up, adds at least one LLM provider key, connects GitHub and one hosting provider.
2. User creates a Run: enters the idea, picks autonomy level, sets budget cap.
3. Clarifier agent asks questions (batched, structured, skippable with "decide for me").
4. User answers. Planner agent produces a plan: product brief, tech stack, milestones, task graph.
5. User reviews/edits/approves the plan (required at Step-by-step and Milestones levels; auto at Autopilot).
6. Orchestrator executes the task graph, spawning sub-agents (Researcher, Architect, Coder, Tester, Reviewer, Deployer) as needed.
7. Approval gates fire according to autonomy level; irreversible actions always gate.
8. Deployer ships the app; Verifier checks the live URL (HTTP 200, smoke test).
9. Reporter produces the final report (Markdown) with links, decisions, cost, test results, known issues, next steps.
10. User can start a follow-up Run on the same Project ("iterate").

## 4. Autonomy Levels & Approval Policy

Every tool/action carries a `risk` class:

| Risk | Examples |
|---|---|
| `read` | read files, web search, fetch pages, list repo |
| `sandbox_write` | write/run code inside the run's sandbox |
| `reversible_external` | commit to a feature branch, open a PR, create a draft |
| `irreversible` | create repo, push to default branch, deploy, spend money, send email/message, delete anything, use stored credentials for a new service, change DNS/env secrets |

Policy matrix (`A` = requires approval, `-` = auto):

| Gate | Step-by-step | Milestones | Autopilot |
|---|---|---|---|
| Clarifying questions answered | A | A | A |
| Plan approval | A | A | - (shown, auto-approved) |
| Each milestone completion | A | A | - |
| `read` | - | - | - |
| `sandbox_write` | A | - | - |
| `reversible_external` | A | - | - |
| `irreversible` | A | A | A |
| Deploy | A | A | A |

Rules:
- `irreversible` ALWAYS requires approval at every level. This is enforced in the gate engine, not in prompts.
- Approval requests include: action, exact payload, risk class, which agent, why, estimated cost, and rollback info.
- Approval states: `pending`, `approved`, `rejected`, `edited` (user modifies payload), `expired`.
- Rejection returns structured feedback to the requesting agent so it can replan.
- Approvals persist; a run waiting on approval consumes no compute.

## 5. Architecture

```
Next.js Web  <-- REST + WebSocket -->  FastAPI API
                                         |
                    +--------------------+--------------------+
                    |                    |                    |
                 Postgres             Redis              Object store
              (state, events)     (queue, pub/sub)     (artifacts, logs)
                    |                    |
                    +-------- Arq Workers (agent runtime) --------+
                                   |            |            |
                              LLM Gateway   Sandbox Mgr   Integrations
                              (LiteLLM)    (Docker/E2B)  (GitHub, Vercel,...)
```

Components:
- **API (FastAPI):** auth, CRUD, WebSocket hub, approval endpoints. Stateless.
- **Workers (Arq):** execute run steps. Durable: every agent step is persisted; a crashed worker's run resumes from the last committed step.
- **Orchestrator:** per-run state machine + task-graph scheduler.
- **Agent Runtime:** generic agent loop (LLM call -> tool calls -> gate check -> execute -> persist), parameterized by role template.
- **LLM Gateway:** provider abstraction, BYOK key resolution, retries, token/cost accounting.
- **Gate Engine:** pure function `(autonomy_level, tool.risk, context) -> auto | require_approval`.
- **Sandbox Manager:** one Docker container per run (resource-limited, network allowlist), workspace volume, exec/read/write/stream-logs API.
- **Integration Layer:** typed adapters (GitHub, Vercel, Netlify, Cloudflare Pages, Render). Credentials injected at the tool layer; never placed in model context.
- **Event Bus:** every state change appends to `events` (ordered by `seq`) and publishes to Redis; WebSocket replays from `seq` on reconnect.

## 6. Run State Machine

```
CREATED -> CLARIFYING -> AWAITING_ANSWERS -> PLANNING -> AWAITING_PLAN_APPROVAL
 -> EXECUTING <-> AWAITING_APPROVAL
 -> VERIFYING -> DEPLOYING -> REPORTING -> COMPLETED
Any state -> PAUSED (user) | FAILED | CANCELLED
PAUSED -> resumes to previous state
```

Transitions are validated in one place (`orchestrator/state.py`) and each emits an event.

## 7. Agent System

### 7.1 Role templates (registry; not a fixed roster)
Each template: `name`, `system_prompt`, `default_model_tier` (`fast|balanced|strong`), `tool_allowlist`, `output_schema`, `max_steps`, `max_cost_usd`.

| Role | Purpose | Key tools |
|---|---|---|
| Clarifier | Produce structured question set | `ask_user` |
| Planner | Product brief, stack, milestones, task graph | `read`, `web_search` |
| Researcher | Gather and summarize external info | `web_search`, `fetch_url` |
| Architect | Concrete file/module design | `read_file`, `write_file` |
| Coder | Implement tasks | `sandbox_*`, `git_*` |
| Tester | Write/run tests, report failures | `sandbox_exec` |
| Reviewer | Review diff against plan and quality bar | `read_file`, `git_diff` |
| Deployer | Push code, trigger deploy, verify URL | `github_*`, `deploy_*`, `http_check` |
| Reporter | Final report | `read`, `write_report` |

### 7.2 Dynamic spawning
- Orchestrator (and Planner via task graph) spawns agents with `spawn_agent(role, task, context_refs, parent_id)`.
- Any agent with the `spawn_agent` tool may spawn children, bounded by: max depth (3), max concurrent agents per run (4), per-agent and per-run budgets.
- Planner may emit ad-hoc roles (custom system prompt) validated against a schema; ad-hoc roles inherit only a safe subset of tools.
- Children return structured results to the parent via `TaskResult` (status, summary, artifacts, follow-ups).

### 7.3 Agent loop (per agent)
1. Build context (task, relevant memory, prior steps, user messages addressed to this agent).
2. LLM call via gateway (tool calling).
3. For each tool call: lookup tool -> gate engine -> (auto | create approval and suspend) -> execute -> persist result.
4. Stop on `finish` tool, max steps, budget exhausted, cancel, or error escalation.
5. Every step writes `agent_steps` row + events before the next step begins (durability).

### 7.4 Human interaction mid-run
- `user.message` may target the run (orchestrator) or a specific `agent_id`; it is injected into that agent's next context turn.
- `plan.edit` modifies the task graph; orchestrator reconciles (cancel removed tasks, schedule new ones).
- `run.pause` stops scheduling new steps after in-flight steps commit.
- Interrupt: cancels in-flight LLM call and sandbox exec for a target agent, marks task `interrupted`, awaits instruction.

### 7.5 Context & memory
- Run-level shared memory: `project_brief`, `decisions`, `plan`, `artifact index`. Agents read via `memory_read`, write via `memory_write` (append-only log + current snapshot).
- Agents get summaries, not full transcripts of siblings. Long histories are compacted per agent.

## 8. Data Model (Postgres)

- `users` (id, email, password_hash/oauth_id, created_at)
- `provider_keys` (id, user_id, provider, key_ciphertext, key_nonce, label, last4, created_at)
- `integrations` (id, user_id, kind, token_ciphertext, token_nonce, scopes, account_label, created_at)
- `projects` (id, user_id, name, repo_url, live_url, created_at)
- `runs` (id, project_id, user_id, idea, autonomy_level, status, budget_usd, spent_usd, model_prefs jsonb, created_at, completed_at)
- `agents` (id, run_id, parent_id, role, config jsonb, status, model, spent_usd, created_at)
- `tasks` (id, run_id, agent_id, parent_task_id, title, description, status, depends_on uuid[], input jsonb, output jsonb)
- `agent_steps` (id, agent_id, idx, kind[llm|tool|user_msg], payload jsonb, tokens_in, tokens_out, cost_usd, created_at)
- `messages` (id, run_id, agent_id nullable, sender[user|agent|system], content, created_at)
- `questions` (id, run_id, schema jsonb, answers jsonb, status)
- `approvals` (id, run_id, agent_id, action_type, risk, payload jsonb, reason, status, edited_payload jsonb, decided_at, decided_by)
- `events` (id, run_id, seq bigint, type, payload jsonb, created_at) — unique (run_id, seq)
- `artifacts` (id, run_id, kind[plan|code|report|log|url], name, uri, meta jsonb)
- `sandboxes` (id, run_id, container_id, status, created_at, destroyed_at)
- `usage_records` (id, user_id, run_id, provider, model, tokens_in, tokens_out, cost_usd, created_at)
- Future (create empty): `plans`, `credit_ledger`, `subscriptions`

All user-owned rows are scoped by `user_id`; every query goes through a repository layer that requires `user_id`.

## 9. API

### REST (prefix `/api/v1`)
- `POST /auth/signup|login|logout`, `GET /me`
- `GET|POST|DELETE /provider-keys` (write-only: never return plaintext; `POST /provider-keys/{id}/test`)
- `GET /integrations`, `POST /integrations/{kind}/connect` (OAuth or token), `DELETE /integrations/{id}`
- `POST /runs` (idea, autonomy_level, budget_usd, model_prefs), `GET /runs`, `GET /runs/{id}`
- `POST /runs/{id}/answers`, `POST /runs/{id}/messages`, `PUT /runs/{id}/plan`
- `POST /runs/{id}/pause|resume|cancel`
- `GET /runs/{id}/approvals`, `POST /approvals/{id}/decide` (approve|reject|edit)
- `GET /runs/{id}/artifacts`, `GET /runs/{id}/report`
- `GET /runs/{id}/events?after_seq=`

### WebSocket `/ws/runs/{id}?after_seq=N`
Server -> client events: `run.state_changed`, `agent.spawned`, `agent.status`, `agent.message`, `agent.tool_call`, `agent.tool_result`, `task.updated`, `question.asked`, `approval.requested`, `approval.resolved`, `cost.updated`, `artifact.created`, `sandbox.log`, `run.completed`, `run.failed`.
Client -> server: `answers.submit`, `approval.decide`, `user.message {agent_id?}`, `plan.edit`, `run.pause|resume|cancel`, `agent.interrupt {agent_id}`.

## 10. LLM Gateway (BYOK)

- Wrap LiteLLM behind `LLMClient.complete(messages, tools, model_tier, run_ctx)`.
- Model tiers map to concrete models per provider in config (`fast|balanced|strong`); user can override per role.
- Resolve keys per user at call time; keys decrypted in memory only for the call.
- Normalize tool-calling, streaming, errors, and rate-limit retries (exponential backoff, provider fallback optional).
- Record tokens and cost per call into `agent_steps` and `usage_records`; enforce run budget before each call (hard stop at cap, soft warning at 80%).
- Platform-paid mode (v2): same interface, key resolved from platform account, cost multiplied and debited from `credit_ledger`.

## 11. Sandbox

- v1: Docker on a single VPS; one container per run, image with Node, Python, git, common build tools.
- Limits: CPU, memory, disk quota, wall-clock per command, max processes. Non-root user. No host mounts besides the run workspace volume.
- Network: default deny; allowlist for package registries (npm, PyPI), GitHub, and explicitly approved domains.
- Interface: `exec(cmd, cwd, timeout)`, `read_file`, `write_file`, `list_dir`, `stream_logs`, `snapshot`, `destroy`.
- Idle timeout and hard TTL; cleanup job destroys orphaned sandboxes.
- Abstract behind `SandboxProvider` so E2B can be swapped in.

## 12. Integrations (free-first)

v1: GitHub (repo create, push, PR), Vercel (project create, deploy, status), Netlify/Cloudflare Pages/Render (adapters after Vercel works), generic `web_search` + `fetch_url`.
- Auth: OAuth where available, else scoped personal tokens.
- Each adapter implements `Integration` protocol: `validate()`, `tools()` (typed tool definitions with risk classes), `describe_scopes()`.
- Default app templates: Next.js (App Router) and Vite+React static, with optional serverless functions; generated apps must build with zero paid services.
- v2: MCP client so any MCP server's tools register into the tool registry with a user-assigned risk class (default `irreversible` until classified).

## 13. Security

- Secrets: AES-256-GCM envelope encryption; master key from environment/secret manager; per-record nonce; never log or return plaintext; never put secrets in model context (tool layer injects them).
- Tenant isolation: repository layer enforces `user_id`; per-user sandboxes; tests assert cross-tenant access fails.
- Prompt injection: tool outputs and fetched web content are marked untrusted; gate engine decisions never depend on model text; irreversible actions require human approval regardless of model reasoning.
- Rate limits: per-user API limits, per-run concurrency, per-user concurrent runs (default 2).
- Sandbox escape mitigations: unprivileged containers, seccomp default profile, no Docker socket inside.
- Audit log: all approvals, credential uses, and deploys recorded in `events`.
- Auth: email+password (argon2) and GitHub OAuth; session cookies HttpOnly, SameSite=Lax; CSRF protection on mutating routes.

## 14. Frontend (Next.js + TypeScript + Tailwind)

Pages:
- `/login`, `/signup`
- `/settings/providers` (add/test/remove keys), `/settings/integrations`
- `/new` (idea input, autonomy level selector with plain-language descriptions, budget cap)
- `/runs` (history), `/runs/[id]` (live workspace)
- `/projects/[id]`

Live workspace layout:
- Left: conversation stream (user + orchestrator + addressed agent threads).
- Center: current stage panel (questions form, plan editor, code/diff viewer, preview iframe/URL).
- Right: agent tree with status, task graph, approvals queue, cost meter, artifacts.
- Persistent controls: pause, resume, cancel, autonomy level switcher (changeable mid-run, takes effect at next gate), "message agent" box.

## 15. Observability

- Structured JSON logs with `run_id`, `agent_id`, `step_idx`.
- OpenTelemetry traces across API -> worker -> LLM/tool calls.
- Metrics: run duration, cost per run, approval wait time, failure rate by role/tool, sandbox usage.
- Run replay: all `events` + `agent_steps` sufficient to reconstruct a run in the UI.

## 16. Final Report (Reporter output)

Markdown with: idea and clarified requirements, decisions made (and who made them), final plan vs. actual, what was built (architecture summary), repo URL, live URL, test results, approvals log, cost breakdown by agent/model, known issues, recommended next steps.

## 17. Repository Layout

```
/
  AGENTS.md
  docs/ SPEC.md BUILD_PLAN.md
  apps/
    api/            # FastAPI app
      app/
        api/        # routers
        core/       # config, security, crypto, db
        models/     # SQLAlchemy models
        repos/      # tenant-scoped repositories
        orchestrator/ # state machine, scheduler
        agents/     # runtime, roles/, tools/
        gate/       # gate engine
        llm/        # gateway
        sandbox/    # providers
        integrations/ # github, vercel, ...
        events/     # bus, replay
        worker.py   # Arq entry
      migrations/   # Alembic
      tests/
    web/            # Next.js
  infra/
    docker-compose.yml
    sandbox/Dockerfile
  .github/workflows/ci.yml
```

## 18. Key Risks

- Scope creep: hold the v1 line in section 2.
- Runaway cost: budget enforcement before every LLM call; hard caps per agent and per run.
- Sandbox abuse/security: strict limits, network allowlist, short TTL.
- Agent reliability: structured outputs, bounded loops, Reviewer + Tester gates before deploy, human gates by default.
- Provider differences in tool calling: normalize in gateway; contract tests per provider.

## 19. Defaults Decided

Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, Postgres 16, Redis 7, Arq, LiteLLM, Pydantic v2, pytest; Next.js (App Router), TypeScript, Tailwind, shadcn/ui; Docker sandboxes on one VPS; BYOK first.

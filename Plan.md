# PLAN — AI Company Research & Lead Qualification (Production-minded Local MVP)

> Current implementation revision: DDD layers + async SQLAlchemy + SQLite/aiosqlite and PostgreSQL/asyncpg.
> The original phased plan below records the initial SQLite MVP. Current architecture, pool,
> lifecycle and PostgreSQL instructions: [docs/architecture.vi.md](docs/architecture.vi.md).
> Async acceptance: 53 tests passed on Windows with real SQLite, PostgreSQL and Redis; provider mocked.
> Linux acceptance: the same 53 tests passed; all service images built; PostgreSQL migration service passed.

> Version: 1.0 — 2026-10-16  
> Stack: **FastAPI + CrewAI + OpenAI API + Celery Worker + Celery Beat + Redis + SQLite (SQLAlchemy/Alembic)**  
> Language: Python 3.11+ (pin versions after initial compatibility smoke test).  
> Goal: Build an end-to-end, locally runnable service with durable step checkpoints, idempotency, crash recovery, and controlled LLM spending.  
> Intended workflow: User provides **company description + optional public website text** (no paid data providers in MVP). The system analyzes industry, determines ICP fit, computes rule-based score, and generates a report. **No live website scraping in MVP** to avoid external dependencies, anti-bot issues, and uncontrolled input.

---

## 0. Rules for the coding agent (READ FIRST)

1. Read this entire `Plan.md`; implement in phases **in order**. Do not replace required features with mock business logic in the final implementation.
2. After each phase, run its verification commands, report results, and update its checklist in this file. Fix test failures before proceeding.
3. Use the **actual OpenAI API** only for explicitly designated manual E2E tests; all automated tests mock the LLM/CrewAI boundary.
4. Keep the project local-first and educational, but implement database transaction boundaries, safe retries, and failure injection correctly.
5. No provider key in source, commits, logs, result payloads, or test fixtures. Use `.env`, include `.env.example` and `.gitignore`.
6. **Do not claim exactly-once execution of external LLM calls.** When request outcome is uncertain, prefer `UNKNOWN` / review over blind retry.
7. Do not hold SQLite transactions open during any network/LLM calls. Commit claim; call LLM; commit checkpoint.
8. Keep CrewAI calls **one checkpointable Crew per AI step**, not one huge multi-step `kickoff()` whose intermediate results cannot be recovered independently.
9. Use safe deterministic scoring in ordinary Python. LLM-generated confidence is explanatory only, not proof of factual correctness.
10. Document the exact library versions installed and verify current public APIs before coding against them.

## 1. Business scenario / user story

An operator wants to qualify candidate B2B companies for a SaaS product. They POST one company's name, website URL (metadata only), company description, and target ICP. The service asynchronously:

1. **RESEARCH**: CrewAI Research Agent extracts a structured summary, industry, products, customers, and evidence *from supplied text only*.
2. **CLASSIFY**: CrewAI Qualification Agent decides potential ICP fit with structured reasons and citations to evidence *within the supplied text*.
3. **SCORE**: deterministic Python function awards points based on configured ICP rules; no LLM call.
4. **REPORT**: CrewAI Report Agent writes a concise, evidence-based sales research report.

Results are checkpointed **after every step**. API clients can poll progress, view each step, and request controlled recovery. Celery Beat scans expired work leases and initiates a recovery decision.

### Non-goals for MVP

- No live web crawling, DataForSEO, email sending, Telegram alerts, frontend, vector database, Kubernetes deployment, or multi-tenancy.
- No promise of eliminating duplicated provider billing in the crash-after-provider-before-DB window.
- No automatic retry of **unknown-outcome** LLM calls.
- No full Temporal migration or LangGraph integration.

### MVP success criteria

- `POST /api/v1/research/jobs` returns **202 + job_id**; duplicates with same idempotency key and same payload return same job; same key with different payload returns **409**.
- Worker completes four steps, checkpointing structured outputs in SQLite. Polling endpoint returns statuses and final result.
- If worker crashes after RESEARCH checkpoint, resumed execution **does not call Research LLM again**.
- Concurrent deliveries must not cause two successful claims for the same step; stale worker may not overwrite checkpoint.
- Beat triggers recovery scan; scanner identifies expired `RUNNING` step, transitions to `UNKNOWN`, and never silently calls an expensive LLM again.
- Mock tests pass with no OpenAI credentials; manual real-provider test runs with a small explicit budget.
- Logs contain `job_id`, `step`, `attempt_id` and failure reason but not full private input or API key.

---

## 2. High-level architecture

```text
                         ┌───────────────────┐
 Client / Swagger ───────▶│ FastAPI REST API  │
                         └────────┬──────────┘
                                  │ create idempotent job
                                  ▼
                         ┌───────────────────┐
                         │ SQLite DB         │◀────────────────────────┐
                         │ jobs, steps,      │                         │
                         │ attempts, events  │                         │
                         └────────┬──────────┘                         │
                                  │ enqueue job_id                     │ checkpoint
                                  ▼                                    │
                          ┌──────────────────┐                       │
                          │ Redis Broker     │                       │
                          └────────┬─────────┘                       │
                                   │                                  │
                       ┌───────────┴──────────┐                       │
                       ▼                      ▼                       │
                 AI queue                maintenance queue            │
                       │                      ▲                       │
                       ▼                      │                       │
                 Celery AI Worker         Celery Beat                 │
                       │                 (every 60 sec)              │
                       │                      │                       │
                       │               Recovery Worker ──────────────┤
                       ▼                                              │
                Checkpoint-aware Runner ──▶ CrewAI ──▶ OpenAI API ─────┘
                   RESEARCH → CLASSIFY → SCORE → REPORT
```

**Ownership:** FastAPI validates & enqueues; Celery transports tasks; SQLite is the business state source of truth; Runner controls step claims/checkpoints; CrewAI performs only AI reasoning; Beat only schedules inspection; Recovery service decides how to reconcile/requeue. Never use Celery task state as the job's authoritative status.

### SQLite local-development constraints

- One local machine and **one AI worker process, one maintenance worker process, one Beat scheduler** to minimize SQLite writer contention.
- Configure SQLite WAL, `busy_timeout`, short transactions, `check_same_thread=False` with per-session SQLAlchemy sessions, and retry transient `database is locked` errors with bounded backoff.
- SQLite has **one writer at a time**. `SELECT ... FOR UPDATE SKIP LOCKED` is **not supported**; use atomic conditional `UPDATE ... WHERE ... RETURNING` plus SQLite write serialization. For initial row creation use UNIQUE constraints and catch IntegrityError.
- Start with `celery worker --pool=solo` on Windows/WSL/Docker local testing; run proper Linux prefork worker during future production migration. Avoid relying on hard time limits with `solo` (they may not behave like prefork).
- Share SQLite file only on the local filesystem accessible to all processes; **do not** put SQLite on a network volume across Kubernetes pods.
- Redis is the **broker**, not the authoritative record of workflow completion. No Redis result backend is required for the API (optionally use Redis result backend only for local debugging).

---

## 3. Repository layout

```text
ai-company-research/
├── Plan.md
├── README.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── alembic.ini
├── alembic/versions/
├── docker-compose.yml                 # Full app stack; external Redis by default
├── scripts/
│   ├── bootstrap_dev.py
│   ├── seed_demo.py
│   ├── run_demo.py
│   └── inject_failure.py
├── src/company_ai/
│   ├── __init__.py
│   ├── main.py                         # FastAPI factory and routers
│   ├── settings.py                     # pydantic-settings
│   ├── api/
│   │   ├── schemas.py
│   │   ├── routes.py
│   │   └── dependencies.py
│   ├── database/
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── contracts.py
│   │   ├── hashing.py
│   │   └── errors.py
│   ├── ai/
│   │   ├── crew_factory.py
│   │   ├── prompts.py
│   │   ├── schemas.py
│   │   └── llm_adapter.py              # mockable boundary and usage metadata
│   ├── services/
│   │   ├── create_job.py
│   │   ├── execute_workflow.py
│   │   ├── step_executor.py
│   │   ├── scoring.py
│   │   └── recovery.py
│   ├── celery_app.py
│   └── tasks/
│       ├── ai_tasks.py
│       └── maintenance_tasks.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    └── e2e/
```

Use Python package imports under `src/company_ai`. Include executable examples and `Makefile` or `scripts/*.ps1`/README equivalents for Windows.

---

## 4. Core database design (SQLAlchemy 2.0 + Alembic)

### `ai_jobs`

- `id` UUID string primary key
- `idempotency_key` string not null UNIQUE
- `request_hash` SHA-256 not null (canonical stable request)
- `status`: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `NEEDS_REVIEW`
- `input_json` JSON not null
- `result_json` JSON nullable
- `error_code`, `error_message` nullable (sanitized)
- `created_at`, `updated_at`, `completed_at` UTC timestamps
- `total_estimated_cost_usd` numeric/decimal (optional derived, not a trusted billing ledger)

### `ai_steps`

- `id` UUID string PK; `job_id` FK with index; `step_name` enum `RESEARCH|CLASSIFY|SCORE|REPORT`
- `status`: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `UNKNOWN`, `NEEDS_REVIEW`
- `input_hash` string nullable until its input is materialized; hashes must include `input data + previous step outputs/version hashes + prompt version + model + AI settings + schema version` for AI steps
- `output_json` JSON nullable, `output_hash` nullable
- `attempt_count` integer not null default 0
- `lease_token` UUID nullable; `lease_expires_at`, `last_heartbeat_at` nullable
- `started_at`, `completed_at`, `updated_at` timestamps
- UNIQUE(`job_id`,`step_name`)

### `ai_step_attempts` (audit/recovery)

- `id` UUID PK (`attempt_id`); `step_id` FK; `lease_token` UUID
- `attempt_no` integer; `status` `CLAIMED|REQUEST_STARTED|SUCCEEDED|FAILED|UNKNOWN`
- `provider_request_id` nullable (only if provided); `llm_started_at` nullable; `error_type` nullable
- `tokens_input`, `tokens_output` nullable; `estimated_cost_usd` nullable
- `created_at`, `finished_at` timestamps
- UNIQUE(`step_id`, `attempt_no`)

### `ai_job_events` (optional but recommended)

- `id`, `job_id`, `step_id` nullable, `event_type`, `details_json` (sanitized), `created_at`.
- Important types: `JOB_CREATED`, `STEP_CLAIMED`, `LLM_REQUEST_STARTED`, `STEP_CHECKPOINTED`, `LEASE_EXPIRED`, `STEP_UNKNOWN`, `STEP_RECOVERED`, `JOB_COMPLETED`, `JOB_NEEDS_REVIEW`.

### DB invariants

1. Never overwrite `COMPLETED` output with another attempt.
2. Step claim is an **atomic conditional UPDATE** on eligible status, creates new `lease_token` and attempt record in one transaction.
3. Step completion is an **atomic conditional UPDATE** requiring `status='RUNNING'`, matching `lease_token`, `input_hash`; rowcount must be 1. In the same transaction update next step/job status and write audit event.
4. If `lease_token` changed, previous worker cannot commit (`fencing`).
5. `UNKNOWN` LLM step is **not** automatically eligible for execution.
6. Multiple different requests with one idempotency key -> HTTP 409; do not infer duplicate solely from company name.
7. UTC timestamps consistently; do not assume naive Python datetimes represent user timezone.

### SQLite claim example (illustrative SQL)

```sql
UPDATE ai_steps
SET status = 'RUNNING',
    lease_token = :token,
    lease_expires_at = :lease_expiration,
    attempt_count = attempt_count + 1,
    updated_at = :now
WHERE id = :step_id AND status = 'PENDING'
RETURNING id, lease_token, attempt_count;
```

Implement lease-expiry state transition with another conditional UPDATE; avoid read-then-write race. For SQLite use the SQLAlchemy version plus an integration test with two concurrent claim attempts.

---

## 5. Contract of each step

| Step | Type | Input | Output checkpoint | Retry after known failure |
|---|---|---|---|---|
| RESEARCH | CrewAI single-agent/single-task | company name + description | `ResearchOutput` | bounded if **confirmed** not executed / safe category |
| CLASSIFY | CrewAI single-agent/single-task | research checkpoint + ICP | `ClassificationOutput` | same policy |
| SCORE | Pure Python | classification + evidence | `ScoreOutput` | safe / deterministic |
| REPORT | CrewAI single-agent/single-task | all previous outputs | `ReportOutput` | bounded if safe; UNKNOWN needs reconciliation |

Pydantic `ResearchOutput`: `company_name`, `industry`, `products: list[str]`, `customer_segments: list[str]`, `evidence: list[str]`, `limitations: list[str]`.

Pydantic `ClassificationOutput`: `icp_match: bool`, `matching_signals: list[str]`, `missing_signals: list[str]`, `reasoning: str`. No unsupported invented customer counts/revenue.

Pydantic `ScoreOutput`: `score: int (0..100)`, `rule_breakdown: list[dict]`, `qualification: HIGH|MEDIUM|LOW`.

Pydantic `ReportOutput`: `executive_summary`, `strengths`, `risks`, `next_questions`, `sources_note` (all from supplied input).

**Scoring rule example:** 40 points if classified as B2B SaaS; 30 points if ICP match; 20 points if SME target; 10 points if a clear product exists. Mark score as rule-based estimate, not externally verified company quality. Include explicit missing/unsupported signals.

**CrewAI usage:** one `Crew` per expensive step, `Agent` + `Task(output_pydantic=...)`, `Process.sequential` where needed. Set bounded max iteration, `allow_delegation=False` for MVP, explicit provider/model config, temperature and timeout. Make provider interface mockable. Treat input text as untrusted; never let it override system instructions or call arbitrary tools. Avoid granting network filesystem/database tools to Agent.

---

## 6. API contracts

### `POST /api/v1/research/jobs`

Headers: `Idempotency-Key: <stable-client-generated-id>` (required); optionally `X-Request-ID` for tracing. Limit payload size and description length.

Body example:

```json
{
  "company_name": "Acme CRM",
  "website": "https://example.com",
  "company_description": "Acme builds a subscription CRM platform for small and medium businesses in Vietnam.",
  "icp": "Vietnam-based B2B SaaS selling to SMEs"
}
```

- `202` created `{ "job_id": "...", "status": "PENDING", "status_url": "/api/v1/research/jobs/..." }`.
- Same key + identical canonical payload: `200` or `202` with same `job_id`, clearly document chosen behavior.
- Same key + different payload: `409 IDEMPOTENCY_KEY_CONFLICT`.
- Invalid input: `422`. Do not pass arbitrary URL to crawler in MVP; `website` is treated as metadata, preventing SSRF.

### `GET /api/v1/research/jobs/{job_id}`

Returns `job_id`, `status`, created/updated times, ordered steps with status, attempt_count and timestamps, sanitized error summary, and `result` if completed. Return 404 when unknown.

### `GET /api/v1/research/jobs/{job_id}/events`

Returns sanitized progress/audit events for development diagnostics, optionally paginated.

### `POST /api/v1/research/jobs/{job_id}/retry`

For local demo only, require local admin token; accept `{ "step_name": "REPORT", "acknowledge_possible_duplicate_charge": true }` for a step in `UNKNOWN`/`NEEDS_REVIEW`; log audit event. Reject `COMPLETED`/`RUNNING` step retries. Prevent stale downstream outputs by only allowing retry at the next unfinished step in MVP. **Never silently retry UNKNOWN LLM call.**

### `GET /health` and `GET /ready`

`/health` is liveness; `/ready` checks SQLite connectivity and optionally broker reachability with a short timeout. Never hit OpenAI in a healthcheck.

### `GET /api/v1/admin/metrics` (optional MVP+)

Counts by status, recovered steps, unknown steps, completed jobs, LLM usage counts, approximate costs; local-only auth.

---

## 7. Workflow runner algorithm (implementation contract)

```text
run_job(job_id):
  obtain latest job state from SQLite
  if COMPLETED/NEEDS_REVIEW: return
  for step in [RESEARCH, CLASSIFY, SCORE, REPORT]:
    load previous COMPLETED outputs
    compute canonical input_hash (prompt/model/config version included)
    if step COMPLETED and hash matches: reuse output; continue
    if step COMPLETED but hash differs: stop; explicit new job/version is required
    if step RUNNING with valid lease: exit without duplicate execution
    if step UNKNOWN/NEEDS_REVIEW: stop + mark job NEEDS_REVIEW
    atomically claim PENDING step and create attempt
    commit DB transaction
    try:
      if LLM step: record REQUEST_STARTED before external call; then execute one CrewAI call
      else: deterministic score function
      validate result Pydantic
      in a new transaction:
        conditional UPDATE guarded by lease token + input hash
        persist output, event, usage metadata, and advance state
      if lease lost: discard stale result safely (audit)
    except provider timeout / ambiguous outcome:
      status UNKNOWN; stop; no blind retry
    except known deterministic validation failure:
      status FAILED/NEEDS_REVIEW; stop
  if all steps COMPLETE: commit final job result + COMPLETED
```

**Important:** recording `REQUEST_STARTED` before calling provider helps forensics, but does **not** tell whether remote provider processed the call. A worker can crash in the small gap after commit but before sending. Treat these as ambiguous, not as proof of billing.

### Failure recovery rules

- Lease TTL sample: 5 minutes; LLM HTTP timeout sample: 90 seconds; maintain a heartbeat during long-running activity or choose a conservative TTL **longer than the max allowed execution**. Heartbeat must stop if worker/process dies. Heartbeat alone does not prove the provider call is progressing.
- Celery Beat every 60 seconds **schedules** `scan_stale_steps`. It does not execute business logic itself.
- `scan_stale_steps` uses bounded batch and atomic conditional UPDATE to transition expired `RUNNING` to `UNKNOWN` (for LLM) or requeue safely (for deterministic SCORE).
- If step has already checkpointed, resume from next step; if request outcome ambiguous, `NEEDS_REVIEW` / deliberate manual retry.
- `reconcile_unknown_step` checks **only** provider-supported reliable lookup/idempotency functionality, if available. If not available, do not assert completion and do not auto-redo.
- All enqueue/redelivery paths call the same `claim_step` gate. No direct `.kickoff()` outside runner.
- If process dies after DB commit but before enqueueing the next task, Beat's separate `resume_pending_jobs` scan can requeue **only eligible PENDING work**, guarded by DB status. This solves the enqueue gap; use DB-backed outbox for larger production systems.
- If an LLM call completes but crash occurs before DB checkpoint, duplicate provider charges **cannot be categorically prevented** without provider-level guarantees.

### Handling Celery redelivery

- Configure `task_acks_late=True`; evaluate `task_reject_on_worker_lost` carefully (it can cause requeue loops). Never rely on it alone for recovery.
- Prefer **DB-driven recovery** and explicit retry ceiling; avoid infinite autoretry for expensive LLM operations.
- Celery queues: `ai`, `maintenance`; one worker per queue locally. Maintenance tasks `ignore_result=True`.
- Set `worker_prefetch_multiplier=1`; Redis `visibility_timeout` larger than longest normal task duration; worker concurrency low for SQLite.
- Use broker publish retry policy; if enqueue fails after job insert, scheduled `resume_pending_jobs` recovers it.

---

## 8. Celery Beat schedules

| Schedule key | Interval | Queue | Action |
|---|---|---|---|
| `scan-stale-ai-steps` | Every 60 seconds | maintenance | Detect expired RUNNING leases; UNKNOWN or safe recovery |
| `resume-pending-jobs` | Every 60 seconds | maintenance | Find eligible jobs with PENDING step and no active lease; enqueue `run_job(job_id)` (dedup via DB) |
| `cleanup-old-job-events` | Every day 02:00 UTC | maintenance | Cleanup old audit records according to retention (not active jobs) |

Run **one** Beat process. Protect scanners from overlap with DB-conditional claims; schedule intervals are approximate. Celery Beat is not a substitute for heartbeat or process supervision.

---

## 9. Local configuration / secrets / cost limits

`.env.example`:

```dotenv
APP_ENV=development
DATABASE_URL=sqlite:///./data/ai_research.db
REDIS_URL=redis://localhost:6379/0
OPENAI_API_KEY=replace_me
OPENAI_MODEL=replace_with_available_low_cost_model
LLM_TEMPERATURE=0
LLM_REQUEST_TIMEOUT_SECONDS=90
MAX_LLM_CALLS_PER_JOB=3
MAX_AGENT_ITERATIONS=2
MAX_INPUT_CHARS=6000
MAX_OUTPUT_TOKENS_PER_STEP=500
MAX_ESTIMATED_USD_PER_JOB=0.20
LEASE_TTL_SECONDS=300
RECOVERY_SCAN_SECONDS=60
ADMIN_TOKEN=local-development-only-secret
LOG_LEVEL=INFO
```

- Never embed key in prompts, logs, or exceptions. Never commit `.env` or SQLite DB.
- Real cost cap is **best effort**: preflight estimated token usage and actual provider usage may differ. Enforce provider-side spend limits separately if available. Stop before launching next call if job budget exceeded.
- Prompt/version hashes must be stable; cache only per appropriate input/tenant/model/prompt identity. Avoid cross-tenant leakage in future.
- OpenAI model names and CrewAI provider configuration must be verified at implementation time; avoid hardcoding an assumed currently-available model.

---

## 10. Implementation phases (agent execution checklist)

### Phase 0 — Setup & dependency sanity

- [x] Create Python virtual environment, `pyproject.toml` dependencies: FastAPI, Uvicorn, CrewAI, Celery, Redis, SQLAlchemy, Alembic, pydantic-settings, pytest, httpx, pytest-asyncio (as needed), ruff.
- [x] Pin tested working versions; verify `crewai` single-task structured result using a **mock** provider or official current API.
- [x] Add `.env.example`, `.gitignore`, formatter/linter, README setup for Windows PowerShell and WSL/Docker.
- [x] Compose Redis is optional; per user instruction use existing external Redis. PING verified.
- **Done when:** imports work, Redis reachable, no key required for unit tests.

### Phase 1 — Models, migrations, repository

- [x] Implement SQLAlchemy models & Alembic migration for `ai_jobs`, `ai_steps`, `ai_step_attempts`, `ai_job_events`.
- [x] Enable SQLite WAL and `busy_timeout`; session-per-operation and short transactions.
- [x] Implement canonical request hash + step input hash.
- [x] Implement `create_job_idempotently`, `claim_step`, `complete_step_with_fencing`, `mark_step_unknown`, `list_stale_steps`, `list_resumable_jobs`, `get_job`.
- [x] Concurrency test: two threads/processes race to claim one step; only one obtains lease.
- **Done when:** Alembic upgrade and DB tests pass.

### Phase 2 — FastAPI endpoints

- [x] Pydantic input/output schemas; size and URL validation; implement endpoints from §6.
- [x] Idempotency-key conflict response and safe errors.
- [x] Dependency-injected DB sessions; OpenAPI documentation.
- [x] TestAPI with FastAPI TestClient/HTTPX mocked enqueue.
- **Done when:** 202, duplicate return, 409, 422, 404 tests pass.

### Phase 3 — CrewAI steps and deterministic score

- [x] Create research, classify, and report Crews with one Agent/Task each and typed Pydantic outputs.
- [x] Build `LLMAdapter` protocol for mocking, token usage and exception categorization.
- [x] Prompts mandate evidence-only output and prohibit inventing facts; bound iterations and output length.
- [x] Implement score as pure Python with unit tests and transparent rules.
- [x] Unit tests mock AI boundary and check no duplicate call on checkpoint hit.
- **Done when:** simulated four-step job completes without OpenAI calls.

### Phase 4 — Celery integration and checkpoint-aware orchestration

- [x] Wire `run_job(job_id)` task on `ai` queue; payload contains only job ID.
- [x] Implement step executor from §7, commit before network call and after success, guard writes by lease token.
- [x] Carry `job_id`, `step_name`, `attempt_id` in structured logs.
- [x] Ensure errors are classified; uncertain LLM outcome goes UNKNOWN/NEEDS_REVIEW, not autoretry.
- [x] Integration tests: checkpoint reuse; stale worker commit fails; SCORE deterministic retry safe.
- **Done when:** mocked worker finishes job and repeat dispatch adds **zero** AI calls.

### Phase 5 — Celery Beat and recovery

- [x] Configure three schedules from §8.
- [x] `scan_stale_steps` marks expired LLM step UNKNOWN via conditional UPDATE and audit event.
- [x] `resume_pending_jobs` re-enqueues eligible work, including job created but enqueue failed.
- [x] `cleanup_old_job_events` honors retention.
- [x] Provide safe manual retry endpoint for UNKNOWN steps (admin token + explicit duplicate-cost acknowledgement).
- [x] Failure injection hooks enabled only with `APP_ENV=development`, never in production.
- **Done when:** recovery finds stuck rows and never automatically re-calls unknown expensive LLM step.

### Phase 6 — Real OpenAI E2E smoke test

- [ ] User adds API key to local `.env` (never send to assistant or commit).
- [x] Select inexpensive supported model and inspect model pricing from provider docs.
- [x] Start FastAPI, AI worker, maintenance worker, Beat against existing external Redis.
- [ ] Submit one short company research job via Swagger or `curl`; poll to COMPLETED.
- [ ] Inspect checkpoint rows, usage logs and generated report.
- [ ] Submit same idempotency key/payload again; assert same job ID and **no further LLM calls**.
- [ ] Force simulated crash after first checkpoint and confirm resume from CLASSIFY.
- **Done when:** at least one real job completes, cost is recorded as estimate, and crash demo is reproducible.

### Phase 7 — Documentation and handover

- [x] README architecture, environment, start commands, API examples, sample responses, troubleshooting, and failure demo.
- [x] Document SQLite limitations and PostgreSQL migration differences.
- [x] Final test/lint report, known limitations, next improvements.
- **Done when:** a fresh developer can run local project using README only.

---

## 11. Runbook (commands to implement and verify)

Example PowerShell setup:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Edit .env locally, set OPENAI_API_KEY; don't commit.
# Existing Redis must be reachable at localhost:6379.
alembic upgrade head
uvicorn company_ai.main:app --reload --port 8000
```

Run each of the following in a **separate terminal** with venv activated:

```powershell
celery -A company_ai.celery_app:celery_app worker -Q ai --pool=solo -l info
celery -A company_ai.celery_app:celery_app worker -Q maintenance --pool=solo -l info
celery -A company_ai.celery_app:celery_app beat -l info
```

For Redis-only Docker Compose, host Redis at `localhost:6379`. Commands/arguments must be adjusted if the module object is named differently; favor keeping the documented module and object names stable.

Submit request (PowerShell):

```powershell
$headers = @{ "Idempotency-Key" = "demo-company-acme-v1" }
$body = @{
  company_name = "Acme CRM"
  website = "https://example.com"
  company_description = "Acme provides subscription CRM tools to Vietnamese SMEs."
  icp = "B2B SaaS providers serving SMEs in Vietnam"
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/research/jobs" -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

Test commands:

```powershell
pytest -q
ruff check .
```

Swagger: `http://localhost:8000/docs`.

---

## 12. Required failure-injection test matrix

| Test | Injection | Expected behavior | Avoid duplicate LLM? |
|---|---|---|---|
| T01 API duplicate | Same key + body twice | Same job ID | Yes |
| T02 idempotency conflict | Same key + different body | 409 | Yes |
| T03 worker loss after checkpoint | Crash after RESEARCH DB commit | Next delivery loads RESEARCH output | Yes |
| T04 pre-call crash | Crash after claim, before provider request | Lease expires; conservative UNKNOWN unless safely proven not sent | Avoid by policy |
| T05 in-call crash | Crash during LLM request | UNKNOWN, manual/conditional reconcile | **Not guaranteed** |
| T06 post-response pre-commit crash | LLM finished, DB not committed | UNKNOWN, may require deliberate repeat | **Not guaranteed** |
| T07 lost lease | Worker A continues after lease expired | Stale A cannot write output; fencing enforced | Prevents overwrite |
| T08 concurrent delivery | Same `job_id` in two broker messages | Only one DB claim succeeds | Yes for successfully claimed step |
| T09 enqueue gap | Job DB committed but Redis unavailable | Beat later discovers PENDING job | Yes via DB claim |
| T10 score failure | Crash during SCORE | Repeat deterministic SCORE | No LLM involved |
| T11 Beat overlap | Two maintenance tasks overlap | Conditional DB updates prevent double claim | Yes via DB claim |
| T12 provider 429 | Rate limited | bounded policy with backoff where known safe | Policy-controlled |
| T13 input version change | Prompt/model/input changed | No stale checkpoint reuse | New intentional call |
| T14 budget exceeded | Estimate hits configured cap | stop before next LLM call | Yes |

Include **mock call counter** in tests: compare counters before/after resume. Use deterministic local test hooks (e.g., `FAIL_AFTER_CHECKPOINT_STEP=RESEARCH` in dev mode) and integration assertions against SQLite records. Do not expose failure hooks through unauthenticated public API.

### Manual crash demonstration

1. Start all services. Submit demo job.
2. With controlled failure injection, terminate AI worker **after RESEARCH checkpoint is committed**.
3. Restart AI worker. Allow redelivery or Beat's `resume_pending_jobs` to enqueue.
4. Confirm Research checkpoint reused; Classification, Score, Report proceed.
5. View job events showing claim/recovery; check mock/OpenAI call logs for no second Research invocation.
6. Separately simulate `REQUEST_STARTED` + expired lease and confirm UNKNOWN is **not** automatically retried.

---

## 13. Observability and security

- JSON logs fields: `service`, `request_id`, `job_id`, `step_name`, `attempt_id`, `event`, `duration_ms`, `error_category`.
- Metrics: jobs created/completed/needs_review, step completed/unknown, checkpoint hits, stale lease count, recovery enqueue count, LLM calls, input/output tokens, estimated cost, retry/rejection counts.
- Sensitive content: do not log API key, full descriptions or raw prompt/response by default; sanitize error messages, redact personal data, enforce request size caps.
- Authentication: MVP API may bind to `127.0.0.1`; manual retry/admin endpoints require token even for local demo. Add proper user auth + per-tenant isolation for deployed version.
- Prompt injection: descriptions are untrusted evidence, no system-message interpolation, no arbitrary shell/network/database tools granted to agents.
- SQLite backups: local DB file is a development artifact, not production HA storage.

---

## 14. Future production migration roadmap

1. PostgreSQL support is implemented via asyncpg, shared Alembic migrations and concurrent recovery tests. Future scaling can add `SELECT ... FOR UPDATE SKIP LOCKED` for batch recovery (not on SQLite).
2. Separate web, AI workers, maintenance workers, and Beat in containers/Kubernetes, deploy only one scheduler leader.
3. Add transactional outbox for DB→Redis enqueue reliability, DLQ/ops dashboard, token usage ledger and provider correlation IDs.
4. Add proper auth, tenant-scoped idempotency keys, request quotas and spending caps, data retention and encryption.
5. Add OpenTelemetry traces + Prometheus/Grafana + APM; alert on UNKNOWN/NEEDS_REVIEW growth and job age.
6. Add company-data retrieval as a deterministic, separate enrichment step (DataForSEO or permitted sources), with provenance and result caching.
7. Consider Temporal only when long-running business orchestration/human approval/many external callbacks make custom recovery too complex.

---

## 15. Definition of Done (final acceptance)

- [x] Full app Compose builds/starts; external Redis PING and API/worker/Beat connectivity verified.
- [x] SQLite schema/migrations work; no migration error.
- [x] All API contracts implemented; idempotency tested.
- [x] 4-step pipeline produces structured research/classification/score/report.
- [ ] CrewAI calls real OpenAI successfully in explicit E2E smoke test.
- [x] All outputs checkpointed; repeat task execution does not re-call completed AI steps.
- [x] Stale worker cannot commit after fencing; concurrent claims safe.
- [x] Beat recovery identifies stuck steps and never auto-retries ambiguous LLM calls.
- [x] Failure injection scenarios T01–T14 documented and tested (automate as many as practical).
- [x] Cost budgets, request limits, secret handling and structured logs included.
- [x] README permits another developer to reproduce the demo in VS Code.

## 16. First prompt to paste into coding assistant in VS Code

> Read `Plan.md` entirely and implement **Phase 0 and Phase 1 only** for the FastAPI + CrewAI + Celery + Celery Beat + Redis + SQLite project. Before writing code, summarize the architecture and critical correctness invariants: atomic claim, lease/fencing, checkpoint reuse, UNKNOWN on ambiguous LLM outcomes, and SQLite limitations. Use current compatible package APIs, pin versions, generate project scaffolding and Alembic migration, run verification commands, and update the phase checkboxes in `Plan.md`. Do not call real OpenAI API in this phase. Once Phase 0–1 tests pass, report modified files, commands, test results and next steps.

---

## Primary official references

- FastAPI: https://fastapi.tiangolo.com/
- CrewAI: https://docs.crewai.com/
- Celery Beat: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- Celery task acknowledgment: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- SQLAlchemy SQLite: https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
- SQLite WAL: https://sqlite.org/wal.html
- OpenAI API: https://platform.openai.com/docs/

> **Architecture guarantee:** exactly-once **database checkpoint** is approachable through transactions, unique constraints and fencing; exactly-once **external LLM invocation/billing** is not guaranteed by this stack. Preserve this distinction in code and documentation.

## Implementation log

- Phase 0: Python 3.13.13; uv.lock created and installed; CrewAI 1.15.28 imports and real Crew/mock SDK smoke passed. Existing external Redis PING passed; per user, do not start Redis.
- Phase 1: Alembic upgrade/current passed; three DB tests passed (idempotency, concurrent claim, fencing/immutability). Step hashes verified with workflow tests.
- Phase 2: five cumulative API/DB tests passed; 202/200/409/422/404, size limits and enqueue gap covered.
- Phase 3: 16 cumulative tests passed, including actual CrewAI with mocked OpenAI SDK, deterministic scoring and checkpoint resume.

- Phases 4-5: 20 cumulative tests passed. Celery task redelivery adds zero LLM calls; three Beat schedules, scanner overlap, safe SCORE retry, admin acknowledgement and retention verified.
- Real Redis/Celery integration: opt-in API -> broker -> solo worker -> SQLite checkpoint test passed against existing Redis on port 6379; provider remained mocked.
- Phase 6: local provider key is absent; real OpenAI calls have not been made. Manual smoke runner and crash runbook are supplied.

- Final Windows acceptance: 24 tests passed (including real Redis E2E, with mock LLM); Ruff lint and format passed; Alembic check reports no pending schema changes.
- Docker: all service images built successfully; migration exited 0; API healthy; workers connected to external Redis; Beat dispatched maintenance tasks successfully.
- Phase 6 real-provider acceptance remains unchecked because OPENAI_API_KEY is not configured. All other acceptance items use verified mocked-provider behavior.

- Linux acceptance: 24 tests passed with real Redis/mock OpenAI; Ruff lint and format passed. Upstream CrewAI emitted deprecation warnings only. Detailed review and test evidence: docs/verification.md.

# AI Company Research & Lead Qualification

FastAPI → Redis/Celery → CrewAI/OpenAI, with async SQLite or PostgreSQL as the workflow source of truth.
Each job runs **RESEARCH → CLASSIFY → SCORE → REPORT**. Every step has an atomic claim,
lease, fencing token, checkpoint and attempt audit. SCORE is deterministic Python.
Company descriptions and optional website text are the only evidence; URLs are metadata.

The code follows a pragmatic DDD layout: `domain` (entities/policies/scoring), `application`
(use cases and ports), `infrastructure` (async SQLAlchemy, AI and messaging adapters), and
`api` (HTTP). ORM objects stay in infrastructure; repository results are domain snapshots.
API, repository, workflow, heartbeat and OpenAI I/O are async. Celery's synchronous task
entrypoints bridge into an event loop with an engine disposed before that loop closes.

Vietnamese guides: [README tiếng Việt](docs/README.vi.md),
[DDD, async và PostgreSQL](docs/architecture.vi.md).

Full service integration walkthrough: [Luồng end to end và xử lý tình huống](docs/end-to-end.vi.md).

Each service also has a standalone `docker-compose.<service>.yml` file.
See [Chạy Compose riêng từng service](docs/compose-services.vi.md) for commands and startup order.
The default `docker-compose.yml` aggregates those definitions and preserves migration dependencies.

## Database configuration

Default: `DB_BACKEND=sqlite`, blank `DATABASE_URL`, using `sqlite+aiosqlite:///./data/ai_research.db`.
For PostgreSQL, leave `DATABASE_URL`/`CONTAINER_DATABASE_URL` blank and set:

```dotenv
DB_BACKEND=postgresql
POSTGRES_HOST=localhost
POSTGRES_DB=company_ai
POSTGRES_USER=company_ai
POSTGRES_PASSWORD=<your-password>
POSTGRES_PORT=5432
CONTAINER_POSTGRES_HOST=host.docker.internal
DB_POOL_MODE=pooled
DB_POOL_SIZE=16
DB_MAX_OVERFLOW=48
DB_POOL_RECYCLE=3600
```

`URL.create` safely builds the asyncpg URL from components. Explicit `DATABASE_URL` overrides
host components; Compose uses `CONTAINER_DATABASE_URL` when set. `DB_POOL_MODE=null` selects
`NullPool`; pool size/overflow options apply only to pooled PostgreSQL engines. Alembic uses
the same async drivers and a short-lived NullPool engine. Run migrations before starting services.
Existing SQLite files remain supported; changing backend does not copy existing data.

## Start with Docker and your existing Redis

```powershell
Copy-Item .env.example .env
# Edit .env locally: OPENAI_API_KEY and a random ADMIN_TOKEN.
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/ready
```

Swagger: http://localhost:8000/docs. Compose uses your Redis published on host port 6379,
via `CONTAINER_REDIS_URL=redis://host.docker.internal:6379/0`. Set this variable for a different
port, password, or host. No Redis container is started by default.

After adding or changing `.env`, run `docker compose up -d --force-recreate` so running
processes receive the new configuration. For the explicit paid smoke test:

```powershell
.\.venv\Scripts\python scripts/run_demo.py --real-provider --acknowledge-cost
```

All application services share the local `app-data` volume for SQLite. The one-shot migration
service finishes before the API/workers/Beat start. Run exactly one AI worker, maintenance
worker and Beat. Each service has its own Dockerfile and starts via `scripts/start-*.sh`.

## Local development (PowerShell)

Python 3.11–3.13; verified here with Python 3.13.13. Install `uv` first.

```powershell
uv sync --extra dev --frozen
Copy-Item .env.example .env
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\uvicorn company_ai.main:app --host 127.0.0.1 --port 8000
```

In separate activated terminals:

```powershell
celery -A company_ai.celery_app:celery_app worker -Q ai --pool=solo -c 1 -l info
celery -A company_ai.celery_app:celery_app worker -Q maintenance --pool=solo -c 1 -l info
celery -A company_ai.celery_app:celery_app beat -l info --schedule data/celerybeat-schedule
```

On WSL/Linux use `.venv/bin/activate`, or run the matching `sh scripts/start-*.sh`.
Keep SQLite on one local filesystem; a network-mounted database is unsupported.
`uv.lock` pins all direct/transitive dependencies for both Windows and Linux.

## API example

```powershell
$headers = @{ 'Idempotency-Key' = 'demo-acme-v1' }
$body = @{
  company_name = 'Acme CRM'
  website = 'https://example.com'
  company_description = 'Acme provides B2B SaaS CRM software for SMEs in Vietnam.'
  website_text = ''
  icp = 'B2B SaaS for SMEs in Vietnam'
} | ConvertTo-Json
$job = Invoke-RestMethod http://localhost:8000/api/v1/research/jobs -Method Post -Headers $headers -ContentType application/json -Body $body
Invoke-RestMethod "http://localhost:8000/api/v1/research/jobs/$($job.job_id)"
Invoke-RestMethod "http://localhost:8000/api/v1/research/jobs/$($job.job_id)/events"
```

New requests return 202. The same canonical payload/key returns 200 and the original job;
changed payload with the same key returns 409. Invalid input returns 422, body size overflow
413, unknown jobs 404. Poll results contain ordered step outputs, attempts, UTC timestamps,
sanitized error code and estimated cost. Completed jobs include `result.research`,
`result.classify`, `result.score`, `result.report`.

Manual retry requires `Authorization: Bearer <ADMIN_TOKEN>` and
`{"step_name":"RESEARCH","acknowledge_possible_duplicate_charge":true}` at
`POST /api/v1/research/jobs/{job_id}/retry`. Only the next unfinished UNKNOWN/NEEDS_REVIEW
step can be reset, with no completed downstream steps and below the attempt ceiling.
Completed/running steps cannot be retried. Increasing budget/call limits requires restarting
services. Configuration/prompt mismatch requires a new job and idempotency key.

`GET /health` is liveness. `GET /ready` checks the migrated database and Redis, never OpenAI.
`GET /api/v1/admin/metrics` uses the same bearer token and returns job/step/event counts,
attempted LLM calls, tokens and cost estimates.

## Correctness and cost policy

- A step claim and its attempt are one database transaction. Completion requires current
  lease token, input hash, RUNNING status and an unexpired lease; completed output is immutable.
- No database transaction spans a provider call. A heartbeat renews long work; expired
  leases cannot be revived. Hashes include predecessor data, schema, prompt, model and settings.
- Ambiguous calls/timeouts/crashes become UNKNOWN/NEEDS_REVIEW, never an automatic LLM retry.
  REQUEST_STARTED is evidence of intent, not proof the remote request ran or was billed.
- Explicit 429 rejections get bounded DB-driven exponential backoff. SDK retries are zero.
  Each Crew uses a custom BaseLLM with a one-request gate; repair/iteration cannot add paid calls.
- Before a call, reserve estimated cost using prompt/schema UTF-8 byte count plus framing
  allowance and output ceiling. Reconcile with usage on success; keep reservations for unknown
  outcomes. Call and attempt ceilings include manual retries. This is a best-effort estimate,
  not a billing ledger or provider-enforced hard dollar cap.
- Research quotes must occur in supplied text; classification signals must quote those
  checkpoints. This checks provenance, not factual truth. Scoring rules: B2B SaaS 40, ICP fit
  30, SME evidence 20, product 10. HIGH ≥70, MEDIUM ≥40, otherwise LOW.
- Beat schedules stale scan and pending resume every 60s, event retention cleanup at 02:00 UTC.
  Overlapping scans use conditional updates. Duplicate broker messages still pass the claim gate.
  Pending scans close the DB-commit/broker-publish gap, including a crash after checkpoint.
- Inputs, prompts, raw provider errors and responses are excluded from logs; IDs, categories,
  usage and checkpoints are logged. API responses intentionally expose supplied-derived results.
  CrewAI telemetry is disabled. Admin token is empty/disabled until configured.

## Verification

```powershell
.\.venv\Scripts\pytest -q --basetemp=data/pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\alembic check
```

Automated tests need no OpenAI key and mock only the provider boundary. They include an
actual single-task Crew running against a mocked SDK. See `docs/verification.md` for the
failure matrix and execution evidence. Real-provider smoke tests are explicit/manual only.

Linux verification container (also tests your external Redis with a unique test queue):

```powershell
docker compose --profile test run --rm --no-deps --build -e RUN_REDIS_E2E=1 -e TEST_REDIS_URL=redis://host.docker.internal:6379/15 verification
```

An optional bundled Redis is available with `docker compose --profile bundled-redis up -d redis`.
It publishes host port 6380 by default to avoid the existing Redis; set
`CONTAINER_REDIS_URL=redis://redis:6379/0` for applications to use it.

## Operations and limitations

Public API is intended for a trusted local operator and binds to loopback on the host.
Use authentication, tenant isolation, quotas, HTTPS and retention policies before deployment.
SQLite WAL permits one writer: use short transactions, a 5s busy timeout and bounded retries.
Use SQLite's backup API or stop services before copying a DB; copying only `.db` while WAL
is active can lose data. `docker compose down` keeps volumes; do not remove volumes to restart.

Troubleshooting: `/ready` 503 → check Redis host/port/password and migration logs;
`NEEDS_REVIEW` → inspect events, budget and missing key, then deliberately decide retry/new job;
`PENDING` → check AI and maintenance queues and Beat; DB locked → check duplicate workers or
network volumes. Use `docker compose logs --tail=100 api ai-worker maintenance-worker beat`.

PostgreSQL is supported via asyncpg and verified with the same concurrent recovery tests.
Job-row locks serialize aggregate mutations; conditional step updates retain fencing.
Next production steps are a
transactional outbox, provider usage ledger, proper authentication and operator review UI.
Exactly-once provider invocation/billing is **not guaranteed**, especially after a provider
response but before checkpoint commit.

References verified during implementation: [CrewAI tasks](https://docs.crewai.com/en/concepts/tasks),
[Celery periodic tasks](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html),
[OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
The example model/rates are configurable; confirm model access and pricing for your account.

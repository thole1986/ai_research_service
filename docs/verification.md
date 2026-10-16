# Plan review and verification

## Async + DDD + PostgreSQL revision

The original implementation evidence below describes the initial synchronous SQLite MVP.
The current code uses `domain`, `application`, `infrastructure` and `api` boundaries;
see [the Vietnamese architecture guide](architecture.vi.md) for the current layout/configuration.

- Async SQLAlchemy sessions with aiosqlite (SQLite) and asyncpg (PostgreSQL).
- Domain snapshots replace ORM objects at repository boundaries. Scoring, evidence, retry,
  recovery and budget policies are framework-independent; an architecture test checks imports.
- PostgreSQL aggregate mutations lock the job row first, then step/attempt rows. Conditional
  updates still enforce leases, fencing and immutable completed checkpoints.
- Async API, Redis readiness, workflow, heartbeat and AsyncOpenAI. CrewAI's sync callback and
  Celery's synchronous entrypoint are explicit framework bridges, with loop-owned engine/client
  lifecycles and no global async-engine cache across tasks.
- Windows acceptance: **53 passed**, with both real database backends and real Redis enabled.
- Linux container acceptance: **53 passed**, with SQLite, real PostgreSQL and real Redis.
  Lint/format passed; the migration service image also upgraded the temporary PostgreSQL DB.
  Provider calls remained mocked. Tests cover concurrent idempotency, claim/fencing, scanner
  overlap, duplicate request-start gating, actual Celery engine disposal, nonblocking provider
  waits, session release before LLM, API readiness and the original failure matrix.
- PostgreSQL Alembic upgrade/check/downgrade-to-base/upgrade passed on the isolated test DB.
  SQLite upgrade/current/check passed, including the existing DB path. No schema drift.
- New pinned dependencies: asyncpg 0.32.0 and pytest-asyncio 1.4.0; aiosqlite 0.21.0 is now direct.
- PostgreSQL fixtures create and drop only random test schemas. The dedicated test container
  uses tmpfs, does not share application data, and is removed after validation.
- Real OpenAI E2E remains unverified because no local provider key was configured.

## Initial MVP evidence (historical)

The core plan is retained. Corrections made during implementation:

1. The later user request supersedes Redis-only Compose: each service has a Dockerfile and
   a shell entrypoint; full stack is default. The user's existing Redis is used; bundled Redis
   is opt-in only (`bundled-redis` profile on alternate host port 6380).
2. One-shot Alembic migration avoids racing migrations across API and workers.
3. Worker heartbeat and completion's unexpired-lease predicate prevent expiry from being
   extended or committed by an already stale worker, including before the scanner runs.
4. SDK retries are disabled; a bounded CrewAI BaseLLM permits one SDK request. This avoids
   hidden agent/JSON repair calls multiplying provider spend. 429 retry is DB-driven and bounded.
5. Budget estimates are reserved before a request and retained for ambiguous outcomes.
   Manual retries count against call/attempt/dollar limits.
6. Strict provenance checks reject research quotes absent from the supplied text and
   classification matching signals absent from research evidence. These are not truth checks.
7. Native structured output and a direct trusted prompt are used within a single-task Crew;
   no tool use, web lookup, memory or delegation is enabled. Crew output streams are suppressed
   because upstream error paths may print raw responses even with verbose disabled.

## Automated failure matrix

| Plan | Coverage |
|---|---|
| T01/T02 | `test_api_contracts`, `test_idempotency` |
| T03 | `test_checkpoint_resume_and_version_change` |
| T04/T05/T06 | `test_ambiguous_crash_never_auto_retried`, `test_timeout_and_budget` |
| T07 | `test_concurrent_claim_and_fencing`, `test_manual_retry_auth_ack_and_fence` |
| T08 | `test_concurrent_deliveries` |
| T09 | `test_enqueue_gap_and_limits`, `test_enqueue_gap_recovery_and_retention` |
| T10/T11 | `test_score_crash_and_scanner_overlap` |
| T12 | `test_429_bounded_backoff` and mocked actual SDK/Crew rejection tests |
| T13 | `test_checkpoint_resume_and_version_change` |
| T14 | `test_timeout_and_budget` |

Calls are counted and compared across resume/redelivery. Automated tests use no provider
key, and the CrewAI test runs the actual Crew against a mocked SDK response.

## Real-provider acceptance

Not run until a local OpenAI key is configured. Do not paste secrets into chat.
Run `python scripts/run_demo.py --real-provider --acknowledge-cost` after starting services.
For a checkpoint crash demonstration, set `FAILURE_POINT=checkpoint`,
`FAILURE_STEP=RESEARCH` in `.env`, recreate `ai-worker`, submit a new job, observe exit 86
and the RESEARCH checkpoint, clear FAILURE_POINT and recreate the worker. Beat discovers
remaining PENDING work. Research's attempt count must remain 1.

For ambiguous outcomes set `FAILURE_POINT=request_started` or `response` instead. Clear
the hook and wait for the last heartbeat + lease TTL + scanner interval. Expect
UNKNOWN/NEEDS_REVIEW, never automatic re-invocation. Retry only after explicit acknowledgement.
Failure hooks cannot be enabled outside `APP_ENV=development`.

## Dependencies

The complete cross-platform pin set is `uv.lock`. Initial installed core versions:
Python 3.13.13, CrewAI 1.15.28, FastAPI 0.143.2, Celery 5.6.3,
SQLAlchemy 2.0.54, Alembic 1.20.0, OpenAI SDK 2.54.0, Redis Python 6.4.0,
Pydantic 2.12.5, pydantic-settings 2.16.0, pytest 9.1.1, Ruff 0.17.1.
CrewAI public APIs and the actual installed BaseLLM/Agent/Task signatures were inspected.
Its upstream deprecation warnings are visible in the test report.

## Execution evidence

- External Redis `localhost:6379`: PING passed.
- Phase 1: Alembic upgrade/current passed; 3 repository tests passed.
- Phase 2: 5 cumulative DB/API tests passed.
- Phase 3: 16 cumulative tests passed, including actual Crew with mocked SDK.
- Phases 4–5: 20 cumulative tests passed, including task boundary and recovery.
- Final Linux/container results are recorded after validation below.

Final acceptance on 2026-10-16:

- Windows Python 3.13.13: **24 passed**, including the opt-in real Redis/Celery test.
- Linux container Python 3.13.16: **24 passed**, including real Redis through the host gateway.
- Ruff lint and format checks passed on both platforms. Alembic check found no schema drift.
- Every application service Dockerfile built; migration exited successfully; API `/ready`
  returned ready; AI and maintenance workers connected to existing external Redis.
- Beat sent both periodic maintenance tasks and the maintenance worker completed them.
- CrewAI produced 26 upstream deprecation warnings; there were no test failures.
- SQLAlchemy bound parameters are hidden in database exceptions; repository tests were rerun
  after this privacy hardening and all 3 passed.
- No real OpenAI requests were made: local OPENAI_API_KEY is absent. Phase 6 remains pending.

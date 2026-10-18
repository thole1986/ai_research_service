"""Exercise the actual additive migration against historical SQLite rows."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_llm_migration_preserves_legacy_rows_and_has_no_schema_drift(tmp_path):
    database = tmp_path / "migration.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "LLM_ROUTING_JSON": "",
        "FAILURE_POINT": "",
    }

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    alembic("upgrade", "26a1018_tavily_collection")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO ai_jobs (id,idempotency_key,request_hash,status,input_json,"
            "total_estimated_cost_usd,created_at,updated_at,workflow_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                "legacy-key",
                "legacy-hash",
                "PENDING",
                json.dumps({"company_name": "Acme"}),
                0.1234,
                "2026-10-17 00:00:00",
                "2026-10-17 00:00:00",
                "manual-v1",
            ),
        )
        connection.execute(
            "INSERT INTO ai_steps (id,job_id,step_name,status,attempt_count,updated_at) "
            "VALUES ('step','legacy','RESEARCH','COMPLETED',1,'2026-10-17 00:00:00')"
        )
        connection.execute(
            "INSERT INTO ai_step_attempts (id,step_id,lease_token,attempt_no,status,"
            "estimated_cost_usd,created_at) "
            "VALUES ('attempt','step','token',1,'SUCCEEDED',0.1234,'2026-10-17 00:00:00')"
        )
    alembic("upgrade", "head")
    assert "No new upgrade operations detected" in alembic("check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT estimated_cost_usd FROM ai_step_attempts").fetchone() == (
            0.1234,
        )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,total_estimated_cost_usd,execution_config,execution_identity "
            "FROM ai_jobs WHERE id='legacy'"
        ).fetchone()
        assert row == ("PENDING", 0.1234, None, None)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_step_attempts)")}
        assert {
            "provider",
            "model",
            "profile_id",
            "config_fingerprint",
            "pricing_kind",
            "adapter_version",
        } <= columns
    alembic("downgrade", "26a1018_tavily_collection")
    alembic("upgrade", "head")
    assert "No new upgrade operations detected" in alembic("check")

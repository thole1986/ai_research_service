"""Pin per-job LLM routes and audit per-attempt execution identity."""

import sqlalchemy as sa

from alembic import op

revision = "26a1018_llm_execution_config"
down_revision = "26a1018_tavily_collection"
branch_labels = None
depends_on = None

ATTEMPT_COLUMNS = {
    "provider": 40,
    "model": 256,
    "profile_id": 128,
    "config_fingerprint": 64,
    "pricing_kind": 40,
    "adapter_version": 80,
}


def upgrade():
    for name in ("execution_config", "execution_identity"):
        op.add_column("ai_jobs", sa.Column(name, sa.JSON(), nullable=True))
    for name, length in ATTEMPT_COLUMNS.items():
        op.add_column("ai_step_attempts", sa.Column(name, sa.String(length), nullable=True))


def downgrade():
    # These nullable columns have no constraints. Native DROP COLUMN also avoids
    # rebuilding a parent table referenced by live SQLite step/event rows.
    for name in reversed(ATTEMPT_COLUMNS):
        op.drop_column("ai_step_attempts", name)
    op.drop_column("ai_jobs", "execution_identity")
    op.drop_column("ai_jobs", "execution_config")

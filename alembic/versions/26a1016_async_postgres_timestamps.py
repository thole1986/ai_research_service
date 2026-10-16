"""Use timezone-aware PostgreSQL timestamps; retain SQLite's UTC convention."""

import sqlalchemy as sa

from alembic import op

revision = "26a1016_async_postgres"
down_revision = "458d244e33c7"
branch_labels = None
depends_on = None

TIMESTAMPS = {
    "ai_jobs": ("created_at", "updated_at", "completed_at"),
    "ai_steps": (
        "lease_expires_at",
        "last_heartbeat_at",
        "started_at",
        "completed_at",
        "updated_at",
        "next_retry_at",
    ),
    "ai_step_attempts": ("llm_started_at", "created_at", "finished_at"),
    "ai_job_events": ("created_at",),
}


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table, columns in TIMESTAMPS.items():
            for column in columns:
                op.alter_column(
                    table,
                    column,
                    type_=sa.DateTime(timezone=True),
                    postgresql_using=f"{column} AT TIME ZONE 'UTC'",
                )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table, columns in TIMESTAMPS.items():
            for column in columns:
                op.alter_column(
                    table,
                    column,
                    type_=sa.DateTime(timezone=False),
                    postgresql_using=f"{column} AT TIME ZONE 'UTC'",
                )

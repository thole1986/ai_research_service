"""Version workflows and audit external collection separately from LLM calls."""

import sqlalchemy as sa

from alembic import op

revision = "26a1018_tavily_collection"
down_revision = "26a1016_async_postgres"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_jobs",
        sa.Column("workflow_version", sa.String(40), nullable=False, server_default="manual-v1"),
    )
    op.add_column(
        "ai_step_attempts",
        sa.Column(
            "collection_started_at",
            sa.DateTime(timezone=op.get_bind().dialect.name == "postgresql"),
            nullable=True,
        ),
    )
    op.add_column("ai_step_attempts", sa.Column("collection_usage", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("ai_step_attempts") as batch:
        batch.drop_column("collection_usage")
        batch.drop_column("collection_started_at")
    with op.batch_alter_table("ai_jobs") as batch:
        batch.drop_column("workflow_version")

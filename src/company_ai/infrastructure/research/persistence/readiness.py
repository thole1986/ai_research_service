from sqlalchemy import select

from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.research.persistence.models import Attempt, Event, Job, Step


async def research_database_ready(database: Database) -> None:
    await database.ready()
    # Context-specific schema probes belong to the adapter, not shared DB plumbing.
    async with database.session() as session:
        for model in (Job, Step, Attempt, Event):
            await session.execute(select(model.id).limit(1))

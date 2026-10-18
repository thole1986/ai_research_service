from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from company_ai.application.research.admin_queries import (
    AttemptFilters,
    EventFilters,
    JobFilters,
    Page,
    Paging,
)
from company_ai.application.research.admin_service import ResearchAdminService
from company_ai.application.research.jobs import JobNotFound


async def test_admin_use_cases_only_delegate_to_reader():
    reader = AsyncMock()
    service = ResearchAdminService(reader)
    page = Paging(2, 10)
    for method, args in (
        ("list_jobs", (JobFilters(status="NEEDS_REVIEW"), page)),
        ("get_job_detail", ("job",)),
        ("list_job_events", ("job", EventFilters("JOB_CREATED"), page)),
        ("list_job_attempts", ("job", AttemptFilters("COLLECT", "UNKNOWN"), page)),
        ("get_metrics", ()),
    ):
        result = Page((), 0, 2, 10)
        getattr(reader, method).return_value = result
        assert await getattr(service, method)(*args) is result
        getattr(reader, method).assert_awaited_once_with(*args)
    assert set(service.__dict__) == {"reader"}


@pytest.mark.parametrize(
    "method,args",
    [
        ("get_job_detail", ("missing",)),
        ("list_job_events", ("missing", EventFilters(), Paging())),
        ("list_job_attempts", ("missing", AttemptFilters(), Paging())),
    ],
)
async def test_missing_jobs_use_existing_application_error(method, args):
    reader = AsyncMock()
    getattr(reader, method).return_value = None
    with pytest.raises(JobNotFound):
        await getattr(ResearchAdminService(reader), method)(*args)


def test_filters_normalize_trim_and_utc():
    local = datetime(2026, 10, 18, 7, tzinfo=timezone(timedelta(hours=7)))
    filters = JobFilters(company_name="  Công ty Sen  ", created_from=local)
    assert filters.company_name == "Công ty Sen"
    assert filters.created_from == local.astimezone(UTC)
    assert filters.created_from.hour == 0
    with pytest.raises(ValueError):
        JobFilters(created_from=datetime(2026, 10, 18))
    with pytest.raises(ValueError):
        JobFilters(created_from=local, created_to=local - timedelta(seconds=1))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Paging(-1, 1),
        lambda: Paging(0, 501),
        lambda: Paging(0, 0),
        lambda: JobFilters(status="UNKNOWN"),
        lambda: JobFilters(company_name="  "),
        lambda: JobFilters(company_name="x" * 201),
        lambda: EventFilters("x" * 81),
        lambda: AttemptFilters(step_name="OTHER"),
        lambda: AttemptFilters(status="COMPLETED"),
    ],
)
def test_invalid_filters_and_pages(factory):
    with pytest.raises(ValueError):
        factory()


async def test_job_list_has_stricter_page_bound():
    reader = AsyncMock()
    with pytest.raises(ValueError):
        await ResearchAdminService(reader).list_jobs(JobFilters(), Paging(0, 101))
    reader.list_jobs.assert_not_awaited()

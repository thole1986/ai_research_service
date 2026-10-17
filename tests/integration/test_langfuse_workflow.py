import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from company_ai.application.execute_workflow import run_workflow
from company_ai.domain.contracts import InjectedCrash
from company_ai.infrastructure.ai.llm_adapter import CrewAIAdapter
from company_ai.infrastructure.telemetry.langfuse import LangfuseObserver
from tests.fakes import CountingAdapter


@pytest.mark.parametrize("valid_classification", [True, False])
@pytest.mark.parametrize("capture_content", [False, True])
async def test_actual_langfuse_sdk_traces_workflow_without_network(
    repo, payload, monkeypatch, valid_classification, capture_content
):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(
        public_key=f"pk-lf-test-{uuid4()}",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        environment="test",
        tracer_provider=provider,
        span_exporter=exporter,
    )
    observer = LangfuseObserver(
        client,
        provider,
        capture_content=capture_content,
        propagate_attributes=langfuse.propagate_attributes,
    )
    fake = CountingAdapter()

    async def create(**kwargs):
        step = {
            "ResearchOutput": "RESEARCH",
            "ClassificationOutput": "CLASSIFY",
            "ReportOutput": "REPORT",
        }[kwargs["response_format"]["json_schema"]["name"]]
        result = await fake.execute(step, json.loads(kwargs["messages"][1]["content"]))
        if step == "CLASSIFY" and not valid_classification:
            result.output["matching_signals"] = ["PRIVATE UNSUPPORTED SIGNAL"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(result.output),
                        refusal=None,
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            _request_id="req-test",
        )

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    try:
        job, _ = await repo.create_job_idempotently("langfuse-memory", payload)
        await run_workflow(
            job.id, repo, CrewAIAdapter(repo.settings, sdk, observer), repo.settings, observer
        )
        assert client.get_current_trace_id() is None
        observer.flush()
        spans = exporter.get_finished_spans()
        state, stored_steps = await repo.get_job(job.id)
        assert state.status == ("COMPLETED" if valid_classification else "NEEDS_REVIEW")
        assert fake.calls == (
            ["RESEARCH", "CLASSIFY", "REPORT"]
            if valid_classification
            else (["RESEARCH", "CLASSIFY"])
        )
        assert len(spans) == (8 if valid_classification else 5)
        root = next(s for s in spans if s.name == "company-research")
        assert root.parent is None
        assert sum(s.name == "company-research" for s in spans) == 1
        assert root.attributes["langfuse.observation.status_message"] == (
            "COMPLETED" if valid_classification else "UNSUPPORTED_CLASSIFICATION_SIGNAL"
        )
        if capture_content:
            assert json.loads(root.attributes["langfuse.observation.input"]) == payload
            output = json.loads(root.attributes["langfuse.observation.output"])
            assert output["status"] == state.status
            if valid_classification:
                assert output["result"] == state.result_json
            else:
                assert output["error"]["code"] == "UNSUPPORTED_CLASSIFICATION_SIGNAL"
                assert list(output["result"]) == ["research"]
        steps = (
            ("RESEARCH", "CLASSIFY", "SCORE", "REPORT")
            if valid_classification
            else ("RESEARCH", "CLASSIFY")
        )
        for step_name in steps:
            step = next(s for s in spans if s.name == step_name)
            assert step.parent.span_id == root.context.span_id
            assert step.attributes["session.id"] == job.id
            if capture_content:
                step_input = json.loads(step.attributes["langfuse.observation.input"])
                step_output = json.loads(step.attributes["langfuse.observation.output"])
                if step_name == "CLASSIFY" and not valid_classification:
                    assert step_output["matching_signals"] == ["PRIVATE UNSUPPORTED SIGNAL"]
                else:
                    assert (
                        step_output
                        == next(s for s in stored_steps if (s.step_name == step_name)).output_json
                    )
                if step_name == "RESEARCH":
                    assert step_input["company_description"] == payload["company_description"]
                if step_name == "CLASSIFY":
                    assert step_input["icp"] == payload["icp"]
                if step_name == "SCORE":
                    assert "research" in step_input and "classification" in step_input
                if step_name == "REPORT":
                    assert step_input["company"] == payload
            if step_name != "SCORE":
                generation = next(
                    s for s in spans if s.parent and s.parent.span_id == (step.context.span_id)
                )
                assert generation.name == "openai.chat.completions"
                usage = json.loads(generation.attributes["langfuse.observation.usage_details"])
                assert usage == {"input": 100, "output": 50}
                parameters = json.loads(
                    generation.attributes["langfuse.observation.model.parameters"]
                )
                assert parameters["max_tokens"] == repo.settings.max_output_tokens_per_step
                assert generation.attributes["session.id"] == job.id
                assert (
                    generation.attributes["langfuse.trace.metadata.attempt_id"]
                    == (step.attributes["langfuse.trace.metadata.attempt_id"])
                )
                if capture_content:
                    assert json.loads(generation.attributes["langfuse.observation.input"])
                    assert json.loads(generation.attributes["langfuse.observation.output"])
        if not capture_content:
            assert payload["company_description"] not in str([s.attributes for s in spans])
            assert "PRIVATE" not in str([s.attributes for s in spans])
            assert not any(
                "langfuse.observation.input" in s.attributes
                or ("langfuse.observation.output" in s.attributes)
                for s in spans
            )
        assert not any("exception" == event.name for s in spans for event in s.events)
    finally:
        client.shutdown()


async def test_resume_exports_cached_input_output_without_repeating_llm(repo, payload):
    records = []

    @contextmanager
    def start(**fields):
        record = fields.copy()
        records.append(record)
        yield SimpleNamespace(update=lambda **updates: record.update(updates))

    observer = LangfuseObserver(
        SimpleNamespace(start_as_current_observation=start),
        None,
        capture_content=True,
    )
    adapter = CountingAdapter()
    job, _ = await repo.create_job_idempotently("trace-resume", payload)
    repo.settings.failure_point = "checkpoint"
    repo.settings.failure_step = "RESEARCH"
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings, observer)
    repo.settings.failure_point = ""
    records.clear()

    await run_workflow(job.id, repo, adapter, repo.settings, observer)
    state, steps = await repo.get_job(job.id)
    assert state.status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    cached = next(record for record in records if record["name"] == "RESEARCH")
    assert cached["input"]["company_description"] == payload["company_description"]
    assert cached["output"] == steps[0].output_json
    assert cached["metadata"]["checkpoint_hit"] is True
    assert cached["status_message"] == "CHECKPOINT_HIT"
    root = records[0]
    assert root["input"] == payload
    assert root["output"]["result"] == state.result_json

    # Redelivery of a completed job still exposes its existing result on the root.
    records.clear()
    await run_workflow(job.id, repo, adapter, repo.settings, observer)
    assert len(records) == 1
    assert records[0]["output"]["result"] == state.result_json
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]

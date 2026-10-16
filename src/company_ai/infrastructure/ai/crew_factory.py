def make_crew(step, llm, settings, schema):
    from crewai import Agent, Crew, Process, Task

    agent = Agent(
        role=f"Company {step.lower()} analyst",
        goal="Return concise, evidence-only structured company analysis.",
        backstory="A careful analyst who quotes supplied sources and states uncertainty.",
        llm=llm,
        allow_delegation=False,
        tools=[],
        verbose=False,
        max_iter=settings.max_agent_iterations,
        max_retry_limit=0,
        respect_context_window=False,
        allow_code_execution=False,
    )
    task = Task(
        description="Analyze the supplied evidence using the configured structured provider.",
        expected_output="A JSON object conforming to the required output schema.",
        output_pydantic=schema,
        agent=agent,
        guardrail_max_retries=0,
    )
    return Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        memory=False,
        cache=False,
        tracing=False,
    )

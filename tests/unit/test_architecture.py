import ast
from pathlib import Path


def test_dependencies_point_toward_domain():
    package = Path(__file__).resolve().parents[2] / "src/company_ai"
    banned = {
        "domain": (
            "company_ai.application",
            "company_ai.infrastructure",
            "company_ai.api",
            "company_ai.bootstrap",
            "company_ai.main",
            "company_ai.runtime",
            "company_ai.tasks",
            "sqlalchemy",
            "fastapi",
            "crewai",
            "redis",
            "langfuse",
            "opentelemetry",
            "openai",
            "anthropic",
            "httpx",
        ),
        "application": (
            "company_ai.infrastructure",
            "company_ai.api",
            "company_ai.bootstrap",
            "company_ai.main",
            "company_ai.runtime",
            "company_ai.tasks",
            "sqlalchemy",
            "crewai",
            "fastapi",
            "redis",
            "langfuse",
            "opentelemetry",
            "openai",
            "anthropic",
            "httpx",
        ),
        "api": (
            "company_ai.infrastructure",
            "company_ai.bootstrap",
            "company_ai.main",
            "company_ai.runtime",
            "company_ai.tasks",
            "sqlalchemy",
            "crewai",
            "redis",
            "langfuse",
            "opentelemetry",
        ),
    }
    for layer, forbidden in banned.items():
        for path in (package / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                for module in modules:
                    assert not module.startswith(forbidden), f"{path}: forbidden import {module}"

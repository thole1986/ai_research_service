FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    CREWAI_TELEMETRY_DISABLED=true OTEL_SDK_DISABLED=true CREWAI_STORAGE_DIR=/app/data/crewai
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.13
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
RUN useradd --uid 10001 --create-home app && mkdir -p /app/data && chown -R app:app /app/data \
    && chmod +x scripts/*.sh
USER app
ENTRYPOINT ["/bin/sh", "/app/scripts/start-migrate.sh"]

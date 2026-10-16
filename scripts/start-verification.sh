#!/bin/sh
set -eu
ruff check --no-cache .
ruff format --no-cache --check .
exec pytest -q --basetemp=/app/data/pytest -o cache_dir=/app/data/pytest-cache

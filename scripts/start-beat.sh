#!/bin/sh
set -eu
mkdir -p data
exec celery -A company_ai.celery_app:celery_app beat --loglevel=INFO --schedule="${BEAT_SCHEDULE_FILE:-./data/celerybeat-schedule}"

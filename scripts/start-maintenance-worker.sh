#!/bin/sh
set -eu
exec celery -A company_ai.celery_app:celery_app worker -Q maintenance --pool=solo --concurrency=1 --hostname=maintenance@%h --loglevel=INFO --without-gossip --without-mingle

#!/bin/sh
set -eu
exec celery -A company_ai.celery_app:celery_app worker -Q ai --pool=solo --concurrency=1 --hostname=ai@%h --loglevel=INFO --without-gossip --without-mingle

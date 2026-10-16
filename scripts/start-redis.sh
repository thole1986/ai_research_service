#!/bin/sh
set -eu
exec redis-server --appendonly yes

#!/usr/bin/env bash
# Cron wrapper: runs the scanner with the project venv, prevents overlapping runs, logs to logs/cron.log.
set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
export TZ=Asia/Taipei
exec /usr/bin/flock -n ./logs/.scanner.lock ./.venv/bin/python main.py "$@" >> logs/cron.log 2>&1

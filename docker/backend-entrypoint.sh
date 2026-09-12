#!/bin/sh
set -eu

mkdir -p /app/.data/media

python -m video_director.cli worker --forever &
worker_pid=$!

shutdown() {
  kill "$worker_pid" 2>/dev/null || true
  wait "$worker_pid" 2>/dev/null || true
}
trap shutdown INT TERM EXIT

exec uvicorn video_director.api:app --host 0.0.0.0 --port 8000

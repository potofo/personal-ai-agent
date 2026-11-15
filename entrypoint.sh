#!/bin/sh
# Container entrypoint (PID 1 is tini, see Dockerfile).
# - Starts the Coder agent in the background when Coder supplies its init script,
#   restarting it if it exits.
# - Runs the FastAPI app in the foreground; if it dies the container exits and
#   Docker's restart policy brings it back.
set -eu

if [ -n "${CODER_AGENT_INIT_SCRIPT:-}" ]; then
  (
    while true; do
      sh -c "$CODER_AGENT_INIT_SCRIPT" || echo "coder agent exited ($?), restarting in 5s" >&2
      sleep 5
    done
  ) &
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'

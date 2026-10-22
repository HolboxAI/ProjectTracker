#!/bin/sh
# The full HTTP suite in one command: start a server on a spare port against a
# throwaway database, create the account the checks sign in as, run them, then
# stop the server.
#
#   sh tests/run_http_check.sh
#   PORT=8100 sh tests/run_http_check.sh
#
# Nothing here touches tracker.sqlite3.

set -eu

PORT="${PORT:-8099}"
BASE="http://127.0.0.1:$PORT"
WORKDIR="$(mktemp -d)"
DB="$WORKDIR/http-check.sqlite3"
USERNAME="${COVEX_TEST_USERNAME:-check-runner}"
PASSWORD="${COVEX_TEST_PASSWORD:-check-the-gate-9}"

cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

python3 server.py --db "$DB" --port "$PORT" >"$WORKDIR/server.log" 2>&1 &
SERVER_PID=$!

# Wait for the port to answer rather than guessing at a sleep.
i=0
while [ "$i" -lt 50 ]; do
  if python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$BASE/login', timeout=1).read()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.2
done

python3 server.py --db "$DB" --add-user --name "Check Runner" \
  --username "$USERNAME" --password "$PASSWORD" --admin >/dev/null

python3 tests/http_check.py "$BASE" --user "$USERNAME" --password "$PASSWORD"

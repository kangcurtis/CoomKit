#!/usr/bin/env bash
# Stop any running CoomKit and start a fresh one in the background.
#
# This used to `kill $(cat pidfile); sleep 0.4; python3 server.py &` and call it
# done. Two ways that lies to you, both of which have cost real debugging time:
#   - 0.4s is not always enough for the port to be released, so the replacement
#     dies with EADDRINUSE while the OLD process keeps serving OLD code;
#   - the pidfile goes stale (crash, manual kill) while something still holds
#     the port, with the same result.
# Either way the pidfile ends up naming a corpse, the server answers normally,
# and you debug a fix that never actually ran. So: wait for the old process to
# really die, reclaim the port from whoever holds it, and refuse to report
# success until /api/health answers.
set -uo pipefail
cd "$(dirname "$0")"

PIDFILE=".coomkit.pid"
PORT="${COOM_PORT:-3939}"

stop() {                        # TERM, then KILL if it will not go
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in $(seq 40); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null
  sleep 0.2
}

if [[ -f "$PIDFILE" ]]; then
  stop "$(cat "$PIDFILE" 2>/dev/null)"
fi

holder="$(ss -ltnpH "sport = :$PORT" 2>/dev/null \
          | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
if [[ -n "${holder:-}" ]]; then
  echo "port $PORT still held by pid $holder — stopping that too"
  stop "$holder"
fi

nohup python3 server.py > coomkit.log 2>&1 &
newpid=$!
echo "$newpid" > "$PIDFILE"

for _ in $(seq 60); do
  if ! kill -0 "$newpid" 2>/dev/null; then
    echo "CoomKit failed to start — last lines of coomkit.log:" >&2
    tail -6 coomkit.log >&2
    exit 1
  fi
  if curl -fsS -m 1 -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
    echo "CoomKit restarted (pid $newpid)"
    exit 0
  fi
  sleep 0.1
done

echo "CoomKit (pid $newpid) did not answer /api/health within 6s:" >&2
tail -6 coomkit.log >&2
exit 1

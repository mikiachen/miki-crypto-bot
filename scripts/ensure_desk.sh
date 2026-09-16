#!/usr/bin/env bash
# Bring the Streamlit desk back if :8501 is down. Never kills a healthy process.
# Detached (nohup) so an aborted agent shell cannot take the page down with it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8501}"
LOG="${ROOT}/grok-trading-desk/logs/desk_ui.log"
mkdir -p "$(dirname "$LOG")"

if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "desk already up  http://127.0.0.1:${PORT}"
  exit 0
fi

echo "desk down — starting in a new session"
# Double-fork + setsid so the page is not a child of this shell.
# A short-lived launcher (agent tool, aborted restart) must not take :8501 down with it.
python3 - "${ROOT}" "${PORT}" "${LOG}" <<'PY'
import os, sys
root, port, log = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(log), exist_ok=True)
pid = os.fork()
if pid > 0:
    os.waitpid(pid, 0)
    raise SystemExit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
dn = os.open("/dev/null", os.O_RDONLY)
os.dup2(dn, 0)
os.chdir(root)
os.execv("/bin/bash", ["bash", os.path.join(root, "scripts/run_arc_desk.sh"), "--port", port])
PY

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "up  http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 0.4
done
echo "still down — tail ${LOG}"
tail -n 40 "${LOG}" || true
exit 1

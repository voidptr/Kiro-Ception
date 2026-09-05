#!/usr/bin/env bash
# dev-engine — run the Kiro-Ception engine headless (no Kiro), on demand.
#
# Targets the isolated test instance in config.copilot-test.toml (its own
# cache_dir + engine_port). Loopback requests skip peer encryption, so
# status/search/rescan talk plain JSON.
#
# Verbs:
#   start    Background, detached. Idempotent (refuses if already up). Records
#            BOTH the follower and engine_main child PID to .dev-engine.pid.
#   stop     Stop exactly the recorded instance (engine child + follower).
#   down     Deliberate SWEEP: pkill every process matching config.copilot-test.toml
#            (followers, engines). Scoped to the instance-unique config name, so
#            production kiro-ception-rearview engines are never touched.
#   restart  stop then start.
#   up       Foreground (Ctrl+C to stop).
#   status | config | search "<q>" [source] | rescan
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="$REPO_ROOT/config.copilot-test.toml"
PID_FILE="$REPO_ROOT/.dev-engine.pid"
OUT_LOG="$REPO_ROOT/.dev-engine.out.log"
ERR_LOG="$REPO_ROOT/.dev-engine.err.log"
DEBUG_PY="$REPO_ROOT/scripts/debug-engine.py"
CONFIG_MATCH="config.copilot-test.toml"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config not found: $CONFIG_PATH" >&2
  exit 1
fi

PORT="$(grep -E '^\s*engine_port\s*=' "$CONFIG_PATH" | head -n1 | grep -oE '[0-9]+' || echo 19766)"
BASE="http://127.0.0.1:$PORT"

engine_child_pid() {
  # The engine_main process for THIS instance, matched by the unique config name.
  pgrep -f "engine_main.*${CONFIG_MATCH}" | head -n1 || true
}

port_answering() {
  curl -sf -o /dev/null --max-time 3 "$BASE/status"
}

start_detached() {
  if [[ -f "$PID_FILE" ]]; then
    local f; f="$(head -n1 "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$f" ]] && kill -0 "$f" 2>/dev/null; then
      echo "[dev-engine] already running (follower PID $f). Use 'restart' or 'stop'."
      return
    fi
  fi
  if port_answering; then
    echo "[dev-engine] port $PORT already answering — an instance is up. Use 'down' to clear stragglers first."
    return
  fi
  nohup uv run --directory "$REPO_ROOT" python "$DEBUG_PY" \
    --config "$CONFIG_PATH" --interval "${INTERVAL:-30}" >"$OUT_LOG" 2>"$ERR_LOG" &
  local follower=$!
  # Record follower and RETURN IMMEDIATELY — do NOT poll for the engine child
  # (it only spawns after a slow model preload; polling would block 'start').
  # 'stop'/'down' derive the engine PID at teardown by config-name match.
  printf '%s\n\n' "$follower" > "$PID_FILE"
  echo "[dev-engine] started detached — follower PID $follower, port $PORT (engine child spawns after model preload)"
  echo "[dev-engine] pid file: $PID_FILE  |  logs: $OUT_LOG / $ERR_LOG"
}

stop_recorded() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "[dev-engine] no .dev-engine.pid recorded. Use 'down' to sweep stragglers."
    return
  fi
  local follower engine
  follower="$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)"
  engine="$(sed -n '2p' "$PID_FILE" 2>/dev/null || true)"
  [[ -z "$engine" ]] && engine="$(engine_child_pid)"   # derive if not captured
  for id in "$engine" "$follower"; do
    if [[ -n "$id" ]] && kill -0 "$id" 2>/dev/null; then
      kill "$id" 2>/dev/null || true
      echo "[dev-engine] stopped PID $id"
    elif [[ -n "$id" ]]; then
      echo "[dev-engine] PID $id already gone"
    fi
  done
  rm -f "$PID_FILE"
  sleep 2
  if port_answering; then echo "[dev-engine] WARNING: port $PORT still answering — run 'down'."; else echo "[dev-engine] confirmed: port $PORT no longer answering."; fi
}

down_sweep() {
  # Deliberate reaper: everything matching the instance-unique config name.
  # Never matches production engines (different --config path).
  if pgrep -f "$CONFIG_MATCH" >/dev/null 2>&1; then
    pkill -f "$CONFIG_MATCH" 2>/dev/null || true
    echo "[dev-engine] down: killed all processes matching $CONFIG_MATCH"
  else
    echo "[dev-engine] down: no copilot-test processes found."
  fi
  rm -f "$PID_FILE"
  sleep 2
  if port_answering; then echo "[dev-engine] WARNING: port $PORT STILL answering after sweep."; else echo "[dev-engine] confirmed clean: port $PORT no longer answering."; fi
}

json_str() { python -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; }

cmd="${1:-}"; shift || true
case "$cmd" in
  start)   start_detached ;;
  stop)    stop_recorded ;;
  down)    down_sweep ;;
  restart) stop_recorded; sleep 2; start_detached ;;
  up)
    echo "[dev-engine] FOREGROUND on port $PORT — Ctrl+C to stop. Use 'start' for background."
    exec uv run --directory "$REPO_ROOT" python "$DEBUG_PY" --config "$CONFIG_PATH" --interval "${INTERVAL:-30}"
    ;;
  status) curl -s "$BASE/status" ;;
  config) curl -s "$BASE/config" ;;
  rescan) curl -s -X POST "$BASE/rescan" -H "Content-Type: application/json" -d '{}' ;;
  search)
    query="${1:-}"; source="${2:-}"
    if [[ -z "$query" ]]; then echo 'Usage: dev-engine.sh search "<query>" [cli|ide|claude|copilot]' >&2; exit 1; fi
    if [[ -n "$source" ]]; then
      body=$(printf '{"query":%s,"max_results":5,"source":"%s"}' "$(printf '%s' "$query" | json_str)" "$source")
    else
      body=$(printf '{"query":%s,"max_results":5}' "$(printf '%s' "$query" | json_str)")
    fi
    curl -s -X POST "$BASE/search" -H "Content-Type: application/json" -d "$body"
    ;;
  *)
    echo "Usage: dev-engine.sh {start|stop|down|restart|up|status|config|search|rescan}" >&2
    exit 1
    ;;
esac

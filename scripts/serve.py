"""Run a Kiro-Ception instance as a standalone, long-lived service.

Normally the engine is spawned by an MCP proxy process, which registers
itself as a follower. The engine shuts itself down if no follower ever
registers (orphan protection, 120s) or once every registered follower has
died — so simply launching `python -m kiro_ception.engine_main` yields an
engine that exits a couple of minutes later.

This supervisor is the missing follower. It spawns the engine if needed and
then holds it open by health-checking on an interval; EngineClient stamps
X-Follower-PID on every request, so each check re-registers this process.
When the supervisor exits, the engine notices its follower is gone and shuts
down cleanly on its own.

Usage:
    python serve.py --config /path/to/config.toml [--interval 30]
"""

import argparse
import signal
import sys
import threading
import time


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Kiro-Ception instance as a standalone service"
    )
    parser.add_argument(
        "--config", metavar="PATH", required=True, help="Path to config.toml"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between keepalive health checks (default: 30)",
    )
    parser.add_argument(
        "--startup-timeout",
        type=int,
        default=300,
        help=(
            "Seconds to wait for the engine to answer its first health check "
            "(default: 300). A cold start preloads torch and the embedding "
            "model, which can take minutes on some machines — far longer than "
            "the 30s ensure_engine_running() waits internally."
        ),
    )
    args = parser.parse_args()

    from kiro_ception.config import get_config, set_config_file

    set_config_file(args.config)
    config = get_config()

    from kiro_ception.engine_client import ensure_engine_running, get_engine_client

    cache_dir = config.embedding.cache_path
    log_path = config.engine_log_path
    print(f"[serve] config      : {args.config}", flush=True)
    print(f"[serve] cache_dir   : {cache_dir}", flush=True)
    print(f"[serve] engine_port : {config.server.engine_port}", flush=True)
    print(f"[serve] engine_log  : {log_path or 'disabled'}", flush=True)

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        print(f"[serve] received signal {signum}, stopping", flush=True)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Spawns the engine if it isn't already up. This returns False on a slow
    # cold start (it only waits 30s), which is not fatal — the engine is still
    # coming up, so we poll below rather than giving up.
    if not ensure_engine_running():
        print(
            "[serve] engine not healthy yet — waiting for cold start",
            flush=True,
        )

    client = get_engine_client()

    # Poll until the engine answers. Each attempt carries X-Follower-PID, so
    # the first successful call also registers us — which must happen inside
    # the engine's 120s orphan-protection window, timed from when its HTTP
    # server starts listening (after the slow preload, not before it).
    deadline = time.monotonic() + args.startup_timeout
    while not stop.is_set():
        if client.health():
            break
        if time.monotonic() >= deadline:
            print(
                f"[serve] engine did not become healthy within "
                f"{args.startup_timeout}s",
                file=sys.stderr,
                flush=True,
            )
            return 1
        stop.wait(3)

    if stop.is_set():
        return 0

    print("[serve] engine running — holding open (Ctrl+C to stop)", flush=True)

    consecutive_failures = 0
    while not stop.is_set():
        # health() registers this PID as a follower via the X-Follower-PID
        # header, which is what keeps the engine from self-terminating.
        if client.health():
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(
                f"[serve] health check failed ({consecutive_failures})", flush=True
            )
            # The engine may have died or been replaced after a code change.
            if consecutive_failures >= 2:
                print("[serve] respawning engine", flush=True)
                if ensure_engine_running():
                    consecutive_failures = 0
                else:
                    print("[serve] respawn failed", file=sys.stderr, flush=True)

        stop.wait(args.interval)

    print("[serve] stopped — engine will shut down once it notices", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

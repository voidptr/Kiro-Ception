"""DEVELOPMENT / DEBUGGING TOOL — hold an engine open with no MCP client.

YOU ALMOST CERTAINLY DO NOT NEED THIS.

In normal use the lifecycle is fully automatic: your editor spawns the MCP
proxy (`kiro-ception`), the proxy spawns the engine and registers itself as a
follower, and the engine shuts down when the last follower exits. Nothing has
to supervise it. Running this tool alongside a normal setup just adds a second
follower and a second process to clean up.

WHEN THIS IS ACTUALLY USEFUL
----------------------------
Only when there is no MCP client attached and you still want a live engine:

  * Verifying a new or reconfigured instance before registering it with an
    editor — e.g. confirming cache_dir/engine_port isolation, or that a config
    change took effect, by hitting /status and /config directly.
  * Headless or CI runs that exercise the HTTP API without an MCP client.
  * Watching indexing progress on a fresh instance without opening an editor.
  * Reproducing engine-side behaviour under a debugger, detached from the
    stdio proxy.

WHY IT IS NEEDED FOR THOSE CASES
--------------------------------
The engine deliberately refuses to outlive its clients. It exits once every
registered follower has died, and — to avoid orphans when a spawning client
dies early — also exits if no follower registers within 120 seconds. So
`python -m kiro_ception.engine_main` on its own gives you an engine that
disappears two minutes later.

This tool is the missing follower. It spawns the engine, then health-checks on
an interval; `EngineClient` stamps `X-Follower-PID` on every request, so each
check re-registers this process. Stop it (Ctrl+C) and the engine notices its
last follower is gone and shuts down cleanly on its own.

USAGE
-----
    python tools/debug_engine.py --config /path/to/config.toml [--interval 30]

    # then, from anywhere:
    curl -s http://127.0.0.1:<engine_port>/status
    curl -s http://127.0.0.1:<engine_port>/config
"""

import argparse
import signal
import sys
import threading
import time


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Development tool: hold a Kiro-Ception engine open with no MCP "
            "client attached. Not needed for normal use — an editor's MCP "
            "proxy already does this."
        )
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
            "Seconds to wait for the engine's first health check (default: "
            "300). Generous on purpose: a cold start preloads torch and the "
            "embedding model, which can far exceed the wait built into "
            "ensure_engine_running()."
        ),
    )
    args = parser.parse_args()

    from kiro_ception.config import get_config, set_config_file

    set_config_file(args.config)
    config = get_config()

    from kiro_ception.engine_client import ensure_engine_running, get_engine_client

    print(f"[debug-engine] config      : {args.config}", flush=True)
    print(f"[debug-engine] instance    : {config.instance_summary}", flush=True)
    print(f"[debug-engine] cache_dir   : {config.embedding.cache_path}", flush=True)
    print(f"[debug-engine] engine_port : {config.server.engine_port}", flush=True)
    print(
        f"[debug-engine] engine_log  : {config.engine_log_path or 'disabled'}",
        flush=True,
    )

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        print(f"[debug-engine] signal {signum} — stopping", flush=True)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Returns False on a slow cold start (it only waits a short while), which
    # is not fatal — the engine is still coming up, so poll below.
    if not ensure_engine_running():
        print("[debug-engine] not healthy yet — waiting for cold start", flush=True)

    client = get_engine_client()

    # Each attempt carries X-Follower-PID, so the first success also registers
    # us. That must happen inside the engine's 120s orphan window, which starts
    # when its HTTP server begins listening — i.e. after the slow preload.
    deadline = time.monotonic() + args.startup_timeout
    while not stop.is_set():
        if client.health():
            break
        if time.monotonic() >= deadline:
            print(
                f"[debug-engine] engine did not become healthy within "
                f"{args.startup_timeout}s",
                file=sys.stderr,
                flush=True,
            )
            return 1
        stop.wait(3)

    if stop.is_set():
        return 0

    port = config.server.engine_port
    print(
        f"[debug-engine] engine up — holding open (Ctrl+C to stop)\n"
        f"[debug-engine] dashboard: http://127.0.0.1:{port}/",
        flush=True,
    )

    consecutive_failures = 0
    while not stop.is_set():
        if client.health():
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(
                f"[debug-engine] health check failed ({consecutive_failures})",
                flush=True,
            )
            # The engine may have died, or been replaced after a code change.
            if consecutive_failures >= 2:
                print("[debug-engine] respawning engine", flush=True)
                if ensure_engine_running():
                    consecutive_failures = 0
                else:
                    print("[debug-engine] respawn failed", file=sys.stderr, flush=True)

        stop.wait(args.interval)

    print(
        "[debug-engine] stopped — engine will shut down once it notices",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

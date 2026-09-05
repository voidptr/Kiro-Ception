"""Standalone engine process for Kiro Ception.

This process owns:
- The file lock (engine.lock)
- The HTTP API server (ThreadingHTTPServer on the main thread)
- The background indexer (embeddings, SQLite cache)
- The in-memory search index (numpy matrix)

Architecture:
- Main thread: runs the HTTP server via serve_forever()
- Background thread: indexer (embedding + SQLite writes)
- Background thread: heartbeat (refreshes engine.json timestamp)

The MCP stdio process (kiro-ception) spawns this as a detached subprocess.
All DLLs are preloaded on the main thread before any background threads start,
preventing the Windows Loader Lock deadlock that occurs when scipy/OpenBLAS
native extensions are loaded from a background thread while other threads exist.

Usage:
    kiro-ception-engine --config /path/to/config.toml

Or via Python:
    python -m kiro_ception.engine_main --config /path/to/config.toml
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Track process start time for uptime reporting
_process_start_time = time.time()


def _log(msg: str, flush: bool = True):
    """Print a timestamped log message."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [kiro-ception] {msg}", flush=flush)



def _preload_native_extensions(backend_type: str):
    """Eagerly import native extensions that deadlock when loaded from background threads.

    On Windows, loading native DLLs (e.g., scipy's OpenBLAS) from a non-main thread
    can deadlock against the Windows Loader Lock when multiple threads are active.
    By importing these on the main thread before spawning any threads, the DLLs are
    already loaded and subsequent imports from the indexer thread are no-ops.
    """
    if sys.platform != "win32":
        return

    if backend_type != "sentence-transformers":
        return

    t0 = time.perf_counter()
    try:
        import scipy.interpolate  # noqa: F401
        import scipy.stats  # noqa: F401
        import sklearn  # noqa: F401
        import sentence_transformers  # noqa: F401
        elapsed = time.perf_counter() - t0
        print(f"Preloaded native extensions in {elapsed:.1f}s")
    except ImportError as e:
        elapsed = time.perf_counter() - t0
        print(f"Preload skipped ({e}) after {elapsed:.1f}s")


# This module is the engine process entrypoint, so import time is process
# start time. Captured once so engine.json can report a fixed started_at.
_PROCESS_START_TIME = time.time()


def _write_engine_info(port: int, pid: int, cache_dir: Path, started_at: float):
    """Write engine info atomically for followers to discover.

    started_at must be this engine's real start time and stay fixed for the
    life of the process. The heartbeat rewrites this file periodically; if it
    refreshed started_at too, a stale engine would keep looking freshly
    spawned and spawn_engine()'s "started_at > spawn_time" check — its only
    way to tell our spawn from a leftover one — would always pass.

    heartbeat_at carries the liveness signal instead.
    """
    info_path = cache_dir / "engine.json"
    info = {
        "port": port,
        "pid": pid,
        "parent_pid": os.getppid(),
        "started_at": started_at,
        "heartbeat_at": time.time(),
    }
    fd, tmp_path = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path).replace(info_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class FollowerRegistry:
    """Tracks registered follower PIDs and checks their liveness.

    Thread-safe: accessed from HTTP handler threads (register) and the main
    loop (has_live_followers, count). All access is protected by a lock.
    """

    def __init__(self):
        self._followers: dict[int, float] = {}  # pid → registration time
        self._lock = threading.Lock()

    def register(self, pid: int):
        """Register or refresh a follower PID."""
        with self._lock:
            is_new = pid not in self._followers
            self._followers[pid] = time.time()
        if is_new:
            print(f"Follower registered: pid={pid} (total: {len(self._followers)})")

    def has_live_followers(self) -> bool:
        """Check if any registered followers are still alive.

        Removes dead PIDs from the registry as a side effect.
        """
        with self._lock:
            if not self._followers:
                return False

            dead_pids = []
            for pid in self._followers:
                if not _is_follower_alive(pid):
                    dead_pids.append(pid)

            for pid in dead_pids:
                del self._followers[pid]
                print(
                    f"Follower deregistered (dead): pid={pid} "
                    f"(remaining: {len(self._followers)})"
                )

            return len(self._followers) > 0

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._followers)


def _is_follower_alive(pid: int) -> bool:
    """Check if a follower PID is still running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _build_request_handler(search_handler, config_handler, indexer_getter, follower_registry, startup_fingerprint):
    """Build the HTTP request handler class with closures over the handlers."""

    class EngineRequestHandler(BaseHTTPRequestHandler):
        """HTTP handler for the engine API."""

        def log_message(self, format, *args):
            """Suppress default HTTP logging."""
            pass

        def _send_json(self, data: dict, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _send_response_maybe_encrypted(self, data: dict, status: int = 200):
            """Send response, encrypting if peer secret is configured."""
            try:
                from .peers import encrypt_response_body
                body_bytes, content_type = encrypt_response_body(data)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body_bytes)
            except ImportError:
                self._send_json(data, status)

        def _track_follower(self):
            """Extract X-Follower-PID header and register the follower."""
            pid_header = self.headers.get("X-Follower-PID")
            if pid_header:
                try:
                    follower_registry.register(int(pid_header))
                except (ValueError, TypeError):
                    pass

        def do_POST(self):
            self._track_follower()
            try:
                content_type = self.headers.get("Content-Type", "application/json")
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"

                # Decrypt if encrypted (only enforce for non-loopback peers)
                client_ip = self.client_address[0] if self.client_address else None
                is_loopback = client_ip in ("127.0.0.1", "::1", None)

                if not is_loopback:
                    _log(f"Incoming POST {self.path} from {client_ip} (content_type={content_type}, len={length})")

                try:
                    from .peers import decrypt_request_body
                    if is_loopback and content_type != "application/x-kiroception-encrypted":
                        # Local MCP proxy — skip encryption check
                        body = json.loads(raw_body) if raw_body else {}
                    else:
                        body = decrypt_request_body(raw_body, content_type)
                except PermissionError as e:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                    return
                except ImportError:
                    body = json.loads(raw_body) if raw_body else {}

                if self.path == "/search":
                    if not is_loopback:
                        body["from_peer"] = True
                    result = search_handler(body)
                    if is_loopback:
                        self._send_json(result)
                    else:
                        self._send_response_maybe_encrypted(result)

                elif self.path == "/reindex":
                    indexer = indexer_getter()
                    indexer.trigger_reindex()
                    self._send_json({"status": "reindex_triggered"})

                elif self.path == "/rescan":
                    indexer = indexer_getter()
                    indexer.trigger_rescan()
                    self._send_json({"status": "rescan_triggered"})

                elif self.path == "/reload-config":
                    from .config import reload_config as _reload, diff_configs
                    old_config, new_config = _reload()
                    changes = diff_configs(old_config, new_config)
                    safe_changes = [c for c in changes if c["impact"] == "safe"]
                    breaking_changes = [c for c in changes if c["impact"] == "requires_reindex"]
                    self._send_json({
                        "status": "config_reloaded" if changes else "no_changes",
                        "changes": changes,
                        "applied": [c["key"] for c in safe_changes],
                        "deferred": [
                            f"{c['key']} — requires rescan(full=True)"
                            for c in breaking_changes
                        ],
                        "warnings": [
                            f"{c['key']} changed: {c['old']} → {c['new']}. "
                            f"Requires rescan(full=True)."
                            for c in breaking_changes
                        ],
                    })

                else:
                    self._send_json({"error": "not found"}, 404)

            except Exception as e:
                _log(f"POST {self.path} error from {self.client_address}: {traceback.format_exc()}")
                self._send_json({"error": str(e)}, 500)

        def do_GET(self):
            self._track_follower()
            try:
                if self.path == "/":
                    self._send_dashboard()

                elif self.path == "/status":
                    indexer = indexer_getter()
                    status = indexer.status.to_dict()

                    from .search import get_search_index
                    from .migrations import get_schema_version

                    search_index = get_search_index()
                    status["search_ready"] = search_index.message_count > 0
                    status["search_message_count"] = search_index.message_count

                    if indexer.cache:
                        try:
                            db_path = Path(indexer.cache.db_path) if not isinstance(
                                indexer.cache.db_path, Path
                            ) else indexer.cache.db_path
                            status["db_size_mb"] = round(
                                db_path.stat().st_size / (1024 * 1024), 2
                            ) if db_path.exists() else 0
                        except (OSError, TypeError):
                            status["db_size_mb"] = None
                        status["schema_version"] = get_schema_version(indexer.cache.conn)
                        try:
                            indexer.cache.conn.execute("SELECT 1 FROM messages_fts LIMIT 0")
                            status["fts_enabled"] = True
                        except Exception:
                            status["fts_enabled"] = False
                    else:
                        status["db_size_mb"] = None
                        status["schema_version"] = None
                        status["fts_enabled"] = False

                    status["uptime_seconds"] = round(time.time() - _process_start_time, 1)

                    # Memory usage
                    try:
                        import resource
                        import platform as _platform
                        mem_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        if _platform.system() == "Darwin":
                            mem_mb = mem_bytes / (1024 * 1024)
                        else:
                            mem_mb = mem_bytes / 1024
                    except ImportError:
                        try:
                            import psutil
                            mem_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
                        except ImportError:
                            # Windows fallback: use ctypes to query process memory
                            mem_mb = 0.0
                            if sys.platform == "win32":
                                try:
                                    import ctypes
                                    from ctypes import wintypes

                                    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                                        _fields_ = [
                                            ("cb", wintypes.DWORD),
                                            ("PageFaultCount", wintypes.DWORD),
                                            ("PeakWorkingSetSize", ctypes.c_size_t),
                                            ("WorkingSetSize", ctypes.c_size_t),
                                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                            ("PagefileUsage", ctypes.c_size_t),
                                            ("PeakPagefileUsage", ctypes.c_size_t),
                                        ]

                                    pmc = PROCESS_MEMORY_COUNTERS()
                                    pmc.cb = ctypes.sizeof(pmc)
                                    handle = ctypes.windll.kernel32.GetCurrentProcess()
                                    if ctypes.windll.psapi.GetProcessMemoryInfo(
                                        handle, ctypes.byref(pmc), pmc.cb
                                    ):
                                        mem_mb = pmc.WorkingSetSize / (1024 * 1024)
                                except Exception:
                                    pass
                    status["memory_used_mb"] = round(mem_mb, 1)

                    from .memory import get_memory_limit
                    memory_limit = get_memory_limit()
                    if memory_limit > 0:
                        status["memory_limit_mb"] = round(memory_limit / (1024 * 1024))
                        status["memory_used_percent"] = round(
                            mem_mb / (memory_limit / (1024 * 1024)) * 100, 1
                        )
                    else:
                        status["memory_limit_mb"] = None
                        status["memory_used_percent"] = None

                    # Newest indexed message timestamp (displayed in local time)
                    if indexer.cache:
                        try:
                            row = indexer.cache.conn.execute(
                                "SELECT MAX(timestamp) FROM messages"
                            ).fetchone()
                            if row and row[0]:
                                from datetime import datetime
                                newest_ts = datetime.fromtimestamp(row[0])
                                status["newest_message_at"] = newest_ts.strftime(
                                    "%Y-%m-%d %I:%M:%S %p"
                                )
                            else:
                                status["newest_message_at"] = None
                        except Exception:
                            status["newest_message_at"] = None
                    else:
                        status["newest_message_at"] = None

                    self._send_json(status)

                elif self.path == "/config":
                    self._send_json(config_handler())

                elif self.path == "/health":
                    self._send_json({
                        "status": "ok",
                        "role": "engine",
                        "pid": os.getpid(),
                        "code_fingerprint": startup_fingerprint,
                    })

                elif self.path == "/role":
                    self._send_json({
                        "role": "engine",
                        "pid": os.getpid(),
                    })

                else:
                    self._send_json({"error": "not found"}, 404)

            except Exception as e:
                _log(f"GET {self.path} error from {self.client_address}: {traceback.format_exc()}")
                self._send_json({"error": str(e)}, 500)

        def _send_dashboard(self):
            """Serve the status dashboard."""
            from .dashboard import DASHBOARD_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

    return EngineRequestHandler


def main():
    """Engine process entry point."""
    parser = argparse.ArgumentParser(description="Kiro Ception Engine Process")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config.toml",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the engine port (default: from config)",
    )
    parser.add_argument(
        "--fingerprint",
        type=str,
        default="unknown",
        help="Code fingerprint passed by the spawning client",
    )
    args = parser.parse_args()

    # Load config first (needed for cache_dir)
    if args.config:
        from .config import set_config_file
        set_config_file(args.config)

    from .config import get_config, expand_path
    config = get_config()

    port = args.port or config.server.engine_port
    cache_dir = expand_path(config.embedding.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Determine bind address: use config if set, otherwise auto-detect
    # (0.0.0.0 if peers enabled for remote connectivity, 127.0.0.1 otherwise)
    if config.server.listen_address:
        bind_address = config.server.listen_address
    elif config.peers.enabled:
        bind_address = "0.0.0.0"
    else:
        bind_address = "127.0.0.1"

    # Configure output — redirect stdout/stderr to file if configured
    log_path = config.engine_log_path
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")
        sys.stdout = log_fh
        sys.stderr = log_fh
        _log(f"Engine starting (pid={os.getpid()}, log={log_path})")

    # === ACQUIRE LEADERSHIP (file lock) ===
    # This must come BEFORE the native-extension preload. Only one process can
    # win the lock, and the preload costs ~12s in isolation — far more when
    # several engines start at once and contend importing torch. Electing first
    # lets losers exit in milliseconds instead of paying the whole import
    # before discovering they are not the leader.
    from filelock import FileLock, Timeout

    lock_path = cache_dir / "engine.lock"
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print(f"Another engine is already running (lock: {lock_path}). Exiting.")
        sys.exit(1)

    # Write engine info
    _write_engine_info(port, os.getpid(), cache_dir, _PROCESS_START_TIME)
    _log(f"Acquired engineship (pid={os.getpid()}, port={port})")

    # === PRELOAD NATIVE EXTENSIONS (before any threads) ===
    # Still has to happen before any thread starts — see the function's
    # docstring for the Windows loader-lock deadlock this avoids — but now only
    # the winner pays for it.
    _preload_native_extensions(config.embedding.backend)

    # === START BACKGROUND INDEXER ===
    from .background_indexer import get_background_indexer
    from .search import get_search_index, handle_search_request

    indexer = get_background_indexer()
    indexer.start()

    # Eagerly load the search index from existing cache
    get_search_index()

    # === BUILD CONFIG HANDLER ===
    import platform as _platform
    from .memory import get_memory_limit

    def config_handler() -> dict:
        """Generate config response (matches server.py get_config tool)."""
        from .config import get_config_file
        current_config = get_config()
        return {
            "instance": {
                "role": "engine",
                "pid": os.getpid(),
                "port": port,
                "degraded": False,
                "spawn_failures": 0,
            },
            "version": {
                "kiro_ception": __import__("kiro_ception").__version__,
                "python": _platform.python_version(),
                "platform": f"{_platform.system()} {_platform.machine()}",
            },
            "instance": {
                "label": current_config.resolved_instance_label or None,
                "label_setting": current_config.server.instance_label or None,
                "summary": current_config.instance_summary,
                "indexes": current_config.indexed_sources,
            },
            "paths": {
                "config_file": str(get_config_file()),
                "config_exists": get_config_file().exists(),
                "cache_dir": str(cache_dir),
                "cache_database": str(indexer.cache.db_path) if indexer.cache else "not initialized",
                "engine_lock": str(lock_path),
                "engine_info": str(cache_dir / "engine.json"),
                "engine_log": str(current_config.engine_log_path)
                if current_config.engine_log_path else "disabled",
            },
            "sources": {
                "cli": {
                    "enabled": current_config.cli.enabled,
                    "database_path": str(current_config.cli.database_path)
                    if current_config.cli.database_path else None,
                },
                "ide": {
                    "enabled": current_config.ide.enabled,
                    "patterns": current_config.ide.patterns,
                },
                "claude": {
                    "enabled": current_config.claude.enabled,
                    "roots": current_config.claude.roots,
                    "active_roots": [str(p) for p in current_config.claude.get_roots()],
                    "include_subagents": current_config.claude.include_subagents,
                    "include_sidechains": current_config.claude.include_sidechains,
                    "include_thinking": current_config.claude.include_thinking,
                    "include_tool_context": current_config.claude.include_tool_context,
                },
                "copilot": {
                    "enabled": current_config.copilot.enabled,
                    "roots": current_config.copilot.roots,
                    "active_roots": [str(p) for p in current_config.copilot.get_roots()],
                },
            },
            "embedding": {
                "backend": current_config.embedding.backend,
                "model": current_config.embedding.model,
                "api_base": current_config.embedding.api_base or None,
                "dimensions": current_config.embedding.dimensions,
                "batch_size": current_config.embedding.batch_size,
                "fingerprint": indexer.backend.fingerprint() if indexer.backend else "not initialized",
            },
            "search": {
                "default_threshold": current_config.search.default_threshold,
                "default_max_results": current_config.search.default_max_results,
                "default_context_window": current_config.search.default_context_window,
            },
            "memory": {
                "fraction": current_config.memory.fraction,
                "limit_mb": current_config.memory.limit_mb,
                "effective_limit_mb": round(get_memory_limit() / 1024 / 1024)
                if get_memory_limit() > 0 else "unlimited",
            },
            "indexing": {
                "throttle_ms": current_config.indexing.throttle_ms,
                "rescan_interval_minutes": current_config.indexing.rescan_interval_minutes,
            },
            "server": {
                "engine_port": current_config.server.engine_port,
                "listen_address": f"{bind_address}:{port}",
            },
            "peers": {
                "enabled": current_config.peers.enabled,
                "nodes": current_config.peers.nodes,
                "secret_configured": bool(current_config.peers.secret),
                "timeout_seconds": current_config.peers.timeout_seconds,
            },
            "cache": {
                "embedding_count": indexer.cache.embedding_count if indexer.cache else 0,
                "message_count": indexer.cache.message_count if indexer.cache else 0,
                "indexed_sessions": indexer.cache.indexed_session_count if indexer.cache else 0,
            },
        }

    # === START HEARTBEAT THREAD ===
    def heartbeat_loop():
        """Periodically refresh engine.json's heartbeat_at timestamp."""
        interval = config.server.heartbeat_interval_seconds
        while True:
            time.sleep(interval)
            try:
                _write_engine_info(port, os.getpid(), cache_dir, _PROCESS_START_TIME)
            except Exception as e:
                _log(f"Heartbeat write failed: {e}")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat")
    heartbeat_thread.start()

    # === START HTTP SERVER (on main thread) ===
    follower_registry = FollowerRegistry()

    RequestHandler = _build_request_handler(
        search_handler=handle_search_request,
        config_handler=config_handler,
        indexer_getter=get_background_indexer,
        follower_registry=follower_registry,
        startup_fingerprint=args.fingerprint,
    )

    # Try primary port, fall back to +1
    try:
        server = ThreadingHTTPServer((bind_address, port), RequestHandler)
    except OSError:
        port += 1
        _write_engine_info(port, os.getpid(), cache_dir, _PROCESS_START_TIME)
        try:
            server = ThreadingHTTPServer((bind_address, port), RequestHandler)
        except OSError:
            print(
                f"ERROR: Could not bind to port {port - 1} or {port}. "
                f"Both are in use. Check for stale engine processes or change "
                f"server.engine_port in your config.",
                flush=True,
            )
            lock.release()
            sys.exit(1)

    server.timeout = 1

    # Handle graceful shutdown
    shutdown_event = threading.Event()

    def _shutdown_handler(signum, frame):
        _log(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    _last_follower_check: float = 0
    _FOLLOWER_CHECK_INTERVAL = 10  # Check follower liveness every 10s
    _NO_FOLLOWER_TIMEOUT = 120  # Shut down if no follower registers within 2 minutes
    _engine_start_time = time.time()

    _log(f"HTTP server listening on {bind_address}:{port}")
    _log(f"Dashboard: http://127.0.0.1:{port}/")

    # Run server on main thread (blocking)
    try:
        while not shutdown_event.is_set():
            server.handle_request()

            # Periodically check if registered followers are still alive
            now = time.time()
            if now - _last_follower_check >= _FOLLOWER_CHECK_INTERVAL:
                _last_follower_check = now
                # Only shut down if at least one follower registered and all are now dead
                if follower_registry.count > 0:
                    if not follower_registry.has_live_followers():
                        _log("All followers are dead — shutting down")
                        break
                elif now - _engine_start_time > _NO_FOLLOWER_TIMEOUT:
                    # No follower ever registered — spawning client likely died
                    # before making any requests. Shut down to avoid orphan.
                    _log(
                        f"No followers registered within {_NO_FOLLOWER_TIMEOUT}s "
                        f"— shutting down (orphan protection)"
                    )
                    break
    except KeyboardInterrupt:
        pass
    finally:
        _log("Shutting down...")
        server.server_close()
        indexer.stop()
        try:
            lock.release()
        except Exception:
            pass
        # Clean up engine.json
        try:
            (cache_dir / "engine.json").unlink(missing_ok=True)
        except OSError:
            pass
        _log("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Last-resort crash logging — write to engine.log if possible
        import traceback as _tb
        crash_msg = (
            f"\n{'='*60}\n"
            f"FATAL ENGINE CRASH at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'='*60}\n"
            f"{_tb.format_exc()}\n"
            f"{'='*60}\n"
        )
        # Try to write to the log file even if stdout redirection failed.
        # Resolve through config so the crash lands in THIS instance's cache
        # directory rather than a hardcoded shared path.
        try:
            from pathlib import Path as _Path

            try:
                from .config import get_config as _get_config
                crash_log = _get_config().engine_log_path
            except Exception:
                crash_log = None
            if crash_log is None:
                crash_log = _Path("~/.cache/kiro-ception/engine.log").expanduser()
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(crash_msg)
        except Exception:
            pass
        # Also print to stderr in case it's still connected
        print(crash_msg, file=sys.__stderr__, flush=True)
        raise

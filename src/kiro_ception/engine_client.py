"""Engine process client: spawn, discover, and communicate with the engine process.

This module replaces the in-process InstanceManager/EngineInstance/FollowerInstance
pattern with a simpler model:

1. The MCP process (kiro-ception) is ALWAYS a client.
2. The engine process is a separate long-lived subprocess.
3. This module handles spawning the engine if it's not running, health-checking it,
   and forwarding HTTP requests to it.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from .config import get_config, expand_path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Ensure messages go to stderr (which Kiro captures)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)

# Default wait for a freshly-spawned engine to become healthy. Overridable via
# server.engine_startup_timeout_seconds — see _startup_timeout().
_ENGINE_STARTUP_TIMEOUT = 30  # seconds
_HEALTH_CHECK_TIMEOUT = 5  # seconds per health check attempt
_HEALTH_CHECK_RETRIES = 2

# Fingerprint captured at process startup — represents what code this client loaded with.
# This never changes for the lifetime of this process.
_CLIENT_STARTUP_FINGERPRINT = None


def _startup_timeout() -> int:
    """Seconds to wait for a spawned engine to answer its first health check.

    Cold starts preload torch and the embedding model and can exceed the
    default on slower machines, so this is configurable. Overrunning it is not
    fatal: the engine keeps starting in the background and later tool calls
    reach it once it is listening — the wait exists so that the common case
    returns a ready engine rather than a transient failure.

    Falls back to the default if config cannot be read or holds a
    non-positive value.
    """
    try:
        configured = get_config().server.engine_startup_timeout_seconds
    except Exception:
        return _ENGINE_STARTUP_TIMEOUT
    return configured if configured and configured > 0 else _ENGINE_STARTUP_TIMEOUT


def _get_client_fingerprint() -> str:
    """Get the client's startup fingerprint (computed once, cached)."""
    global _CLIENT_STARTUP_FINGERPRINT
    if _CLIENT_STARTUP_FINGERPRINT is None:
        _CLIENT_STARTUP_FINGERPRINT = _compute_code_fingerprint()
    return _CLIENT_STARTUP_FINGERPRINT


def _compute_code_fingerprint() -> str:
    """Compute a hash of all source files in the kiro_ception package.

    This detects when source code has changed since the engine was spawned.
    If the MCP client's fingerprint doesn't match the engine's, the engine
    is running stale code and should be restarted.
    """
    package_dir = Path(__file__).parent
    hasher = hashlib.md5()  # Used for change detection only, not cryptographic integrity
    for py_file in sorted(package_dir.glob("*.py")):
        hasher.update(py_file.read_bytes())
    return hasher.hexdigest()[:16]


def _get_cache_dir() -> Path:
    """Get the cache directory (where engine.json and engine.lock live)."""
    config = get_config()
    cache_dir = expand_path(config.embedding.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _read_engine_info() -> dict | None:
    """Read engine.json to discover the engine's port and PID."""
    info_path = _get_cache_dir() / "engine.json"
    try:
        with open(info_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists."""
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


def _check_engine_health(port: int, timeout: float = _HEALTH_CHECK_TIMEOUT) -> bool:
    """Check if the engine HTTP server is responding."""
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/health",
            timeout=timeout,
        )
        if resp.status_code == 200:
            body = resp.json()
            return body.get("status") == "ok"
    except Exception:
        pass
    return False


def is_engine_running() -> bool:
    """Check if a healthy engine process exists.

    Checks both PID liveness and HTTP health.
    """
    info = _read_engine_info()
    if not info:
        return False

    port = info.get("port")
    pid = info.get("pid")

    if not port or not pid:
        return False

    if not _is_pid_alive(pid):
        return False

    return _check_engine_health(port)


def _find_engine_executable() -> list[str]:
    """Find the command to launch the engine process.

    Always uses the same Python interpreter running this process to invoke
    the engine module directly. No compiled entry point needed — editable
    installs pick up source changes immediately.
    """
    return [sys.executable, "-m", "kiro_ception.engine_main"]


def spawn_engine() -> bool:
    """Spawn the engine process as a detached subprocess.

    Returns True if a healthy engine appeared within the timeout.
    We don't track the subprocess PID directly — on Windows the Popen PID
    may be a launcher shim, not the real engine. Instead we poll engine.json
    for the real engine to register itself and respond to health checks.

    To distinguish our spawn from a stale engine.json, we record the current
    time before spawning and only accept an engine.json with a started_at
    timestamp newer than that.
    """
    config = get_config()
    cmd = _find_engine_executable()

    # Pass config file if we have an override
    from .config import get_config_file, CONFIG_FILE
    config_file = get_config_file()
    if config_file != CONFIG_FILE and config_file.exists():
        cmd.extend(["--config", str(config_file)])

    # Pass the code fingerprint so the engine can echo it back on /health
    fingerprint = _compute_code_fingerprint()
    cmd.extend(["--fingerprint", fingerprint])

    logger.info(f"Spawning engine process: {' '.join(cmd)}")

    # Record time before spawn — only accept engine.json newer than this
    spawn_time = time.time() - 1.0  # 1s grace for clock skew

    # Spawn detached so it survives if the MCP process exits
    kwargs = {}
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except (OSError, FileNotFoundError) as e:
        logger.error(f"Failed to spawn engine process: {e}")
        return False

    spawned_pid = proc.pid
    logger.info(f"Engine subprocess launched (spawned_pid={spawned_pid}), polling for health...")

    # Poll engine.json for a FRESH engine (started_at > spawn_time) to appear.
    startup_timeout = _startup_timeout()
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        time.sleep(0.5)

        info = _read_engine_info()
        if not info:
            continue

        # Only accept engine.json written after we spawned
        started_at = info.get("started_at", 0)
        if started_at < spawn_time:
            continue

        port = info.get("port")
        pid = info.get("pid")
        if port and pid and _is_pid_alive(pid) and _check_engine_health(port):
            parent_pid = info.get("parent_pid")
            # On Unix: spawned_pid == pid (no shim, direct process)
            # On Windows: spawned_pid == parent_pid (shim sits between)
            is_ours = spawned_pid in (pid, parent_pid)
            if is_ours:
                logger.info(f"Engine is healthy — spawned by us (pid={pid}, port={port})")
            else:
                logger.info(
                    f"Engine is healthy — spawned by another client "
                    f"(pid={pid}, port={port})"
                )
            return True

    logger.error(
        f"Engine did not become healthy within {startup_timeout}s. It is "
        f"probably still cold-starting (torch + embedding model load) and "
        f"later calls should reach it. Raise "
        f"server.engine_startup_timeout_seconds to wait longer."
    )
    # Last-ditch: one more check without the timestamp guard (maybe clock skew)
    info = _read_engine_info()
    if info:
        port = info.get("port")
        pid = info.get("pid")
        if port and pid and _check_engine_health(port):
            logger.info(f"Engine healthy on final check (pid={pid}, port={port})")
            return True
    return False


def _kill_stale_engine(pid: int) -> bool:
    """Kill a stale engine process, giving it a chance to shut down gracefully.

    On Unix: sends SIGTERM first (allows graceful cleanup — release lock, stop
    indexer, remove engine.json), waits up to 3 seconds, then escalates to
    SIGKILL if the process is still alive.

    On Windows: uses TerminateProcess (there is no graceful signal equivalent;
    Windows processes don't have SIGTERM semantics outside of console apps).
    """
    if pid <= 0 or pid == os.getpid():
        return False

    logger.warning(f"Killing stale engine process (pid={pid})")

    if sys.platform == "win32":
        # Windows has no SIGTERM equivalent for non-console processes.
        # TerminateProcess is the only reliable option.
        import ctypes
        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            result = ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(result)
        return not _is_pid_alive(pid)
    else:
        import signal

        # Try graceful shutdown first (SIGTERM)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True  # Already dead
        except PermissionError:
            return False

        # Wait up to 3 seconds for graceful exit
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not _is_pid_alive(pid):
                logger.info(f"Engine (pid={pid}) exited gracefully after SIGTERM")
                return True
            time.sleep(0.2)

        # Still alive — escalate to SIGKILL
        logger.warning(f"Engine (pid={pid}) did not exit after SIGTERM, sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True  # Died between check and kill
        except PermissionError:
            return False


def ensure_engine_running() -> bool:
    """Ensure a healthy engine process is running. Spawn one if needed.

    This is the main entry point called by the MCP process before forwarding
    requests. It handles:
    1. Checking if an existing engine is healthy → check code fingerprint
    2. If engine is healthy but stale (code changed) → kill and respawn
    3. If engine.json exists but process is dead/unhealthy → kill and respawn
    4. If no engine exists → spawn fresh

    Returns True if a healthy, up-to-date engine is available after this call.
    """
    my_fingerprint = _get_client_fingerprint()
    logger.info(f"Code fingerprint (client): {my_fingerprint}")

    # Check if an existing engine is available
    info = _read_engine_info()
    if not info:
        logger.info("No engine.json found — spawning fresh")
        return spawn_engine()

    port = info.get("port")
    pid = info.get("pid")
    logger.info(f"Found engine.json: port={port}, pid={pid}")

    if not port or not pid:
        logger.info("engine.json missing port/pid — spawning fresh")
        return spawn_engine()

    if not _is_pid_alive(pid):
        logger.info(f"Engine pid={pid} is NOT alive — cleaning up and spawning")
        try:
            (_get_cache_dir() / "engine.lock").unlink(missing_ok=True)
        except OSError:
            pass
        return spawn_engine()

    # PID is alive — check health and fingerprint
    logger.info(f"Engine pid={pid} is alive, checking health...")
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/health",
            timeout=_HEALTH_CHECK_TIMEOUT,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == "ok":
                engine_fingerprint = body.get("code_fingerprint")
                logger.info(
                    f"Engine healthy. Fingerprint: engine={engine_fingerprint}, "
                    f"client={my_fingerprint}, match={engine_fingerprint == my_fingerprint}"
                )
                if engine_fingerprint == my_fingerprint:
                    return True  # Healthy and up-to-date
                else:
                    # Fingerprint mismatch — but are WE stale or is the engine stale?
                    # Re-read source from disk to get the current truth
                    disk_fingerprint = _compute_code_fingerprint()
                    if engine_fingerprint == disk_fingerprint:
                        # Engine is NEWER than us. We're the stale client.
                        # Don't kill the engine — just connect to it.
                        logger.warning(
                            f"Engine has newer code (fingerprint={engine_fingerprint}). "
                            f"This client is stale (loaded fingerprint={my_fingerprint}). "
                            f"Reconnect the MCP server to refresh this client."
                        )
                        return True  # Client is stale, but that's ok, log and keep connecting
                    else:
                        logger.info("Fingerprint MISMATCH — engine is stale, restarting")
        else:
            logger.info(f"Engine health check returned status {resp.status_code}")
    except Exception as e:
        logger.info(f"Engine health check failed: {type(e).__name__}: {e}")

    # Engine is either unhealthy or stale — kill and respawn
    logger.warning(f"Engine (pid={pid}) is stale or unresponsive — killing")
    _kill_stale_engine(pid)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            break
        time.sleep(0.2)

    try:
        (_get_cache_dir() / "engine.lock").unlink(missing_ok=True)
    except OSError:
        pass

    return spawn_engine()


def get_engine_port() -> int | None:
    """Get the current engine's HTTP port, or None if not available."""
    info = _read_engine_info()
    if info:
        return info.get("port")
    return None


class EngineClient:
    """HTTP client for communicating with the engine process.

    Replaces the old FollowerInstance with a simpler interface that
    also handles engine lifecycle (spawn if needed).
    """

    def __init__(self):
        self._session: requests.Session | None = None
        self._port: int | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers["Content-Type"] = "application/json"
            self._session.headers["X-Follower-PID"] = str(os.getpid())
        return self._session

    def _base_url(self) -> str:
        if self._port is None:
            self._port = get_engine_port()
        if self._port is None:
            raise ConnectionError("No engine available")
        return f"http://127.0.0.1:{self._port}"

    def _refresh_port(self):
        """Re-read engine port (handles engine restart on different port)."""
        self._port = get_engine_port()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make a request to the engine, respawning if dead."""
        session = self._get_session()
        try:
            resp = session.request(method, f"{self._base_url()}{path}", **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, OSError, ConnectionError):
            # Engine may have died or restarted — try to bring it back
            logger.info("Engine connection failed — attempting respawn")
            if ensure_engine_running():
                self._refresh_port()
                resp = session.request(method, f"{self._base_url()}{path}", **kwargs)
                resp.raise_for_status()
                return resp
            raise ConnectionError("Engine unavailable after respawn attempt")

    def search(self, request: dict) -> dict:
        """Forward a search request to the engine."""
        resp = self._request("POST", "/search", json=request, timeout=30)
        return resp.json()

    def get_status(self) -> dict:
        """Get indexing status from the engine."""
        resp = self._request("GET", "/status", timeout=5)
        return resp.json()

    def get_config(self) -> dict:
        """Get config from the engine."""
        resp = self._request("GET", "/config", timeout=5)
        return resp.json()

    def trigger_reindex(self) -> dict:
        """Tell the engine to reindex."""
        resp = self._request("POST", "/reindex", json={}, timeout=5)
        return resp.json()

    def trigger_rescan(self) -> dict:
        """Tell the engine to rescan."""
        resp = self._request("POST", "/rescan", json={}, timeout=5)
        return resp.json()

    def reload_config(self) -> dict:
        """Tell the engine to reload config."""
        resp = self._request("POST", "/reload-config", json={}, timeout=5)
        return resp.json()

    def health(self) -> bool:
        """Check engine health."""
        try:
            resp = self._request("GET", "/health", timeout=3)
            return resp.json().get("status") == "ok"
        except Exception:
            return False


# Global singleton
_engine_client: EngineClient | None = None


def get_engine_client() -> EngineClient:
    """Get or create the global engine client."""
    global _engine_client
    if _engine_client is None:
        _engine_client = EngineClient()
    return _engine_client

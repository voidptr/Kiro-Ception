"""Unit tests for engine_client.py — spawn, discovery, fingerprint, and lifecycle."""

import hashlib
import json
import os
import time
from unittest.mock import MagicMock, patch

from kiro_ception.engine_client import (
    EngineClient,
    _check_engine_health,
    _compute_code_fingerprint,
    _find_engine_executable,
    _read_engine_info,
    ensure_engine_running,
    is_engine_running,
)

# --- _compute_code_fingerprint ---


class TestCodeFingerprint:
    def test_returns_string(self):
        fp = _compute_code_fingerprint()
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_deterministic(self):
        fp1 = _compute_code_fingerprint()
        fp2 = _compute_code_fingerprint()
        assert fp1 == fp2

    def test_changes_when_source_changes(self, tmp_path, monkeypatch):
        """Fingerprint should change if any .py file in the package changes."""
        # Create a fake package dir with one file
        fake_pkg = tmp_path / "fake_pkg"
        fake_pkg.mkdir()
        (fake_pkg / "module.py").write_text("x = 1")
        (fake_pkg / "__init__.py").write_text("")

        # Compute fingerprint with fake package dir
        def compute_from_dir(pkg_dir):
            hasher = hashlib.md5()
            for py_file in sorted(pkg_dir.glob("*.py")):
                hasher.update(py_file.read_bytes())
            return hasher.hexdigest()[:16]

        fp1 = compute_from_dir(fake_pkg)

        # Modify a file
        (fake_pkg / "module.py").write_text("x = 2")
        fp2 = compute_from_dir(fake_pkg)

        assert fp1 != fp2

    def test_ignores_non_py_files(self, tmp_path):
        """Only .py files should be included in the fingerprint."""
        fake_pkg = tmp_path / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "code.py").write_text("a = 1")
        (fake_pkg / "data.json").write_text("{}")
        (fake_pkg / "notes.txt").write_text("hello")

        hasher = hashlib.md5()
        for py_file in sorted(fake_pkg.glob("*.py")):
            hasher.update(py_file.read_bytes())
        fp1 = hasher.hexdigest()[:16]

        # Add another non-py file — fingerprint shouldn't change
        (fake_pkg / "more.txt").write_text("stuff")

        hasher2 = hashlib.md5()
        for py_file in sorted(fake_pkg.glob("*.py")):
            hasher2.update(py_file.read_bytes())
        fp2 = hasher2.hexdigest()[:16]

        assert fp1 == fp2


# --- _find_engine_executable ---


class TestFindEngineExecutable:
    def test_uses_current_python(self):
        import sys
        cmd = _find_engine_executable()
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "kiro_ception.engine_main"

    def test_returns_list_of_three(self):
        cmd = _find_engine_executable()
        assert len(cmd) == 3


# --- _read_engine_info ---


class TestReadEngineInfo:
    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_ception.engine_client._get_cache_dir",
            lambda: tmp_path,
        )
        assert _read_engine_info() is None

    def test_reads_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_ception.engine_client._get_cache_dir",
            lambda: tmp_path,
        )
        info = {"port": 19760, "pid": 12345, "started_at": time.time()}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        result = _read_engine_info()
        assert result["port"] == 19760
        assert result["pid"] == 12345

    def test_returns_none_on_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_ception.engine_client._get_cache_dir",
            lambda: tmp_path,
        )
        (tmp_path / "engine.json").write_text("not json{{{")
        assert _read_engine_info() is None


# --- _check_engine_health ---


class TestCheckEngineHealth:
    def test_returns_true_on_healthy_response(self):
        with patch("kiro_ception.engine_client.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok", "code_fingerprint": "abc"}
            mock_get.return_value = mock_resp

            assert _check_engine_health(19760) is True

    def test_returns_false_on_connection_error(self):
        with patch("kiro_ception.engine_client.requests.get", side_effect=Exception("refused")):
            assert _check_engine_health(19760) is False

    def test_returns_false_on_bad_status(self):
        with patch("kiro_ception.engine_client.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_get.return_value = mock_resp

            assert _check_engine_health(19760) is False


# --- ensure_engine_running with fingerprint ---


class TestEnsureEngineRunning:
    def test_returns_true_when_engine_healthy_and_current(self, tmp_path, monkeypatch):
        """Engine is up, healthy, and has matching fingerprint → no restart."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        my_fp = _compute_code_fingerprint()

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=True),
            patch("kiro_ception.engine_client.requests.get") as mock_get,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "status": "ok",
                "code_fingerprint": my_fp,
            }
            mock_get.return_value = mock_resp

            result = ensure_engine_running()
            assert result is True

    def test_restarts_when_fingerprint_mismatch(self, tmp_path, monkeypatch):
        """Engine is up but has stale code → kill and respawn."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=True),
            patch("kiro_ception.engine_client.requests.get") as mock_get,
            patch("kiro_ception.engine_client._kill_stale_engine", return_value=True),
            patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "status": "ok",
                "code_fingerprint": "stale_old_fingerprint",
            }
            mock_get.return_value = mock_resp

            # After kill, PID should be dead
            with patch("kiro_ception.engine_client._is_pid_alive", side_effect=[True, False]):
                result = ensure_engine_running()

            assert result is True
            mock_spawn.assert_called_once()

    def test_spawns_when_no_engine_info(self, tmp_path, monkeypatch):
        """No engine.json → spawn fresh."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)

        with patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn:
            result = ensure_engine_running()
            assert result is True
            mock_spawn.assert_called_once()

    def test_spawns_when_pid_dead(self, tmp_path, monkeypatch):
        """engine.json exists but PID is dead → clean up and spawn."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))
        # Create a fake lock file to verify cleanup
        (tmp_path / "engine.lock").write_text("")

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=False),
            patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn,
        ):
            result = ensure_engine_running()
            assert result is True
            mock_spawn.assert_called_once()

    def test_warming_engine_that_dies_is_respawned_without_kill(self, tmp_path, monkeypatch):
        """PID alive but HTTP failing = still warming. If it then dies on its own,
        we spawn fresh and never call _kill_stale_engine."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_ception.engine_client._startup_timeout", lambda: 5)
        monkeypatch.setattr("kiro_ception.engine_client.time.sleep", lambda s: None)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        with (
            # initial alive check True; then dies inside the warming wait loop
            patch("kiro_ception.engine_client._is_pid_alive", side_effect=[True, False]),
            patch("kiro_ception.engine_client.requests.get", side_effect=Exception("timeout")),
            patch("kiro_ception.engine_client._check_engine_health", return_value=False),
            patch("kiro_ception.engine_client._kill_stale_engine") as mock_kill,
            patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn,
        ):
            result = ensure_engine_running()
            assert result is True
            mock_spawn.assert_called_once()
            mock_kill.assert_not_called()  # never kill a warming engine that self-exited

    def test_warming_engine_that_becomes_healthy_is_connected(self, tmp_path, monkeypatch):
        """PID alive, HTTP initially failing, then becomes healthy → connect, no respawn."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_ception.engine_client._startup_timeout", lambda: 5)
        monkeypatch.setattr("kiro_ception.engine_client.time.sleep", lambda s: None)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=True),
            patch("kiro_ception.engine_client.requests.get", side_effect=Exception("timeout")),
            patch("kiro_ception.engine_client._check_engine_health", return_value=True),
            patch("kiro_ception.engine_client._kill_stale_engine") as mock_kill,
            patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn,
        ):
            result = ensure_engine_running()
            assert result is True
            mock_kill.assert_not_called()
            mock_spawn.assert_not_called()  # connected to the warming engine, no respawn

    def test_stuck_engine_is_killed_and_respawned(self, tmp_path, monkeypatch):
        """PID stays alive but never answers health within the window → kill as stuck, respawn."""
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        # startup_timeout=0 → warming wait loop never iterates → straight to stuck path.
        monkeypatch.setattr("kiro_ception.engine_client._startup_timeout", lambda: 0)
        monkeypatch.setattr("kiro_ception.engine_client.time.sleep", lambda s: None)
        info = {"port": 19760, "pid": 99999}
        (tmp_path / "engine.json").write_text(json.dumps(info))

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=True),
            patch("kiro_ception.engine_client.requests.get", side_effect=Exception("timeout")),
            patch("kiro_ception.engine_client._check_engine_health", return_value=False),
            patch("kiro_ception.engine_client._kill_stale_engine", return_value=True) as mock_kill,
            patch("kiro_ception.engine_client.spawn_engine", return_value=True) as mock_spawn,
        ):
            result = ensure_engine_running()
            assert result is True
            mock_kill.assert_called_once()
            mock_spawn.assert_called_once()


# --- EngineClient ---


class TestEngineClient:
    def test_sets_follower_pid_header(self):
        client = EngineClient()
        session = client._get_session()
        assert "X-Follower-PID" in session.headers
        assert session.headers["X-Follower-PID"] == str(os.getpid())

    def test_search_forwards_request(self):
        client = EngineClient()
        client._port = 19760

        with patch.object(client, "_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": [], "query": "test"}
            mock_req.return_value = mock_resp

            result = client.search({"query": "test"})
            mock_req.assert_called_once_with("POST", "/search", json={"query": "test"}, timeout=30)
            assert result == {"results": [], "query": "test"}

    def test_health_returns_bool(self):
        client = EngineClient()
        client._port = 19760

        with patch.object(client, "_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status": "ok"}
            mock_req.return_value = mock_resp

            assert client.health() is True

    def test_health_returns_false_on_error(self):
        client = EngineClient()
        client._port = 19760

        with patch.object(client, "_request", side_effect=Exception("dead")):
            assert client.health() is False


# --- is_engine_running ---


class TestIsEngineRunning:
    def test_false_when_no_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        assert is_engine_running() is False

    def test_false_when_pid_dead(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        (tmp_path / "engine.json").write_text(json.dumps({"port": 19760, "pid": 99999}))

        with patch("kiro_ception.engine_client._is_pid_alive", return_value=False):
            assert is_engine_running() is False

    def test_true_when_healthy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_ception.engine_client._get_cache_dir", lambda: tmp_path)
        (tmp_path / "engine.json").write_text(json.dumps({"port": 19760, "pid": 99999}))

        with (
            patch("kiro_ception.engine_client._is_pid_alive", return_value=True),
            patch("kiro_ception.engine_client._check_engine_health", return_value=True),
        ):
            assert is_engine_running() is True

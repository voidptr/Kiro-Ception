"""engine.json must report a fixed started_at and a moving heartbeat_at.

spawn_engine() tells its own freshly spawned engine apart from a leftover one
by requiring `started_at > spawn_time`. The heartbeat rewrites engine.json
periodically, so if it refreshed started_at as well, any stale engine would
keep looking freshly spawned and that check could never reject one.
"""

import json
import time
from unittest.mock import MagicMock

from kiro_ception.engine_main import _write_engine_info


def read(cache_dir):
    return json.loads((cache_dir / "engine.json").read_text(encoding="utf-8"))


class TestWriteEngineInfo:
    def test_writes_expected_fields(self, tmp_path):
        _write_engine_info(19761, 4242, tmp_path, 1000.0)
        info = read(tmp_path)

        assert info["port"] == 19761
        assert info["pid"] == 4242
        assert info["started_at"] == 1000.0
        assert "heartbeat_at" in info
        assert "parent_pid" in info

    def test_started_at_is_the_value_passed_in(self, tmp_path):
        # Not "now" — the caller owns this value.
        _write_engine_info(19761, 4242, tmp_path, 1000.0)
        assert read(tmp_path)["started_at"] == 1000.0

    def test_heartbeat_moves_but_started_at_does_not(self, tmp_path):
        start = time.time()
        _write_engine_info(19761, 4242, tmp_path, start)
        first = read(tmp_path)

        time.sleep(0.01)
        _write_engine_info(19761, 4242, tmp_path, start)
        second = read(tmp_path)

        assert second["started_at"] == first["started_at"] == start
        assert second["heartbeat_at"] > first["heartbeat_at"]

    def test_stale_engine_stays_stale_across_heartbeats(self, tmp_path):
        """The freshness check spawn_engine() relies on must still reject."""
        stale_start = time.time() - 3600
        _write_engine_info(19761, 4242, tmp_path, stale_start)
        for _ in range(3):
            _write_engine_info(19761, 4242, tmp_path, stale_start)

        spawn_time = time.time() - 1.0
        assert read(tmp_path)["started_at"] < spawn_time

    def test_fresh_engine_passes_the_freshness_check(self, tmp_path):
        spawn_time = time.time() - 1.0
        _write_engine_info(19761, 4242, tmp_path, time.time())
        assert read(tmp_path)["started_at"] > spawn_time

    def test_write_is_atomic_no_temp_files_left(self, tmp_path):
        _write_engine_info(19761, 4242, tmp_path, time.time())
        assert [p.name for p in tmp_path.iterdir()] == ["engine.json"]

    def test_overwrites_cleanly(self, tmp_path):
        _write_engine_info(19761, 1, tmp_path, 1000.0)
        _write_engine_info(19762, 2, tmp_path, 2000.0)
        info = read(tmp_path)

        assert info["port"] == 19762
        assert info["pid"] == 2
        assert info["started_at"] == 2000.0


class TestEngineElectionOrder:
    """A process that loses the election must exit before doing heavy work.

    _preload_native_extensions imports torch/sentence-transformers, ~12s in
    isolation and far worse when several engines start at once and contend for
    CPU. Measured: with the preload ahead of the lock, a loser took 49.5s to
    discover it was not the leader; with the lock first, 1.9s.

    The loser must also not touch engine.json — that file belongs to whichever
    process holds the lock.
    """

    def _config_file(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[embedding]\n"
            f'cache_dir = "{cache.as_posix()}"\n'
            "[server]\n"
            "engine_port = 19998\n"
            # Empty disables log redirection, which would hijack stdout.
            'engine_log_file = ""\n',
            encoding="utf-8",
        )
        return cfg, cache

    def test_loser_exits_without_preloading_or_writing_engine_info(
        self, tmp_path, monkeypatch
    ):
        import sys as _sys

        import pytest
        from filelock import FileLock

        from kiro_ception import config as config_module
        from kiro_ception import engine_main

        cfg, cache = self._config_file(tmp_path)
        # Restore the raw override, not get_config_file() — that returns the
        # default path when no override is set, so feeding it back through
        # set_config_file() would install an override that was never there and
        # leak into every later test.
        original_override = config_module._config_file_override

        held = FileLock(str(cache / "engine.lock"), timeout=0)
        held.acquire(timeout=0)

        preload = MagicMock()
        write_info = MagicMock()
        monkeypatch.setattr(engine_main, "_preload_native_extensions", preload)
        monkeypatch.setattr(engine_main, "_write_engine_info", write_info)
        monkeypatch.setattr(_sys, "argv", ["engine", "--config", str(cfg)])

        try:
            with pytest.raises(SystemExit) as exc:
                engine_main.main()
            assert exc.value.code == 1
            preload.assert_not_called()
            write_info.assert_not_called()
        finally:
            held.release()
            config_module._config_file_override = original_override
            config_module.get_config.cache_clear()

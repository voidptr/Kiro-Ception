"""engine.json must report a fixed started_at and a moving heartbeat_at.

spawn_engine() tells its own freshly spawned engine apart from a leftover one
by requiring `started_at > spawn_time`. The heartbeat rewrites engine.json
periodically, so if it refreshed started_at as well, any stale engine would
keep looking freshly spawned and that check could never reject one.
"""

import json
import time

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

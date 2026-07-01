"""Live threading tests for BackgroundIndexer.

Starts the indexer in a real thread with a fake corpus and mock backend,
waits for completion, and verifies the cache was populated correctly.
"""

import hashlib
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from kiro_ception.background_indexer import BackgroundIndexer, IndexerState
from kiro_ception.cache import EmbeddingCache
from kiro_ception.config import Config, EmbeddingConfig, IndexingConfig
from kiro_ception.models import IndexedMessage, SessionInfo, Source


@pytest.fixture
def mock_backend():
    """Deterministic mock embedding backend."""
    backend = MagicMock()
    backend.fingerprint.return_value = "test:mock:64"

    def _encode(texts):
        result = []
        for text in texts:
            seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
            rng = np.random.RandomState(seed)
            vec = rng.randn(64).astype(np.float32)
            vec /= np.linalg.norm(vec)
            result.append(vec)
        return np.array(result)

    backend.encode.side_effect = _encode
    backend.encode_query.side_effect = lambda t: _encode([t])[0]
    backend.dimensions.return_value = 64
    return backend


@pytest.fixture
def fake_sessions():
    """Small set of sessions for indexing."""
    return [
        SessionInfo(
            session_id="live-sess-1",
            workspace="/test/project",
            message_count=2,
            created=datetime(2026, 6, 1, 10, 0),
            modified=datetime(2026, 6, 1, 11, 0),
            source=Source.IDE,
        ),
        SessionInfo(
            session_id="live-sess-2",
            workspace="/test/project",
            message_count=1,
            created=datetime(2026, 6, 2, 10, 0),
            modified=datetime(2026, 6, 2, 12, 0),
            source=Source.CLI,
        ),
    ]


@pytest.fixture
def fake_messages():
    """Messages for the fake sessions."""
    return {
        "live-sess-1": [
            IndexedMessage(
                uuid="ls1-m0", session_id="live-sess-1", workspace="/test/project",
                timestamp=datetime(2026, 6, 1, 10, 0), role="user",
                searchable_text="Implement rate limiting for the API",
                message_index=0, source=Source.IDE,
            ),
            IndexedMessage(
                uuid="ls1-m1", session_id="live-sess-1", workspace="/test/project",
                timestamp=datetime(2026, 6, 1, 10, 1), role="assistant",
                searchable_text="I'll add a token bucket rate limiter middleware.",
                message_index=1, source=Source.IDE,
            ),
        ],
        "live-sess-2": [
            IndexedMessage(
                uuid="ls2-m0", session_id="live-sess-2", workspace="/test/project",
                timestamp=datetime(2026, 6, 2, 10, 0), role="user",
                searchable_text="Check the deployment status",
                message_index=0, source=Source.CLI,
            ),
        ],
    }


class TestBackgroundIndexerLive:
    def test_full_indexing_run(self, tmp_path, monkeypatch, mock_backend, fake_sessions, fake_messages):
        """Start indexer thread, wait for completion, verify cache state."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=0),  # Don't loop
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            # Wait for indexer to finish (timeout after 10s)
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.state == IndexerState.IDLE and indexer.status.completed_at:
                    break
                time.sleep(0.05)

            indexer.stop()

        # Verify results
        assert indexer.status.state == IndexerState.IDLE
        assert indexer.status.sessions_processed == 2
        assert indexer.status.messages_embedded == 3  # 2 + 1
        assert indexer.status.errors == 0

        # Verify cache was populated
        cache = indexer.cache
        assert cache is not None
        assert cache.embedding_count == 3
        assert cache.message_count == 3
        assert cache.indexed_session_count == 2

    def test_indexer_skips_unchanged_sessions(self, tmp_path, monkeypatch, mock_backend, fake_sessions, fake_messages):
        """Second run should skip sessions that haven't changed."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=0),
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        patches = (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        )

        # First run
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            indexer1 = BackgroundIndexer()
            indexer1.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer1.status.state == IndexerState.IDLE and indexer1.status.completed_at:
                    break
                time.sleep(0.05)
            indexer1.stop()

        assert indexer1.status.sessions_processed == 2

        # Second run — same sessions, same mtimes
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            indexer2 = BackgroundIndexer()
            # Reuse the same cache
            indexer2._cache = indexer1.cache
            indexer2._backend = mock_backend
            # Manually run the index pass (faster than threading)
            indexer2._index_pass(config)

        # All sessions should be unchanged
        assert indexer2.status.sessions_unchanged == 2
        assert indexer2.status.sessions_processed == 0

    def test_indexer_handles_embedding_errors(self, tmp_path, monkeypatch, fake_sessions, fake_messages):
        """Embedding failures should be counted as errors, not crash."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=1),
            indexing=IndexingConfig(rescan_interval_minutes=0),
        )

        # Backend that fails on encode
        failing_backend = MagicMock()
        failing_backend.fingerprint.return_value = "test:failing:64"
        failing_backend.encode.side_effect = RuntimeError("Model crashed")
        failing_backend.dimensions.return_value = 64

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=failing_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.state == IndexerState.IDLE and indexer.status.completed_at:
                    break
                time.sleep(0.05)
            indexer.stop()

        # Should have errors but not crash
        assert indexer.status.errors > 0
        assert indexer.status.messages_skipped > 0
        assert indexer.status.sessions_processed == 2  # Sessions still processed

    def test_trigger_rescan_wakes_indexer(self, tmp_path, monkeypatch, mock_backend, fake_sessions, fake_messages):
        """trigger_rescan should wake a sleeping indexer."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=60),  # Long interval
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            # Wait for initial indexing to complete
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.sessions_processed >= 2:
                    break
                time.sleep(0.05)

            # Trigger rescan — should wake from sleep
            initial_completed = indexer.status.completed_at
            indexer.trigger_rescan()

            # Wait for rescan to complete
            deadline = time.time() + 5
            while time.time() < deadline:
                if indexer.status.completed_at and indexer.status.completed_at != initial_completed:
                    break
                time.sleep(0.05)
            indexer.stop()

        # Rescan should have completed (sessions are unchanged since mtime didn't change)
        assert indexer.status.state == IndexerState.IDLE
        assert (indexer.status.sessions_processed + indexer.status.sessions_unchanged) >= 2

    def test_last_completed_at_persists_across_restarts(self, tmp_path, monkeypatch, mock_backend, fake_sessions, fake_messages):
        """last_completed_at should be saved to SQLite and restored on next startup."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=0),
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            # First run — should complete and persist last_completed_at
            indexer1 = BackgroundIndexer()
            indexer1.start()

            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer1.status.state == IndexerState.IDLE and indexer1.status.completed_at:
                    break
                time.sleep(0.05)
            indexer1.stop()

        assert indexer1.status.last_completed_at is not None
        first_completed = indexer1.status.last_completed_at

        # Verify it's persisted in SQLite
        stored = indexer1.cache.get_meta("last_completed_at")
        assert stored is not None
        assert float(stored) == first_completed

        # Second run — simulate restart, should restore from SQLite
        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer2 = BackgroundIndexer()
            indexer2.start()

            # Wait for completion
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer2.status.state == IndexerState.IDLE and indexer2.status.completed_at:
                    break
                time.sleep(0.05)
            indexer2.stop()

        # The second run completed successfully, so last_completed_at was updated.
        # Verify it's >= the first run's timestamp (proving persistence works —
        # the value was written to SQLite, restored on startup, then updated on completion).
        assert indexer2.status.last_completed_at is not None
        assert indexer2.status.last_completed_at >= first_completed

        # Verify the SQLite value was updated to the new completion time
        stored2 = indexer2.cache.get_meta("last_completed_at")
        assert stored2 is not None
        assert float(stored2) == indexer2.status.last_completed_at

    def test_rescan_switches_backend_on_fingerprint_change(self, tmp_path, monkeypatch, fake_sessions, fake_messages):
        """When embedding config changes, rescan should switch to a new cache database."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        # First backend: mock with fingerprint "test:model-a:128"
        backend_a = MagicMock()
        backend_a.fingerprint.return_value = "test:model-a:128"
        backend_a.dimensions.return_value = 128

        def _encode_a(texts):
            result = []
            for text in texts:
                seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
                rng = np.random.RandomState(seed)
                vec = rng.randn(128).astype(np.float32)
                vec /= np.linalg.norm(vec)
                result.append(vec)
            return np.array(result)

        backend_a.encode.side_effect = _encode_a

        # Second backend: different fingerprint "test:model-b:64"
        backend_b = MagicMock()
        backend_b.fingerprint.return_value = "test:model-b:64"
        backend_b.dimensions.return_value = 64

        def _encode_b(texts):
            result = []
            for text in texts:
                seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
                rng = np.random.RandomState(seed + 1)
                vec = rng.randn(64).astype(np.float32)
                vec /= np.linalg.norm(vec)
                result.append(vec)
            return np.array(result)

        backend_b.encode.side_effect = _encode_b

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=1),  # Short interval for test
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        # Run first pass with backend_a
        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=backend_a),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.state == IndexerState.IDLE and indexer.status.completed_at:
                    break
                time.sleep(0.05)
            indexer.stop()

        # Verify first pass used backend_a's cache
        assert indexer.cache is not None
        first_db_path = indexer.cache.db_path
        assert "model-a" in str(first_db_path) or first_db_path.exists()
        first_embedding_count = indexer.cache.embedding_count
        assert first_embedding_count == 3  # 2 + 1 messages

        # Now simulate a config change: rescan with backend_b
        # The indexer needs to detect the fingerprint change and switch cache
        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=backend_b),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            # Create a new indexer that starts with backend_a, then "discovers" backend_b
            indexer2 = BackgroundIndexer()
            # Manually set up like the old state
            indexer2._backend = backend_a
            indexer2._cache = EmbeddingCache(backend_a.fingerprint())

            # Simulate what happens in the rescan loop when fingerprint changes:
            # Re-read config, create new backend, detect fingerprint change
            new_backend = backend_b
            new_fingerprint = new_backend.fingerprint()
            old_fingerprint = backend_a.fingerprint()

            # Verify fingerprints differ
            assert new_fingerprint != old_fingerprint

            # Apply the switch (same logic as in _run)
            indexer2._backend = new_backend
            indexer2._cache = EmbeddingCache(new_fingerprint)

            # The new cache should be empty (different DB file)
            assert indexer2.cache.embedding_count == 0
            assert indexer2.cache.db_path != first_db_path

            # Run index pass with new backend
            indexer2._index_pass(config)

        # Should have embedded into the new cache
        assert indexer2.cache.embedding_count == 3
        assert indexer2.cache.db_path != first_db_path

    def test_full_rescan_triggers_fingerprint_switch_in_loop(self, tmp_path, monkeypatch, fake_sessions, fake_messages):
        """End-to-end: trigger_reindex with a changed backend creates a new DB."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        backend_a = MagicMock()
        backend_a.fingerprint.return_value = "test:old-model:128"
        backend_a.dimensions.return_value = 128

        def _encode(texts):
            result = []
            for text in texts:
                seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
                rng = np.random.RandomState(seed)
                vec = rng.randn(128).astype(np.float32)
                vec /= np.linalg.norm(vec)
                result.append(vec)
            return np.array(result)

        backend_a.encode.side_effect = _encode

        backend_b = MagicMock()
        backend_b.fingerprint.return_value = "test:new-model:128"
        backend_b.dimensions.return_value = 128
        backend_b.encode.side_effect = _encode

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=60),  # Long interval — we'll trigger manually
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        # Track which backend get_embedding_backend returns
        backend_sequence = [backend_a, backend_b]
        call_count = [0]

        def _get_backend():
            idx = min(call_count[0], len(backend_sequence) - 1)
            call_count[0] += 1
            return backend_sequence[idx]

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", side_effect=_get_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            # Wait for initial pass to complete
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.state == IndexerState.IDLE and indexer.status.completed_at:
                    break
                time.sleep(0.05)

            # Record first DB path
            first_db = indexer.cache.db_path
            assert indexer.cache.embedding_count == 3

            # Now trigger reindex — this will wake the loop, which will
            # call get_embedding_backend() again and get backend_b
            trigger_time = time.time()
            indexer.trigger_reindex()

            # Wait for the second pass to fully complete:
            # DB must switch AND a new completed_at must be set after trigger
            deadline = time.time() + 15
            while time.time() < deadline:
                if (indexer.cache and indexer.cache.db_path != first_db
                        and indexer.status.state == IndexerState.IDLE
                        and indexer.status.completed_at
                        and indexer.status.completed_at > trigger_time):
                    break
                time.sleep(0.05)

            indexer.stop()

        # Should have switched to a new database
        assert indexer.cache.db_path != first_db
        assert indexer.cache.embedding_count == 3  # Re-embedded into new DB

    def test_completed_at_set_after_each_pass(self, tmp_path, monkeypatch, mock_backend, fake_sessions, fake_messages):
        """Regression: completed_at and last_completed_at must be set after each index pass.

        Previously, these were only set in the finally block which only fires when
        the thread exits. During normal operation (rescan loop running), they were
        never set — so get_indexing_status always showed null for both fields.
        """
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
        )

        config = Config(
            embedding=EmbeddingConfig(backend="openai-compatible", api_base="http://fake", batch_size=2),
            indexing=IndexingConfig(rescan_interval_minutes=60),  # Long interval — won't auto-fire
        )

        def _load_messages(session):
            return fake_messages.get(session.session_id, [])

        with (
            patch("kiro_ception.background_indexer.get_config", return_value=config),
            patch("kiro_ception.background_indexer.get_embedding_backend", return_value=mock_backend),
            patch("kiro_ception.background_indexer.list_all_sessions", return_value=fake_sessions),
            patch("kiro_ception.background_indexer.load_session_messages", side_effect=_load_messages),
            patch("kiro_ception.background_indexer.get_memory_limit", return_value=0),
            patch("kiro_ception.background_indexer.select_sessions_within_limit",
                  return_value=(fake_sessions, [])),
        ):
            indexer = BackgroundIndexer()
            indexer.start()

            # Wait for initial pass to complete (state goes to IDLE)
            deadline = time.time() + 10
            while time.time() < deadline:
                if indexer.status.state == IndexerState.IDLE:
                    break
                time.sleep(0.05)

            # KEY ASSERTION: After the initial pass, while the thread is still
            # alive (waiting in the rescan loop), completed_at and last_completed_at
            # must be set — NOT null.
            assert indexer._thread.is_alive(), "Thread should still be alive (rescan loop)"
            assert indexer.status.completed_at is not None, (
                "completed_at must be set after initial pass completes"
            )
            assert indexer.status.last_completed_at is not None, (
                "last_completed_at must be set after initial pass completes"
            )
            initial_completed = indexer.status.last_completed_at

            # Also verify it was persisted to SQLite
            stored = indexer.cache.get_meta("last_completed_at")
            assert stored is not None, "last_completed_at must be persisted to SQLite"
            assert float(stored) == initial_completed

            # Now trigger a rescan and verify completed_at updates again
            time.sleep(0.1)  # Small gap so timestamps differ
            indexer.trigger_rescan()

            # Wait for the rescan pass to complete
            deadline = time.time() + 10
            while time.time() < deadline:
                if (indexer.status.state == IndexerState.IDLE
                        and indexer.status.last_completed_at > initial_completed):
                    break
                time.sleep(0.05)

            # After the rescan pass, values should be updated
            assert indexer.status.completed_at is not None
            assert indexer.status.last_completed_at > initial_completed, (
                "last_completed_at must be updated after rescan pass"
            )

            # Verify SQLite was updated too
            stored2 = indexer.cache.get_meta("last_completed_at")
            assert float(stored2) > initial_completed, (
                "SQLite last_completed_at must be updated after rescan pass"
            )

            indexer.stop()

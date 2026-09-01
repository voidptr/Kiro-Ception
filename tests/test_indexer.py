"""System tests for the indexing pipeline and SearchIndex.

Tests the full flow: session discovery → message loading → embedding → search.
Uses a fixture corpus of fake sessions and a mock embedding backend.
"""

import hashlib
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from kiro_ception.background_indexer import BackgroundIndexer, IndexerState, IndexingStatus
from kiro_ception.cache import EmbeddingCache
from kiro_ception.config import Config, EmbeddingConfig, IndexingConfig
from kiro_ception.memory import select_sessions_within_limit
from kiro_ception.models import IndexedMessage, SessionInfo, Source
from kiro_ception.search import SearchIndex


# --- Fixtures ---


@pytest.fixture
def fake_sessions():
    """A small corpus of fake sessions."""
    return [
        SessionInfo(
            session_id="sess-1",
            workspace="/project/alpha",
            message_count=3,
            created=datetime(2026, 6, 1, 10, 0),
            modified=datetime(2026, 6, 1, 11, 0),
            source=Source.IDE,
        ),
        SessionInfo(
            session_id="sess-2",
            workspace="/project/beta",
            message_count=2,
            created=datetime(2026, 6, 2, 10, 0),
            modified=datetime(2026, 6, 2, 12, 0),
            source=Source.IDE,
        ),
        SessionInfo(
            session_id="sess-3",
            workspace="/project/alpha",
            message_count=4,
            created=datetime(2026, 5, 30, 8, 0),
            modified=datetime(2026, 5, 30, 9, 0),
            source=Source.CLI,
        ),
    ]


@pytest.fixture
def fake_messages():
    """Messages for the fake sessions."""
    base = datetime(2026, 6, 1, 10, 0)
    return {
        "sess-1": [
            IndexedMessage(
                uuid="s1-m0", session_id="sess-1", workspace="/project/alpha",
                timestamp=datetime(2026, 6, 1, 10, 0), role="user",
                searchable_text="How do I set up authentication?",
                message_index=0, source=Source.IDE,
            ),
            IndexedMessage(
                uuid="s1-m1", session_id="sess-1", workspace="/project/alpha",
                timestamp=datetime(2026, 6, 1, 10, 1), role="assistant",
                searchable_text="You can use JWT tokens for authentication.",
                message_index=1, source=Source.IDE,
            ),
            IndexedMessage(
                uuid="s1-m2", session_id="sess-1", workspace="/project/alpha",
                timestamp=datetime(2026, 6, 1, 10, 2), role="user",
                searchable_text="Can you show me an example?",
                message_index=2, source=Source.IDE,
            ),
        ],
        "sess-2": [
            IndexedMessage(
                uuid="s2-m0", session_id="sess-2", workspace="/project/beta",
                timestamp=datetime(2026, 6, 2, 10, 0), role="user",
                searchable_text="Fix the database connection timeout bug.",
                message_index=0, source=Source.IDE,
            ),
            IndexedMessage(
                uuid="s2-m1", session_id="sess-2", workspace="/project/beta",
                timestamp=datetime(2026, 6, 2, 10, 1), role="assistant",
                searchable_text="The timeout was caused by missing connection pooling.",
                message_index=1, source=Source.IDE,
            ),
        ],
        "sess-3": [
            IndexedMessage(
                uuid="s3-m0", session_id="sess-3", workspace="/project/alpha",
                timestamp=datetime(2026, 5, 30, 8, 0), role="user",
                searchable_text="Deploy the service to production.",
                message_index=0, source=Source.CLI,
            ),
            IndexedMessage(
                uuid="s3-m1", session_id="sess-3", workspace="/project/alpha",
                timestamp=datetime(2026, 5, 30, 8, 1), role="assistant",
                searchable_text="Deploying version 2.1.0 to production cluster.",
                message_index=1, source=Source.CLI,
            ),
            IndexedMessage(
                uuid="s3-m2", session_id="sess-3", workspace="/project/alpha",
                timestamp=datetime(2026, 5, 30, 8, 2), role="user",
                searchable_text="Check the deployment status.",
                message_index=2, source=Source.CLI,
            ),
            IndexedMessage(
                uuid="s3-m3", session_id="sess-3", workspace="/project/alpha",
                timestamp=datetime(2026, 5, 30, 8, 3), role="assistant",
                searchable_text="All pods are healthy. Deployment complete.",
                message_index=3, source=Source.CLI,
            ),
        ],
    }


@pytest.fixture
def mock_backend():
    """A deterministic mock embedding backend.

    Generates embeddings as normalized random vectors seeded by text hash,
    so the same text always gets the same vector (deterministic similarity).
    """
    backend = MagicMock()
    backend.fingerprint.return_value = "test-backend:mock:128"

    def _encode(texts):
        """Deterministic embeddings from text hash."""
        result = []
        for text in texts:
            seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
            rng = np.random.RandomState(seed)
            vec = rng.randn(128).astype(np.float32)
            vec /= np.linalg.norm(vec)
            result.append(vec)
        return np.array(result)

    def _encode_query(text):
        return _encode([text])[0]

    backend.encode.side_effect = _encode
    backend.encode_query.side_effect = _encode_query
    backend.dimensions.return_value = 128
    return backend


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Real SQLite cache in a temp directory."""
    monkeypatch.setattr(
        "kiro_ception.cache._get_cache_db_path",
        lambda fp: tmp_path / f"cache_{hashlib.md5(fp.encode()).hexdigest()[:12]}.db",
    )
    c = EmbeddingCache("test-backend:mock:128")
    _ = c.conn
    yield c
    c.close()


# --- select_sessions_within_limit ---


class TestSelectSessionsWithinLimit:
    def test_no_limit_returns_all(self, fake_sessions):
        selected, excluded = select_sessions_within_limit(fake_sessions, 0)
        assert len(selected) == 3
        assert len(excluded) == 0

    def test_tight_limit_excludes_oldest(self, fake_sessions):
        # BYTES_PER_MESSAGE = 2600, so 3 msgs = 7800 bytes, 2 msgs = 5200
        # A limit of 15000 bytes should fit sess-2 (newest, 2 msgs=5200) and
        # sess-1 (3 msgs=7800) but exclude sess-3 (oldest, 4 msgs=10400)
        from kiro_ception.config import BYTES_PER_MESSAGE

        # Enough for ~5 messages
        limit = 5 * BYTES_PER_MESSAGE + 1
        selected, excluded = select_sessions_within_limit(fake_sessions, limit)

        # Newest sessions selected first
        selected_ids = {s.session_id for s in selected}
        excluded_ids = {s.session_id for s in excluded}

        # sess-2 (newest, 2 msgs) and sess-1 (3 msgs) = 5 msgs total, fits
        assert "sess-2" in selected_ids
        assert "sess-1" in selected_ids
        # sess-3 (oldest, 4 msgs) would push over limit
        assert "sess-3" in excluded_ids

    def test_sorted_by_newest_first(self, fake_sessions):
        # When limit is applied (>0), results are sorted newest first
        from kiro_ception.config import BYTES_PER_MESSAGE
        limit = 100 * BYTES_PER_MESSAGE  # Enough for all
        selected, _ = select_sessions_within_limit(fake_sessions, limit)
        timestamps = [s.timestamp_fallback for s in selected]
        assert timestamps == sorted(timestamps, reverse=True)


# --- IndexingStatus ---


class TestIndexingStatus:
    def test_to_dict_structure(self):
        status = IndexingStatus(
            state=IndexerState.INDEXING,
            sessions_total=100,
            sessions_processed=50,
            sessions_unchanged=20,
            messages_embedded=200,
            started_at=time.time() - 60,
        )
        d = status.to_dict()

        assert d["state"] == "indexing"
        assert d["sessions_total"] == 100
        assert d["progress_percent"] == 70.0  # (50+20)/100
        assert d["elapsed_seconds"] >= 59.0

    def test_progress_percent_zero_total(self):
        status = IndexingStatus(sessions_total=0)
        assert status.progress_percent == 0.0

    def test_eta_only_when_indexing(self):
        status = IndexingStatus(state=IndexerState.IDLE, rate=10.0)
        assert status.eta_seconds == 0.0

    def test_last_completed_at_in_to_dict(self):
        completed = time.time() - 300  # 5 minutes ago
        status = IndexingStatus(
            state=IndexerState.INDEXING,
            sessions_total=100,
            started_at=time.time(),
            last_completed_at=completed,
        )
        d = status.to_dict()

        assert "last_completed_at" in d
        assert d["last_completed_at"] is not None
        # Should be an ISO formatted string
        assert "T" in d["last_completed_at"]

    def test_last_completed_at_none_on_first_run(self):
        status = IndexingStatus(
            state=IndexerState.INDEXING,
            sessions_total=100,
            started_at=time.time(),
        )
        d = status.to_dict()

        assert d["last_completed_at"] is None


# --- Full indexing pipeline ---


class TestIndexingPipeline:
    def test_full_index_and_search(self, cache, mock_backend, fake_sessions, fake_messages):
        """End-to-end: index sessions → search → get results."""
        # Step 1: Index all messages into the cache
        for session in fake_sessions:
            msgs = fake_messages[session.session_id]
            metadata_rows = []
            texts_to_embed = []
            hashes_to_embed = []

            for msg in msgs:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                metadata_rows.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                ))
                texts_to_embed.append(msg.searchable_text)
                hashes_to_embed.append(text_hash)

            # Store metadata
            cache.put_messages_batch(metadata_rows)

            # Embed and store
            embeddings = mock_backend.encode(texts_to_embed)
            items = list(zip(hashes_to_embed, embeddings))
            cache.put_embeddings_batch(items)

            cache.update_session_state(session.session_id, session.modified.timestamp())

        # Verify cache state
        assert cache.message_count == 9  # 3 + 2 + 4
        assert cache.embedding_count == 9
        assert cache.indexed_session_count == 3

        # Step 2: Build SearchIndex from cache
        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            search_index = SearchIndex()
            search_index._refresh()

        assert search_index.message_count == 9
        assert search_index.is_loading is False

        # Step 3: Search for "authentication"
        query_vec = mock_backend.encode_query("How do I set up authentication?")
        results = search_index.search(query_embedding=query_vec, threshold=0.0, max_results=5)

        # The exact match should score highest (same text = same vector = cos_sim=1.0)
        assert len(results) > 0
        assert results[0]["uuid"] == "s1-m0"
        assert results[0]["score"] >= 0.99

    def test_workspace_filter(self, cache, mock_backend, fake_sessions, fake_messages):
        """Search filtered by workspace only returns matching sessions."""
        # Index everything
        for session in fake_sessions:
            msgs = fake_messages[session.session_id]
            metadata_rows = []
            texts_to_embed = []
            hashes_to_embed = []
            for msg in msgs:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                metadata_rows.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                ))
                texts_to_embed.append(msg.searchable_text)
                hashes_to_embed.append(text_hash)
            cache.put_messages_batch(metadata_rows)
            embeddings = mock_backend.encode(texts_to_embed)
            cache.put_embeddings_batch(list(zip(hashes_to_embed, embeddings)))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            search_index = SearchIndex()
            search_index._refresh()

        # Search for "database" with workspace=/project/beta
        query_vec = mock_backend.encode_query("Fix the database connection timeout bug.")
        results = search_index.search(
            query_embedding=query_vec,
            workspace="/project/beta",
            threshold=0.0,
            max_results=10,
        )

        # All results should be from /project/beta
        for r in results:
            assert r["workspace"].startswith("/project/beta")

    def test_source_filter(self, cache, mock_backend, fake_sessions, fake_messages):
        """Search filtered by source only returns matching source type."""
        for session in fake_sessions:
            msgs = fake_messages[session.session_id]
            metadata_rows = []
            texts_to_embed = []
            hashes_to_embed = []
            for msg in msgs:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                metadata_rows.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                ))
                texts_to_embed.append(msg.searchable_text)
                hashes_to_embed.append(text_hash)
            cache.put_messages_batch(metadata_rows)
            embeddings = mock_backend.encode(texts_to_embed)
            cache.put_embeddings_batch(list(zip(hashes_to_embed, embeddings)))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            search_index = SearchIndex()
            search_index._refresh()

        # Search CLI only
        query_vec = mock_backend.encode_query("Deploy the service")
        results = search_index.search(
            query_embedding=query_vec,
            source="cli",
            threshold=0.0,
            max_results=10,
        )

        for r in results:
            assert r["source"] == "cli"

    def test_date_filter(self, cache, mock_backend, fake_sessions, fake_messages):
        """Search with date filters restricts by timestamp."""
        for session in fake_sessions:
            msgs = fake_messages[session.session_id]
            metadata_rows = []
            texts_to_embed = []
            hashes_to_embed = []
            for msg in msgs:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                metadata_rows.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                ))
                texts_to_embed.append(msg.searchable_text)
                hashes_to_embed.append(text_hash)
            cache.put_messages_batch(metadata_rows)
            embeddings = mock_backend.encode(texts_to_embed)
            cache.put_embeddings_batch(list(zip(hashes_to_embed, embeddings)))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            search_index = SearchIndex()
            search_index._refresh()

        # Only after June 1 — should exclude sess-3 (May 30)
        after_ts = datetime(2026, 6, 1, 0, 0).timestamp()
        query_vec = mock_backend.encode_query("deploy")
        results = search_index.search(
            query_embedding=query_vec,
            after_ts=after_ts,
            threshold=0.0,
            max_results=20,
        )

        for r in results:
            assert r["timestamp"] >= after_ts

    def test_threshold_filter(self, cache, mock_backend, fake_sessions, fake_messages):
        """High threshold should return fewer results."""
        for session in fake_sessions:
            msgs = fake_messages[session.session_id]
            metadata_rows = []
            texts_to_embed = []
            hashes_to_embed = []
            for msg in msgs:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                metadata_rows.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                ))
                texts_to_embed.append(msg.searchable_text)
                hashes_to_embed.append(text_hash)
            cache.put_messages_batch(metadata_rows)
            embeddings = mock_backend.encode(texts_to_embed)
            cache.put_embeddings_batch(list(zip(hashes_to_embed, embeddings)))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            search_index = SearchIndex()
            search_index._refresh()

        query_vec = mock_backend.encode_query("authentication JWT tokens")

        # Low threshold: many results
        low_results = search_index.search(query_embedding=query_vec, threshold=0.0, max_results=20)
        # High threshold: fewer or zero
        high_results = search_index.search(query_embedding=query_vec, threshold=0.99, max_results=20)

        assert len(high_results) <= len(low_results)


# --- BackgroundIndexer unit behavior ---


class TestBackgroundIndexer:
    def test_initial_state(self):
        indexer = BackgroundIndexer()
        assert indexer.status.state == IndexerState.IDLE
        assert indexer.cache is None
        assert indexer.backend is None

    def test_trigger_reindex_sets_event(self):
        indexer = BackgroundIndexer()
        assert not indexer._reindex_event.is_set()
        # Patch start to avoid actually launching a thread
        with patch.object(indexer, "start"):
            indexer.trigger_reindex()
        assert indexer._reindex_event.is_set()

    def test_trigger_rescan_sets_event(self):
        indexer = BackgroundIndexer()
        assert not indexer._rescan_event.is_set()
        with patch.object(indexer, "start"):
            indexer.trigger_rescan()
        assert indexer._rescan_event.is_set()

    def test_stop_sets_event(self):
        indexer = BackgroundIndexer()
        indexer.stop()
        assert indexer._stop_event.is_set()

    def test_get_session_mtime_from_modified(self):
        session = SessionInfo(
            session_id="s1", workspace="/test", source=Source.IDE,
            created=datetime(2026, 1, 1), modified=datetime(2026, 6, 1),
        )
        mtime = BackgroundIndexer._get_session_mtime(session)
        assert mtime == datetime(2026, 6, 1).timestamp()

    def test_get_session_mtime_fallback_to_created(self):
        session = SessionInfo(
            session_id="s1", workspace="/test", source=Source.IDE,
            created=datetime(2026, 1, 1), modified=None,
        )
        mtime = BackgroundIndexer._get_session_mtime(session)
        assert mtime == datetime(2026, 1, 1).timestamp()

    def test_get_session_mtime_no_dates(self):
        session = SessionInfo(
            session_id="s1", workspace="/test", source=Source.IDE,
        )
        mtime = BackgroundIndexer._get_session_mtime(session)
        assert mtime == 0.0


# --- SearchIndex refresh behavior ---


class TestSearchIndexRefresh:
    def test_empty_cache_stays_empty(self, cache):
        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            si = SearchIndex()
            si._refresh()

        assert si.message_count == 0
        assert si.is_loading is False

    def test_embeddings_without_messages_shows_loading(self, cache):
        """If embeddings exist but no messages yet, report loading state."""
        # Put an embedding but no messages
        cache.put_embedding("hash1", np.random.randn(128).astype(np.float32))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            si = SearchIndex()
            si._refresh()

        assert si.message_count == 0
        assert si.is_loading is True
        assert si.embedding_count_hint == 1

    def test_refresh_throttle(self, cache, mock_backend):
        """After successful load, refresh is throttled to 60 seconds."""
        # Put a message and its embedding
        text_hash = hashlib.md5(b"hello").hexdigest()
        cache.put_message("m1", "s1", "/test", time.time(), "user", "hello", 0, "ide", text_hash)
        cache.conn.commit()
        cache.put_embedding(text_hash, np.random.randn(128).astype(np.float32))

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            si = SearchIndex()
            si._refresh()
            assert si._ever_loaded is True

            # Record state after first load
            count_after_first = si.message_count

            # Add another message to the cache
            text_hash2 = hashlib.md5(b"world").hexdigest()
            cache.put_message("m2", "s1", "/test", time.time(), "user", "world", 1, "ide", text_hash2)
            cache.conn.commit()
            cache.put_embedding(text_hash2, np.random.randn(128).astype(np.float32))

            # refresh_if_needed should be throttled — count shouldn't change
            si.refresh_if_needed()
            assert si.message_count == count_after_first

    def test_refresh_not_throttled_before_first_load(self, cache):
        """Before first successful load, every refresh attempt is allowed."""
        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock

            si = SearchIndex()
            assert si._ever_loaded is False

            # Multiple refreshes should all be allowed (not throttled)
            si.refresh_if_needed()
            si.refresh_if_needed()
            si.refresh_if_needed()
            # No error, no throttle — just no data
            assert si.message_count == 0


# --- handle_message_request (full stored text by uuid) ---


class TestHandleMessageRequest:
    """The /message read path: fetch complete stored text, bypassing the
    2000-char cap that search results apply."""

    def _store(self, cache, uuid, text, tier="conversation", tool_name=None, idx=0):
        cache.put_message(
            uuid, "sess-1", "/project/alpha", time.time(), "assistant",
            text, idx, "ide", hashlib.md5(text.encode()).hexdigest(),
            tier, tool_name,
        )
        cache.conn.commit()

    def _call(self, cache, request):
        from kiro_ception.search import handle_message_request

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = cache
            mock_get_indexer.return_value = indexer_mock
            return handle_message_request(request)

    def test_returns_full_text_beyond_display_cap(self, cache):
        long_text = "y" * 7000
        self._store(cache, "m1", long_text)

        response = self._call(cache, {"uuids": ["m1"]})

        assert response["status"] == "ok"
        assert response["messages"][0]["content"] == long_text
        assert len(response["messages"][0]["content"]) == 7000

    def test_includes_locating_metadata(self, cache):
        self._store(cache, "m1", "hello", idx=4)

        msg = self._call(cache, {"uuids": ["m1"]})["messages"][0]

        assert msg["uuid"] == "m1"
        assert msg["role"] == "assistant"
        assert msg["session_id"] == "sess-1"
        assert msg["workspace"] == "/project/alpha"
        assert msg["message_index"] == 4
        assert msg["source"] == "ide"
        assert msg["content_tier"] == "conversation"
        assert "T" in msg["timestamp"]  # ISO formatted

    def test_missing_uuids_reported_not_raised(self, cache):
        self._store(cache, "m1", "hello")

        response = self._call(cache, {"uuids": ["m1", "ghost"]})

        assert [m["uuid"] for m in response["messages"]] == ["m1"]
        assert response["not_found"] == ["ghost"]

    def test_accepts_bare_string_uuid(self, cache):
        self._store(cache, "m1", "hello")

        response = self._call(cache, {"uuids": "m1"})

        assert [m["uuid"] for m in response["messages"]] == ["m1"]

    def test_missing_uuids_key_returns_empty(self, cache):
        response = self._call(cache, {})

        assert response["status"] == "ok"
        assert response["messages"] == []
        assert response["not_found"] == []

    def test_tool_context_carries_index_time_truncation_note(self, cache):
        """tool_context rows were capped before storage, so the caller must be
        told that this text is still not the full original."""
        self._store(cache, "m1", "[Bash] ls -la → completed", tier="tool_context",
                    tool_name="Bash")

        msg = self._call(cache, {"uuids": ["m1"]})["messages"][0]

        assert msg["content_tier"] == "tool_context"
        assert msg["tool_name"] == "Bash"
        assert "max_summary_length" in msg["note"]

    def test_conversation_tier_has_no_note(self, cache):
        self._store(cache, "m1", "hello")

        msg = self._call(cache, {"uuids": ["m1"]})["messages"][0]

        assert "note" not in msg

    def test_still_loading_when_database_not_open(self):
        from kiro_ception.search import handle_message_request

        with patch("kiro_ception.search.get_background_indexer") as mock_get_indexer:
            indexer_mock = MagicMock()
            indexer_mock.cache = None
            mock_get_indexer.return_value = indexer_mock

            response = handle_message_request({"uuids": ["m1"]})

        assert response["status"] == "still_loading"
        assert response["messages"] == []
        assert response["not_found"] == ["m1"]
        assert "hint" in response

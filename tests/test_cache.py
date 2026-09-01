"""Integration tests for cache.py — SQLite-backed embedding and metadata storage.

Uses a real temporary SQLite database per test (via tmp_path fixture).
Validates actual read/write behavior, not mocked interactions.
"""

import time

import numpy as np
import pytest

from kiro_ception.cache import EmbeddingCache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Create an EmbeddingCache pointing at a temp directory."""
    monkeypatch.setattr(
        "kiro_ception.cache._get_cache_db_path",
        lambda fp: tmp_path / f"cache_{fp}.db",
    )
    c = EmbeddingCache("test-fingerprint")
    # Force connection/table creation
    _ = c.conn
    yield c
    c.close()


# --- Embedding operations ---


class TestEmbeddingOperations:
    def test_put_and_get_embedding(self, cache):
        emb = np.random.randn(1024).astype(np.float32)
        cache.put_embedding("hash1", emb)

        result = cache.get_embedding("hash1")
        assert result is not None
        np.testing.assert_array_almost_equal(result, emb)

    def test_get_nonexistent_embedding_returns_none(self, cache):
        assert cache.get_embedding("nonexistent") is None

    def test_has_embedding(self, cache):
        emb = np.random.randn(384).astype(np.float32)
        assert cache.has_embedding("hash1") is False
        cache.put_embedding("hash1", emb)
        assert cache.has_embedding("hash1") is True

    def test_put_embeddings_batch(self, cache):
        items = [
            (f"hash_{i}", np.random.randn(1024).astype(np.float32))
            for i in range(10)
        ]
        cache.put_embeddings_batch(items)

        assert cache.embedding_count == 10
        for text_hash, expected_emb in items:
            result = cache.get_embedding(text_hash)
            np.testing.assert_array_almost_equal(result, expected_emb)

    def test_get_embeddings_batch(self, cache):
        items = [
            (f"hash_{i}", np.random.randn(512).astype(np.float32))
            for i in range(5)
        ]
        cache.put_embeddings_batch(items)

        hashes = [h for h, _ in items]
        result = cache.get_embeddings_batch(hashes)

        assert len(result) == 5
        for text_hash, expected_emb in items:
            np.testing.assert_array_almost_equal(result[text_hash], expected_emb)

    def test_get_embeddings_batch_with_missing(self, cache):
        emb = np.random.randn(256).astype(np.float32)
        cache.put_embedding("exists", emb)

        result = cache.get_embeddings_batch(["exists", "missing"])
        assert "exists" in result
        assert "missing" not in result

    def test_get_embeddings_batch_empty_input(self, cache):
        assert cache.get_embeddings_batch([]) == {}

    def test_get_all_embedding_hashes(self, cache):
        items = [(f"hash_{i}", np.random.randn(128).astype(np.float32)) for i in range(3)]
        cache.put_embeddings_batch(items)

        hashes = cache.get_all_embedding_hashes()
        assert hashes == {"hash_0", "hash_1", "hash_2"}

    def test_embedding_count(self, cache):
        assert cache.embedding_count == 0
        cache.put_embedding("h1", np.random.randn(64).astype(np.float32))
        assert cache.embedding_count == 1
        cache.put_embedding("h2", np.random.randn(64).astype(np.float32))
        assert cache.embedding_count == 2

    def test_put_embedding_upsert(self, cache):
        """Putting the same hash twice should overwrite (not duplicate)."""
        emb1 = np.ones(128, dtype=np.float32)
        emb2 = np.zeros(128, dtype=np.float32)

        cache.put_embedding("hash1", emb1)
        cache.put_embedding("hash1", emb2)

        assert cache.embedding_count == 1
        result = cache.get_embedding("hash1")
        np.testing.assert_array_equal(result, emb2)

    def test_preserves_dimensions(self, cache):
        """Embeddings with different dimensions are stored/retrieved correctly."""
        emb_small = np.random.randn(128).astype(np.float32)
        emb_large = np.random.randn(2048).astype(np.float32)

        cache.put_embedding("small", emb_small)
        cache.put_embedding("large", emb_large)

        assert cache.get_embedding("small").shape == (128,)
        assert cache.get_embedding("large").shape == (2048,)


# --- Message operations ---


class TestMessageOperations:
    def _make_message_tuple(self, uuid="msg-1", session_id="sess-1", workspace="/test",
                            timestamp=None, role="user", text="hello world",
                            message_index=0, source="ide", text_hash="hash1"):
        if timestamp is None:
            timestamp = time.time()
        return (uuid, session_id, workspace, timestamp, role, text, message_index, source, text_hash)

    def test_put_and_get_messages(self, cache):
        msg = self._make_message_tuple()
        cache.put_message(*msg)
        cache.conn.commit()

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 1
        assert messages[0]["uuid"] == "msg-1"
        assert messages[0]["role"] == "user"
        assert messages[0]["searchable_text"] == "hello world"

    def test_put_messages_batch(self, cache):
        msgs = [
            self._make_message_tuple(uuid=f"msg-{i}", message_index=i, text=f"text {i}")
            for i in range(5)
        ]
        cache.put_messages_batch(msgs)

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 5
        # Should be sorted by message_index
        for i, msg in enumerate(messages):
            assert msg["message_index"] == i

    def test_get_session_messages_sorted_by_index(self, cache):
        # Insert out of order
        msgs = [
            self._make_message_tuple(uuid="msg-3", message_index=3),
            self._make_message_tuple(uuid="msg-1", message_index=1),
            self._make_message_tuple(uuid="msg-2", message_index=2),
        ]
        cache.put_messages_batch(msgs)

        messages = cache.get_session_messages("sess-1")
        indices = [m["message_index"] for m in messages]
        assert indices == [1, 2, 3]

    def test_get_session_messages_empty(self, cache):
        assert cache.get_session_messages("nonexistent") == []

    def test_get_messages_returns_untruncated_text(self, cache):
        """get_messages must not apply the 2000-char search display cap."""
        long_text = "x" * 5000
        cache.put_messages_batch([self._make_message_tuple(text=long_text)])

        messages = cache.get_messages(["msg-1"])
        assert len(messages) == 1
        assert messages[0]["searchable_text"] == long_text
        assert len(messages[0]["searchable_text"]) == 5000
        assert "..." not in messages[0]["searchable_text"]

    def test_get_messages_includes_full_column_set(self, cache):
        cache.put_messages_batch([self._make_message_tuple()])
        msg = cache.get_messages(["msg-1"])[0]
        for col in ("uuid", "session_id", "workspace", "timestamp", "role",
                    "searchable_text", "message_index", "source", "text_hash",
                    "content_tier", "tool_name"):
            assert col in msg

    def test_get_messages_empty_list_returns_empty(self, cache):
        """Must short-circuit rather than emitting a malformed IN () query."""
        assert cache.get_messages([]) == []

    def test_get_messages_skips_missing_uuids(self, cache):
        cache.put_messages_batch([
            self._make_message_tuple(uuid="msg-1", message_index=0),
            self._make_message_tuple(uuid="msg-2", message_index=1),
        ])

        messages = cache.get_messages(["msg-1", "nope", "msg-2"])
        assert [m["uuid"] for m in messages] == ["msg-1", "msg-2"]

    def test_get_messages_preserves_requested_order(self, cache):
        cache.put_messages_batch([
            self._make_message_tuple(uuid=f"msg-{i}", message_index=i)
            for i in range(4)
        ])

        requested = ["msg-3", "msg-0", "msg-2"]
        messages = cache.get_messages(requested)
        assert [m["uuid"] for m in messages] == requested

    def test_get_messages_batches_beyond_sqlite_param_limit(self, cache):
        """More than 500 uuids must be split across statements."""
        cache.put_messages_batch([
            self._make_message_tuple(uuid=f"msg-{i}", message_index=i)
            for i in range(1200)
        ])

        requested = [f"msg-{i}" for i in range(1200)]
        messages = cache.get_messages(requested)
        assert [m["uuid"] for m in messages] == requested

    def test_get_messages_duplicate_uuid_requested_once(self, cache):
        cache.put_messages_batch([self._make_message_tuple()])
        messages = cache.get_messages(["msg-1", "msg-1"])
        assert [m["uuid"] for m in messages] == ["msg-1", "msg-1"]

    def test_delete_session_messages(self, cache):
        msgs = [self._make_message_tuple(uuid=f"msg-{i}", message_index=i) for i in range(3)]
        cache.put_messages_batch(msgs)
        assert cache.message_count == 3

        cache.delete_session_messages("sess-1")
        cache.conn.commit()
        assert cache.message_count == 0
        assert cache.get_session_messages("sess-1") == []

    def test_message_count(self, cache):
        assert cache.message_count == 0
        msgs = [
            self._make_message_tuple(uuid="m1", session_id="s1", message_index=0),
            self._make_message_tuple(uuid="m2", session_id="s2", message_index=0),
        ]
        cache.put_messages_batch(msgs)
        assert cache.message_count == 2

    def test_messages_filtered_by_session(self, cache):
        msgs = [
            self._make_message_tuple(uuid="m1", session_id="s1", message_index=0),
            self._make_message_tuple(uuid="m2", session_id="s2", message_index=0),
            self._make_message_tuple(uuid="m3", session_id="s1", message_index=1),
        ]
        cache.put_messages_batch(msgs)

        s1_msgs = cache.get_session_messages("s1")
        assert len(s1_msgs) == 2
        s2_msgs = cache.get_session_messages("s2")
        assert len(s2_msgs) == 1


# --- Session state operations ---


class TestSessionState:
    def test_get_session_mtime_not_tracked(self, cache):
        assert cache.get_session_mtime("unknown-session") is None

    def test_update_and_get_session_state(self, cache):
        mtime = 1717614000.0
        cache.update_session_state("sess-1", mtime)

        result = cache.get_session_mtime("sess-1")
        assert result == mtime

    def test_update_session_state_upsert(self, cache):
        cache.update_session_state("sess-1", 100.0)
        cache.update_session_state("sess-1", 200.0)

        assert cache.get_session_mtime("sess-1") == 200.0

    def test_clear_session_state(self, cache):
        cache.update_session_state("sess-1", 100.0)
        cache.update_session_state("sess-2", 200.0)
        assert cache.indexed_session_count == 2

        cache.clear_session_state()
        assert cache.indexed_session_count == 0
        assert cache.get_session_mtime("sess-1") is None

    def test_indexed_session_count(self, cache):
        assert cache.indexed_session_count == 0
        cache.update_session_state("s1", 100.0)
        cache.update_session_state("s2", 200.0)
        assert cache.indexed_session_count == 2

    def test_session_state_scoped_by_fingerprint(self, tmp_path, monkeypatch):
        """Sessions from a different fingerprint shouldn't count."""
        monkeypatch.setattr(
            "kiro_ception.cache._get_cache_db_path",
            lambda fp: tmp_path / f"cache_{fp}.db",
        )
        cache1 = EmbeddingCache("fingerprint-A")
        cache1.update_session_state("sess-1", 100.0)
        assert cache1.indexed_session_count == 1

        # A different fingerprint on same DB won't see the session
        # (because indexed_session_count filters by fingerprint)
        # But they use different DB files due to _get_cache_db_path
        cache2 = EmbeddingCache("fingerprint-B")
        assert cache2.indexed_session_count == 0

        cache1.close()
        cache2.close()


# --- Execution index operations ---


class TestExecutionIndex:
    def test_put_and_get_exec_files(self, cache):
        cache.put_exec_file("/path/to/exec1.json", "sess-1", 1000.0)
        cache.put_exec_file("/path/to/exec2.json", "sess-1", 2000.0)
        cache.conn.commit()

        files = cache.get_exec_files_for_session("sess-1")
        assert set(files) == {"/path/to/exec1.json", "/path/to/exec2.json"}

    def test_put_exec_files_batch(self, cache):
        items = [
            ("/exec/a.json", "sess-1", 100.0),
            ("/exec/b.json", "sess-1", 200.0),
            ("/exec/c.json", "sess-2", 300.0),
        ]
        cache.put_exec_files_batch(items)

        assert cache.get_exec_index_count() == 3
        assert len(cache.get_exec_files_for_session("sess-1")) == 2
        assert len(cache.get_exec_files_for_session("sess-2")) == 1

    def test_get_all_exec_file_mtimes(self, cache):
        items = [
            ("/exec/a.json", "sess-1", 100.0),
            ("/exec/b.json", "sess-2", 200.0),
        ]
        cache.put_exec_files_batch(items)

        mtimes = cache.get_all_exec_file_mtimes()
        assert mtimes == {"/exec/a.json": 100.0, "/exec/b.json": 200.0}

    def test_get_exec_files_empty_session(self, cache):
        assert cache.get_exec_files_for_session("unknown") == []

    def test_exec_file_upsert(self, cache):
        cache.put_exec_file("/exec/a.json", "sess-1", 100.0)
        cache.put_exec_file("/exec/a.json", "sess-1", 200.0)
        cache.conn.commit()

        assert cache.get_exec_index_count() == 1
        mtimes = cache.get_all_exec_file_mtimes()
        assert mtimes["/exec/a.json"] == 200.0


# --- Content tier operations ---


class TestContentTierStorage:
    """Tests for content_tier and tool_name storage and filtering."""

    def _make_message_tuple(self, uuid="msg-1", session_id="sess-1", workspace="/test",
                            timestamp=None, role="user", text="hello world",
                            message_index=0, source="ide", text_hash="hash1",
                            content_tier="conversation", tool_name=None):
        if timestamp is None:
            timestamp = time.time()
        return (uuid, session_id, workspace, timestamp, role, text,
                message_index, source, text_hash, content_tier, tool_name)

    def test_put_message_with_content_tier(self, cache):
        """Storing a message with content_tier persists it correctly."""
        cache.put_message(
            uuid="msg-1", session_id="sess-1", workspace="/test",
            timestamp=time.time(), role="assistant", searchable_text="tool summary",
            message_index=0, source="ide", text_hash="hash1",
            content_tier="tool_context", tool_name="readFile",
        )
        cache.conn.commit()

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 1
        assert messages[0]["content_tier"] == "tool_context"
        assert messages[0]["tool_name"] == "readFile"

    def test_put_message_defaults_to_conversation(self, cache):
        """A message without explicit content_tier defaults to 'conversation'."""
        cache.put_message(
            uuid="msg-1", session_id="sess-1", workspace="/test",
            timestamp=time.time(), role="user", searchable_text="hello",
            message_index=0, source="ide", text_hash="hash1",
        )
        cache.conn.commit()

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 1
        assert messages[0]["content_tier"] == "conversation"
        assert messages[0]["tool_name"] is None

    def test_put_messages_batch_with_content_tier(self, cache):
        """Batch insert with 11-element tuples persists content_tier and tool_name."""
        msgs = [
            self._make_message_tuple(
                uuid="msg-1", text="user prompt", role="user",
                content_tier="conversation", tool_name=None, message_index=0,
            ),
            self._make_message_tuple(
                uuid="msg-2", text="[readFile] /src/app.ts → completed",
                role="assistant", content_tier="tool_context",
                tool_name="readFile", message_index=1,
            ),
            self._make_message_tuple(
                uuid="msg-3", text="assistant response", role="assistant",
                content_tier="conversation", tool_name=None, message_index=2,
            ),
        ]
        cache.put_messages_batch(msgs)

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 3
        assert messages[0]["content_tier"] == "conversation"
        assert messages[0]["tool_name"] is None
        assert messages[1]["content_tier"] == "tool_context"
        assert messages[1]["tool_name"] == "readFile"
        assert messages[2]["content_tier"] == "conversation"
        assert messages[2]["tool_name"] is None

    def test_put_messages_batch_backward_compat_9_element_tuples(self, cache):
        """9-element tuples (legacy format) default to conversation tier."""
        msgs = [
            ("msg-1", "sess-1", "/test", time.time(), "user", "hello",
             0, "ide", "hash1"),
        ]
        cache.put_messages_batch(msgs)

        messages = cache.get_session_messages("sess-1")
        assert len(messages) == 1
        assert messages[0]["content_tier"] == "conversation"
        assert messages[0]["tool_name"] is None

    def test_get_session_messages_returns_content_tier_fields(self, cache):
        """get_session_messages includes content_tier and tool_name in results."""
        cache.put_message(
            uuid="msg-1", session_id="sess-1", workspace="/test",
            timestamp=time.time(), role="assistant", searchable_text="summary",
            message_index=0, source="ide", text_hash="hash1",
            content_tier="tool_context", tool_name="grep_search",
        )
        cache.conn.commit()

        messages = cache.get_session_messages("sess-1")
        assert "content_tier" in messages[0]
        assert "tool_name" in messages[0]
        assert messages[0]["content_tier"] == "tool_context"
        assert messages[0]["tool_name"] == "grep_search"


class TestFtsSearchContentTierFiltering:
    """Tests for fts_search content_tier filtering."""

    def _insert_mixed_tier_messages(self, cache):
        """Insert messages of both tiers for search testing."""
        now = time.time()
        msgs = [
            ("msg-conv-1", "sess-1", "/test", now, "user",
             "deploy the application server", 0, "ide", "h1",
             "conversation", None),
            ("msg-tool-1", "sess-1", "/test", now, "assistant",
             "[execute_pwsh] deploy script → completed", 1, "ide", "h2",
             "tool_context", "execute_pwsh"),
            ("msg-conv-2", "sess-1", "/test", now, "assistant",
             "I deployed the application server successfully", 2, "ide", "h3",
             "conversation", None),
            ("msg-tool-2", "sess-1", "/test", now, "assistant",
             "[readFile] /deploy/config.yaml → completed", 3, "ide", "h4",
             "tool_context", "readFile"),
        ]
        cache.put_messages_batch(msgs)

    def test_fts_default_returns_conversation_only(self, cache):
        """By default, fts_search only matches conversation tier messages."""
        self._insert_mixed_tier_messages(cache)

        results = cache.fts_search("deploy")
        # Should find "deploy the application server" and "I deployed..." but NOT
        # the tool_context messages
        assert len(results) > 0
        for r in results:
            assert r["content_tier"] == "conversation"

    def test_fts_include_tool_context_returns_both_tiers(self, cache):
        """With include_tool_context=True, search matches both tiers."""
        self._insert_mixed_tier_messages(cache)

        results = cache.fts_search("deploy", include_tool_context=True)
        tiers = {r["content_tier"] for r in results}
        # Should find results in both tiers
        assert "conversation" in tiers
        assert "tool_context" in tiers

    def test_fts_results_include_content_tier_field(self, cache):
        """Search results include content_tier in the returned dicts."""
        self._insert_mixed_tier_messages(cache)

        results = cache.fts_search("deploy", include_tool_context=True)
        assert len(results) > 0
        for r in results:
            assert "content_tier" in r

    def test_fts_results_include_tool_name_when_present(self, cache):
        """Search results include tool_name for tool_context matches."""
        self._insert_mixed_tier_messages(cache)

        results = cache.fts_search("readFile", include_tool_context=True)
        tool_results = [r for r in results if r["content_tier"] == "tool_context"]
        assert len(tool_results) > 0
        assert tool_results[0].get("tool_name") == "readFile"

    def test_fts_conversation_results_omit_tool_name(self, cache):
        """Conversation results don't include tool_name key when it's None."""
        self._insert_mixed_tier_messages(cache)

        results = cache.fts_search("deploy")
        conv_results = [r for r in results if r["content_tier"] == "conversation"]
        assert len(conv_results) > 0
        for r in conv_results:
            assert "tool_name" not in r

    def test_fts_tool_context_excluded_by_default(self, cache):
        """A query that only matches tool_context returns empty when default filtering."""
        now = time.time()
        msgs = [
            ("msg-tool-only", "sess-1", "/test", now, "assistant",
             "[grep_search] uniquepattern123 (5 results) → completed", 0, "ide", "h1",
             "tool_context", "grep_search"),
        ]
        cache.put_messages_batch(msgs)

        # Without include_tool_context, this should not match
        results = cache.fts_search("uniquepattern123")
        assert len(results) == 0

        # With include_tool_context, it should match
        results = cache.fts_search("uniquepattern123", include_tool_context=True)
        assert len(results) == 1
        assert results[0]["content_tier"] == "tool_context"

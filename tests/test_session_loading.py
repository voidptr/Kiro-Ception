"""Integration tests for session loading — all three IDE formats and CLI.

Uses fixture files that mirror the real formats:
- Legacy .chat: actionId/chat/metadata structure with human/bot roles
- Workspace-sessions: history[].message structure with user/assistant roles
- Execution logs: actions[] with actionType="say" for assistant responses
- CLI: SQLite database with conversations_v2 table
"""

import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_ception.models import IndexedMessage, SessionInfo, Source

FIXTURES = Path(__file__).parent / "fixtures"


# --- Legacy .chat format ---


class TestLegacySessionLoading:
    @pytest.fixture
    def legacy_chat_dir(self, tmp_path):
        """Set up a temporary directory with legacy .chat files."""
        workspace_hash = "abcdef1234567890abcdef1234567890"
        chat_dir = tmp_path / workspace_hash
        chat_dir.mkdir()
        # Copy fixture file as a .chat session
        src = FIXTURES / "legacy_session.chat"
        dst = chat_dir / "test-session-id.chat"
        shutil.copy(src, dst)
        return tmp_path, chat_dir

    def test_load_legacy_session_messages(self, legacy_chat_dir):
        tmp_path, chat_dir = legacy_chat_dir
        chat_file = chat_dir / "test-session-id.chat"

        with patch("kiro_ception.ide_loader.get_chat_files", return_value=[chat_file]):
            from kiro_ception.ide_loader import _load_legacy_session_messages

            session = SessionInfo(
                session_id="test-session-id",
                workspace=chat_dir.name,
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1, 1, 0),
                source=Source.IDE,
            )
            messages = _load_legacy_session_messages(session)

        # Should have user and assistant messages (human->user, bot->assistant)
        # "tool" role should be skipped, system prompt content skipped
        assert len(messages) > 0
        roles = [m.role for m in messages]
        assert "user" in roles
        assert "assistant" in roles
        # Tool messages should not appear
        assert "tool" not in roles

    def test_legacy_code_blocks_replaced(self, legacy_chat_dir):
        tmp_path, chat_dir = legacy_chat_dir
        chat_file = chat_dir / "test-session-id.chat"

        with patch("kiro_ception.ide_loader.get_chat_files", return_value=[chat_file]):
            from kiro_ception.ide_loader import _load_legacy_session_messages

            session = SessionInfo(
                session_id="test-session-id",
                workspace=chat_dir.name,
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1, 1, 0),
                source=Source.IDE,
            )
            messages = _load_legacy_session_messages(session)

        # Code blocks should be replaced with placeholders
        for msg in messages:
            assert "```" not in msg.searchable_text
            # Should contain code placeholders instead
            if msg.role == "assistant":
                assert "[code:nginx]" in msg.searchable_text or "[code:" not in msg.searchable_text

    def test_legacy_system_prompts_filtered(self, tmp_path):
        """Messages starting with system prompt prefixes are skipped."""
        chat_dir = tmp_path / "workspace_hash"
        chat_dir.mkdir()
        chat_file = chat_dir / "sys-prompt-session.chat"
        chat_file.write_text(json.dumps({
            "executionId": "test",
            "actionId": "act",
            "context": [],
            "validations": [],
            "chat": [
                {"role": "human", "content": "<identity>\nYou are Kiro, an AI assistant."},
                {"role": "assistant", "content": "Hello! How can I help?"},
                {"role": "human", "content": "## Included Rules\nSome steering rules here."},
                {"role": "human", "content": "What does this function do?"},
                {"role": "assistant", "content": "It calculates the average of a list."},
            ],
            "metadata": {"startTime": 1717200000000, "endTime": 1717200060000},
        }))

        with patch("kiro_ception.ide_loader.get_chat_files", return_value=[chat_file]):
            from kiro_ception.ide_loader import _load_legacy_session_messages

            session = SessionInfo(
                session_id="sys-prompt-session",
                workspace=chat_dir.name,
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1),
                source=Source.IDE,
            )
            messages = _load_legacy_session_messages(session)

        # System prompt and steering rules messages should be filtered out
        texts = [m.searchable_text for m in messages]
        assert not any("<identity>" in t for t in texts)
        assert not any("## Included Rules" in t for t in texts)
        # Real content should remain
        assert any("What does this function do?" in t for t in texts)
        assert any("calculates the average" in t for t in texts)

    def test_list_legacy_sessions(self, legacy_chat_dir):
        tmp_path, chat_dir = legacy_chat_dir
        chat_file = chat_dir / "test-session-id.chat"

        with patch("kiro_ception.ide_loader.get_chat_files", return_value=[chat_file]):
            from kiro_ception.ide_loader import _list_legacy_sessions

            sessions = _list_legacy_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "test-session-id"
        assert sessions[0].source == Source.IDE


# --- Workspace-sessions format ---


class TestWorkspaceSessionLoading:
    @pytest.fixture
    def workspace_sessions_dir(self, tmp_path):
        """Set up a workspace-sessions directory structure."""
        # workspace-sessions/<base64_workspace>/<uuid>.json
        ws_sessions = tmp_path / "workspace-sessions"
        encoded_workspace = "L1VzZXJzL2Rldi9wcm9qZWN0cy9teS1hcHA_"  # base64 encoded
        workspace_dir = ws_sessions / encoded_workspace
        workspace_dir.mkdir(parents=True)

        # Copy fixture
        src = FIXTURES / "workspace_session.json"
        dst = workspace_dir / "session-ws-001.json"
        shutil.copy(src, dst)

        # Also add sessions.json metadata
        src_meta = FIXTURES / "sessions.json"
        shutil.copy(src_meta, workspace_dir / "sessions.json")

        return ws_sessions, workspace_dir

    def test_load_workspace_session_messages(self, workspace_sessions_dir):
        ws_sessions, workspace_dir = workspace_sessions_dir

        with (
            patch("kiro_ception.ide_loader._get_workspace_sessions_dirs", return_value=[ws_sessions]),
            patch("kiro_ception.ide_loader._build_execution_index", return_value={}),
        ):
            from kiro_ception.ide_loader import _load_workspace_session_messages

            session = SessionInfo(
                session_id="session-ws-001",
                workspace="/Users/dev/projects/my-app",
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1, 1, 0),
                source=Source.IDE,
            )
            messages = _load_workspace_session_messages(session)

        assert len(messages) == 4
        # Check roles
        roles = [m.role for m in messages]
        assert roles == ["user", "assistant", "user", "assistant"]
        # Check content extraction
        assert "health check endpoint" in messages[0].searchable_text
        assert "Express server" in messages[0].searchable_text

    def test_workspace_session_code_blocks_replaced(self, workspace_sessions_dir):
        ws_sessions, workspace_dir = workspace_sessions_dir

        with (
            patch("kiro_ception.ide_loader._get_workspace_sessions_dirs", return_value=[ws_sessions]),
            patch("kiro_ception.ide_loader._build_execution_index", return_value={}),
        ):
            from kiro_ception.ide_loader import _load_workspace_session_messages

            session = SessionInfo(
                session_id="session-ws-001",
                workspace="/Users/dev/projects/my-app",
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1, 1, 0),
                source=Source.IDE,
            )
            messages = _load_workspace_session_messages(session)

        # Code blocks should be replaced
        for msg in messages:
            assert "```" not in msg.searchable_text
        # Check assistant messages have code placeholders
        assistant_texts = [m.searchable_text for m in messages if m.role == "assistant"]
        assert any("[code:typescript]" in t for t in assistant_texts)

    def test_workspace_session_uses_workspace_from_file(self, workspace_sessions_dir):
        ws_sessions, workspace_dir = workspace_sessions_dir

        with (
            patch("kiro_ception.ide_loader._get_workspace_sessions_dirs", return_value=[ws_sessions]),
            patch("kiro_ception.ide_loader._build_execution_index", return_value={}),
        ):
            from kiro_ception.ide_loader import _load_workspace_session_messages

            session = SessionInfo(
                session_id="session-ws-001",
                workspace="placeholder",
                created=datetime(2026, 6, 1),
                modified=datetime(2026, 6, 1, 1, 0),
                source=Source.IDE,
            )
            messages = _load_workspace_session_messages(session)

        # workspace should come from file's workspaceDirectory field
        for msg in messages:
            assert msg.workspace == "/Users/dev/projects/my-app"

    def test_list_workspace_sessions(self, workspace_sessions_dir):
        ws_sessions, workspace_dir = workspace_sessions_dir

        with patch("kiro_ception.ide_loader._get_workspace_sessions_dirs", return_value=[ws_sessions]):
            from kiro_ception.ide_loader import _list_workspace_sessions

            sessions = _list_workspace_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "session-ws-001"
        assert sessions[0].source == Source.IDE


# --- Execution logs ---


class TestExecutionLogLoading:
    @pytest.fixture
    def exec_log_dir(self, tmp_path):
        """Set up execution log directory structure."""
        exec_dir = tmp_path / "exec_logs"
        exec_dir.mkdir()
        src = FIXTURES / "execution_log.json"
        shutil.copy(src, exec_dir / "exec-001-hash")
        return exec_dir

    def test_load_execution_log_messages(self, exec_log_dir, tmp_path, monkeypatch):
        """Execution logs with actionType=say should produce assistant messages."""
        # Build a mock execution index
        exec_index = {
            "session-abc-123": [str(exec_log_dir / "exec-001-hash")]
        }

        with patch("kiro_ception.ide_loader._build_execution_index", return_value=exec_index):
            from kiro_ception.ide_loader import _load_execution_log_messages

            messages = _load_execution_log_messages(
                session_id="session-abc-123",
                workspace="/Users/dev/projects/my-app",
                timestamp=datetime(2026, 6, 1),
            )

        # Should only get "say" actions, not readFile/writeFile
        assert len(messages) == 2
        for msg in messages:
            assert msg.role == "assistant"
            assert msg.source == Source.IDE

    def test_execution_log_code_blocks_replaced(self, exec_log_dir):
        exec_index = {
            "session-abc-123": [str(exec_log_dir / "exec-001-hash")]
        }

        with patch("kiro_ception.ide_loader._build_execution_index", return_value=exec_index):
            from kiro_ception.ide_loader import _load_execution_log_messages

            messages = _load_execution_log_messages(
                session_id="session-abc-123",
                workspace="/Users/dev/projects/my-app",
                timestamp=datetime(2026, 6, 1),
            )

        # Code blocks in say messages should be replaced
        for msg in messages:
            assert "```" not in msg.searchable_text
        assert any("[code:typescript]" in m.searchable_text for m in messages)

    def test_execution_log_empty_session(self):
        """Session with no execution logs returns empty list."""
        with patch("kiro_ception.ide_loader._build_execution_index", return_value={}):
            from kiro_ception.ide_loader import _load_execution_log_messages

            messages = _load_execution_log_messages(
                session_id="nonexistent-session",
                workspace="/test",
                timestamp=datetime(2026, 6, 1),
            )

        assert messages == []

    def test_execution_log_timestamps(self, exec_log_dir):
        """Messages should use emittedAt for timestamps."""
        exec_index = {
            "session-abc-123": [str(exec_log_dir / "exec-001-hash")]
        }

        with patch("kiro_ception.ide_loader._build_execution_index", return_value=exec_index):
            from kiro_ception.ide_loader import _load_execution_log_messages

            messages = _load_execution_log_messages(
                session_id="session-abc-123",
                workspace="/test",
                timestamp=datetime(2026, 6, 1),
            )

        # emittedAt is in milliseconds, so timestamps should differ
        assert messages[0].timestamp != messages[1].timestamp


# --- CLI loader ---


class TestCLILoading:
    @pytest.fixture(autouse=True)
    def _isolate_jsonl(self):
        """Keep the SQLite-focused tests hermetic.

        list_cli_sessions() now unions the JSONL session store with the legacy
        SQLite DB. These tests only exercise the SQLite path, so stub the JSONL
        roots to empty — otherwise they'd pick up the developer's real
        ~/.kiro/sessions/cli and the counts would be non-deterministic.
        """
        with patch(
            "kiro_ception.cli_loader._list_jsonl_sessions", return_value=[]
        ):
            yield

    @pytest.fixture
    def cli_db(self, tmp_path):
        """Create a temporary CLI SQLite database with test data."""
        db_path = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE conversations_v2 (
                key TEXT,
                conversation_id TEXT,
                value TEXT,
                created_at INTEGER,
                updated_at INTEGER
            )
        """)

        # Insert a test conversation
        conversation_data = json.dumps({
            "conversation_id": "cli-conv-001",
            "history": [
                {
                    "user": {
                        "content": {"Prompt": {"prompt": "List all running Docker containers"}},
                        "timestamp": "2026-06-01T10:00:00.000Z",
                    },
                    "assistant": {
                        "content": "Here are the running containers:\n\nCONTAINER ID   IMAGE          STATUS\nabc123         nginx:latest   Up 2 hours\ndef456         postgres:15    Up 3 hours"
                    }
                },
                {
                    "user": {
                        "content": {"Prompt": {"prompt": "Stop the nginx container"}},
                        "timestamp": "2026-06-01T10:01:00.000Z",
                    },
                    "assistant": {
                        "content": "Stopped container abc123 (nginx:latest)."
                    }
                }
            ]
        })

        conn.execute(
            "INSERT INTO conversations_v2 (key, conversation_id, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("/Users/dev/projects", "cli-conv-001", conversation_data, 1717236000000, 1717236120000),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_list_cli_sessions(self, cli_db):
        with patch("kiro_ception.cli_loader.get_database_path", return_value=cli_db):
            from kiro_ception.cli_loader import list_cli_sessions

            sessions = list_cli_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "cli-conv-001"
        assert sessions[0].workspace == "/Users/dev/projects"
        assert sessions[0].source == Source.CLI

    def test_load_cli_session_messages(self, cli_db):
        with patch("kiro_ception.cli_loader.get_database_path", return_value=cli_db):
            from kiro_ception.cli_loader import list_cli_sessions, load_cli_session_messages

            sessions = list_cli_sessions()
            messages = load_cli_session_messages(sessions[0])

        # 2 entries × 2 roles = 4 messages
        assert len(messages) == 4
        roles = [m.role for m in messages]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_cli_prompt_extraction(self, cli_db):
        """The {"Prompt": {"prompt": "..."}} structure should be extracted."""
        with patch("kiro_ception.cli_loader.get_database_path", return_value=cli_db):
            from kiro_ception.cli_loader import list_cli_sessions, load_cli_session_messages

            sessions = list_cli_sessions()
            messages = load_cli_session_messages(sessions[0])

        user_msgs = [m for m in messages if m.role == "user"]
        assert "List all running Docker containers" in user_msgs[0].searchable_text
        assert "Stop the nginx container" in user_msgs[1].searchable_text

    def test_cli_response_extraction(self, cli_db):
        """Assistant Response content should be extracted."""
        with patch("kiro_ception.cli_loader.get_database_path", return_value=cli_db):
            from kiro_ception.cli_loader import list_cli_sessions, load_cli_session_messages

            sessions = list_cli_sessions()
            messages = load_cli_session_messages(sessions[0])

        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert "running containers" in assistant_msgs[0].searchable_text
        assert "Stopped container" in assistant_msgs[1].searchable_text

    def test_cli_empty_database(self, tmp_path):
        """Empty database returns no sessions."""
        db_path = tmp_path / "empty.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE conversations_v2 (
                key TEXT, conversation_id TEXT, value TEXT,
                created_at INTEGER, updated_at INTEGER
            )
        """)
        conn.commit()
        conn.close()

        with patch("kiro_ception.cli_loader.get_database_path", return_value=db_path):
            from kiro_ception.cli_loader import list_cli_sessions

            sessions = list_cli_sessions()

        assert sessions == []

    def test_cli_nonexistent_database(self, tmp_path):
        """Nonexistent database returns no sessions."""
        with patch("kiro_ception.cli_loader.get_database_path", return_value=tmp_path / "missing.db"):
            from kiro_ception.cli_loader import list_cli_sessions

            sessions = list_cli_sessions()

        assert sessions == []

    def test_cli_no_database_path(self):
        """None database path returns no sessions."""
        with patch("kiro_ception.cli_loader.get_database_path", return_value=None):
            from kiro_ception.cli_loader import list_cli_sessions

            sessions = list_cli_sessions()

        assert sessions == []


# --- CLI JSONL session store (current format) ---


class TestCLIJsonlLoading:
    """Covers the current Kiro CLI JSONL session store + .json sidecar."""

    def _write_session(self, root: Path, session_id: str, cwd: str,
                       lines: list[dict], created: str, updated: str) -> Path:
        """Write a <session_id>.jsonl transcript and its .json sidecar."""
        transcript = root / f"{session_id}.jsonl"
        with open(transcript, "w", encoding="utf-8") as f:
            for rec in lines:
                f.write(json.dumps(rec) + "\n")
        sidecar = root / f"{session_id}.json"
        sidecar.write_text(json.dumps({
            "session_id": session_id,
            "cwd": cwd,
            "created_at": created,
            "updated_at": updated,
        }), encoding="utf-8")
        return transcript

    @pytest.fixture
    def jsonl_root(self, tmp_path):
        root = tmp_path / "sessions" / "cli"
        root.mkdir(parents=True)
        self._write_session(
            root, "sess-jsonl-1", "/Users/dev/proj",
            lines=[
                {"version": "v1", "kind": "Prompt", "data": {
                    "message_id": "m1",
                    "content": [{"kind": "text", "data": "Find the bug in parser.py"}],
                    "meta": {"timestamp": 1717236000,
                             "additionalContext": "emit your current mood as Mood[...]"},
                }},
                {"version": "v1", "kind": "AssistantMessage", "data": {
                    "message_id": "m2",
                    "content": [
                        {"kind": "thinking", "data": {"text": "secret reasoning"}},
                        {"kind": "text", "data": "The bug is a missing return."},
                        {"kind": "toolUse", "data": {
                            "toolUseId": "t1", "name": "read_file",
                            "input": {"path": "parser.py"}}},
                    ],
                }},
                {"version": "v1", "kind": "ToolResults", "data": {
                    "message_id": "m3",
                    "content": [{"kind": "toolResult", "data": {
                        "toolUseId": "t1",
                        "content": [{"kind": "text", "data": "def parse(): pass"}],
                        "status": "success"}}],
                }},
            ],
            created="2026-06-01T10:00:00.000Z",
            updated="2026-06-01T10:05:00.000Z",
        )
        return root

    def test_list_jsonl_sessions_from_sidecar(self, jsonl_root):
        with patch("kiro_ception.cli_loader.get_config") as gc:
            gc.return_value.cli.get_session_roots.return_value = [jsonl_root]
            gc.return_value.cli.database_path = None
            from kiro_ception.cli_loader import _list_jsonl_sessions

            sessions = _list_jsonl_sessions()

        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "sess-jsonl-1"
        assert s.workspace == "/Users/dev/proj"       # from sidecar cwd
        assert s.source == Source.CLI
        # modified comes from sidecar updated_at, not file mtime
        assert s.modified is not None

    def test_jsonl_messages_and_exclusions(self, jsonl_root):
        from kiro_ception.cli_loader import _build_jsonl_session, _load_jsonl_messages

        transcript = jsonl_root / "sess-jsonl-1.jsonl"
        with patch("kiro_ception.cli_loader.get_config") as gc:
            gc.return_value.cli.get_session_roots.return_value = [jsonl_root]
            gc.return_value.cli.include_thinking = False
            gc.return_value.cli.include_tool_context = True
            gc.return_value.tool_summaries.excluded_tools = []
            gc.return_value.tool_summaries.max_summary_length = 800
            gc.return_value.tool_summaries.include_meaningful_output = True
            session = _build_jsonl_session(transcript)
            messages = _load_jsonl_messages(session, transcript)

        user = [m for m in messages if m.role == "user"]
        assert len(user) == 1
        # additionalContext (hook plumbing) must NOT be indexed
        assert "mood" not in user[0].searchable_text.lower()
        assert "Find the bug" in user[0].searchable_text

        convo = [m for m in messages if m.content_tier.value == "conversation"
                 and m.role == "assistant"]
        assert len(convo) == 1
        # thinking block excluded, text kept
        assert "secret reasoning" not in convo[0].searchable_text
        assert "missing return" in convo[0].searchable_text

        tool_ctx = [m for m in messages if m.content_tier.value == "tool_context"]
        assert len(tool_ctx) == 1
        assert "read_file" in tool_ctx[0].searchable_text
        assert tool_ctx[0].tool_name == "read_file"

    def test_jsonl_thinking_included_when_enabled(self, jsonl_root):
        from kiro_ception.cli_loader import _build_jsonl_session, _load_jsonl_messages

        transcript = jsonl_root / "sess-jsonl-1.jsonl"
        with patch("kiro_ception.cli_loader.get_config") as gc:
            gc.return_value.cli.get_session_roots.return_value = [jsonl_root]
            gc.return_value.cli.include_thinking = True
            gc.return_value.cli.include_tool_context = False
            session = _build_jsonl_session(transcript)
            messages = _load_jsonl_messages(session, transcript)

        assistant_text = " ".join(
            m.searchable_text for m in messages if m.role == "assistant"
        )
        assert "secret reasoning" in assistant_text

    def test_jsonl_tolerates_truncated_final_line(self, tmp_path):
        """A live-appended transcript may end mid-record; must not raise."""
        root = tmp_path / "sessions" / "cli"
        root.mkdir(parents=True)
        transcript = root / "partial.jsonl"
        good = {"version": "v1", "kind": "Prompt", "data": {
            "content": [{"kind": "text", "data": "hello"}],
            "meta": {"timestamp": 1717236000}}}
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n")
            f.write('{"version": "v1", "kind": "Assistant')  # truncated
        (root / "partial.json").write_text(json.dumps({
            "session_id": "partial", "cwd": "/w",
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:01:00Z"}), encoding="utf-8")

        with patch("kiro_ception.cli_loader.get_config") as gc:
            gc.return_value.cli.get_session_roots.return_value = [root]
            gc.return_value.cli.include_thinking = False
            gc.return_value.cli.include_tool_context = True
            from kiro_ception.cli_loader import _build_jsonl_session, _load_jsonl_messages

            session = _build_jsonl_session(transcript)
            messages = _load_jsonl_messages(session, transcript)

        # The good line parses; the truncated one is skipped, no exception.
        assert any("hello" in m.searchable_text for m in messages)

    def test_jsonl_missing_sidecar_falls_back(self, tmp_path):
        """No sidecar: workspace/timestamps fall back to header + file mtime."""
        root = tmp_path / "sessions" / "cli"
        root.mkdir(parents=True)
        transcript = root / "nosidecar.jsonl"
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": "v1", "kind": "Prompt", "data": {
                "content": [{"kind": "text", "data": "hi"}],
                "meta": {"timestamp": 1717236000}}}) + "\n")

        from kiro_ception.cli_loader import _build_jsonl_session

        session = _build_jsonl_session(transcript)
        assert session is not None
        assert session.session_id == "nosidecar"
        # No cwd available anywhere -> empty workspace, but still usable
        assert session.modified is not None

    def test_union_dedup_prefers_jsonl(self, tmp_path):
        """When a session_id exists in both stores, JSONL wins; others union."""
        root = tmp_path / "sessions" / "cli"
        root.mkdir(parents=True)
        self._write_session(
            root, "dupe", "/from/jsonl",
            lines=[{"version": "v1", "kind": "Prompt", "data": {
                "content": [{"kind": "text", "data": "x"}],
                "meta": {"timestamp": 1717236000}}}],
            created="2026-06-01T00:00:00Z", updated="2026-06-01T00:01:00Z")

        jsonl_sessions = [
            SessionInfo(session_id="dupe", workspace="/from/jsonl",
                        source=Source.CLI, modified=datetime(2026, 6, 1)),
        ]
        sqlite_sessions = [
            SessionInfo(session_id="dupe", workspace="/from/sqlite",
                        source=Source.CLI, modified=datetime(2026, 5, 1)),
            SessionInfo(session_id="sqlite-only", workspace="/from/sqlite",
                        source=Source.CLI, modified=datetime(2026, 5, 2)),
        ]
        with patch("kiro_ception.cli_loader._list_jsonl_sessions",
                   return_value=jsonl_sessions), \
             patch("kiro_ception.cli_loader._list_sqlite_sessions",
                   return_value=sqlite_sessions):
            from kiro_ception.cli_loader import list_cli_sessions

            sessions = list_cli_sessions()

        by_id = {s.session_id: s for s in sessions}
        assert set(by_id) == {"dupe", "sqlite-only"}
        # JSONL won the collision
        assert by_id["dupe"].workspace == "/from/jsonl"


# --- Loader facade ---


class TestLoaderFacade:
    def test_list_all_sessions_combines_sources(self, tmp_path):
        """list_all_sessions combines CLI and IDE sources."""
        from kiro_ception.config import Config
        from kiro_ception.sessions import list_all_sessions

        ide_session = SessionInfo(
            session_id="ide-1", workspace="/project",
            created=datetime(2026, 6, 1), modified=datetime(2026, 6, 2),
            source=Source.IDE,
        )
        cli_session = SessionInfo(
            session_id="cli-1", workspace="/project",
            created=datetime(2026, 6, 3), modified=datetime(2026, 6, 4),
            source=Source.CLI,
        )

        with (
            patch("kiro_ception.sessions.get_config", return_value=Config()),
            patch("kiro_ception.sessions.list_ide_sessions", return_value=[ide_session]),
            patch("kiro_ception.sessions.list_cli_sessions", return_value=[cli_session]),
        ):
            sessions = list_all_sessions()

        assert len(sessions) == 2
        # Should be sorted newest first
        assert sessions[0].session_id == "cli-1"  # newer
        assert sessions[1].session_id == "ide-1"

    def test_list_all_sessions_respects_enabled_flags(self, tmp_path):
        """Disabled sources are not queried."""
        from kiro_ception.config import CLISourceConfig, Config, IDESourceConfig
        from kiro_ception.sessions import list_all_sessions

        config = Config(
            cli=CLISourceConfig(enabled=False),
            ide=IDESourceConfig(enabled=True),
        )

        ide_session = SessionInfo(
            session_id="ide-1", workspace="/project",
            created=datetime(2026, 6, 1), modified=datetime(2026, 6, 2),
            source=Source.IDE,
        )

        with (
            patch("kiro_ception.sessions.get_config", return_value=config),
            patch("kiro_ception.sessions.list_ide_sessions", return_value=[ide_session]),
            patch("kiro_ception.sessions.list_cli_sessions") as mock_cli,
        ):
            sessions = list_all_sessions()

        # CLI should not be called since it's disabled
        mock_cli.assert_not_called()
        assert len(sessions) == 1


# --- Memory limit / get_memory_limit ---


class TestGetMemoryLimit:
    def test_explicit_limit_mb(self):
        from kiro_ception.config import Config, MemoryConfig
        config = Config(memory=MemoryConfig(limit_mb=1024))
        with patch("kiro_ception.memory.get_config", return_value=config):
            from kiro_ception.memory import get_memory_limit

            limit = get_memory_limit()

        assert limit == 1024 * 1024 * 1024

    def test_limit_mb_zero_disables(self):
        """Setting limit_mb = 0 disables the memory limit."""
        from kiro_ception.config import Config, MemoryConfig
        config = Config(memory=MemoryConfig(limit_mb=0))
        with patch("kiro_ception.memory.get_config", return_value=config):
            from kiro_ception.memory import get_memory_limit

            limit = get_memory_limit()

        assert limit == 0

    def test_fraction_of_physical_ram(self):
        """When no limit_mb is set, uses fraction of physical RAM."""
        from kiro_ception.config import Config, MemoryConfig
        config = Config(memory=MemoryConfig(fraction=0.5, limit_mb=None))
        with (
            patch("kiro_ception.memory.get_config", return_value=config),
            patch("kiro_ception.memory.get_physical_memory", return_value=16 * 1024 * 1024 * 1024),
        ):
            from kiro_ception.memory import get_memory_limit

            limit = get_memory_limit()

        # 50% of 16GB = 8GB
        assert limit == 8 * 1024 * 1024 * 1024

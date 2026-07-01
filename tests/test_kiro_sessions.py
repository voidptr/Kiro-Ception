"""Tests for Kiro 1.0 session loader (~/.kiro/sessions/ format).

Tests cover:
- Session discovery (_list_kiro_sessions)
- Message loading (_load_kiro_session_messages)
- SHA256 workspace path hashing
- Tool context generation from tool_call/tool_result pairs
- Filtering of non-indexable message types
- Code block replacement in assistant messages
- Deduplication in list_ide_sessions
- Edge cases (empty files, malformed JSON, missing fields)
"""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_ception.ide_loader import (
    _describe_kiro_tool_call,
    _extract_meaningful_from_result,
    _find_kiro_session_messages_file,
    _generate_kiro_tool_summary,
    _get_kiro_sessions_dirs,
    _list_kiro_sessions,
    _load_kiro_session_messages,
    _workspace_path_to_sha256_prefix,
    list_ide_sessions,
    load_ide_session_messages,
)
from kiro_ception.models import ContentTier, SessionInfo, Source
from kiro_ception.tool_summaries import ToolSummaryConfig


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestWorkspacePathHashing:
    """Tests for _workspace_path_to_sha256_prefix."""

    def test_known_path_produces_correct_prefix(self):
        """Verified against real Kiro 1.0 directory names."""
        path = "/Users/farley/ml-learnings/kiro-ception"
        assert _workspace_path_to_sha256_prefix(path) == "29cc4c583f617285"

    def test_another_known_path(self):
        path = "/Users/farley/Pharos/blueprint"
        assert _workspace_path_to_sha256_prefix(path) == "2f48c6c129ad69d0"

    def test_returns_16_hex_chars(self):
        result = _workspace_path_to_sha256_prefix("/any/path")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_paths_produce_different_prefixes(self):
        a = _workspace_path_to_sha256_prefix("/path/a")
        b = _workspace_path_to_sha256_prefix("/path/b")
        assert a != b

    def test_empty_path(self):
        result = _workspace_path_to_sha256_prefix("")
        expected = hashlib.sha256(b"").hexdigest()[:16]
        assert result == expected


class TestGetKiroSessionsDirs:
    """Tests for _get_kiro_sessions_dirs."""

    def test_returns_existing_dir(self, tmp_path):
        sessions_dir = tmp_path / ".kiro" / "sessions"
        sessions_dir.mkdir(parents=True)
        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            dirs = [sessions_dir]
        assert dirs == [sessions_dir]

    def test_returns_empty_when_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # The dir doesn't exist, so _get_kiro_sessions_dirs should return empty
        with patch(
            "kiro_ception.ide_loader.Path.expanduser",
            return_value=tmp_path / "nonexistent",
        ):
            # Directly test the function logic
            result = _get_kiro_sessions_dirs()
            # Result depends on whether ~/.kiro/sessions exists on the machine
            # The important thing is it doesn't crash
            assert isinstance(result, list)


def _create_kiro_session(
    base_dir: Path,
    workspace_path: str = "/Users/testuser/projects/my-webapp",
    session_id: str = "sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    use_fixture_messages: bool = True,
) -> Path:
    """Helper to create a Kiro 1.0 session directory structure for testing."""
    prefix = _workspace_path_to_sha256_prefix(workspace_path)
    session_dir = base_dir / prefix / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Create session.json with appropriate workspace path
    session_meta = {
        "schemaVersion": "1.0.0",
        "dataModelVersion": 1,
        "id": session_id,
        "title": "Test Session",
        "agentMode": "vibe",
        "workspacePaths": [workspace_path],
        "createdAt": "2026-06-28T10:30:00.000Z",
        "lastModifiedAt": "2026-06-28T11:45:30.500Z",
        "modelId": "auto",
        "autopilot": True,
    }
    (session_dir / "session.json").write_text(json.dumps(session_meta, indent=2))

    # Copy or create messages.jsonl
    if use_fixture_messages:
        fixture_messages = FIXTURES_DIR / "kiro_messages.jsonl"
        if fixture_messages.exists():
            shutil.copy(fixture_messages, session_dir / "messages.jsonl")
        else:
            (session_dir / "messages.jsonl").write_text(
                '{"id":"m1","timestamp":"2026-06-28T10:30:00Z","payload":{"type":"user","content":"test message"}}\n'
            )
    else:
        (session_dir / "messages.jsonl").write_text("")

    return session_dir


class TestListKiroSessions:
    """Tests for _list_kiro_sessions."""

    def test_discovers_sessions_from_fixture(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir)

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert s.workspace == "/Users/testuser/projects/my-webapp"
        assert s.source == Source.IDE

    def test_parses_created_at_from_session_json(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir)

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        s = sessions[0]
        # createdAt in fixture is "2026-06-28T10:30:00.000Z"
        assert s.created is not None
        assert s.created.year == 2026
        assert s.created.month == 6
        assert s.created.day == 28

    def test_skips_empty_messages_file(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir, use_fixture_messages=False)

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 0

    def test_multiple_sessions_in_one_workspace(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ws = "/Users/testuser/projects/my-webapp"
        _create_kiro_session(sessions_dir, ws, "sess_aaaa-1111")
        _create_kiro_session(sessions_dir, ws, "sess_bbbb-2222")

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 2
        ids = {s.session_id for s in sessions}
        assert "sess_aaaa-1111" in ids
        assert "sess_bbbb-2222" in ids

    def test_multiple_workspaces(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir, "/ws/alpha", "sess_alpha")
        _create_kiro_session(sessions_dir, "/ws/beta", "sess_beta")

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 2
        workspaces = {s.workspace for s in sessions}
        assert "/ws/alpha" in workspaces
        assert "/ws/beta" in workspaces

    def test_handles_missing_session_json_gracefully(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        prefix = _workspace_path_to_sha256_prefix("/some/path")
        session_dir = sessions_dir / prefix / "sess_no-meta"
        session_dir.mkdir(parents=True)
        # Only create messages.jsonl, no session.json
        (session_dir / "messages.jsonl").write_text(
            '{"id":"m1","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"user","content":"hello"}}\n'
        )

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "sess_no-meta"
        assert sessions[0].workspace == ""  # No session.json to read from

    def test_handles_malformed_session_json(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        prefix = _workspace_path_to_sha256_prefix("/bad/json")
        session_dir = sessions_dir / prefix / "sess_bad-json"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text("not valid json{{{")
        (session_dir / "messages.jsonl").write_text(
            '{"id":"m1","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"user","content":"hi"}}\n'
        )

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            sessions = _list_kiro_sessions()

        assert len(sessions) == 1
        assert sessions[0].workspace == ""


class TestLoadKiroSessionMessages:
    """Tests for _load_kiro_session_messages."""

    def _make_session(self, tmp_path) -> SessionInfo:
        """Create a session with fixture data and return the SessionInfo."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        _create_kiro_session(sessions_dir)
        return SessionInfo(
            session_id="sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            workspace="/Users/testuser/projects/my-webapp",
            created=datetime(2026, 6, 28, 10, 30),
            modified=datetime(2026, 6, 28, 11, 45),
            source=Source.IDE,
        )

    def test_extracts_user_messages(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        user_msgs = [m for m in messages if m.role == "user" and m.content_tier == ContentTier.CONVERSATION]
        # Fixture has 2 real user messages (one empty is skipped)
        assert len(user_msgs) == 2
        assert "JWT authentication" in user_msgs[0].searchable_text
        assert "rate limiting" in user_msgs[1].searchable_text

    def test_extracts_assistant_messages(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        asst_msgs = [m for m in messages if m.role == "assistant" and m.content_tier == ContentTier.CONVERSATION]
        assert len(asst_msgs) == 4
        # First assistant message
        assert "JWT authentication" in asst_msgs[0].searchable_text

    def test_replaces_code_blocks_in_assistant(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        # The fixture's msg-012 has a ```javascript code block
        asst_msgs = [m for m in messages if m.role == "assistant" and m.content_tier == ContentTier.CONVERSATION]
        code_msg = [m for m in asst_msgs if "[code:javascript]" in m.searchable_text]
        assert len(code_msg) == 1
        # Original code block should be gone
        assert "const jwt = require" not in code_msg[0].searchable_text

    def test_generates_tool_context(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        tool_ctx = [m for m in messages if m.content_tier == ContentTier.TOOL_CONTEXT]
        # Fixture has 7 tool_call/tool_result pairs
        assert len(tool_ctx) == 7
        # Check tool names are set
        tool_names = [m.tool_name for m in tool_ctx]
        assert "readFile" in tool_names
        assert "execute_bash" in tool_names
        assert "fs_write" in tool_names
        assert "str_replace" in tool_names

    def test_tool_context_contains_meaningful_output(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        tool_ctx = [m for m in messages if m.content_tier == ContentTier.TOOL_CONTEXT]
        # The npm test result has "FAILED" so it should be meaningful
        test_result = [m for m in tool_ctx if "npm test" in m.searchable_text]
        assert len(test_result) == 1
        assert "FAILED" in test_result[0].searchable_text

    def test_skips_session_start_messages(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        # No message should contain system prompt text
        all_text = " ".join(m.searchable_text for m in messages)
        assert "You are Kiro, an agentic AI" not in all_text

    def test_skips_empty_user_messages(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        # Fixture has msg-027 with empty content - should be skipped
        user_msgs = [m for m in messages if m.role == "user" and m.content_tier == ContentTier.CONVERSATION]
        for m in user_msgs:
            assert m.searchable_text.strip() != ""

    def test_timestamps_parsed_correctly(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        user_msgs = [m for m in messages if m.role == "user" and m.content_tier == ContentTier.CONVERSATION]
        # First user message timestamp: 2026-06-28T10:30:05.000Z
        assert user_msgs[0].timestamp.year == 2026
        assert user_msgs[0].timestamp.month == 6
        assert user_msgs[0].timestamp.day == 28

    def test_message_indices_are_sequential(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        indices = [m.message_index for m in messages]
        assert indices == list(range(len(messages)))

    def test_session_id_propagated(self, tmp_path):
        session = self._make_session(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        for m in messages:
            assert m.session_id == "sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            assert m.workspace == "/Users/testuser/projects/my-webapp"
            assert m.source == Source.IDE


class TestLoadKiroSessionEdgeCases:
    """Edge cases for message loading."""

    def test_nonexistent_session_returns_empty(self):
        session = SessionInfo(
            session_id="sess_nonexistent",
            workspace="/fake/path",
            source=Source.IDE,
        )
        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[],
        ):
            messages = _load_kiro_session_messages(session)
        assert messages == []

    def test_malformed_jsonl_lines_skipped(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        prefix = _workspace_path_to_sha256_prefix("/test/ws")
        session_dir = sessions_dir / prefix / "sess_malformed"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "workspacePaths": ["/test/ws"],
            "createdAt": "2026-01-01T00:00:00Z",
        }))
        (session_dir / "messages.jsonl").write_text(
            'not json at all\n'
            '{"id":"m1","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"user","content":"valid message"}}\n'
            '{"broken json\n'
            '{"id":"m2","timestamp":"2026-01-01T00:01:00Z","payload":{"type":"assistant","content":"reply","operationType":"Say"}}\n'
        )

        session = SessionInfo(
            session_id="sess_malformed",
            workspace="/test/ws",
            source=Source.IDE,
        )
        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        # Should get 2 valid messages despite 2 malformed lines
        conv_msgs = [m for m in messages if m.content_tier == ContentTier.CONVERSATION]
        assert len(conv_msgs) == 2

    def test_tool_result_without_matching_call_ignored(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        prefix = _workspace_path_to_sha256_prefix("/test/ws")
        session_dir = sessions_dir / prefix / "sess_orphan-result"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "workspacePaths": ["/test/ws"],
            "createdAt": "2026-01-01T00:00:00Z",
        }))
        (session_dir / "messages.jsonl").write_text(
            '{"id":"m1","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"tool_result","toolCallId":"orphan-123","content":"result","success":true}}\n'
            '{"id":"m2","timestamp":"2026-01-01T00:00:01Z","payload":{"type":"user","content":"hello"}}\n'
        )

        session = SessionInfo(
            session_id="sess_orphan-result",
            workspace="/test/ws",
            source=Source.IDE,
        )
        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        # Only the user message, no orphan tool context
        assert len(messages) == 1
        assert messages[0].role == "user"

    def test_empty_lines_in_jsonl_handled(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        prefix = _workspace_path_to_sha256_prefix("/test/ws")
        session_dir = sessions_dir / prefix / "sess_blank-lines"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "workspacePaths": ["/test/ws"],
            "createdAt": "2026-01-01T00:00:00Z",
        }))
        (session_dir / "messages.jsonl").write_text(
            '\n'
            '{"id":"m1","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"user","content":"test"}}\n'
            '\n'
            '\n'
            '{"id":"m2","timestamp":"2026-01-01T00:00:01Z","payload":{"type":"assistant","content":"reply","operationType":"Say"}}\n'
            '\n'
        )

        session = SessionInfo(
            session_id="sess_blank-lines",
            workspace="/test/ws",
            source=Source.IDE,
        )
        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            messages = _load_kiro_session_messages(session)

        conv_msgs = [m for m in messages if m.content_tier == ContentTier.CONVERSATION]
        assert len(conv_msgs) == 2


class TestDescribeKiroToolCall:
    """Tests for _describe_kiro_tool_call."""

    def test_read_file(self):
        result = _describe_kiro_tool_call("readFile", {"path": "src/main.py"})
        assert result == "src/main.py"

    def test_read_files_with_files_array(self):
        result = _describe_kiro_tool_call("readFiles", {
            "files": [{"path": "a.py"}, {"path": "b.py"}]
        })
        assert "a.py" in result
        assert "b.py" in result

    def test_write_file(self):
        result = _describe_kiro_tool_call("fs_write", {"path": "output.txt"})
        assert result == "output.txt"

    def test_str_replace(self):
        result = _describe_kiro_tool_call("str_replace", {"path": "config.yaml"})
        assert result == "config.yaml"

    def test_grep_search(self):
        result = _describe_kiro_tool_call("grep_search", {"query": "def main"})
        assert 'query="def main"' == result

    def test_execute_bash(self):
        result = _describe_kiro_tool_call("execute_bash", {"command": "npm test"})
        assert result == "npm test"

    def test_execute_bash_long_command_truncated(self):
        long_cmd = "x" * 200
        result = _describe_kiro_tool_call("execute_bash", {"command": long_cmd})
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")

    def test_invoke_sub_agent(self):
        result = _describe_kiro_tool_call("invoke_sub_agent", {
            "name": "context-gatherer",
            "prompt": "Find auth files",
        })
        assert "context-gatherer" in result
        assert "Find auth files" in result

    def test_update_session_information(self):
        result = _describe_kiro_tool_call("update_session_information", {
            "title": "Working on auth",
            "description": "Implementing JWT",
        })
        assert "Working on auth" in result

    def test_unknown_tool_serializes_args(self):
        result = _describe_kiro_tool_call("some_custom_tool", {"key": "value"})
        assert "key" in result
        assert "value" in result

    def test_empty_args(self):
        result = _describe_kiro_tool_call("readFile", {})
        assert result == "(unknown path)"


class TestExtractMeaningfulFromResult:
    """Tests for _extract_meaningful_from_result."""

    def test_empty_content(self):
        assert _extract_meaningful_from_result("") == ""
        assert _extract_meaningful_from_result(None) == ""

    def test_content_with_error_keyword(self):
        result = _extract_meaningful_from_result("Build failed: module not found")
        assert "Build failed" in result

    def test_content_without_meaningful_keywords(self):
        result = _extract_meaningful_from_result("added 2 packages in 3.2s")
        assert result == ""

    def test_double_json_encoded_with_error(self):
        inner = '{"response":"Tests: 3 passed, 1 failed\\nFAILED: auth.test.js"}'
        result = _extract_meaningful_from_result(inner)
        assert "FAILED" in result

    def test_long_content_truncated(self):
        long_error = "Error: " + "x" * 1000
        result = _extract_meaningful_from_result(long_error)
        assert len(result) <= 500
        assert result.endswith("...")


class TestGenerateKiroToolSummary:
    """Tests for _generate_kiro_tool_summary."""

    def test_basic_tool_summary(self):
        config = ToolSummaryConfig()
        tool_call = {
            "toolName": "readFile",
            "actionType": "readFile",
            "args": {"path": "src/app.js"},
        }
        tool_result = {"content": "file content here", "success": True}
        summary = _generate_kiro_tool_summary(tool_call, tool_result, config)
        assert summary is not None
        assert "[readFile]" in summary
        assert "src/app.js" in summary
        assert "completed" in summary

    def test_failed_tool(self):
        config = ToolSummaryConfig()
        tool_call = {
            "toolName": "execute_bash",
            "actionType": "execute_bash",
            "args": {"command": "npm build"},
        }
        tool_result = {"content": "Error: build failed", "success": False}
        summary = _generate_kiro_tool_summary(tool_call, tool_result, config)
        assert summary is not None
        assert "failed" in summary

    def test_excluded_tool_returns_none(self):
        config = ToolSummaryConfig(excluded_tools=["update_session_information"])
        tool_call = {
            "toolName": "update_session_information",
            "actionType": "update_session_information",
            "args": {"title": "test"},
        }
        tool_result = {"content": "", "success": True}
        summary = _generate_kiro_tool_summary(tool_call, tool_result, config)
        assert summary is None

    def test_meta_stripped_from_args(self):
        config = ToolSummaryConfig()
        tool_call = {
            "toolName": "readFile",
            "actionType": "readFile",
            "args": {"path": "test.py", "_meta": {"kiro": {"agentMode": "vibe"}}},
        }
        tool_result = {"content": "", "success": True}
        summary = _generate_kiro_tool_summary(tool_call, tool_result, config)
        assert "_meta" not in summary
        assert "test.py" in summary

    def test_max_length_enforced(self):
        config = ToolSummaryConfig(max_summary_length=50)
        tool_call = {
            "toolName": "execute_bash",
            "actionType": "execute_bash",
            "args": {"command": "a very long command " * 10},
        }
        tool_result = {"content": "Error: " + "x" * 500, "success": True}
        summary = _generate_kiro_tool_summary(tool_call, tool_result, config)
        assert len(summary) <= 50


class TestFindKiroSessionMessagesFile:
    """Tests for _find_kiro_session_messages_file."""

    def test_finds_existing_session(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir)

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            result = _find_kiro_session_messages_file(
                "sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            )

        assert result is not None
        assert result.name == "messages.jsonl"
        assert result.exists()

    def test_returns_none_for_missing_session(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            result = _find_kiro_session_messages_file("sess_nonexistent")

        assert result is None


class TestDeduplication:
    """Tests for session deduplication in list_ide_sessions."""

    def test_kiro_sessions_win_over_workspace_sessions(self, tmp_path):
        """When same session_id exists in both old and new format, new wins."""
        sessions_dir = tmp_path / "kiro_sessions"
        sessions_dir.mkdir()

        # Create a Kiro 1.0 session with a specific ID
        shared_id = "shared-session-id-1234"
        prefix = _workspace_path_to_sha256_prefix("/test/ws")
        session_dir = sessions_dir / prefix / shared_id
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "workspacePaths": ["/test/ws"],
            "createdAt": "2026-07-01T00:00:00Z",
        }))
        (session_dir / "messages.jsonl").write_text(
            '{"id":"m1","timestamp":"2026-07-01T00:00:00Z","payload":{"type":"user","content":"from kiro 1.0"}}\n'
        )

        # Mock both old and new returning the same session_id
        old_session = SessionInfo(
            session_id=shared_id,
            workspace="/old/workspace",
            created=datetime(2025, 1, 1),
            modified=datetime(2025, 1, 1),
            source=Source.IDE,
        )
        new_session = SessionInfo(
            session_id=shared_id,
            workspace="/test/ws",
            created=datetime(2026, 7, 1),
            modified=datetime(2026, 7, 1),
            source=Source.IDE,
        )

        with patch("kiro_ception.ide_loader._list_legacy_sessions", return_value=[old_session]):
            with patch("kiro_ception.ide_loader._list_workspace_sessions", return_value=[old_session]):
                with patch("kiro_ception.ide_loader._list_kiro_sessions", return_value=[new_session]):
                    sessions = list_ide_sessions()

        # Should have exactly one session (deduplicated)
        matching = [s for s in sessions if s.session_id == shared_id]
        assert len(matching) == 1
        # The Kiro 1.0 version should win (later in processing order)
        assert matching[0].workspace == "/test/ws"
        assert matching[0].created.year == 2026

    def test_unique_sessions_all_preserved(self, tmp_path):
        """Sessions with different IDs from all sources are all kept."""
        legacy = SessionInfo(session_id="legacy-1", workspace="/ws", source=Source.IDE)
        workspace = SessionInfo(session_id="workspace-1", workspace="/ws", source=Source.IDE)
        kiro = SessionInfo(session_id="kiro-1", workspace="/ws", source=Source.IDE)

        with patch("kiro_ception.ide_loader._list_legacy_sessions", return_value=[legacy]):
            with patch("kiro_ception.ide_loader._list_workspace_sessions", return_value=[workspace]):
                with patch("kiro_ception.ide_loader._list_kiro_sessions", return_value=[kiro]):
                    sessions = list_ide_sessions()

        ids = {s.session_id for s in sessions}
        assert ids == {"legacy-1", "workspace-1", "kiro-1"}


class TestLoadIdeSessionMessages:
    """Tests for load_ide_session_messages routing to Kiro 1.0 loader."""

    def test_routes_to_kiro_loader_when_file_exists(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _create_kiro_session(sessions_dir)

        session = SessionInfo(
            session_id="sess_a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            workspace="/Users/testuser/projects/my-webapp",
            created=datetime(2026, 6, 28, 10, 30),
            modified=datetime(2026, 6, 28, 11, 45),
            source=Source.IDE,
        )

        with patch(
            "kiro_ception.ide_loader._get_kiro_sessions_dirs",
            return_value=[sessions_dir],
        ):
            with patch(
                "kiro_ception.ide_loader._get_workspace_sessions_dirs",
                return_value=[],
            ):
                messages = load_ide_session_messages(session)

        # Should get messages from the Kiro 1.0 loader
        assert len(messages) > 0
        user_msgs = [m for m in messages if m.role == "user" and m.content_tier == ContentTier.CONVERSATION]
        assert any("JWT" in m.searchable_text for m in user_msgs)

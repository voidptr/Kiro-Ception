"""Unit tests for claude_loader.py — Claude Code transcript discovery and parsing."""

import json

import pytest

from kiro_ception import claude_loader
from kiro_ception.claude_loader import (
    _decode_project_dir_name,
    _describe_claude_tool_call,
    _generate_claude_tool_summary,
    _strip_system_reminders,
    list_claude_sessions,
    load_claude_session_messages,
)
from kiro_ception.config import ClaudeSourceConfig, Config, ToolSummariesConfig, diff_configs
from kiro_ception.models import ContentTier, Source
from kiro_ception.tool_summaries import ToolSummaryConfig

WORKSPACE = "C:\\Source\\demo-project"
PROJECT_DIR = "C--Source-demo-project"


def write_transcript(path, records):
    """Write a list of records as a JSONL transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def user_record(uuid, text, **overrides):
    record = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-08-11T00:38:04.704Z",
        "cwd": WORKSPACE,
        "sessionId": "sess-1",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }
    record.update(overrides)
    return record


def assistant_record(uuid, blocks, **overrides):
    record = {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-08-11T00:38:52.975Z",
        "cwd": WORKSPACE,
        "sessionId": "sess-1",
        "isSidechain": False,
        "message": {"role": "assistant", "content": blocks},
    }
    record.update(overrides)
    return record


@pytest.fixture
def claude_env(tmp_path, monkeypatch):
    """Point the loader at a temporary Claude projects root.

    Returns a helper object exposing the root, the project dir, and a
    `configure()` method for overriding source/tool-summary settings.
    """
    root = tmp_path / "projects"
    project = root / PROJECT_DIR
    project.mkdir(parents=True)

    state = {
        "claude": ClaudeSourceConfig(roots=[str(root)]),
        "tool_summaries": ToolSummariesConfig(),
    }

    class FakeConfig:
        @property
        def claude(self):
            return state["claude"]

        @property
        def tool_summaries(self):
            return state["tool_summaries"]

    monkeypatch.setattr(claude_loader, "get_config", lambda: FakeConfig())
    # The path index is module-level state shared across loads.
    monkeypatch.setattr(claude_loader, "_session_paths", {})

    class Env:
        def __init__(self):
            self.root = root
            self.project = project

        def configure(self, **kwargs):
            tool_keys = {
                "excluded_tools",
                "max_summary_length",
                "include_meaningful_output",
            }
            for key, value in kwargs.items():
                if key in tool_keys:
                    setattr(state["tool_summaries"], key, value)
                else:
                    setattr(state["claude"], key, value)

    return Env()


# --- Workspace directory decoding ---


class TestDecodeProjectDirName:
    def test_windows_drive_letter_recovered(self):
        # Only the drive prefix is unambiguous; interior hyphens are left as-is
        # because a separator and a literal hyphen encode identically.
        assert _decode_project_dir_name("C--Source-demo") == "C:\\Source-demo"

    def test_drive_letter_uppercased(self):
        assert _decode_project_dir_name("c--Source-demo") == "C:\\Source-demo"

    def test_non_windows_name_passed_through(self):
        # Encoding is lossy for POSIX paths, so the raw name is the best answer.
        assert _decode_project_dir_name("-home-user-proj") == "-home-user-proj"


# --- Session discovery ---


class TestListClaudeSessions:
    def test_discovers_transcripts(self, claude_env):
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u1", "hello")]
        )
        sessions = list_claude_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "abc"
        assert sessions[0].source == Source.CLAUDE

    def test_workspace_read_from_cwd_field(self, claude_env):
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u1", "hello")]
        )
        assert list_claude_sessions()[0].workspace == WORKSPACE

    def test_workspace_falls_back_to_decoded_dir_name(self, claude_env):
        # A transcript with no cwd anywhere in its header.
        write_transcript(
            claude_env.project / "abc.jsonl",
            [{"type": "mode", "mode": "normal", "sessionId": "abc"}],
        )
        # Best-effort only — the encoding is lossy, so this is a fallback for
        # transcripts that never recorded a cwd.
        assert list_claude_sessions()[0].workspace == "C:\\Source-demo-project"

    def test_created_uses_first_record_timestamp(self, claude_env):
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u1", "hello")]
        )
        created = list_claude_sessions()[0].created
        assert created.year == 2026
        assert created.month == 8
        assert created.day == 11

    def test_empty_transcript_skipped(self, claude_env):
        (claude_env.project / "empty.jsonl").write_text("", encoding="utf-8")
        assert list_claude_sessions() == []

    def test_non_jsonl_files_ignored(self, claude_env):
        (claude_env.project / "notes.txt").write_text("hi", encoding="utf-8")
        assert list_claude_sessions() == []

    def test_missing_root_yields_no_sessions(self, claude_env):
        claude_env.configure(roots=[str(claude_env.root / "does-not-exist")])
        assert list_claude_sessions() == []

    def test_subagents_discovered(self, claude_env):
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u1", "hello")]
        )
        write_transcript(
            claude_env.project / "abc" / "subagents" / "sub1.jsonl",
            [user_record("u2", "subagent work")],
        )
        ids = {s.session_id for s in list_claude_sessions()}

        assert ids == {"abc", "abc-sub-sub1"}

    def test_subagents_can_be_disabled(self, claude_env):
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u1", "hello")]
        )
        write_transcript(
            claude_env.project / "abc" / "subagents" / "sub1.jsonl",
            [user_record("u2", "subagent work")],
        )
        claude_env.configure(include_subagents=False)
        ids = {s.session_id for s in list_claude_sessions()}

        assert ids == {"abc"}

    def test_multiple_roots_all_scanned(self, claude_env, tmp_path):
        second_root = tmp_path / "projects2"
        second_project = second_root / "C--Other-proj"
        write_transcript(
            second_project / "def.jsonl", [user_record("u1", "other")]
        )
        write_transcript(
            claude_env.project / "abc.jsonl", [user_record("u2", "first")]
        )
        claude_env.configure(roots=[str(claude_env.root), str(second_root)])
        ids = {s.session_id for s in list_claude_sessions()}

        assert ids == {"abc", "def"}


# --- Message extraction ---


class TestLoadClaudeSessionMessages:
    def _load(self, env, records, name="abc.jsonl"):
        write_transcript(env.project / name, records)
        sessions = list_claude_sessions()
        assert sessions, "expected the transcript to be discovered"
        return load_claude_session_messages(sessions[0])

    def test_string_content_indexed(self, claude_env):
        messages = self._load(claude_env, [user_record("u1", "fix the parser")])

        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].searchable_text == "fix the parser"
        assert messages[0].source == Source.CLAUDE
        assert messages[0].content_tier == ContentTier.CONVERSATION

    def test_text_blocks_indexed(self, claude_env):
        messages = self._load(
            claude_env,
            [assistant_record("a1", [{"type": "text", "text": "Here is the fix."}])],
        )

        assert len(messages) == 1
        assert messages[0].role == "assistant"
        assert messages[0].searchable_text == "Here is the fix."

    def test_meta_records_skipped(self, claude_env):
        messages = self._load(
            claude_env,
            [
                user_record("u1", "Caveat: ignore me", isMeta=True),
                user_record("u2", "real question"),
            ],
        )

        assert [m.searchable_text for m in messages] == ["real question"]

    def test_system_records_skipped(self, claude_env):
        messages = self._load(
            claude_env,
            [
                {
                    "type": "system",
                    "uuid": "s1",
                    "subtype": "local_command",
                    "content": "<local-command-stdout></local-command-stdout>",
                    "cwd": WORKSPACE,
                },
                user_record("u1", "real question"),
            ],
        )

        assert [m.searchable_text for m in messages] == ["real question"]

    def test_harness_plumbing_skipped(self, claude_env):
        messages = self._load(
            claude_env,
            [
                user_record("u1", "<command-name>/clear</command-name>"),
                user_record("u2", "<local-command-stdout>done</local-command-stdout>"),
                user_record("u3", "genuine prompt"),
            ],
        )

        assert [m.searchable_text for m in messages] == ["genuine prompt"]

    def test_system_reminders_stripped_not_dropped(self, claude_env):
        messages = self._load(
            claude_env,
            [
                user_record(
                    "u1",
                    "real ask\n<system-reminder>internal noise</system-reminder>",
                )
            ],
        )

        assert len(messages) == 1
        assert messages[0].searchable_text == "real ask"

    def test_reminder_only_message_dropped(self, claude_env):
        messages = self._load(
            claude_env,
            [user_record("u1", "<system-reminder>only noise</system-reminder>")],
        )

        assert messages == []

    def test_code_blocks_replaced_with_placeholder(self, claude_env):
        messages = self._load(
            claude_env,
            [
                assistant_record(
                    "a1",
                    [{"type": "text", "text": "Try:\n```python\nprint(1)\n```"}],
                )
            ],
        )

        assert "[code:python]" in messages[0].searchable_text
        assert "print(1)" not in messages[0].searchable_text

    def test_thinking_excluded_by_default(self, claude_env):
        messages = self._load(
            claude_env,
            [
                assistant_record(
                    "a1",
                    [
                        {"type": "thinking", "thinking": "let me reason"},
                        {"type": "text", "text": "answer"},
                    ],
                )
            ],
        )

        assert [m.searchable_text for m in messages] == ["answer"]

    def test_thinking_included_when_enabled(self, claude_env):
        claude_env.configure(include_thinking=True)
        messages = self._load(
            claude_env,
            [
                assistant_record(
                    "a1",
                    [
                        {"type": "thinking", "thinking": "let me reason"},
                        {"type": "text", "text": "answer"},
                    ],
                )
            ],
        )

        assert [m.searchable_text for m in messages] == ["let me reason", "answer"]

    def test_sidechains_can_be_excluded(self, claude_env):
        claude_env.configure(include_sidechains=False)
        messages = self._load(
            claude_env,
            [
                user_record("u1", "main turn"),
                user_record("u2", "sidechain turn", isSidechain=True),
            ],
        )

        assert [m.searchable_text for m in messages] == ["main turn"]

    def test_malformed_lines_tolerated(self, claude_env):
        path = claude_env.project / "abc.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps(user_record("u1", "still parsed"))
            + "\n[]\n",
            encoding="utf-8",
        )
        sessions = list_claude_sessions()
        messages = load_claude_session_messages(sessions[0])

        assert [m.searchable_text for m in messages] == ["still parsed"]

    def test_message_index_is_sequential(self, claude_env):
        messages = self._load(
            claude_env,
            [
                user_record("u1", "one"),
                assistant_record("a1", [{"type": "text", "text": "two"}]),
                user_record("u2", "three"),
            ],
        )

        assert [m.message_index for m in messages] == [0, 1, 2]

    def test_uuids_unique_across_blocks(self, claude_env):
        messages = self._load(
            claude_env,
            [
                assistant_record(
                    "a1",
                    [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                )
            ],
        )

        assert len({m.uuid for m in messages}) == 2

    def test_unknown_session_returns_empty(self, claude_env):
        from kiro_ception.models import SessionInfo

        session = SessionInfo(
            session_id="nonexistent", workspace=WORKSPACE, source=Source.CLAUDE
        )
        assert load_claude_session_messages(session) == []


# --- Tool call pairing ---


class TestToolContext:
    def _load(self, env, records):
        write_transcript(env.project / "abc.jsonl", records)
        return load_claude_session_messages(list_claude_sessions()[0])

    def _tool_pair(self, result_content="ok", is_error=False, name="Bash", tool_input=None):
        return [
            assistant_record(
                "a1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": name,
                        "input": tool_input if tool_input is not None else {"command": "pytest"},
                    }
                ],
            ),
            user_record(
                "u1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": result_content,
                        **({"is_error": True} if is_error else {}),
                    }
                ],
            ),
        ]

    def test_pair_becomes_tool_context_message(self, claude_env):
        messages = self._load(claude_env, self._tool_pair())

        assert len(messages) == 1
        assert messages[0].content_tier == ContentTier.TOOL_CONTEXT
        assert messages[0].tool_name == "Bash"
        assert messages[0].searchable_text.startswith("[Bash] pytest → completed")

    def test_error_result_marked_failed(self, claude_env):
        messages = self._load(
            claude_env, self._tool_pair(result_content="boom", is_error=True)
        )

        assert "→ failed" in messages[0].searchable_text
        # Errors are always kept, even without meaningful-output keywords.
        assert "boom" in messages[0].searchable_text

    def test_meaningful_output_included(self, claude_env):
        messages = self._load(
            claude_env, self._tool_pair(result_content="3 tests failed")
        )

        assert "3 tests failed" in messages[0].searchable_text

    def test_uninteresting_output_omitted(self, claude_env):
        messages = self._load(claude_env, self._tool_pair(result_content="a.py b.py"))

        assert "(no meaningful output)" in messages[0].searchable_text

    def test_result_block_list_flattened(self, claude_env):
        messages = self._load(
            claude_env,
            self._tool_pair(
                result_content=[{"type": "text", "text": "build error: missing semicolon"}]
            ),
        )

        assert "build error: missing semicolon" in messages[0].searchable_text

    def test_tool_use_result_used_as_fallback(self, claude_env):
        records = self._tool_pair(result_content="")
        records[1]["toolUseResult"] = {"stderr": "compilation failed"}
        messages = self._load(claude_env, records)

        assert "compilation failed" in messages[0].searchable_text

    def test_excluded_tools_dropped(self, claude_env):
        claude_env.configure(excluded_tools=["Bash"])
        assert self._load(claude_env, self._tool_pair()) == []

    def test_tool_context_can_be_disabled(self, claude_env):
        claude_env.configure(include_tool_context=False)
        records = self._tool_pair()
        records.append(user_record("u2", "follow-up"))
        messages = self._load(claude_env, records)

        assert [m.searchable_text for m in messages] == ["follow-up"]

    def test_unpaired_result_ignored(self, claude_env):
        messages = self._load(
            claude_env,
            [
                user_record(
                    "u1",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan",
                            "content": "error: nothing to pair with",
                        }
                    ],
                )
            ],
        )

        assert messages == []

    def test_unanswered_tool_use_produces_nothing(self, claude_env):
        messages = self._load(
            claude_env,
            [
                assistant_record(
                    "a1",
                    [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
                )
            ],
        )

        assert messages == []

    def test_summary_truncated_to_max_length(self, claude_env):
        claude_env.configure(max_summary_length=40)
        messages = self._load(
            claude_env, self._tool_pair(result_content="error " * 200)
        )

        assert len(messages[0].searchable_text) == 40
        assert messages[0].searchable_text.endswith("...")


class TestDescribeClaudeToolCall:
    @pytest.mark.parametrize(
        "tool_name,tool_input,expected",
        [
            ("Read", {"file_path": "/a/b.py"}, "/a/b.py"),
            ("Write", {"file_path": "/a/b.py"}, "/a/b.py"),
            ("Edit", {"file_path": "/a/b.py"}, "/a/b.py"),
            ("Edit", {"file_path": "/a/b.py", "replace_all": True}, "/a/b.py (replace_all)"),
            ("Bash", {"command": "git status"}, "git status"),
            ("PowerShell", {"command": "Get-ChildItem"}, "Get-ChildItem"),
            ("Glob", {"pattern": "**/*.py"}, 'pattern="**/*.py"'),
            ("Glob", {"pattern": "*.py", "path": "src"}, 'pattern="*.py" in src'),
            ("Grep", {"pattern": "TODO"}, 'pattern="TODO"'),
            ("WebFetch", {"url": "https://example.com"}, "https://example.com"),
            ("WebSearch", {"query": "mcp spec"}, 'query="mcp spec"'),
            ("Task", {"subagent_type": "Explore", "prompt": "find it"}, 'Explore: "find it"'),
            ("Skill", {"skill": "code-review", "args": "--fix"}, "code-review --fix"),
            ("TodoWrite", {"todos": [1, 2, 3]}, "3 todos"),
        ],
    )
    def test_descriptions(self, tool_name, tool_input, expected):
        assert _describe_claude_tool_call(tool_name, tool_input) == expected

    def test_long_command_truncated(self):
        result = _describe_claude_tool_call("Bash", {"command": "x" * 200})
        assert len(result) == 100
        assert result.endswith("...")

    def test_unknown_tool_serializes_input(self):
        result = _describe_claude_tool_call("mcp__thing__do", {"a": 1})
        assert result == '{"a": 1}'

    def test_missing_path_is_labelled(self):
        assert _describe_claude_tool_call("Read", {}) == "(unknown path)"

    def test_non_dict_input_is_safe(self):
        assert _describe_claude_tool_call("Read", "not a dict") == ""


class TestGenerateClaudeToolSummary:
    def test_excluded_tool_returns_none(self):
        config = ToolSummaryConfig(excluded_tools=["Read"])
        summary = _generate_claude_tool_summary(
            {"name": "Read", "input": {"file_path": "a.py"}}, {}, None, config
        )
        assert summary is None

    def test_empty_description_labelled(self):
        config = ToolSummaryConfig(include_meaningful_output=False)
        summary = _generate_claude_tool_summary(
            {"name": "Bash", "input": {}}, {}, None, config
        )
        assert summary == "[Bash] (no command) → completed"

    def test_summary_is_single_line_before_output(self):
        config = ToolSummaryConfig(include_meaningful_output=False)
        summary = _generate_claude_tool_summary(
            {"name": "Bash", "input": {"command": "a\nb"}}, {}, None, config
        )
        assert "\n" not in summary

    def test_unnamed_tool_falls_back(self):
        config = ToolSummaryConfig(include_meaningful_output=False)
        summary = _generate_claude_tool_summary({"input": {}}, {}, None, config)
        assert summary.startswith("[unknown]")


class TestStripSystemReminders:
    def test_multiline_block_removed(self):
        text = "before<system-reminder>\nmulti\nline\n</system-reminder>after"
        assert _strip_system_reminders(text) == "beforeafter"

    def test_multiple_blocks_removed(self):
        text = "a<system-reminder>x</system-reminder>b<system-reminder>y</system-reminder>c"
        assert _strip_system_reminders(text) == "abc"

    def test_text_without_reminders_unchanged(self):
        assert _strip_system_reminders("plain text") == "plain text"


# --- Configuration plumbing ---


class TestClaudeSourceConfig:
    def test_defaults(self):
        config = Config()
        assert config.claude.enabled is True
        assert config.claude.include_subagents is True
        assert config.claude.include_sidechains is True
        assert config.claude.include_thinking is False
        assert config.claude.include_tool_context is True
        assert "~/.claude/projects" in config.claude.roots

    def test_parsed_from_dict(self):
        config = Config.from_dict(
            {
                "sources": {
                    "claude": {
                        "enabled": False,
                        "roots": ["/custom/claude"],
                        "include_thinking": True,
                    }
                }
            }
        )

        assert config.claude.enabled is False
        assert config.claude.roots == ["/custom/claude"]
        assert config.claude.include_thinking is True
        # Unspecified keys keep their defaults
        assert config.claude.include_subagents is True

    def test_absent_section_uses_defaults(self):
        config = Config.from_dict({"sources": {"cli": {"enabled": False}}})
        assert config.claude.enabled is True

    def test_get_roots_filters_missing(self, tmp_path):
        existing = tmp_path / "here"
        existing.mkdir()
        config = ClaudeSourceConfig(roots=[str(existing), str(tmp_path / "gone")])

        assert config.get_roots() == [existing]

    def test_get_roots_deduplicates(self, tmp_path):
        existing = tmp_path / "here"
        existing.mkdir()
        config = ClaudeSourceConfig(roots=[str(existing), str(existing)])

        assert config.get_roots() == [existing]

    def test_config_changes_are_hot_reloadable(self):
        old = Config()
        new = Config(claude=ClaudeSourceConfig(enabled=False, include_thinking=True))
        changes = diff_configs(old, new)
        keys = {c["key"] for c in changes}

        assert "sources.claude.enabled" in keys
        assert "sources.claude.include_thinking" in keys
        for change in changes:
            assert change["impact"] == "safe"


class TestSourceEnum:
    def test_claude_member_exists(self):
        assert Source.CLAUDE.value == "claude"

    def test_constructible_from_string(self):
        assert Source("claude") is Source.CLAUDE

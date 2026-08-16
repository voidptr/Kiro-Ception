"""Tests for instance identity on the MCP surface (server.py).

Concurrent Kiro-Ception instances expose tools with identical names and
identical docstrings. These tests cover the two things that make an instance
distinguishable: --config being honoured before tools are registered, and the
instance summary being appended to every tool description.
"""

import sys

import pytest

from kiro_ception import config as config_module
from kiro_ception import server
from kiro_ception.config import (
    ClaudeSourceConfig,
    Config,
    ServerConfig,
    get_config_file,
    set_config_file,
)


@pytest.fixture(autouse=True)
def restore_config_override():
    """Undo any config-file override a test installs.

    Restores the raw override rather than get_config_file(), which falls back
    to the default path when nothing is set — feeding that back through
    set_config_file() would install an override that was never there and leak
    into later tests.
    """
    original = config_module._config_file_override
    yield
    config_module._config_file_override = original
    config_module.get_config.cache_clear()


# --- --config must win before import-time config reads ---


class TestApplyConfigOverrideFromArgv:
    def test_space_separated_form(self, monkeypatch, tmp_path):
        target = tmp_path / "instance.toml"
        target.write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["kiro-ception", "--config", str(target)])

        server._apply_config_override_from_argv()

        assert get_config_file() == target.resolve()

    def test_equals_form(self, monkeypatch, tmp_path):
        target = tmp_path / "instance.toml"
        target.write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["kiro-ception", f"--config={target}"])

        server._apply_config_override_from_argv()

        assert get_config_file() == target.resolve()

    def test_absent_flag_leaves_override_alone(self, monkeypatch, tmp_path):
        sentinel = tmp_path / "sentinel.toml"
        sentinel.write_text("", encoding="utf-8")
        set_config_file(sentinel)
        monkeypatch.setattr(sys, "argv", ["kiro-ception"])

        server._apply_config_override_from_argv()

        assert get_config_file() == sentinel.resolve()

    def test_dangling_flag_is_ignored(self, monkeypatch, tmp_path):
        sentinel = tmp_path / "sentinel.toml"
        sentinel.write_text("", encoding="utf-8")
        set_config_file(sentinel)
        # --config with no value following it must not raise.
        monkeypatch.setattr(sys, "argv", ["kiro-ception", "--config"])

        server._apply_config_override_from_argv()

        assert get_config_file() == sentinel.resolve()


# --- Tool descriptions carry the instance summary ---


class _RecordingMCP:
    """Stands in for FastMCP, capturing the description each tool registers."""

    def __init__(self):
        self.descriptions: list[str] = []

    def tool(self, description=None, **_kwargs):
        self.descriptions.append(description)
        return lambda fn: fn


def _register(monkeypatch, config: Config) -> str:
    recorder = _RecordingMCP()
    monkeypatch.setattr(server, "mcp", recorder)
    monkeypatch.setattr(server, "_get_config", lambda: config)

    @server._instance_tool
    def search_project_history():
        """Search conversation history for the CURRENT WORKSPACE only.

        Use this to find workspace-specific context.
        """

    assert len(recorder.descriptions) == 1
    return recorder.descriptions[0]


class TestServerNameIsConstant:
    """instance_label distinguishes instances; it must not rename the server.

    The product identity is "kiro-ception" for every instance. Callers select
    an instance by its MCP config key, and tell instances apart by the label
    in each tool description — neither depends on the server name.
    """

    def test_server_name_is_kiro_ception(self):
        assert server.mcp.name == "kiro-ception"

    def test_label_is_not_the_server_name(self, monkeypatch):
        config = Config(server=ServerConfig(instance_label="claude-history"))
        monkeypatch.setattr(server, "_get_config", lambda: config)
        # The label belongs in descriptions only.
        assert server.mcp.name == "kiro-ception"
        assert "claude-history" in config.instance_summary


class TestInstanceAwareToolDescriptions:
    def test_description_keeps_the_original_docstring(self, monkeypatch):
        description = _register(monkeypatch, Config())
        assert "Search conversation history for the CURRENT WORKSPACE only." in description

    def test_description_states_the_indexed_sources(self, monkeypatch):
        description = _register(monkeypatch, Config())
        assert "Indexes: Claude Code, Kiro IDE, Kiro CLI." in description

    def test_description_includes_the_label(self, monkeypatch):
        config = Config(server=ServerConfig(instance_label="claude-history"))
        description = _register(monkeypatch, config)
        assert 'Instance "claude-history".' in description

    def test_two_instances_get_different_descriptions(self, monkeypatch):
        claude_side = _register(
            monkeypatch, Config(server=ServerConfig(instance_label="claude-history"))
        )
        kiro_side = _register(
            monkeypatch,
            Config(
                server=ServerConfig(instance_label="kiro-ception"),
                claude=ClaudeSourceConfig(enabled=False),
            ),
        )

        assert claude_side != kiro_side
        assert "Claude Code" in claude_side
        assert "Claude Code" not in kiro_side

    def test_docstring_is_dedented(self, monkeypatch):
        description = _register(monkeypatch, Config())
        # inspect.cleandoc strips the leading indentation of continuation lines.
        assert "\n        Use this" not in description
        assert "Use this to find workspace-specific context." in description

    def test_summary_is_separated_from_the_docstring(self, monkeypatch):
        description = _register(monkeypatch, Config())
        assert description.endswith("Indexes: Claude Code, Kiro IDE, Kiro CLI.")
        assert "\n\nIndexes:" in description

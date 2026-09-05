"""Tests for the GitHub Copilot Chat (VS Code) conversation loader."""

import json
import shutil
from pathlib import Path

import pytest

from kiro_ception import copilot_loader
from kiro_ception.config import Config, CopilotSourceConfig
from kiro_ception.models import ContentTier, Source

FIXTURES = Path(__file__).parent / "fixtures"


def _make_workspace(root: Path, ws_hash: str, folder_uri: str) -> Path:
    """Build a workspaceStorage/<hash>/chatSessions/ skeleton, return chat dir."""
    ws_dir = root / ws_hash
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": folder_uri}), encoding="utf-8"
    )
    return chat_dir


@pytest.fixture
def copilot_root(tmp_path, monkeypatch):
    """A tmp workspaceStorage root wired into config, with both fixture formats."""
    root = tmp_path / "workspaceStorage"
    chat_dir = _make_workspace(root, "ws1hash", "file:///c%3A/Source/proj")
    shutil.copy(FIXTURES / "copilot_session.json", chat_dir / "aaaaaaaa.json")
    shutil.copy(FIXTURES / "copilot_session.jsonl", chat_dir / "11111111.jsonl")

    cfg = Config(copilot=CopilotSourceConfig(enabled=True, roots=[str(root)]))
    monkeypatch.setattr(copilot_loader, "get_config", lambda: cfg)
    copilot_loader._session_paths.clear()
    return root


class TestUriToPath:
    def test_windows_drive_uri(self):
        assert copilot_loader._uri_to_path("file:///c%3A/Source/proj") == r"C:\Source\proj"

    def test_posix_uri(self):
        assert copilot_loader._uri_to_path("file:///home/me/proj") == "/home/me/proj"

    def test_empty(self):
        assert copilot_loader._uri_to_path("") == ""

    def test_non_file_scheme_passthrough(self):
        assert copilot_loader._uri_to_path("vscode-remote://x/y") == "vscode-remote://x/y"


class TestReadWorkspaceFolder:
    def test_folder_key(self, tmp_path):
        d = tmp_path / "ws"
        d.mkdir()
        (d / "workspace.json").write_text('{"folder": "file:///c%3A/A/B"}', encoding="utf-8")
        assert copilot_loader._read_workspace_folder(d) == r"C:\A\B"

    def test_workspace_key_fallback(self, tmp_path):
        d = tmp_path / "ws"
        d.mkdir()
        (d / "workspace.json").write_text(
            '{"workspace": "file:///home/me/x.code-workspace"}', encoding="utf-8"
        )
        assert copilot_loader._read_workspace_folder(d) == "/home/me/x.code-workspace"

    def test_missing_file(self, tmp_path):
        assert copilot_loader._read_workspace_folder(tmp_path) == ""


class TestApplyPatch:
    def test_set_existing_dict_key(self):
        base = {"a": 1}
        copilot_loader._apply_patch(base, ["a"], 2)
        assert base["a"] == 2

    def test_create_nested_dict(self):
        base = {}
        copilot_loader._apply_patch(base, ["x", "y"], 5)
        assert base == {"x": {"y": 5}}

    def test_grow_list_with_padding(self):
        base = {"items": []}
        copilot_loader._apply_patch(base, ["items", 2], "z")
        assert base["items"] == [None, None, "z"]

    def test_nested_list_index(self):
        base = {"requests": [{"response": ["a"]}]}
        copilot_loader._apply_patch(base, ["requests", 0, "response", 1], "b")
        assert base["requests"][0]["response"] == ["a", "b"]

    def test_empty_keys_noop(self):
        base = {"a": 1}
        copilot_loader._apply_patch(base, [], 99)
        assert base == {"a": 1}

    def test_scalar_midpath_is_rebuilt_not_raised(self):
        base = {"a": 1}
        # A patch navigating through a scalar rebuilds it as a container to
        # land the value — matching VS Code's own replay — and never raises.
        copilot_loader._apply_patch(base, ["a", "b"], 2)
        assert base["a"] == {"b": 2}


class TestMaterializeJsonl:
    def test_replays_base_plus_patches(self):
        obj = copilot_loader._materialize_jsonl(FIXTURES / "copilot_session.jsonl")
        assert obj is not None
        assert obj["customTitle"] == "Implementing a retry helper"
        # patch appended a second request
        assert len(obj["requests"]) == 2
        assert obj["requests"][1]["message"]["text"] == "add exponential backoff"
        # patch filled response[1].value on request 0
        assert obj["requests"][0]["response"][1]["value"].startswith("Here is a simple")
        # top-level patch applied
        assert obj["lastMessageDate"] == 1778475000000

    def test_missing_base_returns_none(self, tmp_path):
        f = tmp_path / "nobase.jsonl"
        f.write_text('{"kind":1,"k":["a"],"v":1}\n', encoding="utf-8")
        assert copilot_loader._materialize_jsonl(f) is None


class TestExtractText:
    def test_user_text_from_dict(self):
        assert copilot_loader._extract_user_text({"text": "hi"}) == "hi"

    def test_user_text_from_parts(self):
        msg = {"parts": [{"text": "a"}, {"text": "b"}]}
        assert copilot_loader._extract_user_text(msg) == "a\nb"

    def test_response_skips_plumbing_kinds(self):
        resp = [
            {"kind": "mcpServersStarting", "didStartServerIds": []},
            {"value": "real answer"},
        ]
        assert copilot_loader._extract_response_text(resp) == "real answer"

    def test_response_bare_string(self):
        assert copilot_loader._extract_response_text(["just text"]) == "just text"


class TestListAndLoad:
    def test_lists_both_formats_with_workspace(self, copilot_root):
        sessions = copilot_loader.list_copilot_sessions()
        assert len(sessions) == 2
        assert all(s.source == Source.COPILOT for s in sessions)
        assert all(s.workspace == r"C:\Source\proj" for s in sessions)
        ids = {s.session_id for s in sessions}
        assert ids == {"copilot-aaaaaaaa", "copilot-11111111"}

    def test_load_json_session(self, copilot_root):
        sessions = {s.session_id: s for s in copilot_loader.list_copilot_sessions()}
        msgs = copilot_loader.load_copilot_session_messages(sessions["copilot-aaaaaaaa"])
        roles = [m.role for m in msgs]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert msgs[0].searchable_text == "how do I reverse a list in python"
        # code block condensed to placeholder
        assert "[code:python]" in msgs[1].searchable_text
        assert all(m.content_tier == ContentTier.CONVERSATION for m in msgs)
        assert all(m.source == Source.COPILOT for m in msgs)

    def test_load_jsonl_session(self, copilot_root):
        sessions = {s.session_id: s for s in copilot_loader.list_copilot_sessions()}
        msgs = copilot_loader.load_copilot_session_messages(sessions["copilot-11111111"])
        texts = [m.searchable_text for m in msgs]
        assert "write a retry decorator" in texts
        assert "add exponential backoff" in texts
        assert any("retries up to n times" in t for t in texts)

    def test_missing_session_returns_empty(self, copilot_root):
        from kiro_ception.models import SessionInfo

        bogus = SessionInfo(session_id="copilot-nope", workspace="", source=Source.COPILOT)
        assert copilot_loader.load_copilot_session_messages(bogus) == []

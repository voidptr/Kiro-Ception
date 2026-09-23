"""Unit tests for MCP tool endpoint functions in server.py.

Since server.py is now a pure proxy (all tools forward to the engine via HTTP),
these tests mock the EngineClient to verify:
- Correct request forwarding (parameters passed correctly)
- Proper response passthrough
- Error handling / graceful degradation
"""

from unittest.mock import MagicMock, patch

import pytest

# --- Shared fixtures ---


@pytest.fixture
def mock_client():
    """Mock the EngineClient returned by get_engine_client()."""
    client = MagicMock()
    client.search.return_value = {
        "results": [],
        "query": "test",
        "total_matches": 0,
        "offset": 0,
        "has_more": False,
    }
    client.get_status.return_value = {
        "state": "idle",
        "sessions_total": 100,
        "sessions_processed": 100,
        "embedding_count": 1000,
        "errors": 0,
        "search_ready": True,
        "search_message_count": 2000,
    }
    client.get_config.return_value = {
        "instance": {"role": "engine", "pid": 12345, "port": 19742},
        "version": {"kiro_ception": "1.0.9"},
        "embedding": {"backend": "sentence-transformers", "model": "all-MiniLM-L6-v2"},
        "server": {"engine_port": 19742, "listen_address": "127.0.0.1:19742"},
        "cache": {"embedding_count": 1000, "message_count": 2000, "indexed_sessions": 100},
        "sources": {"cli": {"enabled": True}, "ide": {"enabled": True}},
        "paths": {"config_file": "/tmp/config.toml", "config_exists": True},
        "memory": {"fraction": 0.5, "limit_mb": 0, "effective_limit_mb": "unlimited"},
        "search": {"default_threshold": 0.2, "default_max_results": 10, "default_context_window": 3},
        "indexing": {"throttle_ms": 0, "rescan_interval_minutes": 10},
        "peers": {"enabled": False, "nodes": []},
    }
    client.trigger_rescan.return_value = {"status": "rescan_triggered"}
    client.trigger_reindex.return_value = {"status": "reindex_triggered"}
    client.reload_config.return_value = {"status": "no_changes", "message": "No changes."}
    return client


@pytest.fixture(autouse=True)
def patch_initialization(mock_client):
    """Patch initialization and engine client for all tests."""
    with (
        patch("kiro_ception.server._initialized", True),
        patch("kiro_ception.server.get_engine_client", return_value=mock_client),
    ):
        yield


# --- search_project_history ---


class TestSearchProjectHistory:
    def test_returns_dict_structure(self, mock_client):
        from kiro_ception.server import search_project_history

        result = search_project_history(query="test query")
        assert isinstance(result, dict)
        assert "results" in result
        assert "query" in result

    def test_passes_workspace_filter(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(query="test", workspace="/my/project")

        call_args = mock_client.search.call_args[0][0]
        assert call_args["workspace"] == "/my/project"

    def test_passes_all_parameters(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(
            query="hello",
            after="2025-01-01",
            before="2025-06-01",
            context_size=5,
            threshold=0.3,
            max_results=20,
            offset=10,
            include_tool_context=True,
            workspace="/ws",
            require_terms=["prod"],
            exclude_terms=["staging", "draft"],
            promote_terms=["rollback"],
            demote_terms=["wip"],
        )

        call_args = mock_client.search.call_args[0][0]
        assert call_args["query"] == "hello"
        assert call_args["after"] == "2025-01-01"
        assert call_args["before"] == "2025-06-01"
        assert call_args["context_size"] == 5
        assert call_args["threshold"] == 0.3
        assert call_args["max_results"] == 20
        assert call_args["offset"] == 10
        assert call_args["include_tool_context"] is True
        assert call_args["workspace"] == "/ws"
        assert call_args["require_terms"] == ["prod"]
        assert call_args["exclude_terms"] == ["staging", "draft"]
        assert call_args["promote_terms"] == ["rollback"]
        assert call_args["demote_terms"] == ["wip"]

    def test_operator_terms_default_to_empty_lists(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(query="hello", workspace="/ws")

        call_args = mock_client.search.call_args[0][0]
        assert call_args["require_terms"] == []
        assert call_args["exclude_terms"] == []
        assert call_args["promote_terms"] == []
        assert call_args["demote_terms"] == []

    def test_no_workspace_arg_runs_unscoped(self, mock_client):
        """No workspace arg + no config pin => unscoped (workspace=None), no guessing."""
        with patch("kiro_ception.server._config_workspace", return_value=None):
            from kiro_ception.server import search_project_history

            search_project_history(query="test")

            call_args = mock_client.search.call_args[0][0]
            assert call_args["workspace"] is None

    def test_workspace_param_overrides_config(self, mock_client):
        """Explicit workspace wins even when a config pin exists."""
        with patch("kiro_ception.server._config_workspace", return_value="/configured"):
            from kiro_ception.server import search_project_history

            search_project_history(query="test", workspace="/correct/project/path")

            call_args = mock_client.search.call_args[0][0]
            assert call_args["workspace"] == "/correct/project/path"

    def test_config_pin_used_when_no_arg(self, mock_client):
        """When no workspace arg is passed, a config pin supplies the default scope."""
        with patch("kiro_ception.server._config_workspace", return_value="/configured/ws"):
            from kiro_ception.server import search_project_history

            search_project_history(query="test")

            call_args = mock_client.search.call_args[0][0]
            assert call_args["workspace"] == "/configured/ws"

    def test_empty_string_workspace_falls_through_to_config(self, mock_client):
        """Empty string is falsy => treated as 'not passed', so config pin applies."""
        with patch("kiro_ception.server._config_workspace", return_value="/configured/ws"):
            from kiro_ception.server import search_project_history

            search_project_history(query="test", workspace="")

            call_args = mock_client.search.call_args[0][0]
            assert call_args["workspace"] == "/configured/ws"

    def test_workspace_resolution_reported_for_explicit(self, mock_client):
        """Passing workspace explicitly reports source='explicit' and scoped=True."""
        from kiro_ception.server import search_project_history

        result = search_project_history(query="test", workspace="/correct/project")

        assert result["workspace_resolution"] == {
            "workspace": "/correct/project",
            "source": "explicit",
            "scoped": True,
        }

    def test_workspace_resolution_reported_for_config(self, mock_client):
        """A config-pinned workspace reports source='config' and scoped=True."""
        with patch("kiro_ception.server._config_workspace", return_value="/configured/ws"):
            from kiro_ception.server import search_project_history

            result = search_project_history(query="test")

            assert result["workspace_resolution"] == {
                "workspace": "/configured/ws",
                "source": "config",
                "scoped": True,
            }

    def test_workspace_resolution_none_runs_unscoped(self, mock_client):
        """No arg + no config => search runs unscoped (workspace=None) and says so."""
        with patch("kiro_ception.server._config_workspace", return_value=None):
            from kiro_ception.server import search_project_history

            result = search_project_history(query="test")

            call_args = mock_client.search.call_args[0][0]
            assert call_args["workspace"] is None
            assert result["workspace_resolution"] == {
                "workspace": None,
                "source": "none",
                "scoped": False,
            }


# --- workspace config pin (reading A: no auto-detection at all) ---


class TestConfigWorkspace:
    """Unit tests for _config_workspace() — the ONLY server-side workspace source.

    Reading A: there is no env-var or cwd detection. The only server-side scope
    is an explicit user-pinned search.workspace_dir; everything else is None
    (unscoped), and the calling agent must pass `workspace` for project scope.
    """

    def test_no_config_pin_returns_none(self):
        cfg = MagicMock()
        cfg.search.workspace_dir = None
        with patch("kiro_ception.server._get_config", return_value=cfg):
            from kiro_ception.server import _config_workspace

            assert _config_workspace() is None

    def test_config_pin_returned_and_expanded(self):
        cfg = MagicMock()
        cfg.search.workspace_dir = "~/configured/ws"
        with (
            patch("kiro_ception.server._get_config", return_value=cfg),
            patch("kiro_ception.server.expand_path", side_effect=lambda p: p.replace("~", "/home")),
        ):
            from kiro_ception.server import _config_workspace

            assert _config_workspace() == "/home/configured/ws"

    def test_env_vars_are_ignored(self, monkeypatch):
        """KIRO_WORKSPACE / CLAUDE_PROJECT_DIR must NOT influence scope under reading A."""
        monkeypatch.setenv("KIRO_WORKSPACE", "/kiro/ws")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/ws")
        cfg = MagicMock()
        cfg.search.workspace_dir = None
        with patch("kiro_ception.server._get_config", return_value=cfg):
            from kiro_ception.server import _config_workspace

            assert _config_workspace() is None


# --- search_global_history ---


class TestSearchGlobalHistory:
    def test_returns_dict(self, mock_client):
        from kiro_ception.server import search_global_history

        result = search_global_history(query="patterns")
        assert isinstance(result, dict)

    def test_no_workspace_filter(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["workspace"] is None

    def test_source_filter_cli(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test", source="cli")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["source"] == "cli"

    def test_source_filter_ide(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test", source="ide")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["source"] == "ide"

    def test_source_filter_all(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test", source="all")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["source"] is None


# --- get_indexing_status ---


class TestGetIndexingStatus:
    def test_returns_status(self, mock_client):
        from kiro_ception.server import get_indexing_status

        result = get_indexing_status()
        assert result["state"] == "idle"
        assert result["search_ready"] is True

    def test_handles_engine_unavailable(self, mock_client):
        mock_client.get_status.side_effect = Exception("connection refused")

        from kiro_ception.server import get_indexing_status

        result = get_indexing_status()
        assert "error" in result
        assert result["state"] == "unknown"


# --- rescan ---


class TestRescan:
    def test_triggers_rescan(self, mock_client):
        from kiro_ception.server import rescan

        result = rescan(full=False)
        mock_client.trigger_rescan.assert_called_once()
        assert result["status"] == "rescan_triggered"

    def test_triggers_full_rescan(self, mock_client):
        from kiro_ception.server import rescan

        result = rescan(full=True)
        mock_client.trigger_reindex.assert_called_once()
        assert result["status"] == "reindex_triggered"

    def test_handles_engine_unavailable(self, mock_client):
        mock_client.trigger_rescan.side_effect = Exception("dead")

        from kiro_ception.server import rescan

        result = rescan()
        assert "error" in result


# --- get_config ---


class TestGetConfig:
    def test_returns_config_structure(self, mock_client):
        from kiro_ception.server import get_config

        result = get_config()
        assert "instance" in result
        assert "version" in result
        assert "embedding" in result

    def test_handles_engine_unavailable(self, mock_client):
        mock_client.get_config.side_effect = Exception("dead")

        from kiro_ception.server import get_config

        result = get_config()
        assert "error" in result
        # Should still have fallback local config
        assert "embedding" in result


# --- reload_config ---


class TestReloadConfig:
    def test_forwards_to_engine(self, mock_client):
        from kiro_ception.server import reload_config

        result = reload_config()
        mock_client.reload_config.assert_called_once()
        assert result["status"] == "no_changes"

    def test_handles_engine_unavailable(self, mock_client):
        mock_client.reload_config.side_effect = Exception("dead")

        from kiro_ception.server import reload_config

        result = reload_config()
        assert "error" in result

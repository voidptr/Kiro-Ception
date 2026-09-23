"""Unit tests for include_tool_context parameter forwarding.

Tests that include_tool_context is correctly passed through the proxy to the engine.
Also tests context_window content_tier field presence.

Requirements: 9.1, 9.2
"""

from unittest.mock import MagicMock, patch

import pytest

from kiro_ception.search_utils import build_context_window


@pytest.fixture
def mock_client():
    """Mock EngineClient."""
    client = MagicMock()
    client.search.return_value = {
        "results": [],
        "query": "test",
        "total_matches": 0,
        "offset": 0,
        "has_more": False,
    }
    return client


@pytest.fixture(autouse=True)
def patch_init(mock_client):
    with (
        patch("kiro_ception.server._initialized", True),
        patch("kiro_ception.server.get_engine_client", return_value=mock_client),
    ):
        yield


class TestSearchProjectHistoryIncludeToolContext:
    """Tests that search_project_history passes include_tool_context correctly."""

    def test_include_tool_context_true_passed_to_search(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(query="test", include_tool_context=True)
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is True

    def test_include_tool_context_false_passed_to_search(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(query="test", include_tool_context=False)
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is False

    def test_omitting_include_tool_context_defaults_false(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(query="test")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is False


class TestSearchGlobalHistoryIncludeToolContext:
    """Tests that search_global_history passes include_tool_context correctly."""

    def test_include_tool_context_true_passed_to_search(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test", include_tool_context=True)
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is True

    def test_include_tool_context_false_passed_to_search(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test", include_tool_context=False)
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is False

    def test_omitting_include_tool_context_defaults_false(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(query="test")
        call_args = mock_client.search.call_args[0][0]
        assert call_args["include_tool_context"] is False


class TestBackwardCompatibility:
    """Omitting include_tool_context produces identical behavior to before."""

    def test_project_search_same_params_as_before(self, mock_client):
        from kiro_ception.server import search_project_history

        search_project_history(
            query="hello",
            after="2025-01-01",
            before="2025-12-31",
            context_size=5,
            threshold=0.3,
            max_results=20,
            offset=10,
            workspace="/workspace",
        )

        call_args = mock_client.search.call_args[0][0]
        assert call_args["query"] == "hello"
        assert call_args["workspace"] == "/workspace"
        assert call_args["after"] == "2025-01-01"
        assert call_args["before"] == "2025-12-31"
        assert call_args["context_size"] == 5
        assert call_args["threshold"] == 0.3
        assert call_args["max_results"] == 20
        assert call_args["offset"] == 10
        assert call_args["include_tool_context"] is False

    def test_global_search_same_params_as_before(self, mock_client):
        from kiro_ception.server import search_global_history

        search_global_history(
            query="hello",
            after="2025-01-01",
            before="2025-12-31",
            context_size=5,
            threshold=0.3,
            max_results=20,
            offset=10,
            source="cli",
        )

        call_args = mock_client.search.call_args[0][0]
        assert call_args["query"] == "hello"
        assert call_args["source"] == "cli"
        assert call_args["include_tool_context"] is False


class TestContextWindowContentTier:
    """Tests that build_context_window includes content_tier in output."""

    def test_content_tier_present_in_context_messages(self):
        """Context messages should include content_tier field."""
        messages = [
            {"uuid": "a", "role": "user", "searchable_text": "hello", "timestamp": 1000.0,
             "message_index": 0, "content_tier": "conversation"},
            {"uuid": "b", "role": "assistant", "searchable_text": "hi there", "timestamp": 1001.0,
             "message_index": 1, "content_tier": "conversation"},
            {"uuid": "c", "role": "assistant", "searchable_text": "ran ls", "timestamp": 1002.0,
             "message_index": 2, "content_tier": "tool_context"},
        ]

        context = build_context_window(
            session_messages=messages,
            match_uuid="a",
            context_size=3,
        )

        for msg in context:
            assert "content_tier" in msg

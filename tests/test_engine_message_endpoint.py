"""Tests for the engine's /message endpoint.

The endpoint returns full stored message text by uuid. Unlike /search, which
deliberately serves remote peers, this one must be loopback-only: a by-uuid
full-text fetch is a far sharper exfiltration primitive than scored excerpts.
"""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_ception.engine_main import _build_request_handler


@pytest.fixture(autouse=True)
def no_peer_secret():
    """Pin peer crypto off so these tests don't depend on the developer's
    own config.toml (a configured secret would 401 plaintext peer requests)."""
    cfg = {"enabled": False, "nodes": [], "secret": "", "timeout_seconds": 5, "key": None}
    with patch("kiro_ception.peers.get_peer_config", return_value=cfg):
        yield


@pytest.fixture
def message_handler():
    """Records the request it was handed and returns a canned response."""
    handler = MagicMock()
    handler.return_value = {
        "status": "ok",
        "messages": [{"uuid": "m1", "content": "z" * 4000}],
        "not_found": [],
    }
    return handler


@pytest.fixture
def handler_cls(message_handler):
    return _build_request_handler(
        search_handler=MagicMock(return_value={"results": []}),
        config_handler=MagicMock(return_value={}),
        indexer_getter=MagicMock(),
        follower_registry=MagicMock(),
        startup_fingerprint="test-fp",
        message_handler=message_handler,
    )


def _post(handler_cls, path, body, client_ip="127.0.0.1"):
    """Drive do_POST without a real socket.

    BaseHTTPRequestHandler.__init__ would try to service a connection, so the
    instance is built unbound and given just the attributes do_POST touches.
    """
    handler = object.__new__(handler_cls)
    raw = json.dumps(body).encode()

    handler.path = path
    handler.client_address = (client_ip, 54321)
    handler.headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    }
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()

    captured = {}
    handler.send_response = lambda code: captured.__setitem__("status", code)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None

    handler.do_POST()

    payload = handler.wfile.getvalue()
    return captured.get("status"), (json.loads(payload) if payload else {})


class TestMessageEndpoint:
    def test_loopback_request_is_served(self, handler_cls, message_handler):
        status, body = _post(handler_cls, "/message", {"uuids": ["m1"]})

        assert status == 200
        assert body["messages"][0]["uuid"] == "m1"
        assert len(body["messages"][0]["content"]) == 4000

    def test_uuids_forwarded_to_handler(self, handler_cls, message_handler):
        _post(handler_cls, "/message", {"uuids": ["m1", "m2"]})

        assert message_handler.call_args[0][0]["uuids"] == ["m1", "m2"]

    def test_ipv6_loopback_is_served(self, handler_cls):
        status, _ = _post(handler_cls, "/message", {"uuids": ["m1"]}, client_ip="::1")
        assert status == 200

    def test_non_loopback_is_forbidden(self, handler_cls, message_handler):
        status, body = _post(
            handler_cls, "/message", {"uuids": ["m1"]}, client_ip="192.168.1.50"
        )

        assert status == 403
        assert body["error"] == "forbidden"
        # The database must not have been touched at all
        message_handler.assert_not_called()

    def test_non_loopback_can_still_search(self, handler_cls):
        """The 403 is specific to /message — peer search is unaffected."""
        status, _ = _post(
            handler_cls, "/search", {"query": "hi"}, client_ip="192.168.1.50"
        )
        assert status == 200

    def test_absent_message_handler_is_404(self):
        """An engine built without the handler reports the route as missing
        rather than raising."""
        handler_cls = _build_request_handler(
            search_handler=MagicMock(return_value={"results": []}),
            config_handler=MagicMock(return_value={}),
            indexer_getter=MagicMock(),
            follower_registry=MagicMock(),
            startup_fingerprint="test-fp",
        )

        status, body = _post(handler_cls, "/message", {"uuids": ["m1"]})

        assert status == 404
        assert body["error"] == "not found"

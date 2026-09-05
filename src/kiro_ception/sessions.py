"""Unified loader combining CLI, IDE, Claude Code, and GitHub Copilot sources."""

from .claude_loader import list_claude_sessions, load_claude_session_messages
from .cli_loader import list_cli_sessions, load_cli_session_messages
from .config import get_config
from .copilot_loader import list_copilot_sessions, load_copilot_session_messages
from .ide_loader import list_ide_sessions, load_ide_session_messages
from .models import IndexedMessage, SessionInfo, Source


def list_all_sessions() -> list[SessionInfo]:
    """List all sessions from enabled sources, sorted by modified time."""
    config = get_config()
    sessions = []

    if config.cli.enabled:
        sessions.extend(list_cli_sessions())

    if config.ide.enabled:
        sessions.extend(list_ide_sessions())

    if config.claude.enabled:
        sessions.extend(list_claude_sessions())

    if config.copilot.enabled:
        sessions.extend(list_copilot_sessions())

    return sorted(sessions, key=lambda s: s.timestamp_fallback, reverse=True)


def load_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages for a session based on its source."""
    if session.source == Source.CLI:
        return load_cli_session_messages(session)
    if session.source == Source.CLAUDE:
        return load_claude_session_messages(session)
    if session.source == Source.COPILOT:
        return load_copilot_session_messages(session)
    return load_ide_session_messages(session)

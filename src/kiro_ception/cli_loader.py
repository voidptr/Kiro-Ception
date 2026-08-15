"""Load conversations from Kiro CLI SQLite database."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import get_config
from .models import IndexedMessage, SessionInfo, Source

logger = logging.getLogger(__name__)


def get_database_path() -> Path | None:
    """Get the CLI database path."""
    return get_config().cli.database_path


def _parse_timestamp(ts: int | str | None) -> datetime | None:
    """Parse a timestamp into naive LOCAL time.

    Mirrors ide_loader._parse_timestamp: offset-aware ISO 8601 strings are
    converted to local before the tzinfo is dropped, so they line up with the
    epoch values that `datetime.fromtimestamp` already returns as local.
    """
    if ts is None:
        return None
    if isinstance(ts, int):
        # Unix timestamp in milliseconds
        return datetime.fromtimestamp(ts / 1000)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    return None


def _extract_text_from_content(content: dict | str | list | None) -> str:
    """Extract searchable text from message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Handle {"Prompt": {"prompt": "..."}} structure
        if "Prompt" in content:
            return content["Prompt"].get("prompt", "")
        if "text" in content:
            return content["text"]
        if "prompt" in content:
            return content["prompt"]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def list_cli_sessions() -> list[SessionInfo]:
    """List all CLI sessions."""
    db_path = get_database_path()
    if not db_path or not db_path.exists():
        return []

    sessions = []
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT key, conversation_id, created_at, updated_at FROM conversations_v2"
        )
        for row in cursor:
            workspace, conv_id, created_at, updated_at = row
            sessions.append(
                SessionInfo(
                    session_id=conv_id,
                    workspace=workspace,
                    created=_parse_timestamp(created_at),
                    modified=_parse_timestamp(updated_at),
                    source=Source.CLI,
                )
            )
        conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Error reading CLI database: {e}")

    return sessions


def load_cli_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages for a CLI session."""
    db_path = get_database_path()
    if not db_path or not db_path.exists():
        return []

    messages = []
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT value FROM conversations_v2 WHERE key = ? AND conversation_id = ?",
            (session.workspace, session.session_id),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return []

        data = json.loads(row[0])
        history = data.get("history", [])

        for idx, entry in enumerate(history):
            # Each entry can have "user" and/or "assistant" keys
            for role in ["user", "assistant"]:
                if role not in entry:
                    continue

                msg_data = entry[role]
                content = msg_data.get("content")
                text = _extract_text_from_content(content)

                if not text.strip():
                    continue

                timestamp = _parse_timestamp(msg_data.get("timestamp"))
                if not timestamp:
                    timestamp = session.created or datetime.now()

                messages.append(
                    IndexedMessage(
                        uuid=f"{session.session_id}-{idx}-{role}",
                        session_id=session.session_id,
                        workspace=session.workspace,
                        timestamp=timestamp,
                        role=role,
                        searchable_text=text,
                        message_index=len(messages),
                        source=Source.CLI,
                    )
                )

    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.warning(f"Error loading CLI session {session.session_id}: {e}")

    return messages

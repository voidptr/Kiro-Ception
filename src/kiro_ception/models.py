"""Pydantic data models for Kiro Ception."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class Source(str, Enum):
    """Conversation source."""

    CLI = "cli"
    IDE = "ide"
    CLAUDE = "claude"


class ContentTier(str, Enum):
    """Content classification for two-tier search model."""

    CONVERSATION = "conversation"
    TOOL_CONTEXT = "tool_context"


class IndexedMessage(BaseModel):
    """A message indexed for search."""

    uuid: str
    session_id: str
    workspace: str
    timestamp: datetime
    role: str  # "user" or "assistant"
    searchable_text: str
    message_index: int = 0  # Position in session for context retrieval
    source: Source = Source.CLI
    content_tier: ContentTier = ContentTier.CONVERSATION
    tool_name: str | None = None  # Populated only for tool_context messages


class SessionInfo(BaseModel):
    """Metadata about a conversation session."""

    session_id: str
    workspace: str
    message_count: int = 0
    created: datetime | None = None
    modified: datetime | None = None
    source: Source = Source.CLI

    @property
    def timestamp_fallback(self) -> datetime:
        """Get a timestamp for sorting, with fallback to epoch."""
        return self.modified or self.created or datetime.min

"""Load conversations from Kiro CLI storage.

Kiro CLI has stored conversations two ways over its lifetime, and this loader
reads both so no history is lost across the format change:

**JSONL session store (current).** One transcript per session at
``<root>/<session_id>.jsonl`` (default root ``~/.kiro/sessions/cli``), with a
companion ``<session_id>.json`` sidecar. Each transcript line is an envelope
``{"version": ..., "kind": ..., "data": {...}}`` where ``kind`` is one of:

- ``Prompt`` — a user turn. Text lives in ``data.content[]`` blocks of
  ``kind == "text"``; the turn timestamp is ``data.meta.timestamp`` (Unix
  seconds). ``data.meta.additionalContext`` is harness/hook plumbing injected
  into the turn and is deliberately NOT indexed.
- ``AssistantMessage`` — an assistant turn. ``data.content[]`` mixes
  ``text`` blocks (indexed), ``thinking`` blocks (skipped unless
  ``include_thinking``), and ``toolUse`` blocks ``{toolUseId, name, input}``.
- ``ToolResults`` — ``data.content[]`` of ``toolResult`` blocks
  ``{toolUseId, content, status}``, paired to a ``toolUse`` by ``toolUseId``.

The sidecar carries the real workspace (``cwd``) and ``created_at`` /
``updated_at`` (ISO 8601). ``updated_at`` is used as the session mtime so the
incremental indexer re-reads a session as it grows. Missing/partial sidecars
fall back to the transcript header and file mtime; a truncated final line
(a live-appended transcript) is tolerated.

**SQLite (legacy).** A ``data.sqlite3`` with a ``conversations_v2`` table whose
``value`` column is JSON holding a ``history[]`` of ``{user, assistant}``
entries. Read exactly as before, so existing installs keep working.

Sessions from both formats are unioned and de-duplicated by ``session_id``;
when an id appears in both, the JSONL transcript wins (it is the current
format), mirroring ide_loader's "prefer the richest/newest format" rule.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import get_config
from .ide_loader import _replace_code_blocks
from .models import ContentTier, IndexedMessage, SessionInfo, Source
from .tool_summaries import ToolSummaryConfig, generate_tool_summary

logger = logging.getLogger(__name__)


def get_database_path() -> Path | None:
    """Get the legacy CLI SQLite database path (or None)."""
    return get_config().cli.database_path


def _parse_timestamp(ts: int | str | float | None) -> datetime | None:
    """Parse a timestamp into naive LOCAL time.

    Mirrors ide_loader._parse_timestamp. Accepts:
      * int/float — Unix seconds if it looks like seconds, else milliseconds
        (the legacy SQLite path stores milliseconds; the JSONL Prompt meta
        stores seconds), disambiguated by magnitude.
      * str — ISO 8601, offset-aware strings converted to local before the
        tzinfo is dropped so they line up with fromtimestamp's local values.
    """
    if ts is None:
        return None
    if isinstance(ts, bool):  # guard: bool is an int subclass
        return None
    if isinstance(ts, (int, float)):
        # ~1e11 seconds is year 5138; anything larger is milliseconds.
        seconds = ts / 1000 if ts > 1e11 else ts
        try:
            return datetime.fromtimestamp(seconds)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    return None


def _tool_summary_config() -> ToolSummaryConfig:
    cfg = get_config().tool_summaries
    return ToolSummaryConfig(
        excluded_tools=list(cfg.excluded_tools),
        max_summary_length=cfg.max_summary_length,
        include_meaningful_output=cfg.include_meaningful_output,
    )


# ---------------------------------------------------------------------------
# JSONL session store (current format)
# ---------------------------------------------------------------------------

_SIDECAR_SUFFIX = ".json"
_HEADER_SCAN_LINES = 40


def _read_sidecar(sidecar: Path) -> tuple[str, datetime | None, datetime | None]:
    """Read (workspace, created, modified) from a session's .json sidecar.

    Returns ("", None, None) if the sidecar is absent or unreadable.
    """
    try:
        raw = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "", None, None
    if not isinstance(data, dict):
        return "", None, None
    workspace = str(data.get("cwd") or "")
    created = _parse_timestamp(data.get("created_at"))
    modified = _parse_timestamp(data.get("updated_at"))
    return workspace, created, modified


def _read_transcript_header(path: Path) -> tuple[str, datetime | None]:
    """Fallback: scan the head of a transcript for a workspace/timestamp.

    The JSONL records themselves carry no ``cwd``; only ``Prompt`` records
    carry a ``data.meta.timestamp``. Used only when the sidecar is missing.
    """
    first_ts: datetime | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f):
                if line_no >= _HEADER_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("kind") == "Prompt":
                    meta = (record.get("data") or {}).get("meta") or {}
                    first_ts = _parse_timestamp(meta.get("timestamp"))
                    if first_ts is not None:
                        break
    except OSError as e:
        logger.debug(f"Could not read CLI transcript header {path}: {e}")
    return "", first_ts


def _build_jsonl_session(transcript: Path) -> SessionInfo | None:
    """Build a SessionInfo for one JSONL transcript, or None if unusable."""
    try:
        stat = transcript.stat()
    except OSError:
        return None
    if stat.st_size == 0:
        return None

    session_id = transcript.stem
    sidecar = transcript.with_suffix(_SIDECAR_SUFFIX)
    workspace, created, modified = _read_sidecar(sidecar)

    if not workspace or modified is None:
        # Fall back to transcript header + filesystem times.
        header_ws, header_ts = _read_transcript_header(transcript)
        workspace = workspace or header_ws
        created = created or header_ts or datetime.fromtimestamp(stat.st_ctime)
        modified = modified or datetime.fromtimestamp(stat.st_mtime)

    return SessionInfo(
        session_id=session_id,
        workspace=workspace,
        created=created,
        modified=modified,
        source=Source.CLI,
    )


def _extract_prompt_text(content: list) -> str:
    """Join text blocks from a Prompt record's content list."""
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("kind") == "text":
            data = block.get("data")
            if isinstance(data, str) and data.strip():
                parts.append(data)
    return "\n".join(parts)


def _extract_assistant_parts(
    content: list,
) -> tuple[str, list[dict]]:
    """From an AssistantMessage content list, return (text, tool_uses).

    ``thinking`` blocks are dropped unless include_thinking is set; ``toolUse``
    blocks are collected for pairing with later ToolResults.
    """
    include_thinking = get_config().claude.include_thinking
    text_parts: list[str] = []
    tool_uses: list[dict] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        data = block.get("data")
        if kind == "text":
            if isinstance(data, str) and data.strip():
                text_parts.append(data)
        elif kind == "thinking" and include_thinking:
            if isinstance(data, dict):
                t = data.get("text")
                if isinstance(t, str) and t.strip():
                    text_parts.append(t)
        elif kind == "toolUse" and isinstance(data, dict):
            tool_uses.append(data)
    return "\n".join(text_parts), tool_uses

def _load_jsonl_messages(session: SessionInfo, transcript: Path) -> list[IndexedMessage]:
    include_tool_context = get_config().claude.include_tool_context
    ts_config = _tool_summary_config()

    messages: list[IndexedMessage] = []
    # Pending tool_use calls awaiting their results, keyed by toolUseId.
    pending_tools: dict[str, dict] = {}
    last_ts: datetime | None = session.created

    def _append(role: str, text: str, tier: ContentTier, ts: datetime | None,
                tool_name: str | None = None) -> None:
        if not text.strip():
            return
        messages.append(
            IndexedMessage(
                uuid=f"{session.session_id}-{len(messages)}-{role}",
                session_id=session.session_id,
                workspace=session.workspace,
                timestamp=ts or last_ts or session.modified or datetime.now(),
                role=role,
                searchable_text=_replace_code_blocks(text),
                message_index=len(messages),
                source=Source.CLI,
                content_tier=tier,
                tool_name=tool_name,
            )
        )

    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Tolerate a truncated final line on a live transcript.
                    continue
                if not isinstance(record, dict):
                    continue

                kind = record.get("kind")
                data = record.get("data") or {}

                if kind == "Prompt":
                    meta = data.get("meta") or {}
                    ts = _parse_timestamp(meta.get("timestamp"))
                    if ts is not None:
                        last_ts = ts
                    # additionalContext is intentionally dropped (hook plumbing).
                    _append("user", _extract_prompt_text(data.get("content")),
                            ContentTier.CONVERSATION, ts)

                elif kind == "AssistantMessage":
                    text, tool_uses = _extract_assistant_parts(data.get("content"))
                    _append("assistant", text, ContentTier.CONVERSATION, last_ts)
                    for tu in tool_uses:
                        tuid = tu.get("toolUseId")
                        if tuid:
                            pending_tools[tuid] = tu

                elif kind == "ToolResults" and include_tool_context:
                    for block in data.get("content") or []:
                        if not isinstance(block, dict) or block.get("kind") != "toolResult":
                            continue
                        rdata = block.get("data") or {}
                        tuid = rdata.get("toolUseId")
                        call = pending_tools.pop(tuid, {}) if tuid else {}
                        summary = _summarize_tool(call, rdata, ts_config)
                        _append("assistant", summary or "", ContentTier.TOOL_CONTEXT,
                                last_ts, tool_name=call.get("name"))
    except OSError as e:
        logger.warning(f"Error reading CLI transcript {transcript}: {e}")

    return messages


def _summarize_tool(call: dict, result: dict, config: ToolSummaryConfig) -> str | None:
    """Condense a toolUse/toolResult pair into one summary line.

    Reuses tool_summaries.generate_tool_summary by adapting the CLI shapes into
    the ``action`` dict it expects.
    """
    name = call.get("name") or "tool"
    status = result.get("status") or ""
    # Flatten result content blocks to a short outcome string.
    outcome_parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict):
            d = block.get("data")
            if isinstance(d, str):
                outcome_parts.append(d)
            elif d is not None:
                outcome_parts.append(json.dumps(d)[:200])
    action = {
        "actionType": name,
        "toolInput": call.get("input"),
        "status": status,
        "output": " ".join(outcome_parts),
    }
    summary = generate_tool_summary(action, config)
    if summary:
        return summary
    # Fallback if generate_tool_summary declined (e.g. unknown shape).
    tail = (" → " + status) if status else ""
    return f"[{name}]{tail}"


def _list_jsonl_sessions() -> list[SessionInfo]:
    config = get_config().cli
    sessions: list[SessionInfo] = []
    seen: set[str] = set()
    for root in config.get_session_roots():
        try:
            transcripts = sorted(root.glob("*.jsonl"))
        except OSError as e:
            logger.warning(f"Could not list CLI transcripts in {root}: {e}")
            continue
        for transcript in transcripts:
            session = _build_jsonl_session(transcript)
            if session and session.session_id not in seen:
                seen.add(session.session_id)
                _jsonl_paths[session.session_id] = transcript
                sessions.append(session)
    return sessions


# session_id -> transcript path, populated by _list_jsonl_sessions().
_jsonl_paths: dict[str, Path] = {}


# ---------------------------------------------------------------------------
# SQLite conversations_v2 (legacy format) — preserved as-is
# ---------------------------------------------------------------------------


def _extract_text_from_content(content: dict | str | list | None) -> str:
    """Extract searchable text from a legacy SQLite message content field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
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


def _list_sqlite_sessions() -> list[SessionInfo]:
    db_path = get_database_path()
    if not db_path or not db_path.exists():
        return []

    sessions: list[SessionInfo] = []
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


def _load_sqlite_messages(session: SessionInfo) -> list[IndexedMessage]:
    db_path = get_database_path()
    if not db_path or not db_path.exists():
        return []

    messages: list[IndexedMessage] = []
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
            for role in ["user", "assistant"]:
                if role not in entry:
                    continue
                msg_data = entry[role]
                text = _extract_text_from_content(msg_data.get("content"))
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


# ---------------------------------------------------------------------------
# Public API — union of both formats
# ---------------------------------------------------------------------------


def list_cli_sessions() -> list[SessionInfo]:
    """List all CLI sessions from both the JSONL store and legacy SQLite.

    De-duplicated by ``session_id`` with JSONL preferred (current format).
    """
    jsonl_sessions = _list_jsonl_sessions()
    seen = {s.session_id for s in jsonl_sessions}
    sessions = list(jsonl_sessions)
    for s in _list_sqlite_sessions():
        if s.session_id not in seen:
            seen.add(s.session_id)
            sessions.append(s)
    return sessions


def load_cli_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages for a CLI session, routing to the format it came from."""
    transcript = _jsonl_paths.get(session.session_id)
    if transcript is None:
        # Not in the JSONL map — rescan once in case it was added after listing.
        _list_jsonl_sessions()
        transcript = _jsonl_paths.get(session.session_id)
    if transcript is not None and transcript.exists():
        return _load_jsonl_messages(session, transcript)
    return _load_sqlite_messages(session)

"""Load conversations from GitHub Copilot Chat (VS Code) storage.

VS Code's Copilot Chat persists one file per chat session under each
workspace's storage directory:

    <user>/globalStorage/../workspaceStorage/<hash>/chatSessions/<uuid>.json
    <user>/globalStorage/../workspaceStorage/<hash>/chatSessions/<uuid>.jsonl

Two on-disk shapes exist, both materializing to the same logical session:

- **``.json`` (older):** a single JSON object with a top-level ``requests``
  array. Each request carries the user ``message`` and the assistant
  ``response``.
- **``.jsonl`` (current, the majority):** an event/patch log. Line 0 is
  ``{"kind":0,"v":{...full session snapshot...}}``; each subsequent line is
  ``{"kind":1,"k":[<path>],"v":<value>}`` — a keyed patch applied to the base
  snapshot (``k`` is a list of dict keys / list indices navigating into the
  object). Replaying the base plus the patches reconstructs the same object
  the ``.json`` form stores directly.

Workspace attribution comes from the sibling ``workspace.json`` in each
``workspaceStorage/<hash>/`` directory, which records the real folder as a
``file://`` URI under either a ``folder`` or ``workspace`` key. This is decoded
back to a filesystem path so ``search_project_history`` scoping works, exactly
as the other sources resolve their workspace.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import CopilotSourceConfig, get_config
from .ide_loader import _replace_code_blocks
from .models import ContentTier, IndexedMessage, SessionInfo, Source

logger = logging.getLogger(__name__)

# Response parts whose text is UI/plumbing rather than assistant prose.
_SKIP_RESPONSE_KINDS = frozenset({
    "mcpServersStarting",
    "prepareToolInvocation",
    "toolInvocationSerialized",
    "codeblockUri",
    "progressTask",
    "progressTaskSerialized",
    "undoStop",
    "textEditGroup",
    "notebookEditGroup",
})

# Maps session_id -> (transcript path, workspace), populated by listing.
_session_paths: dict[str, tuple[Path, str]] = {}


def _get_copilot_config() -> CopilotSourceConfig:
    return get_config().copilot


def get_copilot_roots() -> list[Path]:
    """Return the configured Copilot workspaceStorage roots that exist."""
    return _get_copilot_config().get_roots()


def _uri_to_path(uri: str) -> str:
    """Decode a VS Code ``file://`` URI to a filesystem path.

    ``file:///c%3A/Source/proj`` -> ``C:\\Source\\proj`` on Windows,
    ``file:///home/me/proj`` -> ``/home/me/proj`` elsewhere. Non-file URIs and
    unparseable values are returned unchanged so attribution degrades to the
    raw string rather than being lost.
    """
    if not uri:
        return ""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return uri
    if parsed.scheme and parsed.scheme != "file":
        return uri
    path = unquote(parsed.path)
    # A Windows drive path arrives as "/c:/..." — strip the leading slash,
    # uppercase the drive letter, and normalize separators. A POSIX path
    # ("/home/...") is left as-is.
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
        path = path[0].upper() + path[1:]
        return path.replace("/", "\\")
    return path


def _read_workspace_folder(workspace_dir: Path) -> str:
    """Resolve the real workspace path for a workspaceStorage/<hash>/ dir.

    Reads the sibling ``workspace.json``; prefers the ``folder`` key (a
    single-folder workspace) and falls back to ``workspace`` (a multi-root
    ``.code-workspace`` file). Returns "" when neither is present.
    """
    ws_file = workspace_dir / "workspace.json"
    try:
        data = json.loads(ws_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    uri = data.get("folder") or data.get("workspace") or ""
    return _uri_to_path(str(uri))


def _apply_patch(base: dict, keys: list, value: object) -> None:
    """Apply one ``{"k":[...],"v":...}`` patch into the base snapshot.

    ``keys`` navigates dict keys (str) and list indices (int). Missing dict
    keys and list slots are created on the way down; a list is grown with
    ``None`` padding when an index runs past its end. Malformed paths are
    skipped rather than raising, so one bad patch never loses the session.
    """
    if not keys:
        return
    cursor = base
    try:
        for i, key in enumerate(keys[:-1]):
            nxt = keys[i + 1]
            child_is_list = isinstance(nxt, int)
            if isinstance(key, int):
                if not isinstance(cursor, list):
                    return
                while len(cursor) <= key:
                    cursor.append(None)
                if cursor[key] is None or not isinstance(cursor[key], (dict, list)):
                    cursor[key] = [] if child_is_list else {}
                cursor = cursor[key]
            else:
                if not isinstance(cursor, dict):
                    return
                if key not in cursor or not isinstance(cursor[key], (dict, list)):
                    cursor[key] = [] if child_is_list else {}
                cursor = cursor[key]

        last = keys[-1]
        if isinstance(last, int):
            if not isinstance(cursor, list):
                return
            while len(cursor) <= last:
                cursor.append(None)
            cursor[last] = value
        else:
            if not isinstance(cursor, dict):
                return
            cursor[last] = value
    except (TypeError, KeyError, IndexError):
        return


def _materialize_jsonl(path: Path) -> dict | None:
    """Replay a ``.jsonl`` event log into the full session object.

    Line 0 (``kind`` 0) carries the base snapshot; subsequent ``kind`` 1 lines
    are keyed patches applied in order. Returns None if no base snapshot is
    found.
    """
    base: dict | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("kind")
                if kind == 0:
                    v = record.get("v")
                    if isinstance(v, dict):
                        base = v
                elif kind == 1 and base is not None:
                    keys = record.get("k")
                    if isinstance(keys, list):
                        _apply_patch(base, keys, record.get("v"))
    except OSError as e:
        logger.warning(f"Could not read Copilot jsonl {path}: {e}")
        return None
    return base


def _load_session_object(path: Path) -> dict | None:
    """Load a chat session file (.json direct, .jsonl via patch replay)."""
    if path.suffix == ".jsonl":
        return _materialize_jsonl(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Copilot json {path}: {e}")
        return None
    return data if isinstance(data, dict) else None


def _epoch_ms_to_dt(value: object) -> datetime | None:
    """Convert an epoch-milliseconds number to a local datetime."""
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def list_copilot_sessions() -> list[SessionInfo]:
    """List all Copilot Chat sessions across configured workspaceStorage roots.

    Every configured root that exists is scanned (like the Claude source). For
    each ``workspaceStorage/<hash>/`` directory, the workspace is resolved once
    from ``workspace.json`` and applied to every session file under its
    ``chatSessions/`` folder.
    """
    config = _get_copilot_config()
    sessions: list[SessionInfo] = []
    paths: dict[str, tuple[Path, str]] = {}

    for root in config.get_roots():
        try:
            workspace_dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError as e:
            logger.warning(f"Could not list Copilot storage root {root}: {e}")
            continue

        for workspace_dir in workspace_dirs:
            chat_dir = workspace_dir / "chatSessions"
            if not chat_dir.is_dir():
                continue
            workspace = _read_workspace_folder(workspace_dir)

            try:
                session_files = sorted(
                    list(chat_dir.glob("*.json")) + list(chat_dir.glob("*.jsonl"))
                )
            except OSError as e:
                logger.debug(f"Could not list Copilot sessions in {chat_dir}: {e}")
                continue

            for session_file in session_files:
                try:
                    stat = session_file.stat()
                except OSError:
                    continue
                if stat.st_size == 0:
                    continue
                session_id = f"copilot-{session_file.stem}"
                if session_id in paths:
                    continue
                paths[session_id] = (session_file, workspace)
                sessions.append(
                    SessionInfo(
                        session_id=session_id,
                        workspace=workspace,
                        created=datetime.fromtimestamp(stat.st_ctime),
                        modified=datetime.fromtimestamp(stat.st_mtime),
                        source=Source.COPILOT,
                    )
                )

    _session_paths.update(paths)
    return sessions


def _find_session_file(session_id: str) -> tuple[Path, str] | None:
    """Resolve a session_id to (path, workspace), rescanning if stale."""
    entry = _session_paths.get(session_id)
    if entry is not None and entry[0].exists():
        return entry
    list_copilot_sessions()
    entry = _session_paths.get(session_id)
    if entry is not None and entry[0].exists():
        return entry
    return None


def _extract_user_text(message: object) -> str:
    """Pull the user prompt text out of a request's ``message`` field."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text
        # Fall back to concatenating text parts.
        parts = message.get("parts")
        if isinstance(parts, list):
            out = []
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif isinstance(part, str):
                    out.append(part)
            return "\n".join(out)
    return ""


def _extract_response_text(response: object) -> str:
    """Flatten an assistant ``response`` array into prose text.

    The response is a list of typed parts. Plain text arrives either as a bare
    string or as ``{"value": "..."}`` / ``{"text": "..."}``; parts that are
    pure UI/tool plumbing (``kind`` in the skip set) are dropped.
    """
    if isinstance(response, str):
        return response
    if not isinstance(response, list):
        return ""
    out: list[str] = []
    for part in response:
        if isinstance(part, str):
            out.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("kind") in _SKIP_RESPONSE_KINDS:
            continue
        value = part.get("value")
        if isinstance(value, str) and value.strip():
            out.append(value)
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
    return "\n".join(o for o in out if o.strip())


def _make_message(
    session: SessionInfo,
    uuid: str,
    timestamp: datetime,
    role: str,
    text: str,
    index: int,
    tier: ContentTier = ContentTier.CONVERSATION,
) -> IndexedMessage:
    return IndexedMessage(
        uuid=uuid,
        session_id=session.session_id,
        workspace=session.workspace,
        timestamp=timestamp,
        role=role,
        searchable_text=text,
        message_index=index,
        source=Source.COPILOT,
        content_tier=tier,
    )


def load_copilot_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load indexable messages from one Copilot Chat session.

    Materializes the session object (direct for ``.json``, patch-replay for
    ``.jsonl``), then walks its ``requests`` array emitting the user prompt and
    the assistant response for each turn. Code blocks are condensed to
    ``[code:lang]`` placeholders, matching the other sources.
    """
    entry = _find_session_file(session.session_id)
    if not entry:
        return []
    path, _workspace = entry

    obj = _load_session_object(path)
    if not isinstance(obj, dict):
        return []

    requests = obj.get("requests")
    if not isinstance(requests, list):
        return []

    session_created = _epoch_ms_to_dt(obj.get("creationDate"))
    default_ts = session_created or session.modified or datetime.now()

    messages: list[IndexedMessage] = []
    msg_idx = 0

    for req_no, req in enumerate(requests):
        if not isinstance(req, dict):
            continue

        timestamp = _epoch_ms_to_dt(req.get("timestamp")) or default_ts
        req_id = str(req.get("requestId") or f"{session.session_id}-{req_no}")

        user_text = _extract_user_text(req.get("message")).strip()
        if user_text:
            messages.append(
                _make_message(
                    session,
                    f"{req_id}-user",
                    timestamp,
                    "user",
                    _replace_code_blocks(user_text),
                    msg_idx,
                )
            )
            msg_idx += 1

        response_text = _extract_response_text(req.get("response")).strip()
        if response_text:
            messages.append(
                _make_message(
                    session,
                    f"{req_id}-assistant",
                    timestamp,
                    "assistant",
                    _replace_code_blocks(response_text),
                    msg_idx,
                )
            )
            msg_idx += 1

    return messages

"""Load conversations from Kiro IDE storage.

Supports three formats:
- Legacy: .chat files in globalStorage/kiro.kiroagent/*/*.chat
- Workspace-sessions: workspace-sessions/<base64_workspace>/<uuid>.json
- Kiro 1.0: ~/.kiro/sessions/<sha256_prefix>/<session_id>/{session.json, messages.jsonl}
"""

import base64
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import Config, get_config
from .models import ContentTier, IndexedMessage, SessionInfo, Source
from .tool_summaries import ToolSummaryConfig, generate_tool_summary

logger = logging.getLogger(__name__)


def _decode_workspace_dir_name(dirname: str) -> str:
    """Decode a base64-encoded workspace directory name to a filesystem path.

    Kiro IDE encodes workspace paths as URL-safe base64 for use as directory names.
    The encoding uses '_' as both the URL-safe substitute for '/' AND as padding
    (replacing the standard '=' padding character). We strip trailing '_' before
    adding proper '=' padding to decode correctly.

    Falls back to the raw directory name if decoding fails entirely.
    """
    try:
        # Strip trailing '_' which Kiro uses as padding (replacing '=')
        stripped = dirname.rstrip('_')
        # URL-safe base64: - means +, _ means / (but we already stripped trailing _)
        fixed = stripped.replace('-', '+').replace('_', '/')
        # Add proper padding
        padding = 4 - len(fixed) % 4
        if padding != 4:
            fixed += "=" * padding
        return base64.b64decode(fixed).decode("utf-8")
    except Exception:
        return dirname


# Prefixes for messages that are system/boilerplate content (no search value)
_SKIP_PREFIXES = (
    "# System Prompt",
    "Follow these instructions for user requests related to spec tasks",
    "You are operating in a workspace",
    "<system>",
    "<instructions>",
    "<identity>",
    "## Included Rules",
    "<steering-reminder>",
    "CONTEXT TRANSFER:",
)

# Regex to match fenced code blocks: ```lang\n...\n```
_CODE_BLOCK_RE = re.compile(
    r"```(\w*)\n.*?```",
    re.DOTALL,
)


def _replace_code_blocks(text: str) -> str:
    """Replace fenced code blocks with a compact placeholder.

    ```python
    def foo():
        pass
    ```

    becomes: [code:python]

    Preserves the language tag for searchability.
    The placeholder format is simple and parseable for future use.
    """
    def _replacer(match: re.Match) -> str:
        lang = match.group(1).strip().lower()
        if lang:
            return f"[code:{lang}]"
        return "[code]"

    return _CODE_BLOCK_RE.sub(_replacer, text)


def _parse_timestamp(ts: int | str | None) -> datetime | None:
    """Parse a timestamp into naive LOCAL time.

    Every consumer (recency boost, date filters, stored epochs) treats these
    datetimes as local, which is what `datetime.fromtimestamp` already
    produces for epoch values. ISO 8601 strings carrying an offset — Kiro 1.0
    and Claude Code both emit "...Z" — must therefore be converted to local
    *before* the tzinfo is dropped. Dropping it first would store the UTC
    wall-clock reading as though it were local, shifting every such message
    by the machine's UTC offset.

    Strings without an offset are already local and are returned unchanged.
    """
    if ts is None:
        return None
    if isinstance(ts, int):
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


def _extract_text_from_content(content: str | list | dict | None) -> str:
    """Extract searchable text from message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content:
            return content["text"]
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


def _should_skip_message(role: str, text: str) -> bool:
    """Check if a message should be filtered out (system prompts, boilerplate)."""
    if role in ("user", "human") and any(text.startswith(p) for p in _SKIP_PREFIXES):
        return True
    return False


# --- Legacy .chat format ---


def get_chat_files() -> list[Path]:
    """Get all IDE .chat files (legacy format)."""
    return get_config().ide.get_chat_files()


def _list_legacy_sessions() -> list[SessionInfo]:
    """List all IDE sessions from legacy .chat files."""
    sessions = []
    for chat_file in get_chat_files():
        try:
            stat = chat_file.stat()
            session_id = chat_file.stem
            workspace = _decode_workspace_dir_name(chat_file.parent.name)

            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    workspace=workspace,
                    created=datetime.fromtimestamp(stat.st_ctime),
                    modified=datetime.fromtimestamp(stat.st_mtime),
                    source=Source.IDE,
                )
            )
        except OSError as e:
            logger.debug(f"Could not stat {chat_file}: {e}")

    return sessions


def _load_legacy_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages from a legacy .chat file."""
    chat_files = get_chat_files()
    chat_file = next((f for f in chat_files if f.stem == session.session_id), None)

    if not chat_file or not chat_file.exists():
        return []

    messages = []
    try:
        with open(chat_file, encoding="utf-8") as f:
            data = json.load(f)

        msg_list = data.get("chat", [])

        # Fallback patterns
        if not msg_list:
            msg_list = data.get("messages", data.get("history", []))
        if not msg_list and "conversation" in data:
            msg_list = data["conversation"].get("messages", [])
        if not msg_list and isinstance(data, list):
            msg_list = data

        for idx, msg in enumerate(msg_list):
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", msg.get("type", ""))
            if role not in ("user", "assistant", "human", "ai"):
                continue

            role = "user" if role in ("user", "human") else "assistant"
            content = msg.get("content", msg.get("text", msg.get("message", "")))
            text = _extract_text_from_content(content)

            if _should_skip_message(role, text):
                continue

            if not text.strip():
                continue

            # Replace fenced code blocks with compact placeholders
            text = _replace_code_blocks(text)

            timestamp = _parse_timestamp(msg.get("timestamp", msg.get("created_at")))
            if not timestamp:
                timestamp = session.modified or datetime.now()

            messages.append(
                IndexedMessage(
                    uuid=msg.get("id", msg.get("uuid", f"{session.session_id}-{idx}")),
                    session_id=session.session_id,
                    workspace=session.workspace,
                    timestamp=timestamp,
                    role=role,
                    searchable_text=text,
                    message_index=idx,
                    source=Source.IDE,
                )
            )

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Error loading legacy IDE session {session.session_id}: {e}")

    return messages


# --- Current workspace-sessions format ---


def _get_workspace_sessions_dirs() -> list[Path]:
    """Get workspace-sessions base directories across platforms."""
    config = get_config()
    # Derive workspace-sessions path from existing IDE patterns
    # The patterns point to globalStorage/kiro.kiroagent/*/*.chat
    # workspace-sessions is at globalStorage/kiro.kiroagent/workspace-sessions/
    candidates = [
        "~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions",
        "~/.config/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions",
        "~/AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions",
    ]
    dirs = []
    for candidate in candidates:
        p = Path(candidate).expanduser()
        if p.is_dir():
            dirs.append(p)
    return dirs


def _list_workspace_sessions() -> list[SessionInfo]:
    """List all sessions from workspace-sessions directories.

    Uses stat() only for timestamps — skips expensive sessions.json parsing
    to minimize memory allocations during periodic rescans.
    """
    sessions = []

    for ws_dir in _get_workspace_sessions_dirs():
        # Each subdirectory is a base64-encoded workspace path
        for workspace_dir in ws_dir.iterdir():
            if not workspace_dir.is_dir():
                continue

            # Decode workspace path from directory name
            workspace_path = _decode_workspace_dir_name(workspace_dir.name)

            # Find session JSON files (UUIDs, not sessions.json)
            for session_file in workspace_dir.glob("*.json"):
                if session_file.name == "sessions.json":
                    continue

                try:
                    stat = session_file.stat()
                    session_id = session_file.stem

                    sessions.append(
                        SessionInfo(
                            session_id=session_id,
                            workspace=workspace_path,
                            created=datetime.fromtimestamp(stat.st_ctime),
                            modified=datetime.fromtimestamp(stat.st_mtime),
                            source=Source.IDE,
                        )
                    )
                except OSError as e:
                    logger.debug(f"Could not stat {session_file}: {e}")

    return sessions


# --- Kiro 1.0 ~/.kiro/sessions format ---


def _get_kiro_sessions_dirs() -> list[Path]:
    """Get Kiro 1.0 session directories across platforms.

    Kiro 1.0 stores sessions at ~/.kiro/sessions/<sha256_prefix>/<session_id>/
    where each session directory contains session.json (metadata) and
    messages.jsonl (conversation content).
    """
    candidates = [
        "~/.kiro/sessions",
    ]
    dirs = []
    for candidate in candidates:
        p = Path(candidate).expanduser()
        if p.is_dir():
            dirs.append(p)
    return dirs


def _workspace_path_to_sha256_prefix(workspace_path: str) -> str:
    """Convert a workspace path to the SHA256 prefix used as directory name.

    Kiro 1.0 uses the first 16 hex characters of SHA256(workspace_path)
    as the directory name under ~/.kiro/sessions/.
    """
    return hashlib.sha256(workspace_path.encode()).hexdigest()[:16]


def _list_kiro_sessions() -> list[SessionInfo]:
    """List all sessions from Kiro 1.0 ~/.kiro/sessions/ directories.

    Each session has a session.json with metadata including workspace paths,
    timestamps, title, and mode. Uses stat() for mtime where possible,
    falls back to parsing session.json for timestamps.
    """
    sessions = []

    for sessions_base in _get_kiro_sessions_dirs():
        for workspace_dir in sessions_base.iterdir():
            if not workspace_dir.is_dir():
                continue

            for session_dir in workspace_dir.iterdir():
                if not session_dir.is_dir():
                    continue

                messages_file = session_dir / "messages.jsonl"
                session_file = session_dir / "session.json"

                if not messages_file.exists():
                    continue

                try:
                    # Use stat for mtime (efficient for rescan)
                    msg_stat = messages_file.stat()

                    # Skip empty message files
                    if msg_stat.st_size == 0:
                        continue

                    session_id = session_dir.name
                    workspace = ""
                    created = None

                    # Parse session.json for metadata
                    if session_file.exists():
                        try:
                            meta = json.loads(session_file.read_text(encoding="utf-8"))
                            workspace_paths = meta.get("workspacePaths", [])
                            if workspace_paths:
                                workspace = workspace_paths[0]
                            created = _parse_timestamp(meta.get("createdAt"))
                        except (json.JSONDecodeError, OSError):
                            pass

                    sessions.append(
                        SessionInfo(
                            session_id=session_id,
                            workspace=workspace,
                            created=created or datetime.fromtimestamp(msg_stat.st_ctime),
                            modified=datetime.fromtimestamp(msg_stat.st_mtime),
                            source=Source.IDE,
                        )
                    )
                except OSError as e:
                    logger.debug(f"Could not process session dir {session_dir}: {e}")

    return sessions


def _load_kiro_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages from a Kiro 1.0 session (messages.jsonl format).

    Extracts:
    - user messages (payload.type == "user")
    - assistant messages (payload.type == "assistant")
    - tool context summaries from tool_call/tool_result pairs

    Skips: session_start, session_metadata, session_event, turn_start,
    turn_end, usage_summary, pending_interaction, interaction_resolved,
    tombstone, steering_inclusion, sub_agent_start, sub_agent_complete.
    """
    # Find the messages file
    messages_file = _find_kiro_session_messages_file(session.session_id)
    if not messages_file:
        return []

    messages = []
    pending_tool_calls: dict[str, dict] = {}  # toolCallId -> tool_call payload
    msg_idx = 0

    config = ToolSummaryConfig(
        excluded_tools=[],
        max_summary_length=800,
        include_meaningful_output=True,
    )

    try:
        with open(messages_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = msg.get("payload", {})
                msg_type = payload.get("type")
                timestamp_str = msg.get("timestamp")
                timestamp = _parse_timestamp(timestamp_str) or session.modified or datetime.now()
                msg_id = msg.get("id", f"{session.session_id}-{msg_idx}")

                if msg_type == "user":
                    content = payload.get("content", "")
                    if not content or not content.strip():
                        continue
                    # Apply code block replacement
                    text = _replace_code_blocks(content)
                    # Apply skip check (shouldn't match in new format, but keep for safety)
                    if _should_skip_message("user", text):
                        continue
                    messages.append(
                        IndexedMessage(
                            uuid=msg_id,
                            session_id=session.session_id,
                            workspace=session.workspace,
                            timestamp=timestamp,
                            role="user",
                            searchable_text=text,
                            message_index=msg_idx,
                            source=Source.IDE,
                        )
                    )
                    msg_idx += 1

                elif msg_type == "assistant":
                    content = payload.get("content", "")
                    if not content or not content.strip():
                        continue
                    # Apply code block replacement
                    text = _replace_code_blocks(content)
                    messages.append(
                        IndexedMessage(
                            uuid=msg_id,
                            session_id=session.session_id,
                            workspace=session.workspace,
                            timestamp=timestamp,
                            role="assistant",
                            searchable_text=text,
                            message_index=msg_idx,
                            source=Source.IDE,
                        )
                    )
                    msg_idx += 1

                elif msg_type == "tool_call":
                    # Buffer tool calls to pair with results
                    tool_call_id = payload.get("toolCallId", "")
                    if tool_call_id:
                        pending_tool_calls[tool_call_id] = payload

                elif msg_type == "tool_result":
                    # Match with pending tool call to generate summary
                    tool_call_id = payload.get("toolCallId", "")
                    tool_call = pending_tool_calls.pop(tool_call_id, None)
                    if tool_call:
                        summary = _generate_kiro_tool_summary(
                            tool_call, payload, config
                        )
                        if summary:
                            messages.append(
                                IndexedMessage(
                                    uuid=f"{msg_id}-tool-ctx",
                                    session_id=session.session_id,
                                    workspace=session.workspace,
                                    timestamp=timestamp,
                                    role="assistant",
                                    searchable_text=summary,
                                    message_index=msg_idx,
                                    source=Source.IDE,
                                    content_tier=ContentTier.TOOL_CONTEXT,
                                    tool_name=tool_call.get("toolName"),
                                )
                            )
                            msg_idx += 1

                # All other types (session_start, session_metadata, turn_start,
                # turn_end, usage_summary, session_event, pending_interaction,
                # interaction_resolved, tombstone, steering_inclusion,
                # sub_agent_start, sub_agent_complete) are skipped.

    except OSError as e:
        logger.warning(f"Error loading Kiro session {session.session_id}: {e}")

    return messages


def _find_kiro_session_messages_file(session_id: str) -> Path | None:
    """Find the messages.jsonl file for a Kiro 1.0 session by session_id.

    Searches all workspace directories under ~/.kiro/sessions/ for the
    given session_id directory.
    """
    for sessions_base in _get_kiro_sessions_dirs():
        for workspace_dir in sessions_base.iterdir():
            if not workspace_dir.is_dir():
                continue
            candidate = workspace_dir / session_id / "messages.jsonl"
            if candidate.exists():
                return candidate
    return None


def _generate_kiro_tool_summary(
    tool_call: dict, tool_result: dict, config: ToolSummaryConfig
) -> str | None:
    """Generate a tool context summary from a Kiro 1.0 tool_call/tool_result pair.

    Adapts the inline JSONL tool format to work with the existing summary
    generation pattern.
    """
    tool_name = tool_call.get("toolName", "")
    action_type = tool_call.get("actionType", tool_name)

    # Skip excluded tools
    if action_type in config.excluded_tools or tool_name in config.excluded_tools:
        return None

    # Build description from tool args
    args = tool_call.get("args", {})
    # Remove internal _meta from args
    args = {k: v for k, v in args.items() if k != "_meta"}

    description = _describe_kiro_tool_call(tool_name, args)

    # Build outcome from result
    success = tool_result.get("success", True)
    outcome = "completed" if success else "failed"

    summary = f"[{action_type}] {description} → {outcome}"

    # Extract meaningful output from result content
    if config.include_meaningful_output:
        result_content = tool_result.get("content", "")
        meaningful = _extract_meaningful_from_result(result_content)
        if meaningful:
            summary = f"{summary}\n{meaningful}"
        else:
            summary = f"{summary}\n(no meaningful output)"

    # Enforce max length
    if len(summary) > config.max_summary_length:
        summary = summary[: config.max_summary_length - 3] + "..."

    return summary


def _describe_kiro_tool_call(tool_name: str, args: dict) -> str:
    """Generate a compact description for a Kiro 1.0 tool call."""
    try:
        if tool_name in ("readFile", "readFiles", "read_file", "read_files"):
            path = args.get("path", "")
            paths = args.get("paths", [])
            if path:
                return path
            if paths:
                return ", ".join(paths[:3]) + ("..." if len(paths) > 3 else "")
            # Try files array format
            files = args.get("files", [])
            if files:
                file_paths = [f.get("path", "") for f in files if isinstance(f, dict)]
                return ", ".join(file_paths[:3]) + ("..." if len(file_paths) > 3 else "")
            return "(unknown path)"

        if tool_name in ("writeFile", "fs_write", "str_replace", "fs_append"):
            path = args.get("path", args.get("targetFile", ""))
            return path or "(unknown path)"

        if tool_name in ("grep_search", "file_search"):
            query = args.get("query", "")
            return f'query="{query}"' if query else "(no query)"

        if tool_name in ("execute_bash", "runCommand", "execute_pwsh"):
            command = args.get("command", "")
            if len(command) > 100:
                command = command[:97] + "..."
            return command

        if tool_name == "invoke_sub_agent":
            agent_name = args.get("name", "unknown")
            prompt = args.get("prompt", "")
            if len(prompt) > 100:
                prompt = prompt[:97] + "..."
            return f'{agent_name}: "{prompt}"'

        if tool_name == "update_session_information":
            title = args.get("title", "")
            desc = args.get("description", "")
            return f'title="{title}" desc="{desc[:80]}"' if title or desc else "(update)"

        # Generic: JSON-serialize the args (truncated)
        serialized = json.dumps(args, ensure_ascii=False)
        if len(serialized) > 100:
            serialized = serialized[:97] + "..."
        return serialized

    except (TypeError, ValueError):
        return ""


def _extract_meaningful_from_result(content: str) -> str:
    """Extract meaningful output from a Kiro tool result content string.

    Tool results in JSONL format have content as a string (often JSON-encoded).
    We extract a useful excerpt if it contains error/diagnostic info.
    """
    if not content or not isinstance(content, str):
        return ""

    # Content is often double-JSON-encoded (a JSON string containing JSON)
    # Try to extract the actual text
    text = content
    if text.startswith('{"response":'):
        try:
            parsed = json.loads(text)
            text = parsed.get("response", text)
            # May be double-encoded
            if isinstance(text, str) and text.startswith("{"):
                try:
                    text = json.loads(text)
                    text = json.dumps(text, indent=2)
                except:
                    pass
        except:
            pass

    # Check if content looks meaningful (has error/diagnostic keywords)
    meaningful_indicators = [
        "error", "Error", "ERROR", "fail", "FAIL", "warning", "Warning",
        "WARN", "traceback", "Traceback", "exception", "Exception",
        "PASSED", "FAILED", "passed", "failed", "assert",
    ]

    if any(indicator in text for indicator in meaningful_indicators):
        if len(text) > 500:
            text = text[:497] + "..."
        return text

    return ""


def _get_execution_log_dirs() -> list[Path]:
    """Get all directories containing execution logs across workspaces.

    Execution logs are stored at:
    globalStorage/kiro.kiroagent/<workspace_hash>/414d1636299d2b9e4ce7e17fb11f63e9/
    """
    base_dirs = [
        Path("~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent").expanduser(),
        Path("~/.config/Kiro/User/globalStorage/kiro.kiroagent").expanduser(),
        Path("~/AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent").expanduser(),
    ]
    exec_subdir = "414d1636299d2b9e4ce7e17fb11f63e9"
    dirs = []

    for base in base_dirs:
        if not base.is_dir():
            continue
        for workspace_hash_dir in base.iterdir():
            if not workspace_hash_dir.is_dir():
                continue
            if workspace_hash_dir.name in ("workspace-sessions", "dev_data"):
                continue
            exec_dir = workspace_hash_dir / exec_subdir
            if exec_dir.is_dir():
                dirs.append(exec_dir)

    return dirs


def _get_exec_cache():
    """Get the EmbeddingCache instance for execution index lookups.

    Tries to use the background indexer's cache, or creates a standalone one.
    """
    try:
        from .background_indexer import get_background_indexer
        indexer = get_background_indexer()
        if indexer.cache is not None:
            return indexer.cache
    except Exception:
        pass

    # Fallback: create cache directly
    from .embeddings import get_embedding_backend
    backend = get_embedding_backend()
    from .cache import EmbeddingCache
    return EmbeddingCache(backend.fingerprint())


# In-memory cache for the execution index (session_id -> [file_paths])
_execution_index: dict[str, list[str]] | None = None
_execution_index_built = False


def _build_execution_index() -> dict[str, list[str]]:
    """Build/update the execution index using SQLite cache for persistence.

    On first call: loads cached mappings from sqlite, then only parses
    new/changed files (by mtime). Subsequent calls use the in-memory cache.
    """
    global _execution_index, _execution_index_built

    if _execution_index_built and _execution_index is not None:
        return _execution_index

    cache = _get_exec_cache()

    # Load existing cached mappings
    stored_mtimes = cache.get_all_exec_file_mtimes()

    # Discover all execution log files on disk
    all_files: list[tuple[Path, float]] = []
    for exec_dir in _get_execution_log_dirs():
        try:
            for exec_file in exec_dir.iterdir():
                if not exec_file.is_file():
                    continue
                try:
                    mtime = exec_file.stat().st_mtime
                    all_files.append((exec_file, mtime))
                except OSError:
                    continue
        except OSError:
            continue

    # Determine which files need (re-)parsing
    new_entries: list[tuple[str, str, float]] = []  # (path, session_id, mtime)
    files_to_parse: list[tuple[Path, float]] = []

    for exec_file, mtime in all_files:
        file_str = str(exec_file)
        stored_mtime = stored_mtimes.get(file_str)
        if stored_mtime is not None and mtime == stored_mtime:
            continue  # Unchanged, already in cache
        files_to_parse.append((exec_file, mtime))

    # Parse only new/changed files
    if files_to_parse:
        logger.info(f"Execution index: parsing {len(files_to_parse)} new/changed files (of {len(all_files)} total)")
        for exec_file, mtime in files_to_parse:
            try:
                with open(exec_file, encoding="utf-8") as f:
                    data = json.load(f)

                session_id = data.get("chatSessionId", "")
                if not session_id:
                    for action in data.get("actions", []):
                        session_id = action.get("chatSessionId", "")
                        if session_id:
                            break

                if session_id:
                    new_entries.append((str(exec_file), session_id, mtime))
            except (json.JSONDecodeError, OSError):
                continue

        # Store new entries in sqlite
        if new_entries:
            cache.put_exec_files_batch([(path, sid, mt) for path, sid, mt in new_entries])

    # Build in-memory index from sqlite
    cursor = cache.conn.execute("SELECT exec_file_path, session_id FROM execution_index")
    index: dict[str, list[str]] = {}
    for file_path, session_id in cursor:
        if session_id not in index:
            index[session_id] = []
        index[session_id].append(file_path)

    _execution_index = index
    _execution_index_built = True

    total_cached = cache.get_exec_index_count()
    if files_to_parse:
        logger.info(f"Execution index ready: {total_cached} files, {len(index)} sessions")

    return index


def _extract_user_prompt_from_execution_log(
    exec_file: Path,
    session_id: str,
    workspace: str,
    fallback_timestamp: datetime,
) -> IndexedMessage | None:
    """Extract the user prompt from an execution log file.

    Extracts `input.data.userPrompt` as the authoritative user message for
    that execution. Applies _should_skip_message filter and code-block
    replacement to the extracted prompt.

    Args:
        exec_file: Path to the execution log JSON file.
        session_id: The session this execution belongs to.
        workspace: The workspace path/identifier.
        fallback_timestamp: Timestamp to use if none found in the log.

    Returns:
        An IndexedMessage with role="user" and content_tier=CONVERSATION,
        or None if:
        - The file cannot be parsed as JSON (logs warning, skips)
        - input.data.userPrompt is absent, empty, or null (fallback case)
        - The extracted prompt is filtered by _should_skip_message
        - The extracted prompt is empty after processing
    """
    try:
        with open(exec_file, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"Malformed JSON in execution log {exec_file}: {e}")
        return None
    except OSError as e:
        logger.warning(f"Cannot read execution log {exec_file}: {e}")
        return None

    # Navigate to input.data.userPrompt gracefully
    input_data = data.get("input")
    if not isinstance(input_data, dict):
        return None

    data_field = input_data.get("data")
    if not isinstance(data_field, dict):
        return None

    user_prompt = data_field.get("userPrompt")

    # Fall back when userPrompt is absent, null, or empty string
    if not user_prompt or not isinstance(user_prompt, str) or not user_prompt.strip():
        return None

    # Apply the existing skip filter
    if _should_skip_message("user", user_prompt):
        return None

    # Apply code-block replacement
    text = _replace_code_blocks(user_prompt)

    # Check if text is empty after processing
    if not text.strip():
        return None

    # Try to extract a timestamp from the execution log
    start_time = data.get("startTime")
    msg_ts = _parse_timestamp(start_time) or fallback_timestamp

    # Generate a stable UUID for this user prompt message
    prompt_uuid = f"{session_id}-prompt-{exec_file.stem}"

    return IndexedMessage(
        uuid=prompt_uuid,
        session_id=session_id,
        workspace=workspace,
        timestamp=msg_ts,
        role="user",
        searchable_text=text,
        message_index=0,  # Will be assigned properly in task 4.4
        source=Source.IDE,
        content_tier=ContentTier.CONVERSATION,
    )


def _generate_tool_context_messages(
    actions: list[dict],
    config: Config,
    session_id: str,
    workspace: str,
    timestamp: datetime,
    msg_index_start: int = 20000,
) -> list[IndexedMessage]:
    """Generate tool context IndexedMessages from execution log actions.

    For each non-say action that has both input and output fields, generates
    a compact summary using generate_tool_summary(). Skips excluded tools
    and say actions (handled internally by generate_tool_summary returning None).

    Args:
        actions: List of action dicts from an execution log.
        config: Application Config (provides tool_summaries settings).
        session_id: Session ID for the generated messages.
        workspace: Workspace path for the generated messages.
        timestamp: Fallback timestamp if action has no emittedAt.
        msg_index_start: Starting message_index offset (avoids collisions).

    Returns:
        List of IndexedMessage with content_tier=TOOL_CONTEXT.
    """
    tool_config = ToolSummaryConfig(
        excluded_tools=config.tool_summaries.excluded_tools,
        max_summary_length=config.tool_summaries.max_summary_length,
        include_meaningful_output=config.tool_summaries.include_meaningful_output,
    )

    messages: list[IndexedMessage] = []
    idx = msg_index_start

    for action in actions:
        if not isinstance(action, dict):
            continue

        # Only process actions that have both input and output
        if "input" not in action or "output" not in action:
            continue

        # generate_tool_summary returns None for say actions and excluded tools
        summary = generate_tool_summary(action, tool_config)
        if summary is None:
            continue

        # Derive tool_name from actionType, defaulting to "unknown"
        tool_name = action.get("actionType") or "unknown"

        # Use emittedAt for timestamp if available
        emitted_at = action.get("emittedAt")
        msg_ts = _parse_timestamp(emitted_at) or timestamp

        action_id = action.get("actionId", f"{session_id}-tool-{idx}")

        messages.append(
            IndexedMessage(
                uuid=action_id,
                session_id=session_id,
                workspace=workspace,
                timestamp=msg_ts,
                role="assistant",
                searchable_text=summary,
                message_index=idx,
                source=Source.IDE,
                content_tier=ContentTier.TOOL_CONTEXT,
                tool_name=tool_name,
            )
        )
        idx += 1

    return messages


def _extract_assistant_responses_from_actions(
    actions: list[dict],
    session_id: str,
    workspace: str,
    fallback_timestamp: datetime,
    msg_index_start: int = 30000,
) -> list[IndexedMessage]:
    """Extract full assistant responses from `say` actions in an execution log.

    In execution logs, `say` actions contain the full assistant response text
    in `output.message`. This replaces the stub "On it." messages stored in
    session files with the complete response content.

    Each extracted response is assigned content_tier=CONVERSATION and role="assistant".
    The existing _should_skip_message filter and code-block replacement are applied.

    Args:
        actions: List of action dicts from an execution log.
        session_id: Session ID for the generated messages.
        workspace: Workspace path for the generated messages.
        fallback_timestamp: Timestamp to use if action has no emittedAt.
        msg_index_start: Starting message_index offset (avoids collisions).

    Returns:
        List of IndexedMessage with content_tier=CONVERSATION and role="assistant".
    """
    messages: list[IndexedMessage] = []
    idx = msg_index_start

    for action in actions:
        if not isinstance(action, dict):
            continue

        # Only process say actions
        if action.get("actionType") != "say":
            continue

        # Only process completed say actions
        if action.get("actionState") not in ("completed", "Success"):
            continue

        # Extract the full response text from output.message
        output = action.get("output", {})
        if not isinstance(output, dict):
            continue

        text = output.get("message", "")
        if not text or not isinstance(text, str) or not text.strip():
            continue

        # Apply the existing skip filter
        if _should_skip_message("assistant", text):
            continue

        # Apply code-block replacement
        text = _replace_code_blocks(text)

        # Check if text is empty after processing
        if not text.strip():
            continue

        # Use emittedAt for timestamp if available
        emitted_at = action.get("emittedAt")
        msg_ts = _parse_timestamp(emitted_at) or fallback_timestamp

        action_id = action.get("actionId", f"{session_id}-say-{idx}")

        messages.append(
            IndexedMessage(
                uuid=action_id,
                session_id=session_id,
                workspace=workspace,
                timestamp=msg_ts,
                role="assistant",
                searchable_text=text,
                message_index=idx,
                source=Source.IDE,
                content_tier=ContentTier.CONVERSATION,
            )
        )
        idx += 1

    return messages


def _parse_execution_start_time(exec_file: Path, fallback_timestamp: datetime) -> datetime:
    """Parse the startTime from an execution log file.

    Returns the parsed startTime, or the fallback_timestamp if the file cannot
    be read or startTime is missing/invalid.
    """
    try:
        with open(exec_file, encoding="utf-8") as f:
            data = json.load(f)
        start_time = data.get("startTime")
        parsed = _parse_timestamp(start_time)
        if parsed is not None:
            return parsed
    except (json.JSONDecodeError, OSError):
        pass
    return fallback_timestamp


def _process_execution_logs_for_session(
    session_id: str,
    workspace: str,
    fallback_timestamp: datetime,
    config: Config | None = None,
) -> list[IndexedMessage]:
    """Process all execution logs for a session with proper message index assignment.

    Collects execution log files for the given session, sorts them by startTime
    (falling back to fallback_timestamp for invalid timestamps), then assigns
    sequential message_index values using the scheme:

        base_offset = execution_order * 100
        user prompt:        base + 0
        tool summaries:     base + 1, base + 2, ... base + N
        assistant responses: base + N+1, base + N+2, ...

    This ensures monotonically increasing indices across executions when sorted
    by startTime.

    Args:
        session_id: The session to process.
        workspace: The workspace path/identifier.
        fallback_timestamp: Timestamp to use when startTime cannot be parsed
            (typically the session's modified timestamp).
        config: Application config (for tool summary settings). If None, uses get_config().

    Returns:
        Combined list of IndexedMessages with properly assigned message_index values.
    """
    if config is None:
        config = get_config()

    index = _build_execution_index()
    exec_file_paths = index.get(session_id, [])

    if not exec_file_paths:
        return []

    # Resolve paths and parse startTimes for sorting
    exec_entries: list[tuple[Path, datetime]] = []
    for file_path in exec_file_paths:
        exec_file = Path(file_path)
        if not exec_file.exists():
            continue
        start_time = _parse_execution_start_time(exec_file, fallback_timestamp)
        exec_entries.append((exec_file, start_time))

    # Sort executions by startTime for monotonically increasing indices
    exec_entries.sort(key=lambda entry: entry[1])

    all_messages: list[IndexedMessage] = []

    for execution_order, (exec_file, exec_start_time) in enumerate(exec_entries):
        base_offset = execution_order * 100
        current_index = base_offset

        # 1. Extract user prompt → base + 0
        user_msg = _extract_user_prompt_from_execution_log(
            exec_file, session_id, workspace, exec_start_time
        )
        if user_msg is not None:
            user_msg.message_index = current_index
            all_messages.append(user_msg)
        current_index += 1

        # Read the execution log for actions
        try:
            with open(exec_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        actions = data.get("actions", [])
        if not isinstance(actions, list):
            continue

        # 2. Generate tool context messages → base + 1, base + 2, ...
        tool_messages = _generate_tool_context_messages(
            actions=actions,
            config=config,
            session_id=session_id,
            workspace=workspace,
            timestamp=exec_start_time,
            msg_index_start=current_index,
        )
        # Reassign indices sequentially from current_index
        for tool_msg in tool_messages:
            tool_msg.message_index = current_index
            current_index += 1
        all_messages.extend(tool_messages)

        # 3. Extract assistant say responses → after all tools
        assistant_messages = _extract_assistant_responses_from_actions(
            actions=actions,
            session_id=session_id,
            workspace=workspace,
            fallback_timestamp=exec_start_time,
            msg_index_start=current_index,
        )
        # Reassign indices sequentially from current_index
        for asst_msg in assistant_messages:
            asst_msg.message_index = current_index
            current_index += 1
        all_messages.extend(assistant_messages)

    return all_messages


def _is_stub_message(role: str, text: str) -> bool:
    """Determine if an existing message is a stub that should be replaced.

    Stubs are:
    - User messages that are very short or generic (session file placeholders)
    - Assistant messages that are exactly "On it." or very short boilerplate

    Args:
        role: The message role ("user" or "assistant").
        text: The message searchable_text.

    Returns:
        True if the message appears to be a stub that should be replaced.
    """
    if not text or not text.strip():
        return True

    stripped = text.strip()

    if role == "assistant" and stripped in ("On it.", "On it"):
        return True

    if role == "user":
        # User stubs from session files - single word or very short placeholders
        user_stubs = {
            "continue",
            "go ahead",
            "yes",
            "ok",
            "proceed",
            "do it",
        }
        if stripped.lower() in user_stubs:
            return True

    return False


def _replace_stub_messages(
    new_messages: list[IndexedMessage],
    existing_messages: list[IndexedMessage],
) -> list[IndexedMessage]:
    """Replace previously-indexed stub messages with full content from execution logs.

    When re-indexing a session that was previously indexed by unmodified Kiro Ception,
    the existing messages may contain stubs:
    - User messages with short/generic text like "continue" (from session files)
    - Assistant messages with "On it." (stub from session files)

    This function merges new full-content messages with existing ones, replacing stubs
    where appropriate and ensuring no duplicate conversation-tier messages.

    Deduplication strategy:
    - Messages with the same UUID are updates (new replaces old)
    - For conversation-tier messages: match by role + message_index position
      to detect stubs that should be replaced by execution log content
    - Tool context messages from new extraction are always included (new additions)

    On replacement failure: the existing stub is retained and a warning is logged.

    Args:
        new_messages: Messages extracted from execution logs (full prompts,
            full responses, tool summaries).
        existing_messages: Previously-indexed messages for the session (from session file),
            as list of IndexedMessage objects.

    Returns:
        Merged list of IndexedMessage with stubs replaced and no duplicates.
    """
    # Build a lookup of existing messages by UUID for fast dedup
    existing_by_uuid: dict[str, IndexedMessage] = {
        msg.uuid: msg for msg in existing_messages
    }

    # Build a lookup of existing conversation messages by (role, message_index)
    # This catches stubs that have different UUIDs but occupy the same position
    existing_conv_by_position: dict[tuple[str, int], IndexedMessage] = {}
    for msg in existing_messages:
        if msg.content_tier == ContentTier.CONVERSATION:
            key = (msg.role, msg.message_index)
            existing_conv_by_position[key] = msg

    # Track which existing UUIDs have been superseded by new messages
    superseded_uuids: set[str] = set()

    # Final output messages from new extraction
    final_messages: list[IndexedMessage] = []
    seen_uuids: set[str] = set()

    for new_msg in new_messages:
        # Skip duplicate UUIDs within the new message set
        if new_msg.uuid in seen_uuids:
            continue
        seen_uuids.add(new_msg.uuid)

        try:
            # Case 1: Same UUID already exists — this is a direct update/replacement
            if new_msg.uuid in existing_by_uuid:
                superseded_uuids.add(new_msg.uuid)
                final_messages.append(new_msg)
                continue

            # Case 2: For conversation-tier messages, check if there's an existing
            # stub at the same position that should be replaced
            if new_msg.content_tier == ContentTier.CONVERSATION:
                position_key = (new_msg.role, new_msg.message_index)
                existing_at_position = existing_conv_by_position.get(position_key)

                if existing_at_position is not None:
                    is_stub = _is_stub_message(
                        existing_at_position.role, existing_at_position.searchable_text
                    )
                    if is_stub:
                        # Replace the stub — mark the old UUID as superseded
                        superseded_uuids.add(existing_at_position.uuid)
                        final_messages.append(new_msg)
                        continue

            # Case 3: New message with no existing match — insert
            final_messages.append(new_msg)

        except Exception as e:
            # On replacement failure: log warning and skip this new message
            # (the existing stub will be retained)
            logger.warning(
                f"Failed to process message replacement for UUID {new_msg.uuid}: {e}"
            )
            continue

    # Carry forward existing messages that were NOT superseded
    for msg in existing_messages:
        if msg.uuid in superseded_uuids:
            continue
        if msg.uuid in seen_uuids:
            continue
        # Avoid duplicating conversation-tier messages at positions now occupied
        if msg.content_tier == ContentTier.CONVERSATION:
            position_key = (msg.role, msg.message_index)
            # Check if any new message occupies this position
            new_at_position = any(
                m.role == msg.role
                and m.message_index == msg.message_index
                and m.content_tier == ContentTier.CONVERSATION
                for m in final_messages
            )
            if new_at_position:
                continue

        final_messages.append(msg)

    return final_messages


def _load_execution_log_messages(session_id: str, workspace: str, timestamp: datetime) -> list[IndexedMessage]:
    """Load messages from execution logs for a given session.

    Delegates to _process_execution_logs_for_session which implements proper
    message index assignment: user prompt → tool summaries → assistant responses,
    with cross-execution ordering by startTime.

    Uses the cached execution index for fast lookup.
    Only reads the full file for executions belonging to this session.
    """
    return _process_execution_logs_for_session(
        session_id=session_id,
        workspace=workspace,
        fallback_timestamp=timestamp,
    )


def _load_workspace_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages from a workspace-sessions JSON file."""
    # Find the session file
    session_file = None
    for ws_dir in _get_workspace_sessions_dirs():
        for workspace_dir in ws_dir.iterdir():
            if not workspace_dir.is_dir():
                continue
            candidate = workspace_dir / f"{session.session_id}.json"
            if candidate.exists():
                session_file = candidate
                break
        if session_file:
            break

    if not session_file:
        return []

    messages = []
    try:
        with open(session_file, encoding="utf-8") as f:
            data = json.load(f)

        # Get workspace from file metadata (more accurate than directory name)
        workspace = data.get("workspaceDirectory", data.get("workspacePath", session.workspace))
        history = data.get("history", [])

        for idx, item in enumerate(history):
            if not isinstance(item, dict):
                continue

            # Messages are nested inside a 'message' key
            msg = item.get("message", {})
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", "")
            text = _extract_text_from_content(content)

            if _should_skip_message(role, text):
                continue

            if not text.strip():
                continue

            # Replace fenced code blocks with compact placeholders
            text = _replace_code_blocks(text)

            # No per-message timestamps in this format; use session dates
            timestamp = session.modified or session.created or datetime.now()

            messages.append(
                IndexedMessage(
                    uuid=msg.get("id", f"{session.session_id}-{idx}"),
                    session_id=session.session_id,
                    workspace=workspace,
                    timestamp=timestamp,
                    role=role,
                    searchable_text=text,
                    message_index=idx,
                    source=Source.IDE,
                )
            )

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Error loading workspace session {session.session_id}: {e}")

    # Also load messages from execution logs and replace stubs
    try:
        exec_messages = _load_execution_log_messages(
            session_id=session.session_id,
            workspace=session.workspace,
            timestamp=session.modified or session.created or datetime.now(),
        )
        if exec_messages:
            messages = _replace_stub_messages(
                new_messages=exec_messages,
                existing_messages=messages,
            )
    except Exception as e:
        logger.warning(f"Error loading execution logs for session {session.session_id}: {e}")

    # Sort by message_index to maintain order (user messages have low indices, exec messages have high)
    messages.sort(key=lambda m: m.message_index)

    return messages


# --- Public API ---


def list_ide_sessions() -> list[SessionInfo]:
    """List all IDE sessions from legacy, workspace-sessions, and Kiro 1.0 formats.

    Deduplicates sessions that exist in multiple locations, preferring the
    Kiro 1.0 format (richest data, most recent) over workspace-sessions over legacy.
    """
    # Collect sessions from all sources, with Kiro 1.0 last so it wins on dedup
    sessions_by_id: dict[str, SessionInfo] = {}

    for session in _list_legacy_sessions():
        sessions_by_id[session.session_id] = session

    for session in _list_workspace_sessions():
        sessions_by_id[session.session_id] = session

    for session in _list_kiro_sessions():
        sessions_by_id[session.session_id] = session

    return list(sessions_by_id.values())


def load_ide_session_messages(session: SessionInfo) -> list[IndexedMessage]:
    """Load messages for a session, trying all formats.

    Priority: Kiro 1.0 (richest data) > workspace-sessions > legacy .chat
    """
    # Try Kiro 1.0 format first (has full assistant responses inline)
    messages_file = _find_kiro_session_messages_file(session.session_id)
    if messages_file:
        return _load_kiro_session_messages(session)

    # Try workspace-sessions format
    for ws_dir in _get_workspace_sessions_dirs():
        for workspace_dir in ws_dir.iterdir():
            if not workspace_dir.is_dir():
                continue
            candidate = workspace_dir / f"{session.session_id}.json"
            if candidate.exists():
                return _load_workspace_session_messages(session)

    # Fall back to legacy .chat format
    return _load_legacy_session_messages(session)

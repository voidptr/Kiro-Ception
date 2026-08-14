# Architecture Guide

## System Design

This is a Kiro MCP Power (kiro-ception) that provides semantic search across conversation history. It runs as a background service launched by each Kiro IDE window.

### Core Principles

- **Non-blocking**: The MCP server responds immediately. All heavy work (indexing, embedding) happens in a separate engine process.
- **Process separation**: The MCP process is a thin stdio proxy with no heavy dependencies. A separate engine process owns the search index, embeddings, and indexer. The MCP process spawns the engine on demand and communicates via localhost HTTP.
- **Code fingerprinting**: On reconnect, the MCP client checks if the engine's code matches the current source files. Mismatch triggers automatic engine restart — ensures code changes take effect immediately.
- **PID registry**: The engine tracks which MCP client processes are alive. When all clients die, the engine shuts itself down gracefully.
- **Streaming**: Never load all conversations into memory at once. Process one session at a time, discard text after embedding.
- **Crash-safe**: SQLite with WAL mode. Every embedding and session state update is committed atomically. Ctrl+C loses at most one in-flight message.
- **Incremental**: File mtime tracking skips unchanged sessions. Text hash deduplication avoids re-embedding identical content.
- **Eager cold-start**: Engine loads the SearchIndex from existing SQLite cache immediately on init, before the first search arrives.

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `server.py` | MCP tool definitions, initialization (spawns engine), workspace detection |
| `engine_main.py` | Standalone engine process: HTTP server, indexer lifecycle, DLL preload |
| `engine_client.py` | Client library: spawn/discover/health-check/fingerprint engine, auto-respawn |
| `dashboard.py` | HTML dashboard served by the engine at GET / |
| `search.py` | SearchIndex (in-memory numpy matrix), hybrid search, peer fan-out |
| `search_utils.py` | Pure post-processing: deduplication, context windows, pagination, date parsing |
| `background_indexer.py` | Background thread: discovers sessions, embeds messages, periodic rescan |
| `peers.py` | Cross-machine federation: fan-out search, result merging, Argon2id + AES-256-GCM encryption |
| `cache.py` | SQLite cache: embeddings, message metadata, session state, execution index, FTS5 search |
| `migrations.py` | Schema versioning and migrations (FTS5 index creation, future schema changes) |
| `embeddings.py` | Backend abstraction: sentence-transformers or OpenAI-compatible API |
| `ide_loader.py` | Loads IDE conversations (legacy .chat + workspace-sessions + Kiro 1.0 JSONL + execution logs) |
| `cli_loader.py` | Loads CLI conversations from SQLite database |
| `claude_loader.py` | Loads Claude Code conversations (per-session JSONL transcripts + subagent transcripts) |
| `sessions.py` | Unified entry point combining CLI + IDE + Claude loaders |
| `config.py` | TOML config loading, dataclasses, hot-reload support |
| `memory.py` | Memory limit utilities (get_memory_limit, select_sessions_within_limit) |
| `models.py` | Pydantic data models (IndexedMessage, SessionInfo) |

### Data Flow

1. Session discovery: stat files, compare mtimes against session_state table
2. Message extraction: parse JSON, filter boilerplate, replace code blocks with placeholders
3. Embedding: hash text → check cache → call backend if not cached → store in sqlite
4. Search: numpy dot product against in-memory matrix → FTS5 full-text keyword search → merge hybrid results (70% vector / 30% BM25) → apply recency boost → filter by workspace/source/date → deduplicate → build context windows → paginate

### Search Path (Read)

```
MCP tool call (server.py) → search() (search.py) → engine_search()
  → SearchIndex.search() (numpy cosine similarity — 70% weight)
  → cache.fts_search() (FTS5 BM25 keyword match — 30% weight)
  → _merge_hybrid_results() (combine vector + FTS scores)
  → _apply_recency_boost() (auto-scaled exponential decay favoring recent)
  → deduplicate_results() (search_utils.py)
  → format_search_response() (search_utils.py)
    → build_context_window() for each match
    → generate_hint() for pagination
  → _search_with_peers() → fan_out_search() (peers.py)
  → merge_peer_results()
```

### Indexing Path (Write)

```
BackgroundIndexer._run() → _index_pass()
  → list_all_sessions() (sessions.py)
  → select_sessions_within_limit() (memory.py)
  → For each changed session:
    → load_session_messages() → embed → cache.put_embeddings_batch()
  → SearchIndex picks up new data on next refresh (60s)
```

### Cold-Start Behavior

On engine initialization:
1. BackgroundIndexer starts in background thread
2. SearchIndex._refresh() is called eagerly from _ensure_initialized()
3. If SQLite has data from prior run: matrix loads in <1s, first search works immediately
4. If no prior data: SearchIndex detects "loading" state (embeddings exist but metadata not yet populated) and returns informative "still loading" response instead of empty results
5. The 60-second refresh throttle only activates after the first successful load

### Key Design Decisions

- **Hybrid search (vector + FTS5)**: Combines semantic embeddings (70% weight) with BM25 full-text search (30% weight). Results found by both methods get boosted. FTS handles exact keyword/function name lookups that embeddings miss; embeddings handle meaning-based queries that keywords miss.
- **Recency boost with auto-scaled halflife**: Exponential decay multiplier favoring recent messages. The halflife is auto-calculated from the oldest message in the index so the decay curve scales naturally as history grows. Configurable floor (default 0.85) — set to 1.0 to disable.
- **Schema migrations**: Versioned migrations tracked via `schema_version` in the meta table. On cache init, migrations run sequentially to bring the DB up to date. This allows schema evolution without requiring users to delete their cache.
- **FTS5 with triggers**: The FTS virtual table stays in sync with the messages table via INSERT/UPDATE/DELETE triggers. No manual FTS maintenance needed — writes to messages automatically propagate.
- **SQLite over pickle**: Atomic per-row writes, no full-file rewrites, concurrent reader support
- **Execution logs for assistant responses**: Kiro IDE stores user messages in session files but assistant responses in separate execution log files (actionType="say")
- **Code block placeholders**: `[code:python]` preserves language signal without embedding thousands of code tokens
- **60-second matrix refresh**: Balances search freshness vs memory churn
- **10-minute rescan interval**: Picks up new conversations without hammering the filesystem
- **No separate CLI indexer**: Background indexing in the MCP server handles everything; `rescan(full=True)` tool covers manual rebuilds
- **search_utils.py extraction**: Pure functions for post-processing (dedup, pagination, context) are separated from server.py for independent unit testing
- **search.py extraction**: SearchIndex and all search logic live in `search.py`, keeping `server.py` focused solely on MCP tool proxy definitions
- **Peer federation via HTTP fan-out**: Each machine maintains its own index. Peers are queried in parallel and results are merged by score. No shared state, no sync conflicts.
- **Optional AES-256-GCM encryption for peers**: Key derived via Argon2id (memory-hard KDF). Both peers derive the same key from the same passphrase independently — no key exchange protocol needed. Crypto functions live directly in `peers.py` (no separate module).

### Session Formats (IDE)

The `ide_loader.py` module handles three distinct IDE session formats, plus execution logs:

**Kiro 1.0 (current, primary)**
- Location: `~/.kiro/sessions/<sha256_prefix>/<session_id>/`
- Files: `session.json` (metadata) + `messages.jsonl` (conversation stream)
- Directory naming: first 16 hex chars of `SHA256(workspace_path)`
- Messages are JSONL with `payload.type` field: `user`, `assistant`, `tool_call`, `tool_result`, plus system types we skip
- Full assistant responses stored inline (no execution log fallback needed)
- Tool calls and results are interleaved in the stream as paired messages
- Timestamps: ISO 8601 strings (e.g., `"2026-07-01T01:46:43.643Z"`)
- Session IDs prefixed with `sess_` (e.g., `sess_22a9402b-...`)

**Workspace-sessions (pre-1.0, deprecated)**
- Location: `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions/<base64_path>/<uuid>.json`
- Directory naming: base64url-encoded workspace paths
- Single JSON file per session with `history` array
- Assistant responses are stubs ("On it.") — real responses reconstructed from execution logs
- Timestamps: epoch milliseconds

**Legacy .chat**
- Location: `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/<workspace_hash>/<uuid>.chat`
- Single JSON file with `chat` array containing full conversation
- Oldest format, no longer produced by Kiro

**Execution logs (pre-1.0 supplement)**
- Location: `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/<workspace_hash>/414d1636299d2b9e4ce7e17fb11f63e9/<exec_id>`
- Contains assistant responses (actionType="say") and tool actions
- Used to replace stub messages in workspace-sessions format
- Not needed for Kiro 1.0 format (full responses are inline)

**Deduplication**: When the same session_id exists in multiple formats (due to migration), the loader prefers Kiro 1.0 > workspace-sessions > legacy.

### Session Formats (Claude Code)

The `claude_loader.py` module handles Claude Code transcripts.

**Claude Code JSONL**
- Location: `~/.claude/projects/<encoded_workspace>/<session_uuid>.jsonl`
- Subagents: `~/.claude/projects/<encoded_workspace>/<session_uuid>/subagents/<uuid>.jsonl`
- Directory naming: lossy — every non-alphanumeric character in the workspace path becomes `-`, so it cannot be reliably reversed
- Workspace resolution: read from the `cwd` field present on every conversational record; the decoded directory name is only a fallback
- Records are JSONL with a top-level `type` discriminator; only `user` and `assistant` carry conversation
- `message` follows the Anthropic Messages API shape: `content` is either a string or a list of typed blocks (`text`, `thinking`, `tool_use`, `tool_result`)
- Tool calls pair across records: `tool_use` on an assistant record, answered by `tool_result` on the following user record, matched via `tool_use_id`
- `toolUseResult` carries a richer structured form of the result, used as a fallback when the block content is empty
- Timestamps: ISO 8601 strings (e.g., `"2026-08-11T00:38:04.704Z"`)
- Skipped record types: `mode`, `ai-title`, `last-prompt`, `attachment`, `queue-operation`, `permission-mode`, `file-history-snapshot`, `file-history-delta`, `system`
- Skipped content: records flagged `isMeta` (harness-injected pseudo-turns), slash-command echoes, and local command output; `<system-reminder>` blocks are stripped from otherwise-real user text rather than dropping the message

**Session identity**: main transcripts use the filename stem (a UUID); subagent transcripts use `<parent_session>-sub-<uuid>` so they stay unique and traceable to their parent.

**Path index**: `list_claude_sessions()` populates a module-level `session_id -> Path` map. `load_claude_session_messages()` resolves through it and rescans once if the id is missing, mirroring `_find_kiro_session_messages_file`.

**Multiple roots**: unlike the CLI and IDE sources (first-match-wins), every configured Claude root that exists is scanned, so multiple installations index together.

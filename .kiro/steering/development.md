# Development Guide

## Language & Runtime

- Python 3.11+ (uses `|` union syntax, `tomllib`, `match` patterns)
- Package manager: `uv` (not pip, not poetry)
- Build system: hatchling
- Sync/install: `uv sync` (installs into `.venv`)
- Run commands: `uv run <command>`

## Code Style

- Formatter/linter: `ruff` (configured in pyproject.toml)
- Line length: 100 characters
- Type hints on all function signatures
- Dataclasses for configuration, Pydantic for data models
- Use `from __future__ import annotations` is NOT needed (3.11+ native)

## Key Conventions

- All print statements during indexing use `flush=True` for immediate visibility
- Log prefix: `[kiro-ception]` for all runtime log messages
- Background threads are daemon threads (auto-die with main process)
- SQLite connections use `check_same_thread=False` for cross-thread access
- File locks use the `filelock` library (cross-platform)
- Config uses `@lru_cache(maxsize=1)` with explicit `cache_clear()` for hot-reload

## Package & Entrypoints

- Package name: `kiro-ception`
- Python module: `kiro_ception`
- Single entrypoint: `kiro-ception` (runs the MCP stdio proxy)
- The engine process is spawned automatically by the MCP proxy — no separate CLI needed

## Configuration

- User config: `~/.config/kiro-ception/config.toml`
- Default config: `config.default.toml` in repo root
- Cache directory: `~/.cache/kiro-ception/`
- All paths support `~` expansion via `Path.expanduser()`

## Development Workflow

After modifying code:
```bash
uv sync                              # Rebuild package in venv
uv run pytest tests/ -q              # Run test suite (300+ tests)
uv run kiro-ception                  # Test MCP server starts (Ctrl+C to exit)
```

### Local Power Installation (Dev Mode)

The `mcp.json` at the repo root is what Kiro uses when the power is installed.
In production it points to the GitHub repo via `uv tool run`. For local
development, temporarily change it to point at your local `.venv`:

```json
{
  "mcpServers": {
    "kiro-ception": {
      "command": "/path/to/kiro-ception/.venv/bin/kiro-ception"
    }
  }
}
```

Then reinstall the power from local path (Powers panel → Add power from Local Path →
select the repo folder). This way Kiro runs your local code directly and picks
up changes immediately after `uv sync` + reinstall.

**Do not commit this local path change.** Revert `mcp.json` before pushing.

## Testing

### Running Tests

```bash
uv run pytest tests/ -q              # Quick summary
uv run pytest tests/ -v              # Verbose (see each test name)
uv run pytest tests/test_tools.py    # Run a single file
uv run pytest tests/ -k "search"     # Run tests matching a keyword
```

### Test Structure

```
tests/
├── fixtures/                          # Anonymized sample data files
│   ├── legacy_session.chat            # Legacy .chat format fixture
│   ├── workspace_session.json         # Workspace-sessions format fixture
│   ├── execution_log.json             # Execution log format fixture
│   ├── kiro_session.json              # Kiro 1.0 session metadata fixture
│   ├── kiro_messages.jsonl            # Kiro 1.0 JSONL messages fixture
│   └── sessions.json                  # Sessions metadata fixture
├── test_search_utils.py               # Pure functions: parse_date, deduplicate, context windows
├── test_config.py                     # Config loading, from_dict, diff_configs
├── test_tools.py                      # All MCP tool endpoints (mocked deps)
├── test_cache.py                      # SQLite cache with real temp databases
├── test_ide_loader.py                 # Message filtering, code block replacement
├── test_embeddings.py                 # Backend factory, OpenAI-compatible with mocked HTTP
├── test_session_loading.py            # All session formats: legacy, workspace, exec logs, CLI
├── test_kiro_sessions.py             # Kiro 1.0 session loader: discovery, loading, tool context, dedup
├── test_indexer.py                    # Full pipeline: index → search, SearchIndex, memory limits
├── test_engine.py                     # Engine coordination, file locks
├── test_engine_client.py              # Engine client: spawn, fingerprint, registry, retry
├── test_engine_http.py                # Real HTTP server integration (all endpoints)
├── test_background_indexer_live.py    # Live threading: indexer start → completion → cache verify
├── test_peers.py                      # Peer federation: crypto, fan-out, result merging, encryption
├── test_edge_cases.py                 # Edge case coverage: malformed data, boundary conditions
└── test_remaining_gaps.py             # CLI extract, TOML loading, exec index
```

### Test Tiers

**Tier 1 — Unit tests (no I/O, instant):**
- `test_search_utils.py` — pure functions, zero dependencies
- `test_config.py` — dataclass construction and diffing
- `test_tools.py` — MCP tools with mocked indexer/manager
- `test_ide_loader.py` — message filtering and text processing
- `test_edge_cases.py` — boundary conditions and malformed data

**Tier 2 — Integration tests (temp files/DBs, fast):**
- `test_cache.py` — real SQLite in tmp_path
- `test_embeddings.py` — mocked HTTP for OpenAI-compatible backend
- `test_session_loading.py` — fixture files parsed by real loaders
- `test_peers.py` — crypto round-trips, result merging, fan-out (mocked HTTP)

**Tier 3 — System tests (threads, HTTP, slower):**
- `test_indexer.py` — full indexing pipeline with fixture corpus
- `test_engine.py` — file locks, role assignment
- `test_engine_http.py` — real HTTP server on random port
- `test_background_indexer_live.py` — real daemon threads

### Writing Tests

- Use `pytest` with class-based grouping (`class TestFoo:`)
- Use `tmp_path` fixture for temp directories/files
- Use `monkeypatch` for env vars and module-level patches
- Use `unittest.mock.patch` for function/method mocking
- Fixtures for test data go in `tests/fixtures/`
- Fixture files should be anonymized (no real user data)
- Test names: `test_<what_it_does>` (e.g., `test_empty_input_returns_none`)

## MCP Tools (Current)

6 tools total:

| Tool | Purpose |
|------|---------|
| `search_project_history` | Search current workspace |
| `search_global_history` | Search all workspaces (optional `source` filter: all/cli/ide) |
| `get_indexing_status` | Check indexer progress/state |
| `rescan` | Pick up new sessions (`full=True` to ignore mtime cache) |
| `get_config` | Show config, paths, instance role, cache stats |
| `reload_config` | Hot-reload config from disk |

## Adding New MCP Tools

1. Define the function with `@mcp.tool()` decorator in `server.py`
2. Add `_ensure_initialized()` at the top of the function
3. Forward the request to the engine via `get_engine_client()`
4. Add corresponding HTTP endpoint in `engine_main.py` if new functionality is needed
5. Add tests in `tests/test_tools.py`
6. Update POWER.md tool table and trigger scenarios
7. Update README.md management tools table

## Adding New Config Options

1. Add field to the appropriate dataclass in `config.py`
2. Add to `diff_configs()` safe_keys or reindex_keys list
3. Add to `config.default.toml` with comment
4. Add to `get_config` tool response in `server.py`
5. Add test in `tests/test_config.py`
6. Update README Configuration section

## Message Filtering

Filtering is in `ide_loader.py` via `_SKIP_PREFIXES` and `_should_skip_message()`.
Messages are skipped if they start with system prompt boilerplate:
- `# System Prompt`, `<identity>`, `<system>`, `<instructions>`
- `Follow these instructions for user requests related to spec tasks`
- `## Included Rules`, `<steering-reminder>`
- `CONTEXT TRANSFER:`
- `You are operating in a workspace`

Code blocks are replaced with `[code:language]` placeholders via `_replace_code_blocks()`.

These are tested in `tests/test_ide_loader.py` — update the tests when adding new skip prefixes.

## Embedding Cache

The cache DB is namespaced by backend fingerprint:
`~/.cache/kiro-ception/cache_<hash>.db`

Changing model/backend/dimensions = different hash = different DB file.
Old cache files are preserved for rollback.

## Schema Migrations

Schema versioning lives in `migrations.py`. The current version is tracked in
the `meta` table under the key `schema_version`.

**Version history:**
- v1: Baseline (embeddings, messages, session_state, execution_index, meta)
- v2: FTS5 full-text search index on messages.searchable_text + sync triggers

**Adding a new migration:**
1. Bump `CURRENT_SCHEMA_VERSION` in `migrations.py`
2. Add a `_migrate_vN_to_vN+1(conn)` function with the schema changes
3. Add the `if current < N+1:` call in `run_migrations()`
4. If the migration adds a table/index that needs data, populate it from existing rows
5. Add tests in `tests/test_migrations.py`
6. Update this version history

Migrations run automatically on cache init. They are idempotent (safe to call
multiple times). Existing data is never deleted — migrations only add tables,
columns, or indexes.

## Project Structure

```
src/kiro_ception/
├── server.py              # MCP stdio proxy — thin, no heavy deps
├── engine_main.py         # Standalone engine process (HTTP server, indexer, search)
├── engine_client.py       # Client: spawn, discover, fingerprint, health check, retry
├── dashboard.py           # HTML dashboard served by engine at GET /
├── search.py              # SearchIndex (in-memory numpy) + hybrid search + peers
├── background_indexer.py  # Background thread (write path)
├── search_utils.py        # Pure search post-processing functions (testable)
├── peers.py               # Cross-machine federation (fan-out, merge, Argon2id + AES-256-GCM encryption)
├── coordination.py        # Legacy: file lock utilities, helper functions
├── cache.py               # SQLite-backed storage + FTS5 full-text search
├── migrations.py          # Schema versioning and sequential migrations
├── config.py              # TOML config loading
├── embeddings.py          # Embedding backend abstraction
├── ide_loader.py          # IDE conversation loader
├── cli_loader.py          # CLI conversation loader
├── sessions.py            # Unified loader facade (combines CLI + IDE)
├── memory.py              # Memory limit utilities (get_memory_limit, select_sessions_within_limit)
└── models.py              # Pydantic data models
```

```
scripts/
├── run_mutmut.sh          # Mutation testing runner
└── search-via-peers.py    # Dev helper: search only remote peers (bypasses local index)
```

### Dev Helper: search-via-peers.py

Sends encrypted search requests directly to configured peers, displaying results
without mixing in local index matches. Useful for verifying peer connectivity,
encryption, and result quality during development.

```bash
uv run python3.11 scripts/search-via-peers.py portable-ruby
uv run python3.11 scripts/search-via-peers.py some multi word query here
```

Shows per-peer connection status (✅/❌/⚠️) and results with scores, roles,
content previews, and workspace paths. Reads peer config from
`~/.config/kiro-ception/config.toml`.

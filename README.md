# Kiro Ception

<p align="center">
  <img src="docs/images/header.png" alt="Kiro Ception" width="800" />
</p>

<p align="center">
  <a href="https://github.com/DevOps-Nirvana/Kiro-Ception">GitHub</a> •
  <a href="https://github.com/DevOps-Nirvana/Kiro-Ception/issues">Issues</a> •
  <a href="https://github.com/DevOps-Nirvana/Kiro-Ception/releases">Releases</a>
</p>

> **This is a fork of [DevOps-Nirvana/Kiro-Ception](https://github.com/DevOps-Nirvana/Kiro-Ception) that adds a configurable Claude Code source.**
> Everything upstream does for Kiro CLI and IDE history, this fork also does for Claude Code transcripts — one index, one search, both assistants. See [Claude Code Support](#claude-code-support).

**Your AI now remembers everything you've ever done with it, across every machine you own.** Finally, an elephant-grade memory for your coding assistant, minus the 12,000-pound footprint.

Kiro Ception gives Kiro a long-term memory, persistent recall that spans every session, every window, CLI and IDE, and even across multiple machines. Your agent remembers what you discussed yesterday, last month, or six months ago, in any project, on any computer you work from. It automatically indexes all conversation history in the background and provides instant hybrid search (semantic + keyword) so you can find past discussions, decisions, and implementations by meaning, keywords, date, or any combination.

> *"We discussed this already..."*
> *"What was that approach we used last week?"*
> *"Didn't we solve this exact problem in the other project?"*
> *"How did I usually set up CI pipelines?"*
>
> - All things you can now just *ask*, and actually get an answer.

## How It Works

Kiro Ception is an [MCP Power](https://kiro.dev/docs/powers/) that runs as a background service alongside your Kiro IDE. It:

1. **Discovers** all Kiro CLI and IDE session files on your machine, plus Claude Code transcripts
2. **Extracts** meaningful messages (filtering out system prompts and boilerplate, condensing long code blocks into `[code:lang]` placeholders)
3. **Embeds** each message into a vector representation using your configured model
4. **Indexes** everything into an in-memory numpy matrix for instant hybrid search (semantic + FTS5 keyword)
5. **Serves** search results via MCP tools that Kiro can call naturally during conversation
6. **Federates** across machines, search your laptop and desktop simultaneously with encrypted peer-to-peer queries

Sessions are processed **newest first**, so your most recent conversations are searchable within seconds of startup, even while older history is still being indexed in the background.

Search results include surrounding context (messages before/after each match), relevance scores, workspace origin, and pagination, so Kiro gets the full picture of what was discussed.

### Architecture Highlights

- **Two-process model**: A thin MCP proxy (stdio) spawns a separate engine subprocess that holds the index in RAM. Code changes are detected via fingerprinting — the engine auto-restarts with fresh code when you `git pull && uv sync`.
- **Non-blocking**: Heavy work (indexing, embedding) runs in the engine's background threads. The MCP proxy responds instantly.
- **Hybrid search**: Combines semantic vector similarity (70%) with FTS5 full-text keyword search (30%). Find things by meaning *and* exact names.
- **Recency-aware**: Recent conversations rank higher automatically. The decay curve scales with your history depth, no manual tuning.
- **Multi-window efficient**: All MCP proxy instances share one engine process. A PID registry tracks connected clients — the engine auto-shuts-down when all clients die.
- **Multi-machine**: Optional peer federation searches across all your computers simultaneously with AES-256-GCM encrypted transport.
- **Crash-safe**: SQLite with WAL mode. Lose at most one in-flight message on Ctrl+C/crash/quit.
- **Instant cold-start**: Loads from existing cache in under 1 second. No waiting for re-indexing after restarts.
- **No build step**: Uses editable install (`python -m kiro_ception.engine_main`). Source changes are picked up immediately — no recompile needed.
- **Auto-migrating**: Schema upgrades run automatically on startup, updates never require deleting your cache, future-proofing this tool.
- **Observable**: Built-in status dashboard, indexing progress monitoring, hot-reloadable config, and health diagnostics, all accessible to the agent or via browser.

## Installation

### Prerequisites

- **[Kiro](https://kiro.dev/downloads/)** - the AI-powered IDE
- **[Git](https://git-scm.com/downloads)** - for cloning/updating the power
- **Python 3.11+** (3.12, 3.13 also supported and tested officially)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** - fast Python package manager

### Install as a Kiro Power from Local Clone (Recommended)

Clone the repo and install as a local power. This gives you immediate updates via `git pull` and the full Power experience (keyword triggers, automatic activation, POWER.md guidance):

```bash
git clone https://github.com/DevOps-Nirvana/Kiro-Ception.git
cd Kiro-Ception
uv sync
```

Then in Kiro IDE: Powers panel → **Add power from Local Path** → select the `Kiro-Ception` folder you just cloned.

To update later:

```bash
cd Kiro-Ception
git pull
uv sync
```

Kiro picks up changes automatically — the MCP proxy detects source code changes via fingerprinting and restarts the engine process with the new code. No manual restart needed.

### Install as a Kiro Power from GitHub (Alternative)

If you prefer not to manage a local clone:

1. In Kiro IDE: Powers panel → **Add power from GitHub**
2. Enter: `https://github.com/DevOps-Nirvana/Kiro-Ception`
3. Click Install

> **Note:** Due to current bugs in how Kiro handles MCP servers within Powers installed from GitHub, the local clone method above is more reliable. The GitHub install may have issues with server startup or reconnection and/or with updating due to a possible split-brain scenario.

### Manual MCP Setup (Last Resort)

If you prefer manual configuration without the Power wrapper, add to your Kiro MCP configuration (`~/.kiro/settings/mcp.json`):

> **Warning:** Installing as a Power (above) is strongly recommended. The POWER.md file contains keyword triggers and usage guidance that help Kiro automatically activate search when you reference past conversations. With MCP-only setup, you'll need to explicitly ask Kiro to search history — it won't trigger on its own from phrases like "as we discussed" or "what did we do last time".

```json
{
  "mcpServers": {
    "kiro-ception": {
      "command": "uv",
      "args": ["tool", "run", "--from", "git+https://github.com/DevOps-Nirvana/Kiro-Ception", "kiro-ception"]
    }
  }
}
```

This uses `uv tool run` to fetch and run the package directly from GitHub, no local clone needed.

Alternatively, if you've cloned the repo locally:

```json
{
  "mcpServers": {
    "kiro-ception": {
      "command": "/path/to/Kiro-Ception/.venv/bin/kiro-ception"
    }
  }
}
```

Replace `/path/to/Kiro-Ception` with the actual clone location. Usually just saving your mcp config will do it, but if needed, restart Kiro.

## Claude Code Support

This fork indexes [Claude Code](https://claude.com/claude-code) transcripts alongside Kiro history. It is on by default and needs no configuration if Claude Code stores its history in the standard location.

Claude Code writes one JSONL transcript per session:

```
~/.claude/projects/<encoded-workspace>/<session-uuid>.jsonl
~/.claude/projects/<encoded-workspace>/<session-uuid>/subagents/<uuid>.jsonl
```

The directory name is a lossy encoding of the workspace path (every non-alphanumeric character becomes `-`), so it cannot be reliably reversed. The loader instead reads the real workspace from the `cwd` field carried on every conversational record, which keeps `search_project_history` scoping accurate. The directory name is only used as a fallback for transcripts that never recorded a `cwd`.

### What Gets Indexed

| Record | Indexed as | Notes |
|--------|-----------|-------|
| `user` text | Conversation | `<system-reminder>` blocks are stripped; harness plumbing (`isMeta` turns, slash-command echoes, local command output) is dropped |
| `assistant` text | Conversation | Fenced code blocks are condensed to `[code:lang]` placeholders, same as Kiro sources |
| `assistant` thinking | Conversation | Off by default — see `include_thinking` |
| `tool_use` + `tool_result` | Tool context | Paired by `tool_use_id` and condensed into one `[Tool] description → outcome` summary, only matched when a search passes `include_tool_context: true` |
| Subagent transcripts | Conversation | Indexed as their own sessions (`<parent>-sub-<uuid>`) |
| Everything else | — | `mode`, `ai-title`, `last-prompt`, `attachment`, `queue-operation`, `permission-mode`, `file-history-*`, and `system` records are bookkeeping and skipped |

Tool summaries honor the existing `[tool_summaries]` settings (`excluded_tools`, `max_summary_length`, `include_meaningful_output`), so tuning applies uniformly across Kiro and Claude sources.

### Configuration

```toml
[sources.claude]
enabled = true

# Every root that exists is scanned — unlike the cli/ide sources, which are
# first-match-wins — so multiple Claude installations can be indexed together.
roots = [
    "~/.claude/projects",
    "~/.config/claude/projects",
]

include_subagents = true      # Index <session>/subagents/*.jsonl as their own sessions
include_sidechains = true     # Index subagent turns inlined in the main transcript
include_thinking = false      # Index extended thinking blocks (verbose; larger index)
include_tool_context = true   # Condense tool_use/tool_result pairs into summaries
```

All of these are hot-reloadable — change them and call the `reload_config` tool, no re-index required. Turning `include_thinking` on indexes content that was previously skipped, so follow it with `rescan(full=True)` to backfill.

To disable Claude indexing entirely, set `enabled = false`.

### Searching a Single Assistant

The `source` parameter on `search_global_history` accepts `"claude"` alongside `"cli"` and `"ide"`. Leave it at `"all"` (the default) to search everything.

### Running Alongside Upstream Kiro-Ception

This fork is a full instance, not a plugin. It is built to run **concurrently** with an existing Kiro-Ception without either one clobbering the other. A ready-to-use isolated config ships in the repo: [`config.claude-rearview.toml`](config.claude-rearview.toml).

Exactly three things must be unique per instance:

| # | Setting | Owns |
|---|---------|------|
| 1 | `embedding.cache_dir` | The embedding DB (`cache_<fingerprint>.db`), `engine.lock`, `engine.json`, and `engine.log` |
| 2 | `server.engine_port` | The localhost port the engine binds; only one process can hold it |
| 3 | The config file itself | Passed with `--config`; also register the MCP server under a distinct key |

```toml
[embedding]
cache_dir = "~/.cache/claude-rearview"   # isolation key #1

[server]
engine_port = 19761                      # isolation key #2
engine_log_file = "auto"                 # -> <cache_dir>/engine.log
```

Register it under its own MCP server key so it sits beside your existing entry rather than replacing it:

```json
"claude-rearview": {
  "type": "stdio",
  "command": "/path/to/uv",
  "args": [
    "run", "--directory", "/path/to/claude-rearview",
    "kiro-ception",
    "--config", "/path/to/claude-rearview/config.claude-rearview.toml"
  ]
}
```

Verify isolation at any time by calling `get_config` (or opening the dashboard at `http://127.0.0.1:<engine_port>/`) and checking that every entry under `paths` points inside your intended `cache_dir`.

> **`engine_log_file` defaults to `"auto"`**, which resolves to `<cache_dir>/engine.log`. Earlier versions hardcoded `~/.cache/kiro-ception/engine.log`, so an instance with a custom `cache_dir` still wrote its log into the default instance's directory — two instances silently sharing one log file. Set an explicit path to override, or `""` to disable file logging.

Alternatively, run only this fork — it indexes everything upstream does, plus Claude Code.

### Telling Concurrent Instances Apart

Instances are selected by the **key** you give them in `mcpServers` — Claude Code namespaces tools as `mcp__<key>__<tool>`, so two entries never collide no matter what the servers call themselves internally:

```
"kiro-ception":    { ... }   →  mcp__kiro-ception__search_project_history
"claude-rearview": { ... }   →  mcp__claude-rearview__search_project_history
```

Routing is only half the problem, though. Every instance exposes the *same* tool names with the *same* docstrings, so nothing tells a caller — or the agent — which one indexes what. Set `instance_label` and each instance says so itself:

```toml
[server]
instance_label = "auto"     # or a literal name like "claude-rearview"
```

`"auto"` derives the name rather than making you invent one, keying off resources that are unique per instance by construction: the `cache_dir` name (skipping generic container names like `cache`, so both `~/.cache/claude-rearview` and `<root>/claude-rearview/cache` yield `claude-rearview`), falling back to `port-<engine_port>`. Both keys are enforced rather than conventional — the engine lock lives in `cache_dir`, so two instances cannot share one, and only one process can bind a port.

The label is appended to every tool description, derived from the sources actually enabled:

```
Instance "claude-rearview". Indexes: Claude Code, Kiro IDE, Kiro CLI.
```

An instance with `[sources.claude] enabled = false` advertises `Indexes: Kiro IDE, Kiro CLI.` instead — so the difference is visible at tool-selection time, before anything is called.

The same information is queryable at runtime via the `get_config` tool (or `GET /config`), which returns an `instance` block alongside `paths` and `sources`:

```json
"instance": {
  "label": "claude-rearview",
  "label_setting": "auto",
  "summary": "Instance \"claude-rearview\". Indexes: Claude Code, Kiro IDE, Kiro CLI.",
  "indexes": ["Claude Code", "Kiro IDE", "Kiro CLI"]
}
```

`label` is the resolved name; `label_setting` is what the config asked for, so you can see whether a name was derived or pinned.

`instance_label` is a discriminator, not a rename — the server is always named `kiro-ception`, whichever instance you are talking to. It is applied when the MCP process starts, so restart the server after changing it. Leaving it empty preserves the original behaviour: descriptions carry the bare `Indexes: ...` line.

### Installing an Instance in Its Own Folder

An instance does not need its own folder — pointing `--config` at a config file is enough. But if you want one fully self-contained directory (venv, config, and data), build a wheel and install it into a dedicated venv:

```bash
uv build                                    # -> dist/kiro_ception-<ver>-py3-none-any.whl

ROOT=~/.local/share/claude-rearview         # Windows: %LOCALAPPDATA%\claude-rearview
mkdir -p "$ROOT/cache"
uv venv "$ROOT/.venv"
uv pip install --python "$ROOT/.venv/bin/python" dist/kiro_ception-*.whl
```

Write `$ROOT/config.toml` with `cache_dir` pointing at `$ROOT/cache` and a unique `engine_port`, then register that venv's console script as an MCP server:

```json
"claude-rearview": {
  "type": "stdio",
  "command": "<ROOT>/.venv/bin/kiro-ception",
  "args": ["--config", "<ROOT>/config.toml"]
}
```

Your editor starts it from there on demand — **no separate service or supervisor process is involved**. The MCP proxy registers itself as a follower, and the engine shuts down when the last client exits.

#### Slow cold starts

The first engine start preloads torch and the embedding model, which can take a minute or more. `ensure_engine_running()` waits `server.engine_startup_timeout_seconds` (default 30) for it.

Overrunning that is **not fatal** — the engine keeps starting in the background and later tool calls reach it once it is listening. The only symptom is that tool calls made during the gap report the engine as unavailable. Raise the timeout to trade slower MCP startup for a ready-on-first-call engine:

```toml
[server]
engine_startup_timeout_seconds = 90
```

#### Debugging an instance with no client attached

`tools/debug_engine.py` holds an engine open when there is **no MCP client** — for verifying a new instance's isolation before registering it, headless/CI runs, or watching a first index without opening an editor. It is a development tool, not part of normal operation:

```bash
python tools/debug_engine.py --config "$ROOT/config.toml"

curl -s http://127.0.0.1:<engine_port>/status    # indexing progress, search readiness
curl -s http://127.0.0.1:<engine_port>/config    # instance identity and every local path
```

It exists because the engine refuses to outlive its clients: it exits once every registered follower has died, or after 120s if none ever registers (orphan protection). So `python -m kiro_ception.engine_main` alone gives you an engine that disappears two minutes later. The tool is the missing follower — each health check re-registers it via `X-Follower-PID`. Stop it and the engine shuts down cleanly. Don't run it alongside a normal editor setup; it only adds a second follower.

### Workspace Detection

`search_project_history` scopes to the current workspace, resolved in this order:

1. `search.workspace_dir` in the config file
2. `KIRO_WORKSPACE` (set by Kiro IDE/CLI)
3. `CLAUDE_PROJECT_DIR` (set by Claude Code)
4. The current working directory

## Configuration

Create `~/.config/kiro-ception/config.toml` to customize behavior. If this file doesn't exist, sensible defaults are used (local CPU-based embeddings with `all-MiniLM-L6-v2`).  Query the tool `get_config` for full information on your file location(s) for your config and database.

A full annotated default config is in [`config.default.toml`](config.default.toml); copy it as a starting point:

```bash
mkdir -p ~/.config/kiro-ception
cp config.default.toml ~/.config/kiro-ception/config.toml
```

### Minimal Config (Zero Setup)

With no config file at all, Kiro Ception uses:

- **Backend**: `sentence-transformers` (local, CPU-based, no API/GPU needed)
- **Model**: `all-MiniLM-L6-v2` (384 dimensions, ~80MB download on first run)
- **Sources**: Auto-discovers Kiro CLI and IDE conversations in both old and new formats, plus Claude Code transcripts under `~/.claude/projects`
- **Memory**: Uses up to 1/3 of available RAM for the index (by default)

This is a good starting point; it runs entirely on CPU with no external dependencies.

### GPU-Accelerated with Ollama (Recommended for Power Users)

If you have Ollama running with a GPU, you can use much larger, higher-quality embedding models by putting something like the following in your config file:

```toml
[embedding]
backend = "openai-compatible"
model = "qwen3-embedding:4b"
api_base = "http://localhost:11434/v1"
dimensions = 1024
batch_size = 1
```

**Setup:**

```bash
# Install Ollama (if not already): https://ollama.com
ollama pull qwen3-embedding:4b
```

This gives significantly better search quality than MiniLM, especially for nuanced queries. The `4b` model runs comfortably on a 6GB+ GPU and indexes at ~3–5 messages/second.

### OpenAI / Hosted Providers

```toml
[embedding]
backend = "openai-compatible"
model = "text-embedding-3-large"
api_base = "https://api.openai.com/v1"
api_key = "sk-..."
dimensions = 1024
```

### LM Studio

```toml
[embedding]
backend = "openai-compatible"
model = "your-model-name"
api_base = "http://localhost:1234/v1"
dimensions = 768
```

## MCP Tools

Kiro can call these tools naturally during conversation:

| Tool | Purpose |
|------|---------|
| `search_project_history` | Search conversations scoped to the current workspace |
| `search_global_history` | Search across all workspaces (supports `source` filter: all/cli/ide) |
| `get_indexing_status` | Check indexer progress, rate, errors, ETA |
| `rescan` | Trigger a rescan for new sessions (`full=True` to re-read everything) |
| `get_config` | Show effective config, paths, cache stats, instance role, etc |
| `reload_config` | Hot-reload config from disk without requiring restart of Kiro |

### Search Parameters

Both search tools accept:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `query` | *(required)* | Natural language search query |
| `after` | — | Only messages on/after this date (ISO 8601) |
| `before` | — | Only messages before this date (ISO 8601) |
| `context_size` | 3 | Messages before/after each match to include |
| `threshold` | 0.2 | Minimum similarity score (0–1) |
| `max_results` | 10 | Maximum results to return |
| `offset` | 0 | Skip results for pagination |

## Technologies & Libraries

| Component | Library | Purpose |
|-----------|---------|---------|
| MCP Server | [mcp](https://github.com/modelcontextprotocol/python-sdk) (FastMCP) | Exposes tools to Kiro via Model Context Protocol |
| Embedding (local) | [sentence-transformers](https://www.sbert.net/) | Local CPU/GPU embeddings (default: all-MiniLM-L6-v2) |
| Embedding (API) | [requests](https://docs.python-requests.org/) | OpenAI-compatible HTTP API for Ollama/LM Studio/OpenAI |
| Vector Search | [numpy](https://numpy.org/) | In-memory cosine similarity via dot product |
| Data Models | [Pydantic](https://docs.pydantic.dev/) | Typed data validation and serialization |
| Cache | SQLite (stdlib) | Persistent embedding + metadata storage (WAL mode) |
| Process Coordination | [filelock](https://py-filelock.readthedocs.io/) | Engine process election via file locks |
| Encryption | [cryptography](https://cryptography.io/) + [argon2-cffi](https://argon2-cffi.readthedocs.io/) | AES-256-GCM peer encryption with Argon2id key derivation |
| Build | [hatchling](https://hatch.pypa.io/) | PEP 517 build backend |
| Package Manager | [uv](https://docs.astral.sh/uv/) | Fast dependency resolution and venv management |
| Linter/Formatter | [ruff](https://docs.astral.sh/ruff/) | Linting and formatting |
| Tests | [pytest](https://pytest.org/) | Test framework (300 tests) |

## Optional Features

### Peer Federation

Search across multiple machines (e.g., your laptop + desktop). Each machine runs its own independent index. When you search, queries fan out to all peers in parallel and results are merged.

```toml
[peers]
enabled = true
nodes = ["192.168.1.50:19742", "workpc.tailscale:19742"]
secret = "my-shared-passphrase"  # Optional: encrypts all peer traffic with AES-256-GCM
timeout_seconds = 5
```

Peers communicate over HTTP. If `secret` is set, payloads are encrypted with AES-256-GCM (key derived via Argon2id from the passphrase). Both machines must use the same secret. Without a secret, traffic is plaintext; fine on VPNs or Tailscale or when local-only at your own house (up to you).

### Memory Limits

Control how much RAM the index uses:

```toml
[memory]
fraction = 0.33     # Use up to 1/3 of RAM (default)
# limit_mb = 512    # Or set an explicit limit
# limit_mb = 0      # Disable limit (use all available)
```

### Indexing Throttle

Reduce GPU/CPU load during active work:

```toml
[indexing]
throttle_ms = 5000   # Sleep 5000ms (5 seconds) between embedding batches (default: 0)
rescan_interval_minutes = 10  # Check for new sessions every 10 minutes (this is the default)
```

Once your initial index is built, it can be quite nice to add the throttle_ms value of 5-10 seconds (5000-10000) to ensure your computer runs quickly and your usage is not negatively affected.  This is especially valuable if you are using a large local GPU-based model.

Secondarily, if you are trying to be sparing on battery life, and/or if you don't care about getting your index up to date so quickly, you can greatly increase the rescan interval to 60 minutes, OR you can disable this automated rescan/reindexing process by setting this to 0.


## Performance

| Metric | Value |
|--------|-------|
| First-time indexing (MiniLM, CPU) | ~4 minutes (4300+ sessions) |
| First-time indexing (Qwen3-Embedding:4b, GPU) | ~35 minutes (4300+ sessions) |
| Subsequent startups | <2 seconds |
| Search latency | <10ms |
| Index refresh (backgrounded) | Every 60 seconds |
| Periodic rescan to update indexes (backgrounded) | Every 10 minutes |
| Embedding rate (Qwen3-Embedding:4b) | ~3–5 messages/second |

**Indexing order:** Sessions are indexed **newest first**, so your most recent conversations become searchable within seconds of startup. Older conversations fill in progressively in the background.

## Troubleshooting

### "Backend not ready" or "still loading"

On first startup, the index eagerly loads from SQLite into RAM. If embeddings exist but metadata hasn't populated yet, you'll see a "still loading" message. Retry in a few seconds.  Also, as your size of your embeddings increases this may make it take a little longer.  I have six months of Kiro work across 4300 chat documents with an (currently) 300MB embedding db, and it takes 10-15 seconds to load the index into RAM.

### Empty search results

- Check `get_indexing_status`; indexing may still be in progress
- Use `rescan()` to immediately pick up recent conversations
- Verify your config with `get_config`
- Check "Kiro Powers / MCP" log

### Embedding errors / timeouts

- For Ollama: ensure it's running (`ollama ps`) and the model is pulled
- Very long messages (>50K chars) may timeout; they're skipped with a warning
- Check your "Kiro Powers" outputs for logs/errors

### Config changes not taking effect

- Use `reload_config` tool (applies safe changes immediately)
- Model/backend/dimensions changes require `rescan(full=True)`

### Multiple windows

All Kiro windows share a single engine process automatically. Each MCP proxy registers its PID with the engine. If the engine dies, the next proxy request will respawn it. Use `get_config` to see the engine PID and port. If the engine has stale code (you updated the source), it will be killed and restarted automatically via fingerprint comparison.

### Nuclear option

If the database is corrupt or everything is broken, find your file path to your database calling the `get_config` tool.  Then, once you find it, uninstall this power (or disable the MCP) then remove your database, then reinstall this power (or re-enable MCP).

```bash
rm -rf ~/.cache/kiro-ception/
```

When you Restart Kiro (or re-enable  MCP) it will rebuild the embeddings database from scratch.

## Development

```bash
uv sync                         # Install deps
uv run pytest tests/ -q         # Run tests (300, ~30s)
uv run ruff check src/          # Lint
uv run kiro-ception             # Run MCP server locally
```

## Data Locations

For information about where your data is being kept, call the MCP tool "get_config".  On an unix-ey system, the file(s) at are...

| Path | Contents |
|------|----------|
| `~/.config/kiro-ception/config.toml` | User configuration |
| `~/.cache/kiro-ception/cache_<hash>.db` | SQLite database (embeddings, metadata) |
| `~/.cache/kiro-ception/engine.lock` | Engine process file lock |
| `~/.cache/kiro-ception/engine.json` | Engine port/PID info for MCP proxies |

The cache DB filename includes a hash of the backend configuration. Changing model/backend/dimensions creates a new DB file (old ones are preserved for rollback).

### Session Data Sources (macOS)

Kiro Ception auto-discovers and indexes conversations from three IDE formats plus the CLI:

| Format | Location (macOS) | Notes |
|--------|-----------------|-------|
| **Kiro 1.0 (current)** | `~/.kiro/sessions/<sha256_prefix>/<session_id>/messages.jsonl` | Primary format since Kiro IDE 1.0. Each session has `session.json` (metadata) + `messages.jsonl` (JSONL stream). Full assistant responses stored inline alongside tool calls. Directory names are the first 16 hex chars of SHA256(workspace_path). |
| **Workspace-sessions (pre-1.0)** | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions/<base64_path>/<uuid>.json` | Older format where sessions were JSON files with a `history` array. Directory names are base64url-encoded workspace paths. Assistant responses were stubs ("On it.") — real responses came from execution logs. |
| **Legacy .chat** | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/<workspace_hash>/<uuid>.chat` | Earliest format. Full conversations in a single JSON file with `chat` array. |
| **Execution logs (pre-1.0)** | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/<workspace_hash>/414d1636299d2b9e4ce7e17fb11f63e9/<exec_id>` | Separate files containing assistant responses (actionType="say") and tool actions. Used to reconstruct full conversations for workspace-sessions format. |
| **CLI** | `~/.kiro/cli/conversations.db` | SQLite database with `conversations_v2` table. Indexed automatically. |
| **Claude Code** | `~/.claude/projects/<encoded-workspace>/<session-uuid>.jsonl` | One JSONL transcript per session, plus subagent transcripts under `<session-uuid>/subagents/`. Workspace is read from each record's `cwd` field. See [Claude Code Support](#claude-code-support). |

When the same session exists in multiple formats (e.g., migrated from workspace-sessions to Kiro 1.0), deduplication ensures it is only indexed once, preferring the richest format (Kiro 1.0 > workspace-sessions > legacy).

**Privacy:** All data is processed and stored locally on your machine. No telemetry, no external API calls, and no data leaves your device; unless you explicitly configure a third-party embedding provider (e.g., OpenAI). The default configuration uses fully local, offline embeddings.

## Contributing

Found a bug? Have a feature request? [Open an issue](https://github.com/DevOps-Nirvana/Kiro-Ception/issues) on GitHub.

### Areas Where Help is Wanted

If you're looking to contribute, here are some areas where we'd love help:

- **Cross-platform testing** — The codebase targets macOS, Windows, and Linux. We develop primarily on macOS and have done targeted Windows work, but need broader real-world testing on Windows (especially around the engine subprocess lifecycle, file locking, and native DLL preloading) and Linux (various distros, ARM64).

- **Integration tests / CI pipeline** — Currently all tests are unit/mock-based. We need end-to-end integration tests that spin up the actual engine process with test fixture data and exercise the full MCP proxy → HTTP → engine → SQLite → search path. This would enable a proper GitHub Actions CI matrix across OS and Python versions.

- **Remove legacy workspace decode fallback** — The vector search path (`search.py`) and FTS search path (`cache.py`) include fallbacks that decode base64-encoded workspace values at query time. These handle indexes created before the `_decode_workspace_dir_name` bug was fixed. After a couple release cycles, these become dead code and can be safely removed.

- **Migrate engine_main.py from print() to logging** — The engine process uses bare `print()` for all status messages. Switching entire codebase to Python's `logging` module would give levels, timestamps, and configurable filtering while still routing through the existing log file support.

- **SIGTERM-based graceful shutdown on Windows** — On Unix, stale engines receive SIGTERM before SIGKILL for graceful cleanup. Windows has no SIGTERM equivalent for non-console processes, so we use `TerminateProcess` directly. A Windows-native approach (e.g., named event signaling) could enable graceful shutdown there too.

## Support

Found a bug? Have a feature request? [Open an issue](https://github.com/DevOps-Nirvana/Kiro-Ception/issues) on GitHub.

## License

MIT - See: [LICENSE](LICENSE).

## Attribution

Built by [Farley Farley](https://github.com/AndrewFarley) ([DevOps-Nirvana](https://github.com/DevOps-Nirvana)), based upon [Kiro Total Recall](https://github.com/danilop/kiro-total-recall) by Danilo Poccia (MIT licensed). The original session loaders, data models, and core embed/search concept originate from that project. Kiro Ception is a ground-up rewrite for production use; see the Architecture Highlights above for what's different.
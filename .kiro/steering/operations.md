---
inclusion: always
description: "claude-rearview operations — embedding backends, performance characteristics, search behavior, cache locations, and the troubleshooting runbook (still-loading, embedding errors, stale results, engine/peer issues, clean-slate)."
---

# Operations Guide

## Embedding Backend

The default backend is `sentence-transformers` with `all-MiniLM-L6-v2` (384 dimensions, CPU-only, no external dependencies). For higher-quality results, users can configure an OpenAI-compatible backend (e.g., Ollama with a GPU model).

Example Ollama configuration (`~/.config/kiro-ception/config.toml`):

```toml
[embedding]
backend = "openai-compatible"
model = "qwen3-embedding:4b"
api_base = "http://localhost:11434/v1"
dimensions = 1024
batch_size = 1
```

When using Ollama, it must be running locally. If embedding fails,
check that Ollama is up and the model is pulled (`ollama pull <model>`).

## Performance Characteristics

- **Subsequent startups**: <2 seconds (all sessions tracked via mtime)
- **Search latency**: <10ms (numpy dot product against in-memory matrix)
- **Matrix refresh**: every 60 seconds (picks up newly embedded messages)
- **Cold-start**: Eager load from existing SQLite cache on engine init (<1s if prior data exists)
- **Periodic rescan**: every 10 minutes (checks for new/changed session files)

First-time indexing speed depends on your embedding backend and corpus size.
CPU-based sentence-transformers is fast but produces lower-quality embeddings.
GPU-based models (e.g., Ollama) are slower to embed but yield better search results.

## Search Behavior

- **Hybrid scoring**: Results are scored 70% semantic similarity + 30% FTS5 BM25 keyword match. This means exact function names and identifiers surface even when the embedding model misses them.
- **Recency boost**: Recent messages get a slight score advantage via exponential decay. The halflife auto-scales based on your history depth. Configurable via `search.recency_floor` (default: 0.85 = oldest messages get 85% of their raw score). Set to 1.0 to disable.
- **Context windows**: Each result includes surrounding messages (configurable via `context_size`, default 3 before + 3 after the match).

## Cache Location

All persistent data lives in `~/.cache/kiro-ception/`:
- `cache_<hash>.db` — SQLite database (embeddings, metadata, session state, exec index)
- `engine.lock` — File lock for engine process
- `engine.json` — Engine port/PID info for MCP clients

## Troubleshooting

### "Backend not ready" or "still loading" response
- On fresh startup, the index eagerly loads from SQLite cache
- If embeddings exist but metadata hasn't populated yet, the response says "still loading" with embedding count — retry in a few seconds
- The 60-second refresh throttle does NOT apply until the first successful load

### Embedding errors / timeouts
- For Ollama: check it's running (`ollama ps`) and the model is pulled
- Very long messages (>50K chars) may timeout — they're processed individually with fallback
- Context overflow errors are logged and the message is skipped

### Stale results (missing recent conversations)
- Periodic rescan runs every 10 minutes
- Use `rescan` tool for immediate pickup
- Check `get_indexing_status` to see if indexing is active

### Config changes not taking effect
- Use `reload_config` tool (applies safe changes immediately)
- Model/backend/dimensions changes require `rescan(full=True)`
- The rescan interval is re-read on each loop iteration

### GitHub-Power update not picked up (uv cache split-brain)
Symptom: after a `git push`/release, `get_config` still shows old code (e.g. a
newly added source missing) even after restarting Kiro. Cause: a GitHub-install
Power runs via `uv tool run --from git+<url>`, which **caches the git build and
does not re-fetch new commits without `--refresh`**; a persistent
`uv tool install kiro-ception` (check `uv tool list`) can also shadow the
git-run; and running engines/`uv tool run` wrappers keep respawning old code
while holding the uv cache lock (so `uv cache clean` times out while Kiro runs).
- The engine's fingerprint self-restart compares process-vs-local-files; it
  **cannot** detect that *git* moved forward for a cached `uv tool run` build.
- Reliable fix (in order): **fully quit Kiro** (releases the cache lock) →
  `uv cache clean kiro-ception` in a plain terminal → **reopen Kiro** (fresh
  cold build from git HEAD; MCP panel shows "loading" for minutes) → `get_config`
  to confirm → `rescan` to index the new source.
- To verify the *actually-running* code: find the engine PID's exe path; a
  `uv\cache\archive-v0\<hash>\...` path is a cached-run build. A local-clone
  Power (`git pull && uv sync`, editable) avoids this entirely.
- Full user-facing writeup: README Troubleshooting "Power update not taking effect".

### Multiple windows / engine process issues
- Use `get_config` to check engine status (see `instance` section)
- If engine dies, next MCP tool call auto-respawns it
- Engine lock file: `~/.cache/kiro-ception/engine.lock`
- If lock is stale (process dead), it's auto-cleaned on next spawn
- Code fingerprinting: reconnecting the MCP server restarts the engine if source has changed
- Engine shuts down automatically when all MCP client processes exit

### Peer federation issues
- Use `get_config` to verify `peers` section shows enabled=true and correct nodes
- Test connectivity: `curl http://peer-address:19742/health` should return `{"status":"ok"}`
- If using encryption: both machines MUST have the same `secret` value
- Mismatched secret → 401 rejection (logged as warning on the receiving end)
- Peers that timeout are silently skipped — local results always return
- Each peer indexes independently — no shared state to corrupt

## Index Health Checks

Via MCP tools:
- `get_indexing_status` — see state, progress, errors, rate
- `get_config` — verify embedding count, cache path, session count, instance role

## Forcing a Clean Slate

For most issues, `rescan(full=True)` is sufficient — it re-reads all sessions
but preserves existing embeddings.

If something is fundamentally broken (corrupt database, wrong dimensions cached):
```bash
rm -rf ~/.cache/kiro-ception/
```
Then restart Kiro — it will rebuild everything from scratch.

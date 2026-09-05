---
inclusion: always
description: "claude-rearview workbench orientation — what this project is, how its steering is laid out, the build/test gate commands (for rules-build-discipline), tool routing, and hard-won gotchas. The one steering file authored specifically for this workbench."
---

# claude-rearview — Workbench Orientation

This is the always-on entry point for the `claude-rearview` code workbench. It ties together
the project-specific guides and the installed behavioral-rule modules, and it carries the
**concrete gate commands** that the project-agnostic `rules-build-discipline` module points at.

## What this project is

`claude-rearview` is a **fork of [DevOps-Nirvana/Kiro-Ception](https://github.com/DevOps-Nirvana/Kiro-Ception)**
that adds a configurable **Claude Code** source. It is a Kiro MCP Power (`kiro-ception`) giving
Kiro long-term semantic memory: it indexes Kiro CLI + IDE history **and** Claude Code
transcripts into one hybrid (vector + FTS5) search index, served to the agent via MCP tools.

The fork is kept somewhat separate from upstream — local auto-commits here do not entangle
with the upstream remote, and this toolbag never pushes.

- **Runtime:** Python 3.11+, `uv` (not pip/poetry), hatchling build.
- **Architecture:** two-process — a thin MCP stdio proxy (`server.py`) spawns a separate
  engine process (`engine_main.py`) that owns the index/embeddings/indexer and speaks
  localhost HTTP. Full detail in `architecture.md`.

## Steering layout in this workbench

| File | Role |
|---|---|
| `claude-rearview-project.md` (this file) | Always-on orientation + gate commands + gotchas |
| `architecture.md` | System design, module responsibilities, data/search/index flow, session formats |
| `development.md` | Toolchain, code style, tests, procedures (add MCP tool / config / migration / skip filter) |
| `operations.md` | Embedding backends, performance, search behavior, cache locations, troubleshooting runbook |
| `mutation-testing.md` | `inclusion: manual` — mutmut workflow; load only when doing mutation testing |
| `modules/rules-*.md` | Installed behavioral-rule modules (safety, common-procedure, code-workbench, build-discipline, worktree-core + merge-lock). Copies with provenance headers — edit the source in Utilities, not here. |

## Build / test gate commands (authoritative for rules-build-discipline)

`rules-build-discipline` is the *discipline*; these are the *commands*. Run from the repo root.
All must pass with **zero** errors/failures before any unit of work is marked done.

```bash
uv sync                         # Sync deps into .venv (prerequisite)
uv run pytest tests/ -q         # Full test suite (~926 tests). MUST be 0 failures.
uv run ruff check src/          # Lint. MUST be clean.
```

- **Primary gate:** `uv run pytest tests/ -q`. There is no compile step (Python) — the test
  suite is the build.
- **Lint gate:** `uv run ruff check src/` (ruff config in `pyproject.toml`, line length 100).
- **Proof vs inference:** there is no CI matrix in this fork yet, so a local green suite is
  the strongest evidence available — treat it as proof for local correctness, not as a claim
  about upstream CI.
- **Timeout is not success** (per `rules-build-discipline`): the suite runs in ~30s; if it
  hangs far past that, suspect a live/blocking test invocation, not a pass. Never run the MCP
  server or engine as a "verification" — that is a run-forever process (safety-core N28); use
  the test suite.
- **Mutation testing** is a separate, optional quality check — see `mutation-testing.md`; not
  part of the standard done-gate.

## Tool routing (this project's own MCP tools)

The workbench exposes 6 MCP tools (see `development.md` for the full table). When *using* them
against this instance, they are namespaced under this instance's server key. Note the
`get_config` tool reports the instance identity (`label`, `sources`, `paths`) — use it to
confirm you are talking to the right instance and that every path sits inside the intended
`cache_dir`.

The accepted `source` filter values are **all / cli / ide / claude** (confirmed in
`server.py`: `source_filter = source if source in ("cli","ide","claude")`).

## Gotchas (hard-won)

- **`mcp.json` local-dev toggle must not be committed.** For local dev it points at the local
  `.venv`; production points at GitHub via `uv tool run`. Revert before committing/pushing
  (see `development.md` → Local Power Installation).
- **Instance isolation is three keys:** `embedding.cache_dir`, `server.engine_port`, and the
  `--config` file / MCP server key. This fork ships `config.claude-rearview.toml` to run
  concurrently with upstream Kiro-Ception without clobbering it. Verify via `get_config`.
- **`engine_log_file = "auto"`** resolves to `<cache_dir>/engine.log`. An explicit hardcoded
  default path once caused two instances to silently share one log — keep it `auto` or set an
  explicit per-instance path.
- **Claude source scans ALL configured roots** (union), unlike cli/ide which are
  first-match-wins. Workspace is read from each record's `cwd`, not the lossy directory name.
- **Schema changes require a migration** (`migrations.py`) — never delete a user's cache to
  force a schema change; migrations are additive and idempotent (see `development.md`).
- **Slow first engine start** (torch + model preload) can exceed
  `engine_startup_timeout_seconds`; that is not fatal — the engine keeps starting and later
  calls reach it. Don't treat the startup-gap "unavailable" as a real failure.

## Worktree-resilience is live here

This repo has `.autocommit` + is a git work tree, so `rules-worktree-core` (+ merge-lock
addendum A) applies to substantive code changes: isolate work in `.worktrees/<slug>-<ts>/`,
keep the append-only `.kiro/GENERAL-WORK-LOG.jsonl` (union-merged via `.gitattributes`), and
run post-merge verification in a disposable verify worktree — never in the main working copy.
Trivial one-line edits and read-only investigation are exempt.

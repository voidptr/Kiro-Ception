"""FastMCP server for Kiro Ception — MCP tool definitions and entrypoint.

This process is a thin MCP stdio proxy. It spawns and communicates with a
separate engine process that owns the embeddings, indexer, and HTTP API.
All tool calls forward to the engine via HTTP.

Architecture:
- This process: MCP stdio server (asyncio, no heavy deps, fast startup)
- Engine process: HTTP server + indexer + search (separate PID, detached)
"""

import inspect
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import get_config as _get_config
from .config import expand_path, set_config_file
from .engine_client import ensure_engine_running, get_engine_client
from .models import Source


def _apply_config_override_from_argv() -> None:
    """Honour --config before any import-time config read.

    Tool descriptions and the conditional peer tool are decided while this
    module is being imported, but main() does not parse --config until
    afterwards. Without this, an instance started with --config would
    register tools describing the *default* config rather than its own.
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            set_config_file(argv[i + 1])
            return
        if arg.startswith("--config="):
            set_config_file(arg.split("=", 1)[1])
            return


_apply_config_override_from_argv()

# The server name is the product identity and is deliberately constant across
# instances — server.instance_label distinguishes instances in tool
# descriptions, it does not rename the server.
mcp = FastMCP("kiro-ception")


def _instance_tool(fn):
    """Register an MCP tool, naming this instance's sources in its description.

    Several Kiro-Ception instances can run side by side, and each exposes the
    same tool names with the same docstrings. The description is therefore the
    only signal a caller has for choosing between them, so every tool states
    which sources its instance indexes.
    """
    doc = inspect.cleandoc(fn.__doc__ or "")
    summary = _get_config().instance_summary
    return mcp.tool(description=f"{doc}\n\n{summary}")(fn)


# --- Initialization ---

_initialized = False


def _initialize():
    """Ensure the engine process is running and register this process as a follower.

    Spawns the engine subprocess if it's not already running.
    This is fast (<1s) if the engine is already healthy.

    After confirming the engine is healthy, immediately registers this process's
    PID via the EngineClient (which sends X-Follower-PID on every request).
    This ensures the engine knows about us even if no tool call is ever made,
    so it can shut down gracefully when we exit.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True
    ensure_engine_running()

    # Register our PID with the engine immediately so it tracks us as a follower.
    # Without this, the engine's FollowerRegistry stays empty if no tool calls
    # are made before the MCP process exits (e.g., power is uninstalled).
    try:
        client = get_engine_client()
        client.health()
    except Exception:
        pass  # Non-fatal — PID will be registered on the first tool call anyway


def _ensure_initialized():
    """Ensure initialization has run (called at the top of each tool)."""
    if not _initialized:
        _initialize()


def _get_current_workspace() -> str | None:
    """Get current workspace path.

    Priority:
    1. Config file: search.workspace_dir (if set)
    2. Environment: KIRO_WORKSPACE (set by Kiro IDE/CLI)
    3. Environment: CLAUDE_PROJECT_DIR (set by Claude Code)
    4. Current working directory (PWD or cwd)
    """
    config = _get_config()
    if config.search.workspace_dir:
        return str(expand_path(config.search.workspace_dir))
    if val := os.environ.get("KIRO_WORKSPACE"):
        return val
    if val := os.environ.get("CLAUDE_PROJECT_DIR"):
        return val
    return os.environ.get("PWD") or str(Path.cwd())


# --- MCP Tools ---


@_instance_tool
def search_project_history(
    query: str,
    after: str | None = None,
    before: str | None = None,
    context_size: int = 3,
    threshold: float = 0.2,
    max_results: int = 10,
    offset: int = 0,
    include_tool_context: bool = False,
    workspace: str | None = None,
) -> dict:
    """
    Search conversation history for the CURRENT WORKSPACE only.

    Use this to find workspace-specific context: past decisions, implementation
    details, bugs discussed, architecture choices in this codebase.

    Args:
        query: Keywords or sentence describing what to find
        after: Filter to messages on/after this date (ISO 8601: "2025-01-15")
        before: Filter to messages before this date (ISO 8601)
        context_size: Messages to include before AND after each match (default: 3)
        threshold: Minimum similarity 0-1 (default: 0.2)
        max_results: Maximum results to return (default: 10)
        offset: Skip results for pagination (default: 0)
        include_tool_context: Also search tool call summaries (default: false).
                When false, only conversation messages (user prompts and assistant
                responses) are matched. When true, tool context summaries are
                also included as searchable content.
        workspace: Workspace root path to scope results to. If provided, overrides
                automatic workspace detection. Use this when the MCP server's
                working directory differs from the project you're working in
                (e.g., multi-root workspaces).

    Returns:
        Search results with matched messages, scores, context, and pagination info
    """
    _ensure_initialized()
    client = get_engine_client()
    effective_workspace = workspace if workspace else _get_current_workspace()
    return client.search({
        "query": query,
        "workspace": effective_workspace,
        "source": None,
        "after": after,
        "before": before,
        "context_size": context_size,
        "threshold": threshold,
        "max_results": max_results,
        "offset": offset,
        "include_tool_context": include_tool_context,
    })


@_instance_tool
def search_global_history(
    query: str,
    after: str | None = None,
    before: str | None = None,
    context_size: int = 3,
    threshold: float = 0.2,
    max_results: int = 10,
    offset: int = 0,
    source: str = "all",
    include_tool_context: bool = False,
) -> dict:
    """
    Search conversation history across ALL WORKSPACES.

    Use this to find cross-project knowledge: user preferences, coding patterns,
    common solutions, and insights from all previous work.

    Args:
        query: Keywords or sentence describing what to find
        after: Filter to messages on/after this date (ISO 8601: "2025-01-15")
        before: Filter to messages before this date (ISO 8601)
        context_size: Messages to include before AND after each match (default: 3)
        threshold: Minimum similarity 0-1 (default: 0.2)
        max_results: Maximum results to return (default: 10)
        offset: Skip results for pagination (default: 0)
        source: Filter by conversation source. Options: "all" (default), "cli",
                "ide", "claude". Only narrow the source if the user explicitly
                asks to search only Kiro CLI, only Kiro IDE, or only Claude Code
                conversations. Otherwise leave as "all".
        include_tool_context: Also search tool call summaries (default: false).
                When false, only conversation messages (user prompts and assistant
                responses) are matched. When true, tool context summaries are
                also included as searchable content.

    Returns:
        Search results with matched messages, scores, workspace, context, pagination
    """
    _ensure_initialized()
    source_filter = source if source in ("cli", "ide", "claude") else None

    client = get_engine_client()
    return client.search({
        "query": query,
        "workspace": None,
        "source": source_filter,
        "after": after,
        "before": before,
        "context_size": context_size,
        "threshold": threshold,
        "max_results": max_results,
        "offset": offset,
        "include_tool_context": include_tool_context,
    })


@_instance_tool
def get_indexing_status() -> dict:
    """
    Get the current status of the background indexer and search readiness.

    Returns progress, rate, ETA, errors, and whether indexing is active.
    Also reports whether the search index is loaded and ready for queries
    (it can be ready from a prior cache while new indexing is in progress).
    """
    _ensure_initialized()
    client = get_engine_client()
    try:
        return client.get_status()
    except Exception:
        return {"error": "Engine unavailable", "state": "unknown"}


@_instance_tool
def rescan(full: bool = False) -> dict:
    """
    Trigger a rescan for new or changed conversations.

    By default, checks all session files for mtime changes and indexes only
    new/modified sessions. This is fast since unchanged files are skipped.

    Set full=True to ignore mtime caching and re-read all sessions from disk.
    This is useful after changing message filter rules or if the index seems
    stale. Existing embeddings are preserved — only truly new text gets
    re-embedded, so even a full rescan is relatively quick.

    Args:
        full: If True, clear mtime cache and re-read all sessions. If False
              (default), only process files whose mtime has changed.

    Returns:
        Confirmation that rescan was triggered
    """
    _ensure_initialized()
    client = get_engine_client()
    try:
        if full:
            return client.trigger_reindex()
        return client.trigger_rescan()
    except Exception:
        return {"error": "Engine unavailable"}


@_instance_tool
def get_config() -> dict:
    """
    Get the current computed configuration for Kiro Ception.

    Shows the effective configuration including defaults, user overrides,
    and computed values like the embedding backend fingerprint.

    Use this to find out which conversation sources THIS instance indexes
    (the "instance" block), and where its data lives on disk (the "paths"
    block). When several instances run side by side, that is how you tell
    them apart — every instance exposes these same tool names.

    Returns:
        Full configuration details, including instance identity and sources
    """
    _ensure_initialized()
    client = get_engine_client()
    try:
        return client.get_config()
    except Exception:
        # Fallback: return local config info
        config = _get_config()
        return {
            "error": "Engine unavailable — showing local config only",
            "instance": {
                "label": config.resolved_instance_label or None,
                "label_setting": config.server.instance_label or None,
                "summary": config.instance_summary,
                "indexes": config.indexed_sources,
            },
            "embedding": {"backend": config.embedding.backend, "model": config.embedding.model},
            "server": {"engine_port": config.server.engine_port},
        }


@_instance_tool
def reload_config() -> dict:
    """
    Reload configuration from disk and apply safe changes immediately.

    Re-reads ~/.config/kiro-ception/config.toml and applies changes.
    Safe changes (throttle_ms, batch_size, search defaults) take effect immediately.
    Breaking changes (model, backend, dimensions) are detected but require
    rescan(full=True) to take effect.

    Returns:
        List of changes detected, what was applied, and any warnings
    """
    _ensure_initialized()
    client = get_engine_client()
    try:
        return client.reload_config()
    except Exception:
        return {"error": "Engine unavailable"}


# --- Conditional tools (registered at import time based on config) ---

_startup_config = _get_config()
if _startup_config.peers.enabled and _startup_config.peers.debug_tool_enabled:

    @_instance_tool
    def search_peer_history(
        query: str,
        after: str | None = None,
        before: str | None = None,
        context_size: int = 3,
        threshold: float = 0.2,
        max_results: int = 10,
        offset: int = 0,
        source: str = "all",
    ) -> dict:
        """
        Search conversation history on REMOTE PEERS ONLY (excludes local results).

        Debug/advanced tool for querying peer machines directly. Only available
        when peers are enabled and peers.debug_tool_enabled = true in config.

        Args:
            query: Keywords or sentence describing what to find
            after: Filter to messages on/after this date (ISO 8601)
            before: Filter to messages before this date (ISO 8601)
            context_size: Messages to include before AND after each match (default: 3)
            threshold: Minimum similarity 0-1 (default: 0.2)
            max_results: Maximum results to return (default: 10)
            offset: Skip results for pagination (default: 0)
            source: Filter by source — "all" (default), "cli", "ide", or "claude"

        Returns:
            Search results from peer machines only (local index is not searched)
        """
        _ensure_initialized()

        from .peers import fan_out_search

        source_filter = None
        if source in ("cli", "ide", "claude"):
            source_filter = Source(source)

        peer_request = {
            "query": query,
            "workspace": None,
            "source": source_filter.value if source_filter else None,
            "after": after,
            "before": before,
            "context_size": context_size,
            "threshold": threshold,
            "max_results": max_results,
            "offset": offset,
        }

        peer_responses = fan_out_search(peer_request)
        if not peer_responses:
            return {
                "results": [],
                "query": query,
                "total_matches": 0,
                "hint": "No peer responses received. Check peer connectivity with get_config.",
            }

        # Merge all peer responses
        all_results = []
        for resp in peer_responses:
            all_results.extend(resp.get("results", []))

        # Sort by score descending and paginate
        all_results.sort(key=lambda r: r.get("score", 0), reverse=True)
        total = len(all_results)
        paginated = all_results[offset:offset + max_results]
        has_more = offset + len(paginated) < total

        return {
            "results": paginated,
            "query": query,
            "total_matches": total,
            "offset": offset,
            "has_more": has_more,
            "peers_responded": len(peer_responses),
            "hint": f"Showing {offset + 1}-{offset + len(paginated)} of {total} from {len(peer_responses)} peer(s)." if paginated else "No matches from peers.",
        }


def main():
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Kiro Ception MCP server")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config.toml (overrides ~/.config/kiro-ception/config.toml)",
    )
    args = parser.parse_args()

    if args.config:
        from .config import set_config_file
        set_config_file(args.config)

    config = _get_config()
    if not config.server.deferred_init:
        _initialize()
    mcp.run()


if __name__ == "__main__":
    main()

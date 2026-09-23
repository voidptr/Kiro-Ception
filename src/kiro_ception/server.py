"""FastMCP server for Kiro Ception — MCP tool definitions and entrypoint.

This process is a thin MCP stdio proxy. It spawns and communicates with a
separate engine process that owns the embeddings, indexer, and HTTP API.
All tool calls forward to the engine via HTTP.

Architecture:
- This process: MCP stdio server (asyncio, no heavy deps, fast startup)
- Engine process: HTTP server + indexer + search (separate PID, detached)
"""

from mcp.server.fastmcp import FastMCP

from .config import expand_path
from .config import get_config as _get_config
from .engine_client import ensure_engine_running, get_engine_client
from .models import Source

mcp = FastMCP("kiro-ception")

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


def _config_workspace() -> str | None:
    """Return the workspace configured in ``search.workspace_dir``, or ``None``.

    This is the ONLY server-side workspace source, and it is opt-in: it exists
    solely so a user can pin a default scope in their config file. There is
    deliberately no environment-variable or current-working-directory detection
    — the engine is a long-lived process spawned once, so its cwd/env bear no
    relation to whichever workspace the calling agent is actually in, and
    guessing produced confidently-wrong scoping. When this returns ``None`` the
    search runs UNSCOPED (all workspaces). An agent that wants project scope is
    responsible for determining its own workspace root and passing ``workspace``
    explicitly.
    """
    config = _get_config()
    if config.search.workspace_dir:
        return str(expand_path(config.search.workspace_dir))
    return None


# --- MCP Tools ---


@mcp.tool()
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
    Search conversation history scoped to a workspace you specify.

    Use this to find workspace-specific context: past decisions, implementation
    details, bugs discussed, architecture choices in a particular codebase.

    IMPORTANT — you must supply the scope. This tool does NOT auto-detect your
    workspace. It only scopes when you pass `workspace` explicitly (or when the
    user has pinned `search.workspace_dir` in their config). If you don't pass
    `workspace`, the search runs UNSCOPED across ALL workspaces — identical to
    search_global_history. So: figure out your own workspace root and pass it.
    You typically know it from your operating context — the workspace/project
    root shown in your system prompt, environment, or the path of files you're
    working on. Pass that absolute path as `workspace`. Check the response's
    `workspace_resolution` field to confirm the scope that was actually applied.

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
        workspace: Absolute path of the workspace root to scope results to. YOU
                (the calling agent) are responsible for determining this from
                your own operating context and passing it — the tool never
                guesses it for you. When omitted, results are NOT scoped: the
                search covers all workspaces. The one exception is a user-pinned
                `search.workspace_dir` in the config file, which is used as the
                default scope when you omit this. The response's
                `workspace_resolution` field reports the scope actually applied
                (`source`: "explicit" you passed it, "config" from config file,
                or "none" unscoped/all-workspaces).

    Returns:
        Search results with matched messages, scores, context, and pagination info
    """
    _ensure_initialized()
    client = get_engine_client()
    if workspace:
        effective_workspace, resolution_source = workspace, "explicit"
    elif config_ws := _config_workspace():
        effective_workspace, resolution_source = config_ws, "config"
    else:
        effective_workspace, resolution_source = None, "none"
    result = client.search({
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
    # Surface how the workspace was resolved so the scope in effect is visible.
    #   "explicit" — you passed `workspace`
    #   "config"   — picked up from search.workspace_dir in the config file
    #   "none"     — UNSCOPED: searched across ALL workspaces. This tool does NOT
    #                auto-detect your workspace; pass `workspace` for project scope.
    if isinstance(result, dict):
        result["workspace_resolution"] = {
            "workspace": effective_workspace,
            "source": resolution_source,
            "scoped": effective_workspace is not None,
        }
    return result


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def get_config() -> dict:
    """
    Get the current computed configuration for Kiro Ception.

    Shows the effective configuration including defaults, user overrides,
    and computed values like the embedding backend fingerprint.

    Returns:
        Full configuration details
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
            "embedding": {"backend": config.embedding.backend, "model": config.embedding.model},
            "server": {"engine_port": config.server.engine_port},
        }


@mcp.tool()
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

    @mcp.tool()
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

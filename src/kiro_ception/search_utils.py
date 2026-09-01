"""Pure utility functions for search post-processing.

These functions handle deduplication, pagination, context windowing,
date parsing, and response formatting — all without external I/O dependencies.
"""

from collections import defaultdict
from datetime import datetime


def parse_date(value: str | None) -> datetime | None:
    """Parse ISO 8601 date string to datetime.

    Returns None for None input or unparseable strings.
    """
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def deduplicate_results(results: list[dict], context_size: int) -> list[dict]:
    """Deduplicate results with overlapping context windows.

    Within each session, if two matches are within 2*context_size message
    indices of each other, keep only the higher-scoring one. Results are
    returned sorted by score descending.
    """
    if not results:
        return []

    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_session[r["session_id"]].append(r)

    dedup_distance = 2 * context_size
    deduplicated = []

    for session_results in by_session.values():
        session_results.sort(key=lambda x: x["message_index"])
        kept = []
        for r in session_results:
            if not kept:
                kept.append(r)
                continue
            if r["message_index"] - kept[-1]["message_index"] <= dedup_distance:
                if r["score"] > kept[-1]["score"]:
                    kept[-1] = r
            else:
                kept.append(r)
        deduplicated.extend(kept)

    deduplicated.sort(key=lambda x: x["score"], reverse=True)
    return deduplicated


def generate_hint(total: int, offset: int, count: int, max_results: int, has_more: bool) -> str:
    """Generate a human-readable pagination hint string."""
    if total == 0:
        return "No matches found. Try different search terms or lower the threshold."
    start, end = offset + 1, offset + count
    if has_more:
        return f"Showing {start}-{end} of {total}. Use offset: {offset + max_results} for more."
    if start == 1:
        return f"Showing all {total} matches."
    return f"Showing {start}-{end} of {total} (final page)."


def truncate_content(text: str, max_length: int = 2000) -> str:
    """Truncate text to max_length, appending '...' if truncated."""
    return text if len(text) <= max_length else text[:max_length] + "..."


def build_context_window(
    session_messages: list[dict],
    match_uuid: str,
    context_size: int,
) -> list[dict]:
    """Build the context window around a matched message.

    Args:
        session_messages: All messages in the session, ordered by message_index.
            Each dict must have keys: uuid, role, searchable_text, timestamp.
            May include content_tier field (defaults to "conversation").
        match_uuid: The UUID of the matched message.
        context_size: Number of messages before and after the match to include.
            Both conversation and tool_context messages count toward this limit.

    Returns:
        List of context message dicts with role, content, content_tier,
        timestamp, is_match. Messages are interleaved by message_index order.

    Overflow logic:
        When the window exceeds 20 messages, drop oldest tool_context messages
        first while retaining all conversation messages.
    """
    if not session_messages:
        return []

    match_pos = None
    for i, msg in enumerate(session_messages):
        if msg["uuid"] == match_uuid:
            match_pos = i
            break

    if match_pos is None:
        return []

    start = max(0, match_pos - context_size)
    end = min(len(session_messages), match_pos + context_size + 1)

    window_messages = session_messages[start:end]

    # Apply overflow logic: when window exceeds 20 messages,
    # drop oldest tool_context first, retain all conversation messages
    if len(window_messages) > 20:
        window_messages = _apply_overflow_logic(window_messages, match_uuid)

    context = []
    for msg in window_messages:
        text = msg["searchable_text"]
        content_tier = msg.get("content_tier", "conversation")
        context.append({
            "uuid": msg["uuid"],
            "role": msg["role"],
            "content": truncate_content(text),
            "content_tier": content_tier,
            "timestamp": datetime.fromtimestamp(msg["timestamp"]).isoformat(),
            "is_match": msg["uuid"] == match_uuid,
        })
    return context


def _apply_overflow_logic(window_messages: list[dict], match_uuid: str) -> list[dict]:
    """Apply overflow logic to trim window to 20 messages.

    Drops oldest tool_context messages first while retaining all
    conversation messages. The matched message is never dropped.

    Args:
        window_messages: Messages in the window, ordered by message_index.
        match_uuid: UUID of the matched message (never dropped).

    Returns:
        Trimmed list of messages, still ordered by message_index.
    """
    max_window = 20

    if len(window_messages) <= max_window:
        return window_messages

    # Separate messages into:
    # - protected: conversation messages + the matched message (never dropped)
    # - droppable: tool_context messages that are NOT the match
    protected_msgs = []
    droppable_tool_msgs = []

    for msg in window_messages:
        tier = msg.get("content_tier", "conversation")
        if tier == "tool_context" and msg["uuid"] != match_uuid:
            droppable_tool_msgs.append(msg)
        else:
            protected_msgs.append(msg)

    # Calculate how many tool_context messages we need to drop
    num_to_drop = len(window_messages) - max_window
    # Drop oldest tool_context first (they're already in message_index order)
    if num_to_drop < len(droppable_tool_msgs):
        kept_tool_msgs = droppable_tool_msgs[num_to_drop:]
    else:
        # Not enough droppable tool_context — keep all protected messages
        # (window may still exceed 20 if there are >20 protected messages)
        kept_tool_msgs = []

    result = protected_msgs + kept_tool_msgs
    # Re-sort by original message_index order
    result.sort(key=lambda m: m.get("message_index", 0))

    return result


def format_search_response(
    scored_results: list[dict],
    query: str,
    offset: int,
    max_results: int,
    context_size: int,
    get_session_messages: callable,
) -> dict:
    """Format raw scored results into the final search response dict.

    Args:
        scored_results: Already-deduplicated results sorted by score.
        query: The original search query string.
        offset: Pagination offset.
        max_results: Maximum results to return.
        context_size: Messages before/after each match for context.
        get_session_messages: Callable(session_id) -> list[dict] that
            returns messages for a session (for context window assembly).

    Returns:
        Complete response dict with results, pagination info, and hint.
    """
    total = len(scored_results)
    paginated = scored_results[offset:offset + max_results]

    results = []
    for match in paginated:
        session_msgs = get_session_messages(match["session_id"])
        context = build_context_window(session_msgs, match["uuid"], context_size)

        results.append({
            "matched_message": {
                "role": match["role"],
                "content": match["content"],
                "timestamp": datetime.fromtimestamp(match["timestamp"]).isoformat(),
                "workspace": match["workspace"],
                "session_id": match["session_id"],
                "uuid": match["uuid"],
                "source": match["source"],
            },
            "score": round(match["score"], 4),
            "context": context,
        })

    has_more = offset + len(results) < total
    hint = generate_hint(total, offset, len(results), max_results, has_more)

    return {
        "results": results,
        "query": query,
        "total_matches": total,
        "offset": offset,
        "has_more": has_more,
        "hint": hint,
    }

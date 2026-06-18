#!/usr/bin/env python3
"""Search only remote peers (bypasses local index).

A development/testing helper for verifying peer federation is working.
Sends encrypted search requests directly to configured peers and displays
results without mixing in local index matches.

Usage:
    uv run python3 scripts/search-via-peers.py portable-ruby and other text here

Prerequisites:
    - Peers configured in ~/.config/kiro-ception/config.toml ([peers] section)
    - Remote peer engine(s) running and reachable on the configured port
    - Same secret configured on both sides (if encryption is enabled)

Output:
    For each configured peer, shows connection status and search results
    with scores, roles, content previews, and workspace paths.
"""

import os
import sys

# Allow running from repo root or scripts/ directory
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(repo_root, "src"))

from kiro_ception.peers import get_peer_config, _send_to_peer


def main():
    if len(sys.argv) < 2:
        print("Usage: scripts/search-via-peers.py <query>")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    cfg = get_peer_config()

    print(f"Query: {query}")
    print(f"Peers enabled: {cfg['enabled']}")
    print(f"Nodes: {cfg['nodes']}")
    print(f"Encryption: {'yes' if cfg['key'] else 'no'}")
    print()

    if not cfg["enabled"] or not cfg["nodes"]:
        print("No peers configured.")
        sys.exit(1)

    for node in cfg["nodes"]:
        print(f"--- {node} ---")
        result = _send_to_peer(
            node,
            "/search",
            {"query": query, "max_results": 5, "threshold": 0.15},
            cfg["key"],
            timeout=10,
        )

        if result is None:
            print("  ❌ No response (unreachable, timeout, or decryption error)")
        elif not result.get("results"):
            print(f"  ⚠️  Connected but no results (total_matches: {result.get('total_matches', 0)})")
        else:
            count = len(result["results"])
            total = result.get("total_matches", count)
            print(f"  ✅ {count} results (of {total} total)")
            for r in result["results"]:
                score = r.get("score", 0)
                msg = r.get("matched_message", {})
                content = msg.get("content", "")[:150]
                role = msg.get("role", "?")
                workspace = msg.get("workspace", "?")
                print(f"    [{score:.3f}] ({role}) {content}")
                print(f"           workspace: {workspace}")
        print()


if __name__ == "__main__":
    main()

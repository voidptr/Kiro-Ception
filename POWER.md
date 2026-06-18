---
name: "kiro-ception"
version: "1.0.8"
displayName: "Kiro Ception"
description: "Access all Kiro CLI and IDE conversation history as searchable memory - recall past discussions by meaning, actions, responses, and date"
keywords: ["history", "recall", "remember", "remind", "remind me", "past conversation", "previous chat", "what did we discuss", "how did we", "like we discussed", "as I mentioned", "what did we", "what were those", "we implemented", "we built", "we did", "we added", "we changed", "we fixed", "we decided", "previously", "last time", "earlier", "before", "that thing we", "what was that", "what approach", "how did I", "where did we leave off", "yesterday", "last week", "recently", "I don't remember", "I forget", "didn't we already", "same as we did", "why did we choose", "what was the reasoning", "what was the solution"]
author: "Farley Farley via DevOps-Nirvana (originally by Danilo Poccia)"
---

# Kiro Ception

Total recall for Kiro — persistent memory that spans every session, every window, CLI and IDE, and even across multiple machines. Automatically indexes all conversation history in the background and provides instant hybrid search (semantic + keyword) so the agent can find past discussions by meaning, keywords, date, or any combination.

## Important: Background Indexing

Kiro Ception indexes conversations in the background. On first launch or after configuration changes, indexing may take a few minutes. Search results will be available immediately for previously-indexed conversations, and new conversations appear as indexing progresses.

Use `get_indexing_status` to check if indexing is complete or still in progress.

## When to Trigger

### 1. Recovering Context
When the user references something discussed earlier:
- "Continue with the approach we discussed"
- "Like I mentioned before..."
- "What did we decide about..."
- "As we talked about earlier..."

```
User: "Continue implementing the auth system like we discussed"
→ search_project_history(query="auth system implementation")
```

### 2. Cross-Session Memory
Find discussions from previous sessions:
- "How did we fix that bug yesterday?"
- "What approach did we decide on last week?"
- "Remember when we refactored the database?"
- "What did we do last time?"

```
User: "How did we fix that auth bug last week?"
→ search_project_history(query="auth bug fix", after="2026-05-28")
```

### 3. Cross-Project Patterns
Find user preferences across all projects:
- "How do I usually handle errors?"
- "What's my preferred testing approach?"
- "What package manager do I use?"
- "Show me how we typically set up..."

```
User: "How do I usually structure React components?"
→ search_global_history(query="React component structure")
```

### 4. Recalling Past Work
When the user asks what was previously built, implemented, or changed:
- "Remind me what we did for..."
- "What features did we implement for X?"
- "We built something for Y, what was it?"
- "What changes did we make to..."
- "What was that approach we used for..."
- "We added something to handle Z..."

```
User: "Remind me for incidentfox what reliability features we implemented"
→ search_global_history(query="incidentfox reliability features")
```

### 5. Past Decisions and Reasoning
When the user asks about why something was done a certain way:
- "Why did we choose X over Y?"
- "What was the reasoning behind..."
- "Didn't we already handle this?"
- "We had a similar issue before"
- "What was the solution for..."

```
User: "Why did we choose Go over .NET for that service?"
→ search_global_history(query="Go vs .NET decision reasoning")
```

### 6. Unfinished or Continuing Work
When the user references where they left off:
- "Where did we leave off?"
- "What was the last thing we did on this?"
- "I was working on something yesterday..."
- "Can we continue from where we stopped?"

```
User: "Where did we leave off with the payment integration?"
→ search_project_history(query="payment integration", after="2026-06-01")
```

### 7. Implicit Past References
When the user mentions a project or feature implying past context exists:
- "For incidentfox we implemented..."
- "In the webui project we..."
- "Same as we did for the customer-key-service"
- "Like we did in the other project"

```
User: "For incidentfox I think we implemented some reliability features, what were those?"
→ search_global_history(query="incidentfox reliability features implementation")
```

### 8. Forgotten Details
When the user explicitly says they don't remember:
- "I don't remember how we..."
- "I forget what we did for..."
- "What was that thing we..."
- "There was something about..."

```
User: "I forget how we set up the CI pipeline for that service"
→ search_global_history(query="CI pipeline setup configuration")
```

### 9. Monitoring & Troubleshooting
Check indexing progress or debug configuration:
- "Is the search index ready?"
- "What embedding model is configured?"
- "Pick up my latest conversations"
- "Reload the config"
- "Re-index everything from scratch"

```
User: "Is Kiro Ception fully indexed?"
→ get_indexing_status()

User: "Pick up my recent conversations"
→ rescan()

User: "I changed the config, reload it"
→ reload_config()

User: "Re-index everything from scratch"
→ rescan(full=True)
```

## Available Tools

### Search Tools

| Tool | Scope | Use Case |
|------|-------|----------|
| `search_project_history` | Current workspace | Bugs, decisions, implementations in *this* codebase |
| `search_global_history` | All workspaces | User preferences, patterns across *all* work. Optional `source` param: `"all"` (default), `"cli"`, or `"ide"` |

### Management Tools

| Tool | Use Case |
|------|----------|
| `get_indexing_status` | Check indexing progress, rate, ETA, errors |
| `rescan` | Pick up new/changed conversations. Use `full=True` to re-read all sessions (ignores mtime cache) |
| `reload_config` | Re-read config file and apply safe changes without restart |
| `get_config` | Show effective configuration, file paths, instance role, cache stats |

## Parameters

All search tools accept:
- `query` (required): Keywords or sentence to search
- `after` (optional): Filter to messages on/after this date (ISO 8601)
- `before` (optional): Filter to messages before this date (ISO 8601)
- `context_size` (default: 3): Messages before/after each match
- `threshold` (default: 0.2): Minimum similarity (0-1)
- `max_results` (default: 10): Results to return
- `offset` (default: 0): Skip results for pagination

Additionally, `search_global_history` accepts:
- `source` (default: "all"): Filter by source — `"all"`, `"cli"`, or `"ide"`. Only use `"cli"` or `"ide"` if the user explicitly asks to search only CLI or IDE conversations.

## Date Filtering Examples

**Single day:**
```
search_project_history(query="auth", after="2026-06-01", before="2026-06-02")
```

**Past week:**
```
search_project_history(query="database", after="2026-05-28")
```

**Recently (last few days):**
```
search_global_history(query="deployment issue", after="2026-06-03")
```

## Tips

- Results include **context** (surrounding messages)
- Use `offset` to paginate when `has_more` is true
- Higher scores (closer to 1.0) = better semantic matches
- The `source` field shows whether a result is from CLI or IDE
- If results seem incomplete, check `get_indexing_status` — indexing may still be in progress
- Use `rescan()` to immediately pick up recent conversations
- Use `rescan(full=True)` after changing message filter rules
- Use `reload_config` after editing the config file
- For project-specific questions, prefer `search_project_history` (narrows to current workspace)
- For cross-project or preference questions, use `search_global_history`
- When the user mentions a specific project name, include it in the query
- If the database is corrupt or search returns garbage, the user can reset with: `rm -rf ~/.cache/kiro-ception/` then restart Kiro

## License and Support

This power is licensed under the [MIT License](https://github.com/DevOps-Nirvana/Kiro-Ception/blob/main/LICENSE).

**Privacy:** All data is processed and stored locally on your machine. No telemetry, no external API calls, and no data leaves your device — unless you explicitly configure a third-party embedding provider (e.g., OpenAI). The default configuration uses fully local, offline embeddings.

- [Privacy & Data Locations](https://github.com/DevOps-Nirvana/Kiro-Ception#data-locations)
- [Support & Issues](https://github.com/DevOps-Nirvana/Kiro-Ception/issues)

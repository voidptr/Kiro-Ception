---
inclusion: always
description: "Git workflow for this fork: the three-location model (upstream / fork main mirror / rearview trunk), the two-track rule (rearview = integrated superset that must hold ALL work and never blocks; pr/* = clean single-feature branches cut off main and PR'd to upstream), and the content-level merge-tree test for 'is this work already in rearview' — because commit-graph/ancestry/diff heuristics MISREPORT here. Read this before branching, merging, or auditing what rearview contains."
---

# Git Workflow — Fork Topology & The Two Tracks

This fork does **not** use a plain feature-branch-into-main flow. Getting this wrong has
cost real sessions ~30 minutes of thrashing (false "8 branches are missing from rearview"
alarms). Read this before creating a branch, merging, or judging whether some work is
already in `rearview`.

## The three git locations

| Location | Ref | Role |
|---|---|---|
| **upstream** | `upstream/main` (`DevOps-Nirvana/Kiro-Ception`) | The real project. Destination for *contribution-worthy slices only*. |
| **fork main** | `fork/main` (`voidptr/Kiro-Ception`), local `main` tracks it | A **clean mirror of upstream/main**. Never receives our work directly — it only ever follows upstream. It is the **clean base** we cut PR branches from. |
| **rearview** | `fork/rearview`, local `rearview` | **Our integrated trunk.** Where we actually develop and run. The **superset**: every feature + personal-only stuff (steering, configs, dev scripts) that must NEVER go upstream. |

`fork/main == upstream/main` at rest — it is a mirror, not a work line. Do not commit
feature work onto it. It advances only by following upstream (fast-forward after an upstream
merge).

## The two tracks (they do not block each other)

**Track 1 — rearview (primary; must always be complete).**
All new work lands on `rearview` so the trunk has everything and stays runnable. rearview
**never waits** on upstream. This is the source of truth for "what we actually have."

**Track 2 — upstream sharing (secondary; non-blocking).**
For each contribution-worthy feature, cut a clean **`pr/<feature>`** branch **off `main`**
(NOT off rearview — so it carries none of our personal stuff), push to `fork`, open a PR
`fork:pr/<feature>` → `upstream:main`. Whether/when DevOps-Nirvana merges is *their* call and
never gates rearview.

```
                upstream/main  ──────────────────────────►  (maintainer merges our PRs here)
                     │  (mirror)                                        ▲
                     ▼                                                  │ PR fork→upstream
   local main ═══ fork/main ──┬── cut pr/<feature> off main ──► push to fork ──┘
                              │        (one feature, no personal stuff)
                              │
                              └── (rearview is NOT cut from here per-feature; it is the
                                   long-lived integrated trunk that ACCUMULATES every
                                   feature's CONTENT + personal-only files)
   fork/rearview ══ our trunk: superset of all features + steering/configs/dev-scripts
```

### Do / Don't

- **DO** cut `pr/*` branches off `main` (clean base), one feature each.
- **DO** land everything on `rearview`; keep it the complete, runnable superset.
- **DON'T** cut a `pr/*` branch off `rearview` — it would drag personal-only files (steering
  modules, `config.claude-rearview.toml`, `scripts/dev-engine.*`, etc.) into an upstream PR.
- **DON'T** merge feature branches into `fork/main` yourself — main only follows upstream.
- **DON'T** let an unmerged upstream PR block rearview work — the tracks are independent.

## "Is this work already in rearview?" — the CONTENT test (critical)

rearview integrates features by **re-applying / cherry-picking content**, not by merging the
`pr/*` branch tips. Consequence: **a `pr/*` branch is almost never an ancestor of rearview
even when its full content is present.** Every commit-graph heuristic therefore MISREPORTS:

| Heuristic | Why it LIES here |
|---|---|
| `git rev-list --count rearview..pr/X` | Counts commits rearview lacks by SHA. rearview has the *content* under different (squashed/re-applied) SHAs → reports "ahead" though nothing is missing. |
| `git cherry` / patch-id | Patch-ids differ after squash/rebase/hand-integration → false "unapplied". |
| `git merge-base --is-ancestor pr/X rearview` | Tips are never ancestors (content re-applied) → always "not ancestor" even when fully contained. |
| `git diff rearview...pr/X` | rearview is the **superset** (feature + other features + personal edits), so shared files (server.py, config.py, engine_main.py) always differ → "DIFF" means "rearview has MORE", not "rearview is missing this". |

**The only trustworthy test is a real 3-way content merge and a tree diff:**

```bash
# For branch pr/X, does merging it into rearview change rearview's tree at all?
merged=$(git merge-tree --write-tree fork/rearview fork/pr/X | head -1)
git diff --stat fork/rearview "$merged"
#   EMPTY output  → pr/X is FULLY CONTAINED in rearview (adds nothing). Done.
#   Non-empty     → shows exactly what pr/X would ADD; inspect those hunks.
```

Notes:
- `git merge-tree --write-tree` (Git ≥ 2.38) does an in-memory 3-way merge and prints the
  merged tree OID on line 1. Requires no checkout and ignores the dirty working tree.
- **Do NOT compare the merged tree OID by string** to `rev-parse rearview^{tree}` — formatting/
  newline differences make `-eq` fail even when identical. Use `git diff --stat` (empty = same).
- A **merge-tree exit code 1 = a merge CONFLICT, not "missing"**: rearview and the branch both
  changed the same lines relative to base. The content can still be fully present — verify by
  reading rearview's blob for the feature's signature lines
  (`git show fork/rearview:<file>` and grep for the added code).
- Substring/line-containment scripts are fragile (crash on `[`/glob chars; test the wrong tree
  if run against a dirty checkout). Prefer `merge-tree`.

## Working discipline for changes to this repo

This repo has `.autocommit` + is a git worktree, so **`rules-worktree-core` (+ merge-lock
addendum A) apply** to substantive changes (including steering/docs): isolate in
`.worktrees/<slug>-<ts>/`, keep the append-only `.kiro/GENERAL-WORK-LOG.jsonl`, hold the
merge-lock only around `git merge --no-ff`, verify in a disposable verify worktree. Trivial
one-line edits and read-only investigation are exempt. This toolbag **never pushes**
automatically; pushing to `fork` and opening upstream PRs are explicit, developer-gated steps
(safety-core N2/N25).

## Quick recipe: landing a new change

1. **rearview first:** do the work (in a worktree), merge onto `rearview`. rearview now has it.
2. **Share upstream (optional, non-blocking):** `git switch -c pr/<feature> main`, apply just
   that feature's clean hunks (no personal files), push to `fork`, open PR → `upstream:main`.
3. Verify rearview containment any time with the `merge-tree` test above — never the graph
   heuristics.

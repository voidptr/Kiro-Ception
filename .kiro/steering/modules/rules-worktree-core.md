---
inclusion: always
description: "Worktree-resilience CORE — git-worktree isolation + append-only work log + resume + checkpoint discipline for any substantive code change in a repo. The base of the rules-worktree module family; addenda (merge-lock, execution-log, independent-review) layer on top. Installed into code/general workbenches; dormant in the toolbag."
---

<!-- kiro-module: rules-worktree-core
     source: Utilities/.kiro/steering/modules/rules-worktree-core.md
     version: 8eaefb25df24
     installed: 2026-09-05
     NOTE: copied by workbench-init. Do NOT hand-edit — edit the source module in
     Utilities and re-run workbench-update. Local edits will be flagged.
     INCLUSION: source is `inclusion: manual`; this installed copy is rewritten to
     `inclusion: always` because this IS a code workbench. Installed with addendum A
     (merge-lock). workbench-update must preserve this rewrite — it is not drift. -->

## Worktree-Resilience: Core

Two failure modes motivate this pattern, for any substantive code change in a repo:

1. **Working-directory collisions between concurrent sessions.** If the repo is open in
   more than one Kiro window (or worked by more than one person) at once, two sessions
   editing the same working copy interleave with no isolation. Build-tool lock contention
   (two `dotnet build`/`npm run build` fighting over one output dir) is a special case.
2. **Unrecoverable kills.** A session that dies mid-change leaves no durable record of what
   was in flight in a shared working copy.

The fix is two mechanisms together: **git worktrees** (each unit of work gets its own
directory + branch sharing the one `.git`, so collisions are impossible by construction),
and an **append-only work log** (JSONL lifecycle events, so a dead session's in-flight
state is recoverable).

**Detection — this applies to a repo if:** the repo root (via `git rev-parse
--show-toplevel`) contains a `.autocommit` file AND is a git working tree
(`git rev-parse --is-inside-work-tree`). Roots only, never subfolders — resolve the true
root first if the opened folder isn't it.

**Scope — code.** Substantive changes to tracked content (code, config, in-repo docs,
steering, specs). NOT read-only investigation or trivial one-line edits — use judgment; the
overhead isn't justified for something that small.

---

## Part 1: Worktree Scheme

### Layout

```
<repo-root>/                 <- main working copy, stays on its normal branch
<repo-root>/.worktrees/      <- gitignored, holds per-unit-of-work worktrees
<repo-root>/.worktrees/<slug>-<yyyyMMdd-HHmmss>/   <- one worktree per unit of work
```

- Branch naming: `wt/general/<slug>-<yyyyMMdd-HHmmss>` (a spec task uses addendum B's
  `wt/<spec-slug>/<task-id>` scheme instead).
- Base: branch from the current tip of whatever branch the session started on.

### Creating a worktree

```
git worktree add .worktrees/<slug>-<timestamp> -b wt/general/<slug>-<timestamp> <base-branch-or-sha>
```

Log a `WORK_STARTED` event immediately after this succeeds (Part 2), then write an owner
marker to `.worktrees/.owners/<slug>-<timestamp>.json`:

```json
{"work_id":"<slug>-<timestamp>","session_id":"<sess-yyyyMMdd-HHmm>","created_ts":"<ISO 8601>","branch":"wt/general/<slug>-<timestamp>","worktree":".worktrees/<slug>-<timestamp>"}
```

`session_id` is derived once per session as `sess-<yyyyMMdd-HHmm>` and reused on every log
line that session. The marker lets a later session answer "is this mine?" by reading a
value instead of judging by appearance. Keep it under `.worktrees/.owners/` (inside the
gitignored `.worktrees/`, so it never dirties a checkout's `git status`). Delete it
alongside `WORKTREE_REMOVED`.

Do all reads/writes/edits for this unit of work relative to the worktree path. Build/test
inside the worktree — it has its own output dir, so no lock contention.

**⚠️ The shell's working directory persists between commands, and one stray `cd` silently
redirects every later relative path** — with no error, just a plausible wrong answer. Use
absolute paths or `git -C <path>`, and re-assert the worktree root per command. Prefer the
writing tool's own success signal over re-reading a relative path to verify an edit.

### Merging back

1. Log `MERGE_STARTED`. (If addendum A `merge-lock` is installed, acquire the lock first —
   see that file; it governs the branch ref, complementary to `merge=union` on the log.)
2. From the main working copy: `git merge --no-ff wt/general/<slug>-<timestamp>`.
3. **Clean:** log `MERGE_COMPLETED` with the merge commit SHA. Run verification build+test
   in a disposable verification worktree (Part 1a), never in the main working copy.
4. **Conflict:** log `MERGE_CONFLICT` with the file list. Resolve deliberately — surface it,
   don't auto-resolve.
5. Remove the worktree(s) after a successful merge (and verify, if run):
   `git worktree remove .worktrees/<slug>-<timestamp>`. Log `WORKTREE_REMOVED`; delete the
   owner marker only after confirming removal succeeded (check the exit code — don't infer
   from absent output). Delete the branch too unless a historical trail is wanted.
   - **Removal failing on a lock** (`Permission denied`) is common. Do NOT force it: forced
     or recursive deletion is developer-gated (safety-core NEVER #25). Two causes: (a) your
     shell cwd was inside → git deletes files, fails the final `rmdir`, leaves an **empty**
     dir → `rmdir` from outside; (b) a `node_modules`/`bin`/`obj` handle → **non-empty** dir →
     recursive delete, which is gated. **On your own confirmed-merged worktree use `--force`**
     — plain `remove` can hang on an interactive prompt. Prevent (a): never `cd` into a
     worktree; use `git -C` + absolute paths. If removal fails, KEEP the marker and rewrite
     it with the failure + the merge commit + the recovery step (deleting the marker before
     confirming removal creates the no-ownership-record condition NEVER #24 warns about).
6. **On failure/abandonment:** do not remove the worktree. Log `WORK_ABANDONED` with a
   reason — the worktree/branch is the evidence trail.

---

## Part 1a: Verification Builds Are Isolated Too

The whole point is that collisions are impossible by construction (separate directories). A
post-merge verification build run in the main working copy reintroduces exactly that
collision. **Run any post-merge verification build/test in its own disposable worktree
checked out at the merge commit — never in the main working copy.**

```
git worktree add .worktrees/verify-<slug>-<timestamp> <merge_commit>
```

Run the repo's build/test there, then remove it whether it passed or failed (the log event +
build output capture the failure). Log `VERIFY_STARTED` before and `VERIFY_COMPLETED`
(`build`/`test` PASS/FAIL) after.

The true git-level merge race (two sessions `git merge` the same branch at once) is NOT
covered here — isolating the *build* doesn't address the ref-update race. Addendum A
(`merge-lock`) mitigates that; if A is not installed, flag the race if observed.

---

## Part 2: The Work Log

### Location

`.kiro/GENERAL-WORK-LOG.jsonl` at the repo root. Append-only — **always append, never
rewrite, never shell-redirect.** A crash mid-append corrupts at most the last line; a crash
mid-rewrite can corrupt the whole history.

**The log lives in the worktree and is committed with the work.** A `.gitattributes` rule
(`.kiro/GENERAL-WORK-LOG.jsonl merge=union`) makes concurrent appends from different
worktrees merge without conflict — git keeps all lines from both sides. Caveat: union merge
concatenates but does not sort, so lines may interleave slightly out of time order at a merge
seam — readers key on each line's `ts`, not line order, so it is cosmetic. (Rare edge: if a
worktree is discarded before merging, log lines written only there are lost with it — write a
`WORK_ABANDONED` from the main copy if abandoning deliberately.)

### Format

One JSON object per line. Required: `ts` (ISO 8601), `event`, `work_id` (the slug-timestamp
used in the worktree/branch name), and `session_id`.

| Event | When | Extra fields |
|---|---|---|
| `WORK_STARTED` | Worktree+branch created | `worktree`, `branch`, `base_commit`, `description` |
| `PROGRESS_NOTE` | A sub-step finished, short of a full checkpoint commit | `note`, `files` (optional) |
| `CHECKPOINT_COMMIT` | An incremental commit was made | `commit`, `note` |
| `MERGE_STARTED` | Before `git merge` | `target_branch` |
| `MERGE_COMPLETED` | Merge succeeded | `merge_commit` |
| `MERGE_CONFLICT` | Merge failed | `files` (array) |
| `VERIFY_STARTED` | Before creating the verify worktree (Part 1a), if run | `verify_worktree`, `merge_commit` |
| `VERIFY_COMPLETED` | Verification build+test finished | `build` (PASS/FAIL), `test` (PASS/FAIL) |
| `WORKTREE_REMOVED` | After successful cleanup | `kind` (`work`/`verify`) |
| `WORK_COMPLETED` | Fully done, merged, cleaned up | — |
| `WORK_ABANDONED` | Given up on / interrupted without recovery | `reason` |

(Addenda add events: merge-lock adds `MERGE_LOCK_*`; execution-log adds `TASK_*`;
independent-review adds `REVIEW_*`.)

### Why `PROGRESS_NOTE` exists

`CHECKPOINT_COMMIT` fires only when a logical unit is complete, leaving a blind spot if the
work crashes partway through a unit. `PROGRESS_NOTE` is a cheap, no-git-operation log line
written after any meaningful sub-step (a sub-part done, about to start a sub-part, before
running something that might fail/hang). Breadcrumbs between commits — so a mid-unit crash
leaves a narrative, not a bare uncommitted diff.

### Committing the log is MANDATORY

Writing survives the process dying; committing survives everything else. Commit it after any
terminal event (`WORK_COMPLETED`/`WORK_ABANDONED`), after `MERGE_COMPLETED`, and **before
yielding any turn in which you appended to it**. Because the log lives in-worktree with
`merge=union`, it rides the worktree's own commits — another session's lines merging
alongside yours is expected and correct (every line carries `session_id`); never edit an
append-only file to separate them.

### Resume procedure

At session start, read `.kiro/GENERAL-WORK-LOG.jsonl`. Classify each `work_id` by last event:
**clean-completed** (`WORK_COMPLETED`, or `MERGE_COMPLETED`+`WORKTREE_REMOVED`) → nothing to
do; **abandoned** (`WORK_ABANDONED`) → needs a developer decision, do not silently retry;
**dangling** (`WORK_STARTED` with no terminal after it, or `MERGE_COMPLETED`/`VERIFY_STARTED`
with no `VERIFY_COMPLETED`/`WORK_COMPLETED` after) → resumption case. (If addendum C is
installed, also dangling: a `REVIEW_SUBAGENT_FAILED` or `REVIEW_FALLBACK_TASK_CREATED` with no
later `REVIEW_COMPLETED`/`REVIEW_DEFERRED` — the unit is blocked pending an independent review,
not done; re-run the independent review before trusting it.)

**Partition dangling `work_id`s by ownership before touching anything.** A `work_id` is yours
only if it belongs to work you were asked to do this session — read it from the `session_id`
on its `WORK_STARTED` line and the `.worktrees/.owners/<work_id>.json` marker; absent or
mismatched = not yours. A worktree that *looks* like debris (zero commits, only build-cache
churn, old base) is NOT thereby yours (safety-core NEVER #24).

- **Yours + worktree exists:** read every `PROGRESS_NOTE`/`CHECKPOINT_COMMIT` for it in order,
  read the worktree's `git log`/diff vs `base_commit`, cross-check (a note saying "about to
  wire X into Y" with Y untouched = crash before that step = not-yet-done). Verify against
  intent before continuing — never trust partial work just because it compiles.
- **Yours + worktree gone:** treat as abandoned, surface it.
- **Not yours:** report state only and stop — do not remove/prune/commit/append under it, and
  do not propose a disposition (offering to clean it up is the overstep NEVER #24 names).

**Always report findings to the developer before resuming** — never silently resume. Split
the report: **yours** (state + the action you'll take) and **observed, not yours** (state
only).

---

## Part 3: Checkpoint Commit Discipline

Commit incrementally as logical units complete — one commit per logically complete unit (a
function + its test, a config change + the code that consumes it), not per-file and not
only-at-the-end.

**Every checkpoint commit carries a subject AND a body.** `git commit -m "<subject>" -m
"<body>"`. Subject: imperative, ≤72 chars, no trailing period, names the change not the file
(never `wip`/`progress`/`misc`/a bare filename). Body: required — what changed (each file/area),
why, ripple, what was verified (or explicit "not verified"). These commits exist so a
*different* session with no memory of this one can reconstruct what happened; a subject-only
checkpoint hands it a bare diff. If a change truly has nothing past the subject, say so in the
body.

Checkpoints alone are too coarse to survive a mid-unit crash — that is what `PROGRESS_NOTE`
(Part 2) covers. Rough guide: if you'd be upset to lose more than a few minutes to a crash,
write a progress note before the next sub-step.

---

## Constraints Recap

- Detection: repo root has `.autocommit` + is a git working tree. Roots only.
- Worktrees under `.worktrees/` (gitignored); owner marker at `.worktrees/.owners/<id>.json`.
- One worktree + branch per nontrivial unit; the log lives in-worktree with `merge=union`.
- Every commit: subject AND body. The log is append-only, committed before yielding any turn
  you appended in.
- `PROGRESS_NOTE` between checkpoints so a mid-unit crash leaves a narrative.
- Resume detection is automatic, ownership-partitioned, and always reported — never silent.
  Dangling work that isn't yours is reported and left alone (NEVER #24).
- Partial work in a dangling worktree that IS yours is never trusted by default.
- Post-merge verification runs in a disposable `.worktrees/verify-*` worktree (Part 1a).
- Forced/recursive deletion and shared-ref mutation are developer-gated (NEVER #25).
- Applies to code/tracked-content changes — not read-only or trivial one-line edits.

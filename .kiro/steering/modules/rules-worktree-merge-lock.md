---
inclusion: always
description: "Worktree-resilience ADDENDUM A — the merge-lock protocol: serialize merges into a shared branch via an atomic exclusive-create lock file, released the instant the merge returns, with steal-and-verify reclaim of expired locks. Default-on addendum to rules-worktree-core; opt out only for single-window workbenches."
---

<!-- kiro-module: rules-worktree-merge-lock
     source: Utilities/.kiro/steering/modules/rules-worktree-merge-lock.md
     version: a51b254f8187
     installed: 2026-09-05
     NOTE: copied by workbench-init. Do NOT hand-edit — edit the source module in
     Utilities and re-run workbench-update. Local edits will be flagged.
     INCLUSION: source is `inclusion: manual`; this installed copy is rewritten to
     `inclusion: always`. Addendum A of the rules-worktree family — layers on
     rules-worktree-core (also installed here). workbench-update must preserve the rewrite. -->

## Worktree-Resilience Addendum A: The Merge-Lock Protocol

**Why.** Worktrees make file/build collisions impossible by construction — separate
directories. They do NOT serialize the merge itself: every worktree updates the *same*
shared branch ref, so two sessions running `git merge` into that branch at the same moment is
a genuine ref-update race isolation cannot prevent. This closes it with a lock whose critical
section is **the merge step alone**. It composes with core's `merge=union` on the log — that
governs the log *file*; this governs the branch *ref*.

### The critical section is the merge, and nothing else

**Hold the lock only around `git merge --no-ff` and its ref update.** Acquire → merge →
release. Build-verification (core Part 1a), review, and any checkbox flip happen AFTER release,
outside the lock. This keeps the TTL short (a merge is seconds) so a slow build can never make
a live merger look crashed and get its lock stolen mid-work. Widening the section to cover
verify is the one change that breaks this protocol — do not.

### The lock file

- **Path:** `.worktrees/.merge-lock` (under gitignored `.worktrees/`, one per repo root).
- **Contents (JSON, one object):**
  ```json
  {"work_id":"<slug-or-task-id>","session_id":"<sess-...>","acquired_ts":"<ISO 8601>","expires_ts":"<ISO 8601>"}
  ```
  `expires_ts` = `acquired_ts` + a TTL sized to worst-case *merge* time with margin (merge
  only, NOT merge+verify — verify is outside the lock). A few minutes is generous; sizing it
  to a build would be the bug.

### Acquire (the race-winning primitive)

**Acquire by atomic exclusive-create — create-if-absent, fail-if-exists.** This is the only
step that decides the race, so it must be the atomic primitive, NOT
write-then-read-back-and-check (two writers can both write their own content and both read
back their own, both concluding they won).

- PowerShell: `New-Item -Path <lock> -ItemType File` fails if it exists; or
  `[System.IO.File]::Open(<lock>,'CreateNew')` which throws if it exists (`O_EXCL` semantics).
- Do NOT use `Set-Content`/`Out-File`/`>`/a write-tool to acquire — those overwrite, the
  non-atomic trap.

Then write the JSON into the file you just exclusively created.

- **Create succeeds → you hold the lock.** Proceed to merge.
- **Create fails (exists) → you lost.** Read the existing lock; wait (poll with short backoff,
  retry acquire) or, if expired, attempt the steal-and-verify reclaim below. Never
  delete-then-create a lock you didn't win.

> **Filesystem caveat:** exclusive-create atomicity holds on NTFS and local POSIX FSes; it is
> NOT guaranteed on some network filesystems (older NFS without proper `O_EXCL`). Local NTFS
> is sound. Treat a double-merge symptom as a lock-atomicity failure to investigate.

### Merge, then release immediately

1. Log `MERGE_LOCK_ACQUIRED` (with `expires_ts`), then `MERGE_STARTED`.
2. `git merge --no-ff <branch>` into the shared branch.
3. **Release the instant `git merge` returns — clean OR conflict — before anything else.**
   Delete `.worktrees/.merge-lock`. Log `MERGE_LOCK_RELEASED` (`result`: clean/conflict). A
   conflict is still "the merge returned"; releasing first means no other stage blocks behind
   an unresolved conflict.
4. **Then** handle the outcome, outside the lock:
   - **Clean:** log `MERGE_COMPLETED`; proceed to core Part 1a verification (own worktree).
   - **Conflict:** log `MERGE_CONFLICT`; surface it as the developer-resolved hard stop (not
     auto-resolved even in autonomous mode). The lock is already released, so other stages
     continue merging their own branches while this waits on the developer.

### Reclaim an expired lock — steal-and-verify

A merger can die between acquiring and releasing, orphaning the lock. Only an **expired** lock
(past `expires_ts`) may be reclaimed, and not by plain delete-and-recreate (two waiters could
both do that and both believe they won). Reclaim by steal-and-verify:

1. **Read** the expired lock's full contents (owner `work_id`/`session_id`, `acquired_ts`).
2. **Compare-and-swap on identity via a rename dance:** rename the stale lock to a private name
   unique to you (`.merge-lock.stealing.<your-session_id>`); rename is atomic, only one racer's
   rename of that source succeeds. If yours fails, someone else reclaimed it — go back to
   waiting. If it succeeds, write your own lock contents and delete the stolen private file.
   Log `MERGE_LOCK_RECLAIMED` (`prior_owner`, `landing_check`).
3. **Landing-check before merging.** A merge can *complete* and the process die *before
   releasing*. So before merging, check whether the dead merger's merge already landed:
   `git log --merges --grep=<their-branch>` on the shared branch, or
   `git merge-base --is-ancestor <their-branch> <integration-branch>`.
   - **Already landed →** do NOT re-merge; release and treat as done (log a note).
   - **Not landed →** proceed with the merge (you hold the lock).

   The landing-check runs on the reclaim path ONLY — it does not widen the normal critical
   section.

### What the lock does NOT do

- It does not serialize builds, tests, reviews, or checkbox flips — only the merge.
- It does not replace core's per-worktree isolation or Part 1a's verify isolation — it
  composes with them, and with `merge=union` on the log (ref vs. file).
- It does not require a dedicated integrator actor — every merger self-serializes through the
  one lock file. This is what lets parallel stages merge safely.

### Log events this addendum adds

| Event | When | Extra fields |
|---|---|---|
| `MERGE_LOCK_ACQUIRED` | Exclusive-create of `.worktrees/.merge-lock` succeeded | `expires_ts` |
| `MERGE_LOCK_RECLAIMED` | An expired lock was stolen-and-verified | `prior_owner`, `landing_check` |
| `MERGE_LOCK_RELEASED` | Lock deleted immediately after merge returned | `result` (clean/conflict) |

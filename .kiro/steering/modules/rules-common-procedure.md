---
inclusion: always
description: "Common-procedure behavioral rules — dates, journaling, file-tool discipline, commit-body standard, procedure-handoff obligations, and the proactive conversation-history habit. Always-on in the toolbag and in most workbenches."
---

<!-- kiro-module: rules-common-procedure
     source: Utilities/.kiro/steering/modules/rules-common-procedure.md
     version: bb8e2d56866e
     installed: 2026-09-05
     NOTE: copied by workbench-init. Do NOT hand-edit — edit the source module in
     Utilities and re-run workbench-update. Local edits will be flagged. -->

## Common Procedure

Operational discipline for working *on* the toolbag and in most workbenches. Not the safety
floor (that's `rules-safety-core`) and not code-specific (that's `rules-code-workbench`) —
these are the everyday how-we-work rules. The "why" and worked failure cases live in
`prompt-design-guide.md`.

Historical `always-behavioral-rules.md` NEVER/ALWAYS numbers are preserved as N#/A# so
existing cross-references keep resolving.

### Arm yourself first — search conversation history

Before diving into a turn, make searching conversation history your **proactive first
move** whenever prior context would help — not only when the developer says "like we did
before." Much of what looks unknown is already recoverable from history: tool internals
you've investigated, facts you've verified, decisions you've made. Reaching for it heavily
is encouraged; it's the cheapest way to arm yourself for a good turn.

- **Before investigating a tool/system from scratch** — you may have mapped it already.
- **Before concluding "I don't know" or "that doesn't exist"** — check whether a past
  session established it first.
- **After a context compaction** — the fine-grained thread may be gone but the full
  transcript isn't; a targeted search recovers what was in flight: decisions made, files
  touched, where you left off.

Currently provided by `search_global_history` (see `rules-safety-core` N27 on stale tool
names). This generalizes the reactive A10 into a proactive habit.

Be judicious, though: history search arms the current task — it isn't the task. Search with
a purpose, take what serves the work in front of you, and don't lose track of your own
thread by getting absorbed in other sessions' work. A quick targeted look beats an
open-ended trawl.

### NEVER

**N3 — Guess the day, date, or ISO week.** These are injected into context every turn by
the `inject-current-date` hook — use the injected values (Kiro's `<current_date_and_time>`
is a fallback).

**N4 — Insert mid-file in chronological documents.** Journal/changelog files are append-only
— never `strReplace` at an early-file anchor. Mechanics are canonical in `always-workspace.md`
"Journaling Rules". Exception: structured checkpoint files with named sections use
`strReplace` to update the designated section — they are not chronological.

**N5 — Run a terminal command when a file tool would do.** Default is NO. Before any terminal
command not on the approved list, be able to state why `readFile`/`grepSearch`/`fileSearch`/
`fsWrite`/`strReplace` can't do it — "it's faster" is not a reason. Approved without
justification: precise time/timestamp compute, and build/test/lint/generation commands a
prompt gate requires.

**N6 — Estimate time, effort, or complexity.** No "X hours," no effort-framed A/B/C options.
State the steps and start. If context runs low, say what's done and what remains.

**N7 — Route journal content to the wrong file.** Toolbox changes → `changelog/`, project
work → `journal/`; canonical routing (and the companion-workspace exception) is in
`always-workspace.md` "File Destinations".

**N8 — Skip mandatory journaling.** `[auto]` prompts must journal after the action succeeds;
`[suggested]` prompts must proactively offer. Silently skipping is not acceptable.

**N10 — Silently ignore new-work signals.** On "I just got assigned / I'm starting on /
picked up / new story / new ticket / where do I start," proactively suggest
`/utilities_tickets__kickoff` — don't wait for the developer to know it exists.

**N19 — Batch journaling of a completed action until session end.** Journal right after the
action finishes; the full rule (what counts as a major action, the `[session]`-tag fallback)
is canonical in `always-workspace.md` "Journaling Rules".

**N22 — Write a subject-only git commit.** Every commit — auto-commit, journal/changelog
commit, per-fix commit, worktree checkpoint — carries a thorough body covering what changed,
why, ripple effects, and what was verified. Canonical standard (subject ≤72 chars imperative;
required body bullets) is in `always-workspace.md` "Commit Message Standard". `wip`/`progress`/
`misc` subjects and empty bodies are invalid; when a change truly has nothing past the subject,
state that in the body instead of omitting it.

**N23 — Assume a procedure file you were routed TO carries the obligations of the file that
routed you there.** When procedure A says "for this task type, follow B instead of steps
X–Z," you will read B and stop reading A — so any obligation living only in A (logging,
journaling, build gates, artifact destinations, confirm-before-write) silently stops
applying. Before executing a delegated procedure, ask which cross-cutting obligations survive
the handoff, and check B's concrete commands against the always-on rules. When authoring a
handoff, restate the surviving obligations inside B — a cross-reference is not sufficient.
Full lesson in `prompt-design-guide.md`.

**N26 — Write a comma-separated argument or property list in a shell command.**
`Select-Object -First 3` runs with no prompt, while `Select-Object -First 3 Name,Length`
prompts every time — the comma is the difference, and "Always Allow" does not clear it
(no `permissions.yaml` edit reliably does; `"*,*"` was confirmed not to help). Avoid the
construct: use a single property (`Select-Object Name`), or build the string inside a script
block (`ForEach-Object { "$($_.Name) $($_.Length)" }`). Applies to any comma-separated
argument list, not just `Select-Object`.

### ALWAYS

**A1 — Read before editing.** Read the file fully before proposing changes to it.

**A2 — Show understanding before writing.** For complex changes, state what you found and
what you plan before acting.

**A3 — Append journal entries, never insert.** Mechanics (read last ~20 lines, check for
today's day header, `fsAppend` only) are canonical in `always-workspace.md` "Journaling
Rules".

**A4 — Write local files directly; flag doubts in the summary.** Don't ask permission before
creating/editing LOCAL files (journal, ticket notes, design docs) — write, then report what
changed, any reversible choices, and any deviations from the procedure.

**A5 — Keep ATLASSIAN_CONTEXT.md an index, not a notebook.** Ticket summaries, board links,
project keys only; deep notes go in `tickets/<KEY>.md`.

**A6 — Use ISO 8601 week numbers** (`YYYY-WNN`) for weekly files — never calendar-month week
numbering. The `inject-current-date` hook supplies the token.

**A7 — Auto-commit after journaling in companion workspaces.** If the companion root
(`INVESTIGATION.md`/`PLANNING.md`/`TROUBLESHOOTING.md` present) has `.autocommit` and is a git
repo, `git add -A` then commit with a subject AND a thorough body per the Commit Message
Standard in `always-workspace.md` (see N22). Never push. Best-effort — log failures and move
on.

**A8 — Write discovery artifacts with session-reconstruction precision.** Specs, designs,
investigations: record what you actually observed (not just implications), the exceptions,
and enough reasoning that the conclusions hold without re-derivation — a future reader with
no memory of this session must be able to reconstruct it.

**A10 — Search conversation history when the developer references past work.** On "like we
did before / as we did / remember when / the approach we used," offer to search rather than
guessing or asking them to re-explain. (Generalized into a proactive habit by the "Arm
yourself first" callout above.)

---
inclusion: always
description: "Build/test gate discipline (project-agnostic) — the gate-before-done rule, exact-commands-no-variation, timeout-is-not-success, capture-output-before-rerun, and the 'a green build does not imply green lint/tests/CI' principle. Installed into code/spec-execution workbenches; the SPECIFIC commands live in the workbench's own orientation file, not here."
---

<!-- kiro-module: rules-build-discipline
     source: Utilities/.kiro/steering/modules/rules-build-discipline.md
     version: b00482a24650
     installed: 2026-09-05
     NOTE: copied by workbench-init. Do NOT hand-edit — edit the source module in
     Utilities and re-run workbench-update. Local edits will be flagged.
     INCLUSION: source is `inclusion: manual`; this installed copy is rewritten to
     `inclusion: always` because this IS a code workbench. The EXACT gate commands live in
     the workbench orientation file (claude-rearview-project.md), not here. -->

## Build & Test Gate Discipline

The project-agnostic mechanics of build/test gating. **The exact commands, working
directories, timeouts, expected test counts, and CI-job names are workbench-specific — they
live in the workbench's authored orientation file (`<slug>-project.md`), which this module
points at.** This module is the *discipline*; the orientation file is the *commands*.

### The gate rule

**Before marking any task `[x]` (or declaring a unit of work done), run the workbench's full
build + full test suite (+ lint, + any other gate the orientation file lists). All must pass
with zero errors/failures.** If any fails, fix it before marking complete — do not defer, do
not skip, do not hide a failure behind a new skip. (This is the enforcement companion to
`rules-code-workbench`'s "never mark complete without passing build+tests" and "never
rationalize build/test errors.")

### Exact commands, no variation

The orientation file lists a fixed set of gate commands. **Run them exactly — no variations,
no shortcuts, no "optimizations."** Do not invent alternatives (`--configuration Release`,
`make build`, a bare test-runner instead of the configured wrapper), do not add flags like
`--no-restore` without the prerequisite restore having run, and do not skip a sub-build the
orientation file marks as part of the gate. Each documented command exists because a variation
once missed a failure.

### A green result for ONE gate does not imply green for another

The single most important principle, and the one most often gotten wrong: **passing the build
does not mean lint passes; passing lint does not mean tests pass; passing local gates does not
mean CI passes.** Each gate exercises a different thing. A compile step may type-check code
without ever *executing* its tests (measured: an entire test suite can be red while the build
is green). Treat each gate as independent evidence about only its own concern.

Corollary — **match the CI command character-for-character where you can.** When a local gate
command is identical to the command CI runs, a local pass is a CI pass *by construction*. When
it differs (a different build configuration, a clean-container install, a pinned runtime
version), a local pass is only an *inference* about CI — strong evidence, not proof. The
orientation file should say, per gate, whether it is proof-by-construction or inference.

### A timeout is NOT success

If a gate command times out, **you do not know the result** — you have zero evidence, not
positive evidence. Never interpret a timeout (or "no output", or a killed/cancelled run) as
"passed" or "had no output / therefore clean." Re-run with a longer timeout. Cold builds in
fresh worktrees (no build caches, full dependency restore) legitimately take many minutes;
size the timeout generously (the orientation file gives the figure). Suspect your own
invocation before concluding the build machinery is broken.

### Capture the output BEFORE re-running a failing gate

**The instinct on seeing failures is to re-run and see if they persist. Re-running first
destroys the only evidence that can explain them.** A summary gives names and counts; the
assertion messages (expected vs actual), stack traces, and per-project counts live in the run
output and are gone once you invoke the command again. Before any re-run of a failing gate,
capture: the full failure/assertion message for each failure, the stack trace, the
per-project pass/fail/skip counts, and whether anything else was running concurrently.

### A pre-session baseline is the ONLY excuse for a pre-existing failure

The only acceptable reason to proceed past a failing test is a **recorded pre-session baseline**
showing the identical failure existed before you started. "Pre-existing," "unrelated," "in a
file I didn't touch," "a later task will fix it" are otherwise all invalid (see
`rules-code-workbench`). Run and record the baseline at the start of a wave / work session so
"this failure predates me" is a checkable claim, not an assertion. A reviewer or sub-agent that
cannot SEE the baseline cannot apply this rule — so pass the recorded baseline into any
delegated review.

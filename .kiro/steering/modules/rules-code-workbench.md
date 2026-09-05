---
inclusion: always
description: "Code-workbench behavioral rules — build/test gates, no-rationalizing-errors, no-duplicated-code-in-tests, decompile-after-GitLab-search, and no-unfounded-future-task claims. Installed ONLY in code workbenches; dormant (not loaded) elsewhere."
---

<!-- kiro-module: rules-code-workbench
     source: Utilities/.kiro/steering/modules/rules-code-workbench.md
     version: dcf19bc450eb
     installed: 2026-09-05
     NOTE: copied by workbench-init. Do NOT hand-edit — edit the source module in
     Utilities and re-run workbench-update. Local edits will be flagged.
     INCLUSION: source is `inclusion: manual` (dormant in the no-code toolbag); this
     installed copy is rewritten to `inclusion: always` because this IS a code workbench.
     workbench-update must preserve this rewrite — it is not drift. -->

## Code Workbench Rules

These apply only when the workbench does actual code / build / test work. They are the
worked example of "activation follows the work": each is a sound rule, but loading it in a
docs or planning workspace where no code lives would be pure noise. The "why" and worked
failure cases live in `prompt-design-guide.md`.

Historical `always-behavioral-rules.md` NEVER numbers are preserved as N# so existing
cross-references keep resolving.

### NEVER

**N11 — Duplicate production code in tests.** Tests must exercise the real code. If a file
can't be sourced cleanly (side effects, heavy deps), refactor it into a sourceable unit
rather than copying the algorithm into the test — duplicated-code tests pass even when
production drifts.

**N15 — Mark a spec task or wave complete without passing build + tests.** Not done until
the workbench's build passes (0 errors) AND its tests pass (0 failures) — e.g. `dotnet build`
+ `dotnet test` for .NET, `uv run pytest` for Python, `npm run build` + `npm test` for Node.
The exact gate commands live in the workbench's orientation file (see `rules-build-discipline`);
some ecosystems have no separate compile step, so the test suite is then the build. Fix
breakage you caused; don't defer it, don't skip tests, don't hide failures with new skips.
See N18.

**N16 — Decompile or reflect a NuGet package before searching GitLab for its source.**
Search GitLab (`mcp_gitlab_search`, `scope: "blobs"`) and read the real `.cs` first.
Decompile only after confirming the source is genuinely unavailable. **No GitLab file-fetch
MCP tool exists here** — this instance's `/api/v4/mcp` predates `get_repository_file`, and
`search` returns 3-line windows only. **Never `web_fetch` `gitlab.pharos-shared.com`**:
unauthenticated requests 302 to `/users/sign_in` and surface as "Could not extract readable
content" — indistinguishable from "file does not exist", so it yields a false negative.
Read whole files over SSH, which is already authenticated:
`git clone --filter=blob:none --no-checkout --depth 1 ssh://git@gitlab.pharos-shared.com/<path>.git <dir>`
then `git -C <dir> show HEAD:<file>`; resolve `<path>` via a `scope: projects` search.

**N17 — Claim a future task will fix a current gap without evidence.** Don't say "task X
handles this" unless X's description explicitly says so. If nothing covers the gap, say so
— then fix it or propose a task.

**N18 — Rationalize build or test errors.** Errors → fix → re-run → confirm zero.
"Pre-existing," "unrelated," "in a file I didn't touch," "a later task will fix it" are all
invalid; the only excuse is a pre-session build baseline showing the identical error.
Constructing an argument for why errors are acceptable IS the violation.

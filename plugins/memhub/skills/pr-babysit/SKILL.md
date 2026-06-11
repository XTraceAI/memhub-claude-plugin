---
description: Use when a PR should be babysat to green — poll its review bots (Cursor bugbot, OpenAI Codex) and CI, fix the real findings, push, and when clean save the whole fixing process to the repo's MemHub room (e.g. "babysit this PR", "watch PR 14 and fix the bot findings", or auto-armed by the memhub hook right after `gh pr create`). Designed as the body of a self-paced /loop — one poll→fix→push pass per invocation; the final pass writes the memory and ends the loop.
argument-hint: [pr-number-or-url]
allowed-tools: mcp__memhub-staging__list_context_bases, mcp__memhub-staging__create_context_base, mcp__memhub-staging__add_memory, Bash, Read, Edit, Write, Glob, Grep
---

Babysit a pull request until its review bots are satisfied, then bank what
was learned into team memory. Each invocation is ONE pass; state between
passes (handled comment ids, the room id, the MemHub conversation id, pass
counters) lives in the loop's conversation context — re-derive nothing that
an earlier pass already resolved.

## Every pass

1. **Resolve the PR.** From `$ARGUMENTS` (number or URL) or, absent that,
   `gh pr view --json number,url,state,headRefName` on the current branch.
   PR merged or closed → report that and END the loop (no further passes).
2. **Resolve the repo's room** (first pass only — reuse the id afterwards).
   Same convention as the spec skill: name `Repo: <org>/<name>` from
   `git remote get-url origin` (host and `.git` stripped); no remote →
   `Repo: ` + basename of `git rev-parse --show-toplevel`. Match it EXACTLY
   in `list_context_bases` — a teammate may have created it; use theirs.
   No match → `create_context_base` (omit `workspace_id`).
3. **Collect findings** (`{owner}/{repo}` and `{n}` from step 1):
   - `gh pr view <n> --json state,mergeable,statusCheckRollup`
   - `gh api repos/{owner}/{repo}/pulls/{n}/comments --paginate` (inline
     review comments), `.../pulls/{n}/reviews --paginate` (review bodies),
     `.../issues/{n}/comments --paginate` (top-level comments).
   - A finding is: a comment/review from a bot reviewer — login containing
     `cursor` or `bugbot` (Cursor BugBot) or `codex`/`chatgpt` (OpenAI
     Codex), typically with a `[bot]` suffix — or a FAILING required check
     in `statusCheckRollup`. Skip comment ids already handled in a previous
     pass.
4. **Triage and fix.** For each new finding, read the code it points at and
   judge it — bots are wrong often enough that "a bot said so" is not a
   reason to change code.
   - Real → fix it on the PR's head branch (check it out if HEAD moved;
     `git pull` first; NEVER force-push). One commit per finding or one per
     coherent batch, message naming what the bot caught.
   - False positive → record the rejection rationale for step 6, and
     best-effort reply to the comment thread with one line of why
     (`gh api repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies -f body=...`;
     if the reply fails, move on — it's cosmetic).
   - Push once at the end of the pass, after all of the pass's commits.
5. **Decide: another pass, or done?**
   - Pushed fixes this pass → NOT clean; the bots need time to re-review.
     End the turn so the loop re-wakes; bots typically take a few minutes,
     so self-pace around 4–5 minutes (stay under the 5-minute cache window).
   - Clean = a pass that pushed nothing AND found no new findings AND no
     required check is failing or pending on the head commit. First such
     pass after any push → proceed to step 6.
   - Safety valve: findings still arriving after ~10 passes, or the same
     finding reopening repeatedly → stop looping, summarize the impasse to
     the user, and still do step 6 with what happened so far.

## Final pass — save the process to MemHub, then end the loop

Write the fixing process into the repo's room via `add_memory`, one
conversation for the whole babysit (pass the `conversation_id` returned by
the first call into every later call):

- **One turn per finding**: `user_message` = bot name, the finding verbatim
  (trimmed), `file:line`, PR/commit refs; `assistant_message` = what was
  done — the fix in one or two sentences plus the commit SHA, or the
  rejection rationale for false positives.
- **One closing summary turn**: PR url and title, branch, findings per bot
  and accepted/rejected counts, the bug classes that came up, and anything
  genuinely useful for future context — recurring mistake patterns in this
  repo, each bot's false-positive tendencies observed here, gotchas hit
  while fixing. Skip boilerplate; a future session should be able to read
  this and know what this PR's review cycle taught us.

Then report to the user (PR state, what was fixed, where the memory went)
and END the loop — do not schedule another wake-up.

Plain-English output throughout. If the memhub-staging MCP is not
connected, do the fixing anyway and tell the user the memory save needs
`/mcp` authentication — don't fail the babysit over it.

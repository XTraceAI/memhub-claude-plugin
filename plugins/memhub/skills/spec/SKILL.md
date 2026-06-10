---
description: Use when the user wants spec-driven development backed by team memory — create, revise, drift-check, or report on a spec held in MemHub (e.g. "start a spec for X", "save this as the team spec", "revise the spec", "did the spec change under me?", "what's the status of the retry-policy spec?"). Specs are versioned artifacts in the repo's shared context base; every revision carries a rationale and is diffable.
argument-hint: <init|revise|check|status> [file|topic] [...]
allowed-tools: mcp__memhub-staging__search_memory, mcp__memhub-staging__get_artifact, mcp__memhub-staging__get_artifact_lineage, mcp__memhub-staging__diff_artifact_versions, mcp__memhub-staging__list_context_bases, mcp__memhub-staging__create_context_base, mcp__memhub-staging__share_context_base, mcp__memhub-staging__list_teammates, mcp__memhub-staging__list_tags, Bash
---

Run spec-driven development on top of MemHub. The model:

- **One context base per repo** — the repo's shared room. Its exact name is
  derived from the git remote: `Repo: <org>/<name>` (from
  `git remote get-url origin`, host and `.git` stripped — e.g.
  `Repo: XTraceAI/memhub-claude-plugin`); no remote → `Repo: ` + basename of
  `git rev-parse --show-toplevel`. ALL of the repo's specs live there,
  alongside reviews, ADRs, and imported implementation sessions — share it
  once per teammate and every current and future spec in the repo is visible
  to them.
- The **spec is a versioned artifact** (`artifact_type: "spec"`) inside that
  room. Revisions are versions with a `rationale`; `diff_artifact_versions`
  shows what moved and `get_artifact_lineage` shows why, in order.
- A **work-item tag `spec:<slug>`** (kebab-case from the title) goes on the
  spec and every related artifact. Many specs share one room, so the tag
  (plus the artifact name `Spec: <title>`) is how revise/check/status pick
  out THIS spec — never guess by name similarity alone.
- The spec also lives **in the repo as a file** (default
  `docs/specs/<slug>.md`). The file is what implementers read in their
  worktree; the artifact is the shared truth. `check` compares the two.
- Sharing is **read-only**: teammates can search/check/status the room, but
  uploads into it work only for its creator. The intended flow: the spec
  owner runs `init`/`revise`; read-only members propose changes by editing
  the repo file (PR), and the owner lands them as a revision.

File uploads ALWAYS go through the helper script (never call the
`save_artifact` MCP tool directly, never re-emit file contents):

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
  --file "<path>" --name "Spec: <title>" --type spec \
  --context-base-id "<repo-cb-id>" --tags "spec,spec:<slug>" \
  [--parent-id "<latest-version-id>"] [--rationale "<why>"]
```

Arguments: `$ARGUMENTS` — the first token is the subcommand and is consumed
before the per-subcommand parsing below; each subcommand reads only the
REMAINING tokens. With no recognized subcommand, infer one from what the user
said (creating → init, editing → revise, "did it change" → check, "where are
we" → status) and treat all of `$ARGUMENTS` as its arguments.

Every subcommand starts by **resolving the repo's room**: derive the name as
above, then match it EXACTLY in `list_context_bases` — it may be one a
teammate created and shared with you; use theirs rather than creating a
duplicate. Only `init` creates it when missing (`create_context_base`, omit
`workspace_id` — you need creator access to share it); the other subcommands
stop and point at init if no room exists. Not a git repo → ask which context
base to use.

## init `[file-path | title...] [for <teammates>]`

1. Get the spec content. If the first remaining token is an existing file
   path (e.g. `init docs/specs/retry.md` → `docs/specs/retry.md`), that
   file IS the spec. Otherwise compose the spec from the current conversation
   (sections: Goal, Non-goals, Design, Decisions, Open questions, Milestones)
   and write it to `docs/specs/<slug>.md` in the repo — composing it yourself
   is the point here; this is NOT the file-upload case the save-artifact
   skill guards against. Derive `<title>` from the argument or content;
   `<slug>` is its short kebab-case form.
2. Resolve the repo's room; create it only if no exact-name match exists.
3. Check whether THIS spec already has a lineage there: `search_memory` with
   `memory_type: "artifacts"`, `tags: ["spec:<slug>"]`, and the room's
   `context_base_id`. A hit → STOP the init flow and run the **revise**
   steps instead (`--parent-id` the newest version, rationale required) —
   uploading without a parent would create a second root artifact and break
   check/revise diffs. Any `for <teammates>` sharing still applies (step 5).
4. Upload the file with the script (no `--parent-id` — this is the first
   version of a fresh lineage).
5. If the user named teammates ("for Alice and Bob"), resolve each via
   `list_teammates` (case-insensitive; ambiguous → show candidates and ask,
   never guess between two people) and `share_context_base` with each
   `user_id`. Tell the user this opens the repo's WHOLE room — every spec
   and imported session in it, now and future — not just this spec. Nobody
   named → skip; note it may already be shared from an earlier spec.
6. Report: artifact id, room name, file path, the `spec:<slug>` tag, who can
   see it, and the line teammates send their agent verbatim:

   > Ask your agent: *search the "Repo: <org>/<name>" context base in
   > memhub for "<title>"*

## revise `[file-path] [rationale...]`

1. Resolve the room, then the spec inside it: `search_memory` with
   `memory_type: "artifacts"`, the room's `context_base_id`, and
   `tags: ["spec:<slug>"]` if the slug is known from context, else
   `tags: ["spec"]` plus a query for the topic. Several candidates → ask.
2. `get_artifact_lineage` on it; the NEWEST version's id is the
   `--parent-id`.
3. The revised content is the repo file (default: the file `init` wrote; or
   the path given). If the change was discussed but not yet applied, edit the
   file first. A rationale is REQUIRED — take it from the arguments or the
   conversation; if you can't state why this version supersedes the last, ask.
4. Upload with the script (`--parent-id`, `--rationale`, same `--name` and
   tags as before).
5. `diff_artifact_versions` (previous → new) and report the delta in plain
   English plus the rationale. Remind the user that teammates' agents see the
   new version on their next `check` — there is no push notification.

If the upload fails on permissions, the room belongs to a teammate and you
are a read-only member: don't fight it — put the change in the repo file via
the normal PR flow and tell the user the room's owner runs `spec revise` to
land it as a version.

## check `[file-path]`

Answer: "is the spec I'm building against still the spec?"

1. Resolve the room and the spec (as in revise), `get_artifact` the latest
   version.
2. Find the local spec file (argument, `docs/specs/<slug>.md`, or the file
   from earlier in this session). Compare contents:
   - identical → in sync; say so, one line, done.
   - local matches an OLDER version in the lineage (walk `get_artifact_lineage`,
     compare against each) → the spec moved underneath: report every newer
     version's rationale in order, `diff_artifact_versions` from the local
     version to latest, and which changed sections touch work from this
     session.
   - local matches NO version → local edits never landed: show the
     local-vs-latest difference and offer `revise` (if local should win) or
     overwriting the file with the latest artifact content (if the team
     version should win). Never overwrite without asking.
3. No local file at all → print the latest version's content summary,
   rationale chain, and where to write the file.

## status `[topic]`

The multiplayer view: what the team's memory holds about a spec — or the
whole repo.

1. Resolve the room. No topic given → repo overview: `search_memory` the room
   for artifacts tagged `spec`, list each spec with its version count and
   latest rationale plus any recent related activity, and stop.
2. With a topic, pick the spec (as in revise), then `search_memory` the room
   with `memory_type: "all"`, a raised `top_k` (~30), and the spec title +
   topic as the query. The room is repo-wide — facts and episodes from OTHER
   specs' sessions will surface; filter by relevance and drop them rather
   than padding the report.
3. Report, citing memory types: current version + how many revisions and the
   latest rationale; decisions recorded (facts/episodes from imported
   implementation sessions); related artifacts (reviews, ADRs, handoffs);
   open questions still in the spec. If nothing relevant exists beyond the
   spec artifact itself, say so plainly — no implementation session touching
   this spec has been imported yet.

To land an implementation session into the repo's room, import it with a
fresh conversation id (re-imports dedup per conversation_id globally) and a
title naming the spec:

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" --context-base-id "<repo-cb-id>" \
  --conversation-id "$(uuidgen)" --title "Spec: <title> — <what was built>"
```

Plain-English output throughout; surface ids only where the user needs them
(artifact id, context base id for scripts). On first ever script run the
browser may open once for OAuth approval — expected, not an error.

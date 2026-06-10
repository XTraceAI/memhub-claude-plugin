---
description: Use when the user wants spec-driven development backed by team memory — create, revise, drift-check, or report on a spec held in MemHub (e.g. "start a spec for X", "save this as the team spec", "revise the spec", "did the spec change under me?", "what's the status of the retry-policy spec?"). Specs are versioned artifacts in a shareable context base; every revision carries a rationale and is diffable.
argument-hint: <init|revise|check|status> [file|topic] [...]
allowed-tools: mcp__memhub-staging__search_memory, mcp__memhub-staging__get_artifact, mcp__memhub-staging__get_artifact_lineage, mcp__memhub-staging__diff_artifact_versions, mcp__memhub-staging__list_context_bases, mcp__memhub-staging__create_context_base, mcp__memhub-staging__share_context_base, mcp__memhub-staging__list_teammates, mcp__memhub-staging__list_tags, Bash
---

Run spec-driven development on top of MemHub. The model:

- The **spec is a versioned artifact** (`artifact_type: "spec"`). Revisions are
  versions with a `rationale`; `diff_artifact_versions` shows what moved and
  `get_artifact_lineage` shows why, in order.
- The spec lives in its own **context base** (`Spec: <title>`) — the spec's
  room. Reviews, ADRs, and imported implementation sessions land there too, so
  one search answers "everything about this spec".
- A **work-item tag `spec:<slug>`** (kebab-case from the title) goes on the
  spec and every related artifact. The tag is how `revise`/`check`/`status`
  find the spec later — never guess by name alone.
- The spec also lives **in the repo as a file** (default
  `docs/specs/<slug>.md`). The file is what implementers read in their
  worktree; the artifact is the shared truth. `check` compares the two.

File uploads ALWAYS go through the helper script (never call the
`save_artifact` MCP tool directly, never re-emit file contents):

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
  --file "<path>" --name "Spec: <title>" --type spec \
  --context-base-id "<cb-id>" --tags "spec,spec:<slug>" \
  [--parent-id "<latest-version-id>"] [--rationale "<why>"]
```

Arguments: `$ARGUMENTS` — first token is the subcommand; with no subcommand,
infer it from what the user said (creating → init, editing → revise, "did it
change" → check, "where are we" → status).

## init `[file-path | title...] [for <teammates>]`

1. Get the spec content. If the first token is an existing file path, that
   file IS the spec. Otherwise compose the spec from the current conversation
   (sections: Goal, Non-goals, Design, Decisions, Open questions, Milestones)
   and write it to `docs/specs/<slug>.md` in the repo — composing it yourself
   is the point here; this is NOT the file-upload case the save-artifact
   skill guards against. Derive `<title>` from the argument or content;
   `<slug>` is its short kebab-case form.
2. `create_context_base` with `name: "Spec: <title>"` and a one-line
   description. Omit `workspace_id` (you need creator/contributor access to
   share it). If `list_context_bases` shows one with this exact name already,
   reuse it instead and treat this as a revise.
3. Upload the file with the script (no `--parent-id` on first version).
4. If the user named teammates ("for Alice and Bob"), resolve each via
   `list_teammates` (case-insensitive; ambiguous → show candidates and ask,
   never guess between two people) and `share_context_base` with each
   `user_id`. Nobody named → skip; say it's shareable on request.
5. Report: artifact id, context base name, file path, the `spec:<slug>` tag,
   who can see it, and the line teammates send their agent verbatim:

   > Ask your agent: *search the "Spec: <title>" context base in memhub*

## revise `[file-path] [rationale...]`

1. Resolve the spec: `search_memory` with `memory_type: "artifacts"` and
   `tags: ["spec:<slug>"]` if the slug is known from context, else
   `tags: ["spec"]` plus a query for the topic. Several candidates → ask.
2. `get_artifact_lineage` on it; the NEWEST version's id is the
   `--parent-id`. Use the artifact's context base for `--context-base-id`.
3. The revised content is the repo file (default: the file `init` wrote; or
   the path given). If the change was discussed but not yet applied, edit the
   file first. A rationale is REQUIRED — take it from the arguments or the
   conversation; if you can't state why this version supersedes the last, ask.
4. Upload with the script (`--parent-id`, `--rationale`, same `--name` and
   tags as before).
5. `diff_artifact_versions` (previous → new) and report the delta in plain
   English plus the rationale. Remind the user that teammates' agents see the
   new version on their next `check` — there is no push notification.

## check `[file-path]`

Answer: "is the spec I'm building against still the spec?"

1. Resolve the spec (as in revise) and `get_artifact` the latest version.
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

The multiplayer view: everything the team's memory holds about this spec.

1. Resolve the spec and its context base.
2. `search_memory` with `context_base_id`, `memory_type: "all"`, a raised
   `top_k` (~30), and the topic (or the spec title) as the query.
3. Report, citing memory types: current version + how many revisions and the
   latest rationale; decisions recorded (facts/episodes from imported
   implementation sessions); related artifacts (reviews, ADRs, handoffs);
   open questions still in the spec. If the context base holds nothing beyond
   the spec itself, say so plainly — that means no implementation session has
   been imported into it yet.

To land an implementation session into the spec's room, import it with a
fresh conversation id (re-imports dedup per conversation_id globally):

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" --context-base-id "<cb-id>" \
  --conversation-id "$(uuidgen)" --title "Spec: <title> — <what was built>"
```

Plain-English output throughout; surface ids only where the user needs them
(artifact id, context base id for scripts). On first ever script run the
browser may open once for OAuth approval — expected, not an error. If
`share_context_base` fails on permissions, the context base wasn't created by
you — create your own (init step 2) and retry.

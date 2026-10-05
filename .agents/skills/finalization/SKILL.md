---
name: finalization
description: Validate cloudflared-alias launcher, templates, tests, documentation and change hygiene after implementation or documentation work, before handoff or commit drafting.
---

# Finalization — cloudflared-alias

## Review the change

- Read root `AGENTS.md` and inspect the complete change set against the current request.
- Keep the CLI, configuration precedence, mode-specific routing, process cleanup, registry and history consistent with README.
- Check that matching keyed routes reach the backend and unmatched requests are rejected. The key is obscurity, not authentication.
- Preserve unrelated user work, running processes and `.runtime/`. Use temporary project copies, mocked daemons and synthetic credentials for tests.
- Keep credentials, generated configs, URLs containing live keys, logs, local tasks and agent transcripts out of tracked content. Confirm local artifacts stay ignored.
- Keep `.agents/skills/` canonical and `.claude/skills` a relative symlink to it.
- Keep `AGENTS.md` canonical and `CLAUDE.md` a relative symlink to it.

## Required checks

Run from the repository root, in this order:

```bash
bash scripts/check.sh
git diff --check HEAD
```

The first command owns Bash syntax checks, ShellCheck and offline regression tests. Dependencies are Bash, ShellCheck, Python 3, Caddy and the Linux utilities enumerated by `scripts/check.sh`. Missing dependencies are blockers, not successful skipped checks. There is no repository formatter; do not invent a formatting gate.

Fix in-scope failures without weakening checks, then rerun the gate against the resulting content. If an official skill validator is available, run it additionally after editing a skill; do not make the gate depend on an absolute machine-specific tool path.

Preserve the established check inventory and order. If an owning workflow reserves
a check, leave execution to that owner and report it as `NOT_RUN` in the agent
turn. Content repairs invalidate earlier finalization evidence.

## Staging and commit boundary

Finalization never creates a commit or pushes.

Respect any active workflow's staging boundary. Use checks over workspace content
or `HEAD` until that boundary permits staging.

When the user requests finalization and no owning workflow reserves staging,
stage only the relevant paths after checks pass. Verify the staged changes with
`git diff --cached --check` and `git status --short`. Preserve unrelated staged
content. Commit only on an explicit user request.

When drafting a message, use a single Conventional Commit subject, `type(scope): imperative summary`, at most 72 characters, without a body or authorship trailer. Suggested scopes are `tunnel`, `config`, `tests`, `docs` and `agents`; follow existing history when applicable.

## Report

Report `Result: PASS` or `Result: FAIL`, commands and outcomes, relevant files changed, unresolved blockers and Git staging status. Say whether real tunnel operation was exercised; offline checks do not establish public DNS or Cloudflare connectivity. Include a draft subject when staging was requested, and state whether a commit was created.

# Project instructions

This file is the canonical instruction source. `CLAUDE.md` is a relative symlink
to `AGENTS.md`; edit shared instructions here.

## Working agreements

- Keep this a small Linux Bash launcher for a local Caddy gate and an existing named Cloudflare tunnel.
- Keep code comments, logs, documentation, and commit subjects in English.
- Preserve unrelated user changes and existing tunnel processes and runtime state.
- Do not read real Cloudflare credentials or start public tunnels during automated checks. Use synthetic configuration and isolated temporary copies of the project.
- Do not create commits or push unless the user explicitly requests it. Finalization validates and, when requested outside Agent Runner, stages the relevant changes and drafts a message.

## Repository map

| Path | Owner |
| --- | --- |
| `scripts/tunnel.sh` | CLI, config loading, template rendering, process lifecycle, registry and history |
| `cloudflared-alias.conf` | Versioned launcher defaults; environment variables override them |
| `deploy/caddy/` | Caddy templates for path, subdomain and no-key modes |
| `deploy/cloudflared/` | Cloudflared ingress template |
| `scripts/check.sh`, `tests/` | Offline validation and regression tests |
| `scripts/setup.sh`, `scripts/check-env.sh`, `scripts/toolchain.py` | Isolated pinned toolchain preparation and prerequisite verification |
| `pyproject.toml`, `requirements.lock`, `.python-version`, `deploy/toolchain.json` | Runtime and dependency pins, verified installation artifacts |
| `.tools/`, `.venv/` | Ignored local runtimes and dependencies; trusted checks prepare their own scratch installations |
| `README.md` | User-facing operation and Agent Runner setup |
| `AGENTS.md`, `CLAUDE.md` | Shared agent instructions; `CLAUDE.md` links to `AGENTS.md` |
| `.agents/skills/` | Canonical project skills; `.claude/skills` links here |
| `.runtime/` | Ignored live launcher state; never use it as test scratch |
| `LOCAL_ARTIFACTS/` | Ignored local tasks, plans, reports, project configuration and operator guidance |

## Validation and handoff

Use the [finalization skill](.agents/skills/finalization/SKILL.md) after implementation or documentation changes. It owns the required checks and commit boundary. Add regression coverage for observable launcher failures; keep test data synthetic and independent of local Cloudflare setup.

Edit skills through `.agents/skills/`; do not create a second copy under `.claude/`. Update this map and README when layout or public behavior changes.

When Agent Runner owns this worktree, follow its phase permissions. Finalization is content validation; staging belongs to its `COMMIT` or `HANDOFF` phase. Keep frozen inputs, local configuration and finalization guidance unchanged for the lifetime of a run.

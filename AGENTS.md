# Project instructions

This file is the canonical instruction source. `CLAUDE.md` is a relative symlink
to `AGENTS.md`; edit shared instructions here.

## Working agreements

- Keep this a small Linux Bash launcher for a local Caddy gate and an existing named Cloudflare tunnel.
- Keep code comments, logs, documentation, and commit subjects in English.
- Preserve unrelated user changes and existing tunnel processes and runtime state.
- Do not read real Cloudflare credentials or start public tunnels during automated checks. Use synthetic configuration and isolated temporary copies of the project.
- Do not create commits or push unless the user explicitly requests it. Finalization validates and, when requested, stages the relevant changes and drafts a message.

## Agent publication guidance

Always provide a key instead of returning a bare-domain URL. Prefer path mode
with a meaningful and useful slug for ordinary content. For potentially sensitive
or uncertain content, use a cryptographically random opaque key; use an opaque
random key as the fallback when no suitable meaningful key has been chosen.
Do not put sensitive details into a meaningful slug. Agent exposure defaults to
keyed path mode; omitted keys generate 32 random hex characters. Keep `no-key`
an explicit choice. A key provides obscurity, not authentication or access control.

Use the user-registered MCP tools `expose_port`, `expose_files`, `list_shares` and
`stop_share` from any project. File updates default to `live`; use absolute source
paths for other projects. Relative paths resolve from the alias installation root.
The alias owns shares independently of the MCP session.

## Repository map

| Path | Owner |
| --- | --- |
| `scripts/tunnel.sh` | CLI, config loading, template rendering, process lifecycle, registry and history |
| `scripts/share_contract.py` | Typed request-local share results, errors and JSON serialization |
| `scripts/mcp_server.py`, `scripts/mcp.sh` | Thin official SDK stdio adapter and root-resolving entrypoint |
| `README.md` MCP section | One-time user-level Codex and Claude Code registration |
| `scripts/publication.py` | Bounded static copies, private publication generations and content revisions |
| `scripts/reload.js` | Browser SSE subscriptions and reload behavior for served live HTML copies |
| `cloudflared-alias.conf` | Versioned launcher defaults; environment variables override them |
| `deploy/caddy/` | Caddy templates for path, subdomain and no-key modes |
| `deploy/cloudflared/` | Cloudflared ingress template |
| `scripts/check.sh`, `tests/` | Offline validation and regression tests |
| `scripts/setup.sh`, `scripts/check-env.sh`, `scripts/toolchain.py` | Isolated pinned toolchain preparation and prerequisite verification |
| `pyproject.toml`, `requirements.lock`, `.python-version`, `deploy/toolchain.json` | Runtime and dependency pins, verified installation artifacts |
| `.tools/`, `.venv/` | Ignored local runtimes and dependencies; trusted checks prepare their own scratch installations |
| `README.md` | Installation, operation and development guidance |
| `AGENTS.md`, `CLAUDE.md` | Shared agent instructions; `CLAUDE.md` links to `AGENTS.md` |
| `.agents/skills/` | Canonical project skills; `.claude/skills` links here |
| `.runtime/` | Ignored live launcher state; never use it as test scratch |
| `LOCAL_ARTIFACTS/` | Ignored local tasks, plans, reports, project configuration and operator guidance |

## Validation and handoff

Use the [finalization skill](.agents/skills/finalization/SKILL.md) after implementation or documentation changes. It owns the required checks and commit boundary. Add regression coverage for observable launcher failures; keep test data synthetic and independent of local Cloudflare setup.

Edit skills through `.agents/skills/`; do not create a second copy under `.claude/`. Update this map and README when layout or public behavior changes.

Respect existing worktree ownership and validation reservations. Do not change
protected inputs or guidance while an owning workflow is active.

"""Add project-local Codex MCP registration without replacing operator settings."""

import fcntl
import os
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "cloudflared_alias"


def prepare_registration(root: Path) -> None:
    template = (root / "deploy/mcp/codex.config.toml").read_text(encoding="utf-8")
    registration = tomllib.loads(template)["mcp_servers"][SERVER_NAME]
    directory = root / ".codex"
    if directory.is_symlink():
        raise ValueError("Refusing symlinked .codex directory; use project-local configuration")
    directory.mkdir(exist_ok=True)
    config = directory / "config.toml"
    # Lock the config itself; leave no extra local registration artifacts.
    descriptor = os.open(config, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8", newline="") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        original = stream.read()
        settings = tomllib.loads(original)
        servers = settings.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"Conflicting mcp_servers setting in {config}; left unchanged")
        if SERVER_NAME in servers:
            existing = servers[SERVER_NAME]
            if isinstance(existing, dict) and all(
                existing.get(key) == value for key, value in registration.items()
            ):
                return
            raise ValueError(f"Conflicting {SERVER_NAME} registration in {config}; left unchanged")
        addition = template if not original else (
            ("" if original.endswith("\n") else "\n") + "\n" + template
        )
        # TOML inline tables cannot be extended. Reject structural conflicts
        # before writing, rather than reformatting or dropping operator settings.
        expected = settings | {"mcp_servers": servers | {SERVER_NAME: registration}}
        try:
            merged = tomllib.loads(original + addition)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(
                f"Cannot append registration to {config}; left unchanged. "
                "Add deploy/mcp/codex.config.toml to that configuration manually."
            ) from exc
        if merged != expected:
            raise ValueError(f"Conflicting configuration in {config}; left unchanged")
        stream.seek(0, os.SEEK_END)
        stream.write(addition)


def main() -> int:
    try:
        prepare_registration(ROOT)
    except (OSError, ValueError) as exc:
        print(f"[mcp] {exc}", file=sys.stderr)
        return 1
    print("[mcp] Project-local Codex registration ready", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

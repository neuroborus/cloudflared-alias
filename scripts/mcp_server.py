"""Thin stdio MCP adapter; the alias owns routing and share lifetimes."""

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Annotated, TypedDict

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field, PlainValidator, TypeAdapter

from share_contract import ErrorResult, ShareResult, UpdateMode, UrlMode

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE = (
    "Always provide a key instead of returning a bare-domain URL. Prefer path mode "
    "with a meaningful and useful slug for ordinary content. For potentially sensitive "
    "or uncertain content, use a cryptographically random opaque key; use an opaque "
    "random key as the fallback when no suitable meaningful key has been chosen. "
    "Do not put sensitive details into a meaningful slug. Omitted keys generate "
    "32 random hex characters. A key provides obscurity, not authentication or access "
    "control. Bare-domain no-key mode requires an explicit selection."
)

server = MCPServer(
    "cloudflared-alias",
    instructions=(
        "Expose local ports or one selected static file/directory through the existing "
        "named tunnel. The alias owns shares; ending this MCP session leaves them running. "
        "Use list_shares and stop_share to inspect and stop individual IDs. Relative file "
        "paths resolve from the project root. File inputs expose no adjacent assets; "
        "select a directory for a page and its assets. File updates default to live "
        "(native events and HTML reload); manual reads on request, snapshot freezes copies "
        "until republication. Sources remain unchanged. Reusing a key or backend port "
        "replaces its existing share. " + GUIDANCE
    ),
)

Port = Annotated[int, Field(ge=1, le=65535, strict=True, description="Local backend TCP port")]
KeyValue = Annotated[str, Field(
    min_length=1, max_length=32, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$",
    description="URL key; omit for a cryptographically random key. Incompatible with no-key.",
)]
# SDK JSON pre-parsing rewrites "null" for union parameters. Keep string
# arguments literal while accepting actual null and publishing its schema.
Key = Annotated[str, PlainValidator(
    TypeAdapter(KeyValue | None).validate_python, json_schema_input_type=KeyValue | None,
)]
SourcePath = Annotated[str, Field(
    min_length=1, pattern=r"^[^\x00\r\n\t]+$",
    description="One file or recursive directory; relative to the project root. No symlinks.",
)]
ShareId = Annotated[str, Field(
    min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    description="Share ID returned by exposure or list_shares",
)]


class SharesResult(TypedDict):
    shares: list[ShareResult]


SHARE_ADAPTER = TypeAdapter(ShareResult)
LIST_ADAPTER = TypeAdapter(list[ShareResult])
ERROR_ADAPTER = TypeAdapter(ErrorResult)


def response(data: ShareResult | SharesResult | ErrorResult, *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=True))],
        structured_content=data, is_error=error,
    )


async def launcher(*arguments: str) -> CallToolResult:
    try:
        process = await asyncio.create_subprocess_exec(
            "bash", str(ROOT / "scripts/tunnel.sh"), *arguments,
            cwd=ROOT, env=os.environ | {"ALIAS_PYTHON": sys.executable},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            # SDK clients terminate their server group; alias-owned daemons must survive it.
            start_new_session=True,
        )
    except OSError as exc:
        return response({"error": {"code": "launcher_unavailable", "message": str(exc)}}, error=True)
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            # Cancellation stops only this invocation; the launcher handles its transaction.
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            await process.wait()
    try:
        data = json.loads(output)
        if process.returncode != 0:
            return response(ERROR_ADAPTER.validate_python(data, strict=True), error=True)
        if arguments[0] == "list-shares":
            return response({"shares": LIST_ADAPTER.validate_python(data, strict=True)})
        return response(SHARE_ADAPTER.validate_python(data, strict=True))
    except ValueError:
        return response({"error": {"code": "invalid_launcher_result",
                                   "message": "Launcher returned invalid JSON share data."}}, error=True)


@server.tool(description="Expose a local port as a persistent share. " + GUIDANCE)
async def expose_port(
    port: Port, url_mode: UrlMode = "path", key: Key = None,
) -> Annotated[CallToolResult, ShareResult]:
    arguments = ["expose-port", str(port), "--url-mode", url_mode]
    if key is not None:
        arguments.extend(["--key", key])
    return await launcher(*arguments)


@server.tool(description=(
    "Expose exactly one static file or recursive directory, preserving original sources. "
    "Relative paths resolve from the project root; no adjacent file discovery or symlinks. "
    "Live (default) publishes native file changes and reloads served HTML over SSE; "
    "manual reads current bytes on request; snapshot freezes bytes until republication. "
    "Other formats display or download normally and get live updates on refresh. " + GUIDANCE
))
async def expose_files(
    path: SourcePath, url_mode: UrlMode = "path",
    update_mode: UpdateMode = "live", key: Key = None,
) -> Annotated[CallToolResult, ShareResult]:
    arguments = ["expose-files", path, "--url-mode", url_mode, "--update-mode", update_mode]
    if key is not None:
        arguments.extend(["--key", key])
    return await launcher(*arguments)


@server.tool(description="List active alias-owned shares, including legacy and degraded shares.")
async def list_shares() -> Annotated[CallToolResult, SharesResult]:
    return await launcher("list-shares")


@server.tool(description="Stop one share by ID, preserving other shares and original source files.")
async def stop_share(id: ShareId) -> Annotated[CallToolResult, ShareResult]:
    return await launcher("stop-share", id)


if __name__ == "__main__":
    server.run(transport="stdio")

"""Request-local share results shared by the launcher and its adapters."""

import json
import secrets
import sys
from typing import Literal, NotRequired, TypedDict

UrlMode = Literal["path", "subdomain", "no-key"]
UpdateMode = Literal["snapshot", "manual", "live"]
ShareState = Literal["active", "degraded", "stopped"]


class PortSource(TypedDict):
    type: Literal["port"]
    port: int


class FileSource(TypedDict):
    type: Literal["file", "directory"]
    path: str


class ShareResult(TypedDict):
    id: str
    url: str
    source: PortSource | FileSource
    url_mode: UrlMode
    update_mode: UpdateMode | None
    state: ShareState
    source_revision: NotRequired[str]
    revision: NotRequired[str]


class ErrorDetail(TypedDict):
    code: str
    message: str


class ErrorResult(TypedDict):
    error: ErrorDetail


def port_share(share_id: str, url: str, port: int, mode: UrlMode,
               state: ShareState) -> ShareResult:
    if not 1 <= port <= 65535:
        raise ValueError("Invalid backend port in share registry")
    if mode not in ("path", "subdomain", "no-key"):
        raise ValueError("Invalid URL mode in share registry")
    if state not in ("active", "degraded", "stopped"):
        raise ValueError("Invalid share state")
    return {"id": share_id, "url": url, "source": {"type": "port", "port": port},
            "url_mode": mode, "update_mode": None, "state": state}


def main() -> None:
    command, *arguments = sys.argv[1:]
    if command == "key":
        print(secrets.token_hex(16))
        return
    if command == "port":
        share_id, url, port, mode, state = arguments
        result = port_share(share_id, url, int(port), mode, state)
    elif command == "collect":
        result = [json.loads(line) for line in sys.stdin if line.strip()]
    elif command == "error":
        code, message = arguments
        result = {"error": {"code": code, "message": message}}
    else:
        raise ValueError(f"Unknown share contract command: {command}")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

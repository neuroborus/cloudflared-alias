"""Selection-bounded static copies; routing and lifecycle stay with the launcher."""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from typing import Literal
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREPARATION_VERSION = "copy-v1"
CONTROL_PATH = "__alias"
EXCLUDED_NAMES = frozenset({
    ".git", ".gitignore", ".gitattributes", ".gitmodules", ".hg", ".svn",
    ".runtime", ".agents", ".claude", ".codex", ".tools", ".venv",
    "LOCAL_ARTIFACTS", "__pycache__", "AGENTS.md", "CLAUDE.md", ".mcp.json",
    "cloudflared-alias.conf",
})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ENTRY_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class PublicationError(ValueError):
    """The selected content could not be safely prepared."""


class _UnstableSource(PublicationError):
    pass


class InvalidRequest(PublicationError):
    """The requested path is outside the publication's public selection."""


@dataclass(frozen=True)
class Selection:
    path: Path
    kind: Literal["file", "directory"]


@dataclass(frozen=True)
class PreparedPublication:
    source_revision: str
    revision: str


def _excluded(name: str) -> bool:
    return name in EXCLUDED_NAMES or name == ".env" or name.startswith(".env.")


def _absolute(path: str | Path) -> Path:
    # Linux resolves double-leading slashes at the ordinary filesystem root.
    return Path("/" + os.path.abspath(path).lstrip("/"))


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # Ignore access time: reading the source must not invalidate its own copy.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _parent(path: Path):
    """Open every ancestor without following links, including replaced parents."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def select_source(path: str | Path, *, project_root: Path = PROJECT_ROOT) -> Selection:
    # Do not normalize '..' across a possible symlink before inspecting ancestors.
    original = Path(path).absolute()
    absolute = _absolute(original)
    if absolute == Path("/") or any(_excluded(name) or name == CONTROL_PATH
                                    for name in original.parts):
        raise PublicationError("Cannot publish root or internal metadata")
    try:
        with _parent(original) as (parent, name):
            descriptor = os.open(name, _ENTRY_FLAGS, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise PublicationError(f"Cannot open selected source safely: {error}") from error
    if stat.S_ISDIR(info.st_mode):
        if _absolute(project_root).is_relative_to(absolute):
            raise PublicationError("Cannot publish the launcher root or its ancestors")
        return Selection(absolute, "directory")
    if stat.S_ISREG(info.st_mode):
        return Selection(absolute, "file")
    raise PublicationError("Select one regular file or directory")


def bundle_revision(manifest: list[tuple[str, str]]) -> str:
    encoded = json.dumps(sorted(manifest), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def effective_revision(source_revision: str, preparation_version: str) -> str:
    encoded = json.dumps([preparation_version, source_revision],
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_entry(parent: int, name: str, destination: Path, relative: str,
                manifest: list[tuple[str, str]], expected_kind: str | None = None) -> None:
    descriptor = os.open(name, _ENTRY_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if expected_kind is not None:
            expected = stat.S_ISREG if expected_kind == "file" else stat.S_ISDIR
            if not expected(before.st_mode):
                raise PublicationError("Selected source type changed")
        if stat.S_ISDIR(before.st_mode):
            destination.mkdir()
            for child in sorted(os.listdir(descriptor)):
                if child == CONTROL_PATH:
                    raise PublicationError("Source collides with the reserved control path")
                if not _excluded(child):
                    child_relative = f"{relative}/{child}" if relative else child
                    _copy_entry(descriptor, child, destination / child,
                                child_relative, manifest)
        elif stat.S_ISREG(before.st_mode):
            digest = hashlib.sha256()
            with destination.open("xb") as output:
                while data := os.read(descriptor, 1024 * 1024):
                    output.write(data)
                    digest.update(data)
            manifest.append((relative, digest.hexdigest()))
        else:
            raise PublicationError("Publication contains a non-regular file")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _signature(before) != _signature(after) or _signature(after) != _signature(named):
            raise _UnstableSource("Source changed while preparing its copy")
    finally:
        os.close(descriptor)


class Publication:
    """Private generations with one atomically switched, byte-only public root.

    The launcher serializes preparation for a share. The only served symlink is
    generated here; source symlinks are never followed. Metadata and incomplete
    generations remain outside the root that Caddy will serve.
    """

    def __init__(self, source: str | Path, share_id: str, *,
                 project_root: Path = PROJECT_ROOT,
                 preparation_version: str = PREPARATION_VERSION):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", share_id):
            raise PublicationError("Invalid share ID")
        self.project_root = Path(project_root).absolute()
        self.selection = select_source(source, project_root=self.project_root)
        self.state_dir = self.project_root / ".runtime" / "publications" / share_id
        self.public_root = self.state_dir / "public"
        self.preparation_version = preparation_version
        self._request_lock = threading.Lock()

    def _ensure_state(self) -> None:
        with _parent(self.project_root / ".runtime") as (descriptor, _), ExitStack() as stack:
            # Only create this share's private directories; reject existing links.
            for name in (".runtime", "publications", self.state_dir.name):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                stack.callback(os.close, descriptor)

    def _generation(self) -> Path | None:
        try:
            target = os.readlink(self.public_root)
        except FileNotFoundError:
            return None
        if not re.fullmatch(r"generation-[a-zA-Z0-9_]+/public", target):
            raise PublicationError("Unexpected managed publication root")
        return self.state_dir / Path(target).parent

    def current(self) -> PreparedPublication | None:
        generation = self._generation()
        if generation is None:
            return None
        result = json.loads((generation / "metadata.json").read_text(encoding="utf-8"))
        if result["source_revision"] is None:
            return None  # A manual standalone-file request accepted its deletion.
        return PreparedPublication(result["source_revision"], result["revision"])

    def prepare(self, *, attempts: int = 3) -> PreparedPublication:
        if not 1 <= attempts <= 3:
            raise PublicationError("Preparation attempts must be between 1 and 3")
        try:
            self._ensure_state()
            previous = self._generation()
            for attempt in range(attempts):
                generation = Path(tempfile.mkdtemp(prefix="generation-", dir=self.state_dir))
                pending = generation / "activate"
                try:
                    selection = self.selection
                    manifest = []
                    public = generation / "public"
                    with _parent(selection.path) as (parent, name):
                        if selection.kind == "file":
                            public.mkdir()
                            _copy_entry(parent, name, public / name, name, manifest, selection.kind)
                            source_revision = manifest[0][1]
                        else:
                            _copy_entry(parent, name, public, "", manifest, selection.kind)
                            source_revision = bundle_revision(manifest)
                    revision = effective_revision(source_revision, self.preparation_version)
                    result = PreparedPublication(source_revision, revision)
                    (generation / "metadata.json").write_text(json.dumps({
                        "source_revision": source_revision, "revision": revision,
                        "preparation_version": self.preparation_version,
                        "manifest": manifest,
                    }), encoding="utf-8")
                    pending.symlink_to(f"{generation.name}/public")
                    os.replace(pending, self.public_root)
                except BaseException as error:
                    # Replacement may commit before an interruption reaches Python.
                    if self._generation() == generation:
                        raise
                    shutil.rmtree(generation)
                    if isinstance(error, (_UnstableSource, FileNotFoundError)):
                        if attempt + 1 == attempts:
                            raise PublicationError("Source did not stabilize during preparation") from error
                        continue
                    raise
                # Activation is committed. Cleanup cannot invalidate accepted bytes.
                if previous is not None:
                    shutil.rmtree(previous, ignore_errors=True)
                return result
        except OSError as error:
            raise PublicationError(f"Cannot prepare publication safely: {error}") from error

    def prepare_request(self, uri: str) -> None:
        """Refresh only the requested file/index; Caddy remains the byte server.

        Clone accepted files with hard links, replacing changed entries only in
        the pending generation. No source scan, watcher or in-place write occurs.
        Unsafe or unstable reads leave the accepted generation intact.
        """
        path = uri.split("?", 1)[0]
        try:
            decoded = unquote(path, errors="strict")
        except UnicodeError as error:
            raise InvalidRequest("Invalid request encoding") from error
        parts = [part for part in decoded.split("/") if part]
        if (not path.startswith("/") or "\x00" in decoded or "\\" in decoded
                or any(part in (".", "..", CONTROL_PATH) or _excluded(part)
                       for part in parts)):
            raise InvalidRequest("Path is outside the public selection")
        if self.selection.kind == "file" and parts != [self.selection.path.name]:
            raise InvalidRequest("Only the selected file is published")

        with self._request_lock:
            previous = self._generation()
            if previous is None:
                raise PublicationError("Prepare a publication before serving requests")
            for attempt in range(3):
                generation = Path(tempfile.mkdtemp(prefix="generation-", dir=self.state_dir))
                try:
                    public = generation / "public"
                    shutil.copytree(previous / "public", public, copy_function=os.link)
                    metadata = json.loads((previous / "metadata.json").read_text(encoding="utf-8"))
                    manifest = dict(metadata["manifest"])
                    self._refresh_requested(parts, decoded.endswith("/"), public, manifest)
                    source_revision = (manifest.get(self.selection.path.name)
                                       if self.selection.kind == "file"
                                       else bundle_revision(list(manifest.items())))
                    metadata.update(manifest=sorted(manifest.items()),
                                    source_revision=source_revision,
                                    revision=(effective_revision(source_revision, self.preparation_version)
                                              if source_revision is not None else None))
                    (generation / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                    pending = generation / "activate"
                    pending.symlink_to(f"{generation.name}/public")
                    os.replace(pending, self.public_root)
                except BaseException as error:
                    if self._generation() == generation:
                        raise
                    shutil.rmtree(generation)
                    if isinstance(error, (_UnstableSource, FileNotFoundError)) and attempt < 2:
                        continue
                    if isinstance(error, OSError):
                        raise PublicationError(f"Cannot refresh publication safely: {error}") from error
                    raise
                shutil.rmtree(previous, ignore_errors=True)
                # A valid deletion or directory-to-file replacement is accepted
                # even when this request can no longer reach a published path.
                if not public.joinpath(*parts).exists():
                    raise InvalidRequest("Requested path is no longer published")
                return

    def _refresh_requested(self, parts: list[str], trailing_slash: bool,
                           public: Path, manifest: dict[str, str]) -> None:
        with _parent(self.selection.path) as (parent, selected), ExitStack() as stack:
            if self.selection.kind == "file":
                if trailing_slash:
                    raise InvalidRequest("A file URL cannot end with a slash")
                _refresh_file(parent, selected, public, selected, manifest)
                return
            # The selected directory must still be a real directory. Missing
            # descendants are ordinary deletions; a missing root is a failure.
            descriptor = os.open(selected, _DIRECTORY_FLAGS, dir_fd=parent)
            stack.callback(os.close, descriptor)
            for index, part in enumerate(parts):
                try:
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    _remove_copy(public, "/".join(parts[:index + 1]), manifest)
                    return
                if stat.S_ISREG(info.st_mode):
                    if trailing_slash and index == len(parts) - 1:
                        raise InvalidRequest("A file URL cannot end with a slash")
                    # A regular replacement removes the directory's accepted
                    # descendants only after its bytes are safely prepared.
                    _refresh_file(descriptor, part, public,
                                  "/".join(parts[:index + 1]), manifest)
                    return
                descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            relative = "/".join(parts)
            _ensure_copy_directory(public, parts, manifest)
            for index_name in ("index.html", "index.htm"):
                name = f"{relative}/{index_name}" if relative else index_name
                _refresh_file(descriptor, index_name, public, name, manifest)


def _remove_copy(public: Path, relative: str, manifest: dict[str, str]) -> None:
    destination = public / relative
    if destination.is_dir():
        shutil.rmtree(destination)
    else:
        destination.unlink(missing_ok=True)
    for name in list(manifest):
        if name == relative or name.startswith(relative + "/"):
            del manifest[name]


def _ensure_copy_directory(public: Path, parts: list[str], manifest: dict[str, str]) -> None:
    for index in range(1, len(parts) + 1):
        relative = "/".join(parts[:index])
        destination = public / relative
        if destination.is_file():
            _remove_copy(public, relative, manifest)
        destination.mkdir(exist_ok=True)


def _refresh_file(parent: int, name: str, public: Path, relative: str,
                  manifest: dict[str, str]) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        _remove_copy(public, relative, manifest)
        return
    if not stat.S_ISREG(info.st_mode):
        raise PublicationError("Requested source is not a regular file")
    destination = public / relative
    _ensure_copy_directory(public, relative.split("/")[:-1], manifest)
    _remove_copy(public, relative, manifest)
    copied = []
    _copy_entry(parent, name, destination, relative, copied, "file")
    manifest.update(copied)


def manual_app(publication: Publication):
    """Loopback preparation gate; successful replies contain no static bytes."""
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    def prepare(request):
        uri = request.headers.get("x-forwarded-uri")
        if uri is None:
            return Response(status_code=400)
        if request.headers.get("x-forwarded-method") not in ("GET", "HEAD"):
            return Response(status_code=405, headers={"Allow": "GET, HEAD"})
        try:
            publication.prepare_request(uri)
        except InvalidRequest:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        except (PublicationError, OSError) as error:
            logging.getLogger(__name__).warning("Keeping accepted publication: %s", error)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route("/__alias/prepare", prepare, methods=["GET"])])


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve-manual"])
    parser.add_argument("config", type=Path)
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text(encoding="utf-8"))
    port = config["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        parser.error("Helper port must be between 1 and 65535")
    publication = Publication(config["source"], config["share_id"],
                              project_root=Path(config["project_root"]))
    if publication._generation() is None:
        publication.prepare()
    uvicorn.run(manual_app(publication), host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()

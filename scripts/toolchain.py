"""Prepare the frozen Linux toolchain; trusted checks are strictly offline."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class ToolchainError(Exception):
    pass


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_artifact(path, item):
    if not path.is_file():
        raise ToolchainError(f"Missing artifact: {item['filename']} ({item['sha256']})")
    if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
        raise ToolchainError(f"Corrupt artifact: {item['filename']}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ToolchainError(f"Artifact URL redirected: {req.full_url}")


def collect_artifacts(manifest, directory, download=False):
    opener = urllib.request.build_opener(NoRedirect)
    paths = {}
    for item in manifest["runtime_artifacts"] + manifest["dependency_artifacts"]:
        # Runner supplies hash-named read-only files; local downloads retain names.
        hashed = directory / item["sha256"]
        path = hashed if hashed.exists() else directory / item["filename"]
        if not path.exists() and download:
            directory.mkdir(parents=True, exist_ok=True)
            pending = path.with_suffix(path.suffix + ".part")
            try:
                with opener.open(item["url"], timeout=60) as response, pending.open("wb") as stream:
                    remaining = item["size"]
                    while chunk := response.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ToolchainError(f"Oversized artifact: {item['filename']}")
                        stream.write(chunk)
                verify_artifact(pending, item)
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
        verify_artifact(path, item)
        paths[item["filename"]] = path
    return paths


def run(arguments, cwd=None, timeout=120):
    # Ignore host Python/pip customization and prevent package index access.
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(name, None)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1")
    command = [str(argument) for argument in arguments]
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=sys.stderr, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
    except BaseException:
        # Bound the whole compiler process group, including interrupted builds.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    if code:
        raise subprocess.CalledProcessError(code, command)


def output(arguments):
    return subprocess.check_output([str(argument) for argument in arguments],
                                   text=True, stderr=subprocess.PIPE, timeout=30).strip()


def verify_environment(manifest, python, caddy, cloudflared=None):
    for label, path in (("Python", python), ("Caddy", caddy)):
        if not path.is_file():
            raise ToolchainError(f"Missing {label}: {path}; run bash scripts/setup.sh")
    version = output([python, "-I", "-c", "import platform; print(platform.python_version())"])
    if version != manifest["python"]:
        raise ToolchainError(f"Python version mismatch: expected {manifest['python']}, got {version}")
    version = output([caddy, "version"]).split(" ", 1)[0].removeprefix("v")
    if version != manifest["caddy"]:
        raise ToolchainError(f"Caddy version mismatch: expected {manifest['caddy']}, got {version}")
    expected = {item["name"]: item["version"] for item in manifest["dependency_artifacts"]}
    expected["pip"] = manifest["pip"]
    installed = json.loads(output([python, "-I", "-c",
        "import importlib.metadata as m, json; "
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"]))
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    installed = {normalize(name): version for name, version in installed.items()}
    for name, version in expected.items():
        actual = installed.get(normalize(name), "missing")
        if actual != version:
            raise ToolchainError(f"Dependency version mismatch: {name}, expected {version}, got {actual}")
    # Version metadata alone does not establish that native modules can load.
    output([python, "-I", "-c",
        "import ssl, zlib, bz2, ctypes; "
        "from mcp.server import MCPServer; from mcp import Client, StdioServerParameters; "
        "import pydantic, starlette, sse_starlette, uvicorn, quickjs; "
        "from watchdog.observers.inotify import InotifyObserver"])
    run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"])
    if cloudflared is not None:
        version = output([cloudflared, "--version"])
        if not re.match(r"cloudflared version " + re.escape(manifest["cloudflared"]) + r"(?:\s|$)", version):
            raise ToolchainError(f"cloudflared version mismatch: expected {manifest['cloudflared']}, got {version}")


def prepare(manifest, args):
    if sys.version_info < (3, 11):
        raise ToolchainError("Preparation requires bootstrap Python 3.11 or newer")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ToolchainError("The pinned artifacts require Linux x86_64")
    tools, venv = args.tools.resolve(), args.venv.resolve()
    if args.offline and args.artifacts is None:
        raise ToolchainError("Offline preparation requires --artifacts DIR")
    directory = args.artifacts or tools / "artifacts"
    tools.mkdir(parents=True, exist_ok=True)
    with (tools / ".setup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        paths = collect_artifacts(manifest, directory, download=not args.offline and args.artifacts is None)
        python = venv / "bin/python3"
        caddy = tools / "caddy/usr/bin/caddy"
        stamp = tools / "prepared.json"
        identity = {"manifest": digest(ROOT / "deploy/toolchain.json"),
                    "lock": digest(ROOT / "requirements.lock"), "venv": str(venv)}
        if stamp.exists() and json.loads(stamp.read_text()) == identity:
            verify_environment(manifest, python, caddy)
            return
        source = tools / "python"
        caddy_root = tools / "caddy"
        for path in (source, caddy_root, venv):
            if path.exists():
                raise ToolchainError(f"Unmanaged or incomplete installation: {path}; choose empty paths or remove it explicitly")
        for name in ("cc", "make", "ar", "tar", "xz"):
            if shutil.which(name) is None:
                raise ToolchainError(f"Missing build prerequisite: {name}")
        runtime = {item["name"]: item for item in manifest["runtime_artifacts"]}
        source.mkdir()
        run(["tar", "-xJf", paths[runtime["Python"]["filename"]], "--strip-components=1", "-C", source])
        # The source build is retained as the venv's base runtime; no system install.
        run(["./configure", f"--prefix={tools / 'python-prefix'}", "--with-ensurepip=install"], cwd=source, timeout=300)
        jobs = str(min(8, os.cpu_count() or 1))
        run(["make", f"-j{jobs}"], cwd=source, timeout=1800)
        bundled_pip = source / f"Lib/ensurepip/_bundled/pip-{manifest['pip']}-py3-none-any.whl"
        if not bundled_pip.is_file() or digest(bundled_pip) != manifest["pip_sha256"]:
            raise ToolchainError("Bundled pip hash mismatch")
        # Detect absent required extension headers before creating an environment.
        output([source / "python", "-I", "-c", "import ssl, zlib, bz2, ctypes"])
        run([source / "python", "-I", "-m", "venv", "--without-pip", venv])
        run([python, "-I", "-m", "ensurepip", "--default-pip"])
        wheels = tools / "wheels"
        wheels.mkdir(exist_ok=True)
        for item in manifest["dependency_artifacts"]:
            shutil.copyfile(paths[item["filename"]], wheels / item["filename"])
        run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
             "--no-index", "--find-links", wheels, "--require-hashes", "--only-binary=:all:",
             "--no-deps", "-r", ROOT / "requirements.lock"], timeout=300)
        package = paths[runtime["Caddy"]["filename"]]
        members = [name for name in output(["ar", "t", package]).splitlines() if name.startswith("data.tar")]
        if len(members) != 1:
            raise ToolchainError("Caddy package must contain exactly one data archive")
        caddy_root.mkdir()
        archive = tools / "caddy-data.tar"
        try:
            with archive.open("wb") as stream:
                subprocess.run(["ar", "p", str(package), members[0]], stdout=stream, check=True, timeout=120)
            run(["tar", "-xf", archive, "-C", caddy_root])
        finally:
            archive.unlink(missing_ok=True)
        verify_environment(manifest, python, caddy)
        stamp.write_text(json.dumps(identity) + "\n")


def main():
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("prepare", help="Install pinned artifacts; --artifacts is always offline")
    install.add_argument("--tools", type=Path, default=ROOT / ".tools")
    install.add_argument("--venv", type=Path, default=ROOT / ".venv")
    install.add_argument("--artifacts", type=Path)
    install.add_argument("--offline", action="store_true")
    verify = commands.add_parser("verify", help="Check installed versions, imports and dependency compatibility")
    verify.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python3")
    verify.add_argument("--caddy", type=Path, default=ROOT / ".tools/caddy/usr/bin/caddy")
    verify.add_argument("--cloudflared", type=Path, help="Also check the operator's cloudflared; tests mock it")
    args = parser.parse_args()
    try:
        manifest = json.loads((ROOT / "deploy/toolchain.json").read_text())
        if args.command == "prepare":
            prepare(manifest, args)
        else:
            verify_environment(manifest, args.python, args.caddy, args.cloudflared)
        print(f"[{args.command}] Pinned toolchain ready", file=sys.stderr)
    except (ToolchainError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[{args.command}] {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

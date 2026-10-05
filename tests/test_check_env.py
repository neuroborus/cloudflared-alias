"""Toolchain prerequisite failures, exact pins and offline artifact selection."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("toolchain", ROOT / "scripts/toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolchain)
MANIFEST = json.loads((ROOT / "deploy/toolchain.json").read_text())


class CheckEnvTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="cloudflared-alias-env-")
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.item = {"filename": "synthetic.whl", "sha256": hashlib.sha256(b"synthetic").hexdigest(), "size": 9}
        self.small_manifest = {"runtime_artifacts": [], "dependency_artifacts": [self.item]}
        self.python = self.directory / "python"
        self.caddy = self.directory / "caddy"
        self.python.touch()
        self.caddy.touch()

    def test_metadata_and_lock_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual((ROOT / ".python-version").read_text().strip(), MANIFEST["python"])
        self.assertEqual(project["requires-python"], "==" + MANIFEST["python"])
        expected = {item["name"]: item["version"] for item in MANIFEST["dependency_artifacts"]}
        direct = project["dependencies"] + project["optional-dependencies"]["test"]
        self.assertEqual(len(direct), 7)
        for requirement in direct:
            name, version = requirement.split("==")
            self.assertEqual(version, expected[name])
        lines = (ROOT / "requirements.lock").read_text().splitlines()
        self.assertEqual([line for line in lines if line and not line.startswith("#")], [
            f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}"
            for item in MANIFEST["dependency_artifacts"]])
        artifacts = MANIFEST["runtime_artifacts"] + MANIFEST["dependency_artifacts"]
        self.assertEqual(len(artifacts), MANIFEST["artifact_count"])
        self.assertEqual(sum(item["size"] for item in artifacts), MANIFEST["total_bytes"])

    def test_missing_artifact_never_downloads(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network access")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "Missing artifact: synthetic.whl"):
                toolchain.collect_artifacts(self.small_manifest, self.directory)

    def test_corrupt_artifact_rejected_by_size_and_digest(self):
        path = self.directory / self.item["filename"]
        for content in (b"short", b"different"):
            path.write_bytes(content)
            with self.assertRaisesRegex(toolchain.ToolchainError, "Corrupt artifact"):
                toolchain.collect_artifacts(self.small_manifest, self.directory)

    def test_hash_named_and_filename_artifacts(self):
        for name in (self.item["sha256"], self.item["filename"]):
            path = self.directory / name
            path.write_bytes(b"synthetic")
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network access")):
                self.assertEqual(toolchain.collect_artifacts(self.small_manifest, self.directory),
                                 {self.item["filename"]: path})
            path.unlink()

    def test_corrupt_hash_named_artifact_is_not_replaced(self):
        (self.directory / self.item["sha256"]).write_bytes(b"different")
        (self.directory / self.item["filename"]).write_bytes(b"synthetic")
        with self.assertRaisesRegex(toolchain.ToolchainError, "Corrupt artifact"):
            toolchain.collect_artifacts(self.small_manifest, self.directory)

    def test_redirects_are_rejected(self):
        request = toolchain.urllib.request.Request("https://example.test/artifact")
        with self.assertRaisesRegex(toolchain.ToolchainError, "redirected"):
            toolchain.NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://elsewhere.test/")

    def test_offline_setup_requires_inputs_before_building(self):
        args = argparse.Namespace(tools=self.directory / "tools", venv=self.directory / "venv",
                                  artifacts=None, offline=True)
        with self.assertRaisesRegex(toolchain.ToolchainError, "requires --artifacts"):
            toolchain.prepare(MANIFEST, args)
        args.artifacts = self.directory
        with patch.object(toolchain, "run", side_effect=AssertionError("build started")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "Missing artifact"):
                toolchain.prepare(MANIFEST, args)
        self.assertFalse((args.tools / "python").exists())
        self.assertFalse(args.venv.exists())

    def test_missing_runtime(self):
        self.python.unlink()
        with self.assertRaisesRegex(toolchain.ToolchainError, "Missing Python"):
            toolchain.verify_environment(MANIFEST, self.python, self.caddy)
        self.python.touch()
        self.caddy.unlink()
        with self.assertRaisesRegex(toolchain.ToolchainError, "Missing Caddy"):
            toolchain.verify_environment(MANIFEST, self.python, self.caddy)

    def test_runtime_version_mismatches(self):
        for responses, message in ((["3.12.3"], "Python version mismatch"),
                                   ([MANIFEST["python"], "v2.6.2"], "Caddy version mismatch")):
            with patch.object(toolchain, "output", side_effect=responses):
                with self.assertRaisesRegex(toolchain.ToolchainError, message):
                    toolchain.verify_environment(MANIFEST, self.python, self.caddy)

    def test_missing_or_wrong_dependencies(self):
        installed = {item["name"]: item["version"] for item in MANIFEST["dependency_artifacts"]}
        installed["pip"] = MANIFEST["pip"]
        for replacement in (None, "0.0.0"):
            changed = installed.copy()
            if replacement is None:
                del changed["mcp"]
            else:
                changed["mcp"] = replacement
            with patch.object(toolchain, "output", side_effect=[MANIFEST["python"],
                    "v" + MANIFEST["caddy"], json.dumps(changed)]):
                with self.assertRaisesRegex(toolchain.ToolchainError, "Dependency version mismatch: mcp"):
                    toolchain.verify_environment(MANIFEST, self.python, self.caddy)

    def test_optional_cloudflared_version_mismatch(self):
        installed = {item["name"]: item["version"] for item in MANIFEST["dependency_artifacts"]}
        installed["pip"] = MANIFEST["pip"]
        with patch.object(toolchain, "run"), patch.object(toolchain, "output", side_effect=[
                MANIFEST["python"], "v" + MANIFEST["caddy"], json.dumps(installed), "",
                "cloudflared version 2025.1.0"]):
            with self.assertRaisesRegex(toolchain.ToolchainError, "cloudflared version mismatch"):
                toolchain.verify_environment(MANIFEST, self.python, self.caddy, Path("synthetic-cloudflared"))

    def test_setup_cli_missing_artifacts_does_not_touch_live_state(self):
        project = self.directory / "project"
        shutil.copytree(ROOT / "scripts", project / "scripts")
        shutil.copytree(ROOT / "deploy", project / "deploy")
        result = subprocess.run(["bash", str(project / "scripts/setup.sh"), "--offline",
                                 "--artifacts", str(self.directory / "empty")],
                                env=os.environ.copy(), text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing artifact", result.stderr)
        self.assertFalse((project / ".runtime").exists())
        self.assertFalse((project / ".venv").exists())

    def test_environment_cli_resolves_project_from_subdirectory(self):
        project = self.directory / "project"
        shutil.copytree(ROOT / "scripts", project / "scripts")
        shutil.copytree(ROOT / "deploy", project / "deploy")
        result = subprocess.run(["bash", "check-env.sh"], cwd=project / "scripts",
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"Missing Python: {project / '.venv/bin/python3'}", result.stderr)
        self.assertFalse((project / ".runtime").exists())

    def test_build_timeout_stops_descendants(self):
        record = self.directory / "child.pid"
        code = ("import os, pathlib, sys, time; child = os.fork(); "
                "pathlib.Path(sys.argv[1]).write_text(str(child)) if child else None; time.sleep(30)")
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                toolchain.run([sys.executable, "-I", "-c", code, record], timeout=1)
            self.assertTrue(record.exists(), "The synthetic build never started")
            stat = Path(f"/proc/{record.read_text()}/stat")
            deadline = time.monotonic() + 2
            while True:
                try:
                    state = stat.read_text().rsplit(") ", 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if state in ("Z", "X"):
                    break
                if time.monotonic() >= deadline:
                    self.fail("The synthetic build child survived its timeout")
                time.sleep(0.01)
        finally:
            # Only the child of this test's owned build can require cleanup.
            if record.exists():
                try:
                    os.kill(int(record.read_text()), toolchain.signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()

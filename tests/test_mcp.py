"""Official SDK stdio discovery and calls against isolated synthetic launchers."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import Client, StdioServerParameters

from test_shares import ShareFixture
from test_tunnel import ROOT, running


class MCPResultTests(unittest.TestCase):
    def test_nullable_keys_preserve_literal_strings_in_sdk_validation(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from mcp_server import expose_files, expose_port
        from mcp.server.mcpserver.utilities.func_metadata import func_metadata

        for function, arguments in (
            (expose_port, {"port": 3000}), (expose_files, {"path": "site/page.html"}),
        ):
            metadata = func_metadata(function)
            self.assertIsNone(metadata.validate_arguments(arguments)["key"])
            for key in (None, "null", "true", "false", "1234", "preview"):
                with self.subTest(tool=function.__name__, key=key):
                    self.assertEqual(metadata.validate_arguments(arguments | {"key": key})["key"], key)

    def test_success_and_failure_keep_structured_data_and_error_flags_on_the_wire(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from mcp_server import response
        from mcp.types import CallToolResult

        share = {"id": "9090.synthetic", "url": "https://example.test/preview/",
                 "source": {"type": "port", "port": 3000}, "url_mode": "path",
                 "update_mode": None, "state": "active"}
        failure = {"error": {"code": "synthetic_error", "message": "Synthetic launcher failure."}}
        for data, error in ((share, False), ({"shares": [share]}, False), (failure, True)):
            with self.subTest(error=error, data=data):
                result = response(data, error=error)
                self.assertEqual(result.structured_content, data)
                self.assertIs(result.is_error, error)
                wire = json.loads(result.model_dump_json(by_alias=True))
                self.assertEqual(wire["structuredContent"], data)
                self.assertIs(wire.get("isError", False), error)
                self.assertEqual(json.loads(wire["content"][0]["text"]), data)
                received = CallToolResult.model_validate(wire, by_alias=True)
                self.assertEqual(received.structured_content, data)
                self.assertEqual(bool(received.is_error), error)


class ToolchainSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="alias-toolchain-setup-")
        self.addCleanup(scratch.cleanup)
        self.project = Path(scratch.name) / "project with spaces"
        self.other_project = Path(scratch.name) / "unrelated project with spaces"
        self.other_project.mkdir()
        self.user_home = Path(scratch.name) / "synthetic user home"
        self.user_home.mkdir()
        self.env = os.environ | {
            "HOME": str(self.user_home), "CODEX_HOME": str(self.user_home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.user_home / ".claude"),
        }
        scripts = self.project / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/setup.sh", scripts / "setup.sh")
        (scripts / "toolchain.py").write_text('''import json
import os
from pathlib import Path
import sys
(Path(__file__).resolve().parents[1] / "setup-arguments.json").write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get("TEST_SETUP_EXIT", "0")))
''')

    def setup(self, *arguments, code=0):
        return subprocess.run(["bash", str(self.project / "scripts/setup.sh"), *arguments],
                              cwd=self.other_project,
                              env=self.env | {"TEST_SETUP_EXIT": str(code)},
                              text=True, capture_output=True, timeout=10)

    def test_setup_forwards_preparation_arguments_without_client_configuration(self):
        for arguments in (
            ("--offline", "--artifacts", "owned artifacts", "--tools", "scratch tools",
             "--venv", "scratch venv"), ("--help",), ("-h",),
        ):
            with self.subTest(arguments=arguments):
                result = self.setup(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((self.project / "setup-arguments.json").read_text()),
                                 ["prepare", *arguments])
                self.assertFalse((self.project / ".codex").exists())
                self.assertFalse((self.project / ".mcp.json").exists())
                self.assertEqual(list(self.user_home.iterdir()), [])

    def test_setup_preserves_operator_client_configuration_and_preparation_failure(self):
        codex = b'# Preserve operator formatting.\nmodel = "operator-model"\n'
        claude = b'{"mcpServers":{"other":{"command":"operator-command"}}}\n'
        configurations = {
            self.project / ".codex/config.toml": codex,
            self.project / ".mcp.json": claude,
            self.user_home / ".codex/config.toml": codex,
            self.user_home / ".claude/.claude.json": claude,
            self.user_home / ".claude.json": claude,
        }
        for path, content in configurations.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for code in (0, 7):
            with self.subTest(code=code):
                result = self.setup(code=code)
                self.assertEqual(result.returncode, code, result.stderr)
                for path, content in configurations.items():
                    self.assertEqual(path.read_bytes(), content, str(path))


class MCPFixture(ShareFixture):
    project_name = "alias installation with spaces"

    def setUp(self):
        self.helpers = set()
        super().setUp()
        self.site = self.project / "site"
        self.site.mkdir()
        self.page = self.site / "preview #é.html"
        self.page.write_bytes(b"<h1>preview</h1>")
        (self.site / "style.css").write_text("body { color: red }")
        # Daemons are mocked; real helpers still need occupied loopback ports excluded.
        (self.project / "bin/ss").write_text('''#!/usr/bin/env python3
from pathlib import Path
import re
import sys
port = int(re.search(r":([0-9]+)", sys.argv[-1])[1])
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    for row in Path(table).read_text().splitlines()[1:]:
        fields = row.split()
        if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port:
            print("occupied")
''')

    def remember_helpers(self):
        for path in self.runtime.glob("publications/*/helper.pid"):
            try:
                self.helpers.add(int(path.read_text()))
            except (FileNotFoundError, ValueError):
                pass  # Another request may be starting or stopping this share.

    def stop_test_processes(self):
        self.remember_helpers()
        try:
            super().stop_test_processes()
        finally:
            for pid in self.helpers:
                if running(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 5
            while any(running(pid) for pid in self.helpers) and time.monotonic() < deadline:
                time.sleep(0.05)
            for pid in self.helpers:
                if running(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def client(self, *, registration=None, cwd=None, env=None):
        registration = registration or {
            "command": "bash", "args": [str(self.project / "scripts/mcp.sh")],
        }
        return Client(StdioServerParameters(
            command=registration["command"], args=registration["args"],
            env=self.env if env is None else env, cwd=cwd or self.site,
        ), read_timeout_seconds=30)

    async def call(self, client, name, arguments=None, *, error=False):
        try:
            result = await client.call_tool(name, arguments or {})
        finally:
            self.remember_helpers()
        self.assertEqual(result.is_error, error, result)
        data = result.structured_content
        self.assertIsInstance(data, dict)
        self.assertEqual(json.loads(result.content[0].text), data)
        return data

    def run_client(self, body):
        asyncio.run(asyncio.wait_for(body(), timeout=90))


class MCPTests(MCPFixture):
    def assert_source_guidance(self, text):
        self.assertIn("Relative file paths resolve from the alias installation root", text)
        self.assertIn(str(self.project), text)
        self.assertIn("Use absolute paths for files in other projects.", text)

    def assert_guidance(self, text):
        for phrase in (
            "Always provide a key", "bare-domain", "path mode", "meaningful and useful",
            "potentially sensitive", "uncertain", "cryptographically random", "opaque key", "fallback",
            "Do not put sensitive details", "32 random hex", "obscurity",
            "not authentication", "explicit selection",
        ):
            self.assertIn(phrase, text)

    def test_stdio_initialization_discovery_schemas_and_guidance(self):
        async def body():
            async with self.client() as client:
                self.assert_guidance(client.instructions)
                self.assert_source_guidance(client.instructions)
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                self.assertEqual(set(tools), {"expose_port", "expose_files", "list_shares", "stop_share"})
                for name in ("expose_port", "expose_files"):
                    self.assert_guidance(tools[name].description)
                    properties = tools[name].input_schema["properties"]
                    self.assertEqual(properties["url_mode"]["default"], "path")
                    self.assertEqual(properties["url_mode"]["enum"], ["path", "subdomain", "no-key"])
                    self.assertIsNone(properties["key"]["default"])
                    key = properties["key"]["anyOf"][0]
                    self.assertEqual((key["minLength"], key["maxLength"]), (1, 32))
                    self.assertIn("pattern", key)
                    schema = tools[name].output_schema
                    self.assertTrue({"id", "url", "source", "url_mode", "update_mode", "state"}
                                    <= set(schema["required"]))
                    self.assertIn("source_revision", schema["properties"])
                port = tools["expose_port"].input_schema["properties"]["port"]
                self.assertEqual((port["minimum"], port["maximum"]), (1, 65535))
                files = tools["expose_files"].input_schema
                self.assert_source_guidance(tools["expose_files"].description)
                self.assert_source_guidance(files["properties"]["path"]["description"])
                self.assertEqual(files["required"], ["path"])
                self.assertEqual(files["properties"]["path"]["minLength"], 1)
                self.assertIn("pattern", files["properties"]["path"])
                self.assertEqual(files["properties"]["update_mode"]["default"], "live")
                self.assertEqual(files["properties"]["update_mode"]["enum"], ["snapshot", "manual", "live"])
                self.assertIn("pattern", tools["stop_share"].input_schema["properties"]["id"])
                self.assertEqual(tools["list_shares"].input_schema.get("properties", {}), {})
                self.assertEqual(tools["list_shares"].output_schema["required"], ["shares"])
                self.assertEqual(tools["stop_share"].input_schema["required"], ["id"])
                self.assertEqual(await self.call(client, "list_shares"), {"shares": []})
                self.assertFalse(self.runtime.exists())
        self.run_client(body)

    def test_concurrent_results_survive_session_and_stop_individually(self):
        self.env.update(DEFAULT_MODE="no-key", DETACH="0", ID_LENGTH="4")
        async def body():
            async with self.client() as client:
                shares = await asyncio.gather(*(
                    self.call(client, "expose_port", {"port": port}) for port in (3000, 3001)
                ))
                self.assertEqual([share["source"] for share in shares], [
                    {"type": "port", "port": 3000}, {"type": "port", "port": 3001},
                ])
                self.assertEqual(len({share["id"] for share in shares}), 2)
                for share in shares:
                    self.assertRegex(share["url"], r"^https://example\.test/[a-f0-9]{32}/$")
                    self.assertEqual((share["url_mode"], share["update_mode"], share["state"]),
                                     ("path", None, "active"))
            self.assertTrue(all(running(int(row[4])) for row in self.registry()))
            self.source.unlink()
            (self.runtime / "current-share-url.txt").write_text("not a URL")
            self.assertEqual({share["id"] for share in self.success("list-shares")},
                             {share["id"] for share in shares})
            async with self.client() as client:
                listing = await self.call(client, "list_shares")
                self.assertEqual({share["id"] for share in listing["shares"]},
                                 {share["id"] for share in shares})
                stopped = await self.call(client, "stop_share", {"id": shares[0]["id"]})
                self.assertEqual(stopped, shares[0] | {"state": "stopped"})
                self.assertEqual(await self.call(client, "list_shares"), {"shares": [shares[1]]})
        self.run_client(body)

    def test_file_default_modes_sources_and_session_independence(self):
        async def body():
            async with self.client() as client:
                default = await self.call(client, "expose_files", {"path": "site/preview #é.html"})
                self.assertEqual(default["source"], {"type": "file", "path": str(self.page)})
                self.assertEqual(default["update_mode"], "live")
                self.assertRegex(default["url"],
                                 r"^https://example\.test/[a-f0-9]{32}/$")
                self.assertEqual(default["source_revision"], hashlib.sha256(self.page.read_bytes()).hexdigest())
                self.assertRegex(default["revision"], r"^[a-f0-9]{64}$")
                public = self.runtime / "publications" / default["id"] / "public"
                self.assertEqual([path.name for path in public.iterdir()], [self.page.name])
                manual = await self.call(client, "expose_files", {
                    "path": "site", "url_mode": "subdomain", "update_mode": "manual", "key": "preview",
                })
                self.assertEqual(manual["url"], "https://preview.example.test/")
                self.assertEqual(manual["source"], {"type": "directory", "path": str(self.site)})
                snapshot = await self.call(client, "expose_files", {
                    "path": str(self.page), "url_mode": "no-key", "update_mode": "snapshot",
                })
                self.assertEqual(snapshot["url"], "https://example.test/")
                self.assertEqual(snapshot["update_mode"], "snapshot")
            self.assertEqual(len(self.helpers), 2)
            self.assertTrue(all(running(pid) for pid in self.helpers))
            shares = self.success("list-shares")
            self.assertEqual({share["id"] for share in shares}, {default["id"], manual["id"], snapshot["id"]})
            self.assertTrue(all(share["state"] == "active" for share in shares))
            self.success("stop-share", default["id"])
            self.assertFalse((self.runtime / "publications" / default["id"]).exists())
            self.assertEqual(len(self.success("list-shares")), 2)
            self.assertEqual(self.page.read_bytes(), b"<h1>preview</h1>")
        self.run_client(body)

    def test_concurrent_file_and_port_results_remain_request_local(self):
        async def body():
            async with self.client() as client:
                file_share, port_share = await asyncio.gather(
                    self.call(client, "expose_files", {
                        "path": "site/preview #é.html", "update_mode": "snapshot", "key": "files",
                    }),
                    self.call(client, "expose_port", {"port": 3000, "key": "backend"}),
                )
                self.assertEqual(file_share["source"], {"type": "file", "path": str(self.page)})
                self.assertEqual(port_share["source"], {"type": "port", "port": 3000})
                self.assertNotEqual(file_share["id"], port_share["id"])
                self.assertEqual(file_share["url"], "https://example.test/files/")
                self.assertEqual(port_share["url"], "https://example.test/backend/")
                listing = await self.call(client, "list_shares")
                self.assertEqual({share["id"] for share in listing["shares"]},
                                 {file_share["id"], port_share["id"]})
                await self.call(client, "stop_share", {"id": file_share["id"]})
                self.assertEqual(await self.call(client, "list_shares"), {"shares": [port_share]})
        self.run_client(body)

    def test_schema_rejections_and_structured_launcher_errors(self):
        async def body():
            async with self.client() as client:
                for name, arguments in (
                    ("expose_port", {}), ("expose_port", {"port": 0}),
                    ("expose_port", {"port": 65536}), ("expose_port", {"port": "3000"}),
                    ("expose_port", {"port": True}),
                    ("expose_port", {"port": 3000, "url_mode": "unknown"}),
                    ("expose_port", {"port": 3000, "key": "bad-key-"}),
                    ("expose_port", {"port": 3000, "key": "x" * 33}),
                    ("expose_files", {"path": ""}), ("expose_files", {"path": "site\n"}),
                    ("expose_files", {"path": "site\x00"}),
                    ("expose_files", {"path": "site", "update_mode": "unknown"}),
                    ("stop_share", {"id": "../escape"}),
                ):
                    with self.subTest(name=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertTrue(result.is_error, result)
                        self.assertFalse(self.runtime.exists())
                unknown = await self.call(client, "stop_share", {"id": "9090.unknown"}, error=True)
                self.assertEqual(unknown["error"]["code"], "unknown_share")
                for name, arguments in (
                    ("expose_port", {"port": 3000, "url_mode": "no-key", "key": "preview"}),
                    ("expose_files", {"path": "missing"}),
                ):
                    failure = await self.call(client, name, arguments, error=True)
                    self.assertEqual(failure["error"]["code"], "launcher_error")
                    self.assertTrue(failure["error"]["message"])
                self.assertFalse(self.runtime.exists())
        self.run_client(body)

    def test_argument_arrays_preserve_literal_paths_and_do_not_invoke_shell(self):
        name = "literal $(touch unexpected) `touch also-unexpected`.json"
        selected = self.project / name
        selected.write_text('{"literal":true}')
        async def body():
            async with self.client() as client:
                share = await self.call(client, "expose_files", {
                    "path": name, "update_mode": "snapshot", "key": "literal",
                })
                self.assertEqual(share["source"]["path"], str(selected))
                self.assertFalse((self.project / "unexpected").exists())
                self.assertFalse((self.project / "also-unexpected").exists())
        self.run_client(body)

    def test_nullable_keys_preserve_literal_strings_over_stdio(self):
        async def body():
            async with self.client() as client:
                for key in ("null", "true", "false", "1234"):
                    share = await self.call(client, "expose_port", {"port": 3000, "key": key})
                    self.assertEqual(share["url"], f"https://example.test/{key}/")
                    await self.call(client, "stop_share", {"id": share["id"]})
                files = await self.call(client, "expose_files", {
                    "path": "site/preview #é.html", "update_mode": "snapshot", "key": "null",
                })
                self.assertEqual(files["url"], "https://example.test/null/")
                await self.call(client, "stop_share", {"id": files["id"]})
                generated = await self.call(client, "expose_port", {"port": 3000, "key": None})
                self.assertRegex(generated["url"], r"^https://example\.test/[a-f0-9]{32}/$")
        self.run_client(body)

    def test_invalid_launcher_output_is_a_structured_tool_error(self):
        async def body():
            async with self.client() as client:
                for output in ("not-json", "{}", '[{"id":"incomplete"}]'):
                    (self.project / "scripts/tunnel.sh").write_text("printf '%s' '" + output + "'\n")
                    error = await self.call(client, "list_shares", error=True)
                    self.assertEqual(error["error"]["code"], "invalid_launcher_result")
                (self.project / "scripts/tunnel.sh").write_text("printf '%s' '{}'\nexit 1\n")
                error = await self.call(client, "list_shares", error=True)
                self.assertEqual(error["error"]["code"], "invalid_launcher_result")
                (self.project / "scripts/tunnel.sh").write_text("printf '%s' '{}'\n")
                for name, arguments in (
                    ("expose_port", {"port": 3000}),
                    ("expose_files", {"path": "site"}),
                    ("stop_share", {"id": "9090.unknown"}),
                ):
                    error = await self.call(client, name, arguments, error=True)
                    self.assertEqual(error["error"]["code"], "invalid_launcher_result")
        self.run_client(body)

    def test_user_registered_wrapper_starts_from_any_project_and_shares_foreign_files(self):
        other_project = self.project.parent / "other project with spaces"
        other_project.mkdir()
        page = other_project / "foreign page.html"
        original = b"<h1>other project</h1>"
        page.write_bytes(original)
        registration = {"command": "bash", "args": [str(self.project / "scripts/mcp.sh")]}
        self.assertEqual(os.readlink(ROOT / "CLAUDE.md"), "AGENTS.md")
        self.assertEqual(os.readlink(ROOT / ".claude/skills"), "../.agents/skills")
        async def body():
            for cwd in (self.project, self.site, other_project):
                async with self.client(registration=registration, cwd=cwd) as client:
                    self.assertEqual(await self.call(client, "list_shares"), {"shares": []})
                    self.assert_source_guidance(client.instructions)
            # An installed venv works independently of the client's project.
            (self.project / ".venv").symlink_to(sys.prefix, target_is_directory=True)
            env = self.env.copy()
            env.pop("ALIAS_PYTHON")
            async with self.client(registration=registration, cwd=other_project, env=env) as client:
                share = await self.call(client, "expose_files", {
                    "path": str(page), "update_mode": "snapshot", "key": "foreign-page",
                })
                self.assertEqual(share["source"], {"type": "file", "path": str(page)})
                public = self.runtime / "publications" / share["id"] / "public"
                self.assertEqual((public / page.name).read_bytes(), original)
                self.assertFalse((other_project / ".runtime").exists())
                await self.call(client, "stop_share", {"id": share["id"]})
                self.assertEqual(page.read_bytes(), original)
                self.assertEqual(await self.call(client, "list_shares"), {"shares": []})
        self.run_client(body)

    def test_wrapper_reports_missing_prepared_interpreter_on_stderr(self):
        env = self.env | {"ALIAS_PYTHON": str(self.project / "missing/python")}
        result = subprocess.run(["bash", str(self.project / "scripts/mcp.sh")],
                                cwd=self.site, env=env, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("scripts/setup.sh", result.stderr)
        self.assertFalse(self.runtime.exists())

"""Offline checks: isolated state, synthetic config and mocked daemons."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DAEMON = """#!/usr/bin/env python3
import os
from pathlib import Path
import signal
import sys
import time
name = Path(sys.argv[0]).name
if os.environ.get('FAIL_' + name.upper()) == '1':
    sys.exit(99)
with open(os.environ['TEST_DAEMON_RECORD'], 'a') as record:
    record.write(str(os.getpid()) + '\\t' + name + '\\n')
signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
while True:
    time.sleep(0.1)
"""


def running(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        return state not in ("Z", "X")
    except FileNotFoundError:
        return False


class TunnelFixture(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="cloudflared-alias-test-")
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name) / "project"
        self.project.mkdir()
        shutil.copytree(ROOT / "scripts", self.project / "scripts")
        shutil.copytree(ROOT / "deploy", self.project / "deploy")
        shutil.copyfile(ROOT / "cloudflared-alias.conf", self.project / "cloudflared-alias.conf")
        self.env = os.environ.copy()
        for key in ("DEFAULT_MODE", "CADDY_PORT", "ID_LENGTH", "DETACH", "SUBDOMAIN_DOMAIN", "TUNNEL_NAME", "TUNNEL_HOSTNAME", "TUNNEL_CREDENTIALS_FILE", "CLOUDFLARED_BASE_CONFIG", "ALIAS_PYTHON"):
            self.env.pop(key, None)
        self.source = self.project / "source.yml"
        self.source.write_text("tunnel: synthetic-tunnel\ncredentials-file: /synthetic/never-read.json\ningress:\n  - hostname: example.test\n    service: http_status:404\n")
        self.env.update(CLOUDFLARED_BASE_CONFIG=str(self.source), DETACH="1")
        self.record = self.project / "daemon-record"
        self.env["TEST_DAEMON_RECORD"] = str(self.record)
        binaries = self.project / "bin"
        binaries.mkdir()
        for name in ("caddy", "cloudflared"):
            stub = binaries / name
            stub.write_text(DAEMON)
            stub.chmod(0o755)
        (binaries / "ss").write_text("#!/bin/sh\nexit 0\n")
        (binaries / "ss").chmod(0o755)
        self.env["PATH"] = f"{binaries}:{self.env['PATH']}"
        self.launchers = []
        self.addCleanup(self.stop_test_processes)

    def invoke(self, *arguments, input=None):
        return subprocess.run(["bash", str(self.project / "scripts/tunnel.sh"), *arguments], env=self.env, text=True, capture_output=True, input=input, timeout=30)

    def functions(self, script, *arguments):
        return subprocess.run(["bash", "-c", 'source "$1"; shift; ' + script, "test", str(self.project / "scripts/tunnel.sh"), *arguments], env=self.env, text=True, capture_output=True, timeout=10)

    @property
    def runtime(self):
        return self.project / ".runtime"

    def registry(self):
        path = self.runtime / "registry"
        return [line.split("\t") for line in path.read_text().splitlines()] if path.exists() else []

    def assert_started(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Share URL", result.stdout)

    def daemon_pids(self, name=None):
        if not self.record.exists():
            return []
        return [int(pid) for pid, program in (line.split("\t") for line in self.record.read_text().splitlines()) if name is None or name == program]

    def stop_test_processes(self):
        for launcher in self.launchers:
            if launcher.poll() is None:
                launcher.terminate()
            try:
                launcher.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                launcher.kill()
                launcher.communicate()
        if self.runtime.exists():
            result = self.invoke("stop")
            self.assertEqual(result.returncode, 0, result.stderr)
        # Only exact PIDs recorded by this isolated test may be killed on failure.
        for pid in self.daemon_pids():
            if running(pid):
                os.kill(pid, signal.SIGTERM)

    def foreground(self, *arguments):
        process = subprocess.Popen(["bash", str(self.project / "scripts/tunnel.sh"), *arguments], env=self.env | {"DETACH": "0"}, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.launchers.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current_id = self.runtime / "current-path-id.txt"
            if current_id.exists() and current_id.read_text().strip() == arguments[-1] and (self.runtime / "tunnel-history").exists():
                return process
            if process.poll() is not None:
                self.fail("Foreground start failed: " + repr(process.communicate()))
            time.sleep(0.05)
        self.fail("Foreground start did not finish")


class TunnelTests(TunnelFixture):
    def test_help_has_no_runtime_effect(self):
        for flag in ("--help", "-h"):
            result = self.invoke(flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Usage:", result.stdout)
            self.assertFalse(self.runtime.exists())

    def test_missing_arguments(self):
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertFalse(self.runtime.exists())

    def test_rejects_invalid_configuration_before_runtime(self):
        for port in ("0", "65536", "1oops", "999999999999999999999"):
            result = self.invoke(port)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("backend port", result.stderr)
            self.assertFalse(self.runtime.exists())
        for length in ("", "0", "33", "oops", "1+1", "x[$(touch bad)]"):
            self.env["ID_LENGTH"] = length
            result = self.invoke("3000")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ID_LENGTH", result.stderr)
            self.assertFalse(self.runtime.exists())
        self.env.pop("ID_LENGTH")
        for arguments in (("--unknown", "3000"), ("3000", "bad-key-"), ("--list", "3000"), ("-n", "3000", "key")):
            result = self.invoke(*arguments)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.runtime.exists())

    def test_rejects_hostname_control_characters_preserving_runtime(self):
        self.assert_started(self.invoke("3000", "key"))
        snapshot = {path: path.read_bytes() for path in self.runtime.rglob("*") if path.is_file()}
        pids = self.daemon_pids()
        for hostname in ("example.test\nsecond.test", "example.test\n", "example.test\r", "example.test\t", "example.test\x01", "example.test\x7f"):
            for mode in ("-p", "-s", "-n"):
                with self.subTest(hostname=hostname, mode=mode):
                    self.env["TUNNEL_HOSTNAME"] = hostname
                    arguments = (mode, "3001") if mode == "-n" else (mode, "3001", "other-key")
                    result = self.invoke(*arguments)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid hostname", result.stderr)
                    self.assertNotIn("Starting", result.stdout)
                    self.assertEqual(self.daemon_pids(), pids)
                    self.assertTrue(all(running(pid) for pid in pids))
                    self.assertEqual({path: path.read_bytes() for path in self.runtime.rglob("*") if path.is_file()}, snapshot)

    def test_environment_precedence_and_duplicate_config(self):
        (self.project / "cloudflared-alias.conf").write_text("CADDY_PORT=9091\nCADDY_PORT=9092\nDETACH=0\n")
        result = self.functions('load_config; printf "%s %s" "$CADDY_PORT" "$DETACH"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "9092 1")
        self.env["CADDY_PORT"] = "18080"
        result = self.functions('load_config; printf "%s %s" "$CADDY_PORT" "$DETACH"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "18080 1")

    def test_quoted_values_ignore_quotes_in_inline_comments(self):
        for value, expected in (("'path' # 'default'", "path"), ('"path" # "default"', "path"), ("'a''b #c' # 'comment'", "a'b #c"), ('"a \\\"b\\\" \\\\c #d" # "comment"', 'a "b" \\c #d')):
            with self.subTest(value=value):
                result = self.functions('scalar_value "$1"', value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        (self.project / "cloudflared-alias.conf").write_text("DEFAULT_MODE='path' # 'default'\n")
        self.source.write_text('tunnel: "quoted-tunnel" # "synthetic"\ncredentials-file: \'/synthetic/never-read.json\' # \'credential path\'\ningress:\n  - hostname: "example.test" # "public hostname"\n')
        self.assert_started(self.invoke("3000", "key"))
        self.assertEqual(self.registry()[0][6], "example.test")
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertIn("tunnel: 'quoted-tunnel'", config)
        self.assertIn("credentials-file: '/synthetic/never-read.json'", config)

    def test_numeric_key_and_decimal_ports(self):
        self.env.update(CADDY_PORT="03000", ID_LENGTH="08")
        self.assert_started(self.invoke("03000", "1234"))
        self.assertEqual(self.registry()[0][1:4], ["1234", "3000", "3001"])
        self.assertIn("path: '^/1234(/.*)?$'", (self.runtime / "cloudflared/config.yml").read_text())

    def test_random_key_length(self):
        self.env["ID_LENGTH"] = "08"
        self.assert_started(self.invoke("3000"))
        self.assertRegex(self.registry()[0][1], r"^[a-z0-9]{8}$")

    def test_backend_cannot_use_running_caddy_port(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        snapshot = {path: path.read_text() for path in (
            self.runtime / "cloudflared/config.yml", self.runtime / "cloudflared/cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        for key in ("other-key", "key"):
            with self.subTest(key=key):
                result = self.invoke(rows[0][3], key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("used by a running Caddy instance", result.stderr)
                self.assertEqual(self.registry(), rows)
                self.assertTrue(running(int(rows[0][4])))
                for path, content in snapshot.items():
                    self.assertEqual(path.read_text(), content, str(path))
                self.assertTrue(running(int(snapshot[self.runtime / "cloudflared/cloudflared.pid"])))
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 1)
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_no_key_registry_and_history_reuse(self):
        self.assert_started(self.invoke("-n", "3000"))
        row = self.registry()[0]
        self.assertEqual(row[:4], ["no-key", "", "3000", "9090"])
        self.assertTrue(running(int(row[4])))
        original = (self.runtime / "tunnel-history").read_text().split("\t")
        self.assertEqual(original[:4], ["no-key", "", "3000", "https://example.test/"])
        self.assert_started(self.invoke("--list", input="01\n\n"))
        self.assertEqual(len(self.registry()), 1)
        history = (self.runtime / "tunnel-history").read_text().splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].split("\t")[4], original[4])
        self.assertIn("service: http://localhost:", (self.runtime / "cloudflared/config.yml").read_text())

    def test_environment_tunnel_values_and_quoted_yaml(self):
        self.env.update(TUNNEL_NAME="override-tunnel", TUNNEL_HOSTNAME="override.test", TUNNEL_CREDENTIALS_FILE="/synthetic/a: b's #file.json")
        self.source.unlink()
        self.assert_started(self.invoke("3000", "key"))
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertIn("tunnel: 'override-tunnel'", config)
        self.assertIn("credentials-file: '/synthetic/a: b''s #file.json'", config)
        self.assertIn("hostname: 'override.test'", config)
        self.assertNotIn("synthetic-tunnel", config)

    def test_quoted_source_config(self):
        self.source.write_text("tunnel: \"quoted-tunnel\" # comment\ncredentials-file: 'creds:a #b.json'\ningress:\n  - hostname: '*.example.test'\n")
        self.assert_started(self.invoke("-s", "3000", "test-key"))
        self.assertEqual(self.registry()[0][6], "test-key.example.test")
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertIn(f"credentials-file: '{self.project}/creds:a #b.json'", config)
        self.assertIn("hostname: 'test-key.example.test'", config)

    def test_path_ingress_does_not_shadow_other_keys(self):
        self.assert_started(self.invoke("3000", "key"))
        self.env["TUNNEL_HOSTNAME"] = "second.test"
        self.assert_started(self.invoke("3001", "key-two"))
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertIn("hostname: 'example.test'", config)
        self.assertIn("hostname: 'second.test'", config)
        patterns = re.findall(r"path: '([^']+)'", config)
        self.assertEqual(len(patterns), 2)
        self.assertTrue(re.search(patterns[0], "/key"))
        self.assertTrue(re.search(patterns[0], "/key/swagger"))
        self.assertFalse(re.search(patterns[0], "/key-two/swagger"))
        self.assertFalse(re.search(patterns[0], "/other/key/swagger"))
        self.assertTrue(re.search(patterns[1], "/key-two/swagger"))

    def test_no_key_ingress_follows_keyed_rules(self):
        self.assert_started(self.invoke("-n", "3000"))
        self.assert_started(self.invoke("3001", "key"))
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertLess(config.index("path:"), config.index("service: http://localhost:9090"))
        self.assertEqual(len(self.registry()), 2)

    def test_path_ingress_precedes_hostname_rules(self):
        self.assert_started(self.invoke("-s", "3000", "host-key"))
        self.env["TUNNEL_HOSTNAME"] = "host-key.example.test"
        self.assert_started(self.invoke("-n", "3001"))
        self.assert_started(self.invoke("3002", "path-key"))
        rows = self.registry()
        self.assertEqual([row[0] for row in rows], ["subdomain", "no-key", "path"])
        self.assertTrue(all(row[6] == "host-key.example.test" for row in rows))
        config = (self.runtime / "cloudflared/config.yml").read_text()
        services = [config.index("service: http://localhost:" + row[3]) for row in rows]
        self.assertLess(services[2], services[0])
        self.assertLess(services[0], services[1])

    def test_failed_daemon_start_rolls_back(self):
        for daemon in ("CADDY", "CLOUDFLARED"):
            self.env[f"FAIL_{daemon}"] = "1"
            result = self.invoke("3000", "key")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("failed to start", result.stderr)
            self.assertEqual(self.registry(), [])
            self.assertFalse((self.runtime / "current-share-url.txt").exists())
            self.assertFalse((self.runtime / "tunnel-history").exists())
            self.assertFalse(any(running(pid) for pid in self.daemon_pids()))
            self.env.pop(f"FAIL_{daemon}")

    def test_failed_caddy_start_preserves_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        shared_pid = (self.runtime / "cloudflared/cloudflared.pid").read_text()
        config = (self.runtime / "cloudflared/config.yml").read_text()
        history = (self.runtime / "tunnel-history").read_text()
        self.env["FAIL_CADDY"] = "1"
        result = self.invoke("3001", "key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Caddy failed to start", result.stderr)
        self.assertEqual(self.registry(), rows)
        self.assertTrue(running(int(rows[0][4])))
        self.assertEqual((self.runtime / "cloudflared/cloudflared.pid").read_text(), shared_pid)
        self.assertTrue(running(int(shared_pid)))
        self.assertEqual((self.runtime / "cloudflared/config.yml").read_text(), config)
        self.assertEqual((self.runtime / "tunnel-history").read_text(), history)
        self.assertEqual((self.runtime / "current-share-url.txt").read_text().strip(), "https://example.test/key/")

    def test_failed_cloudflared_start_preserves_instances_and_replacements(self):
        foreground = self.foreground("3000", "key-one")
        self.assert_started(self.invoke("-n", "3001"))
        rows = self.registry()
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_text() for path in (
            shared / "config.yml", shared / "cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        self.env["FAIL_CLOUDFLARED"] = "1"
        for arguments in (("3002", "key-two"), ("3002", "key-one"), ("3000", "other-key"), ("-n", "3002")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cloudflared failed to start", result.stderr)
                self.assertEqual(self.registry(), rows)
                self.assertTrue(all(running(int(row[4])) for row in rows))
                self.assertIsNone(foreground.poll())
                for path, content in snapshot.items():
                    self.assertEqual(path.read_text(), content, str(path))
                self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 2)
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)
                self.assertEqual(sorted(path.name for path in shared.iterdir()), ["cloudflared.log", "cloudflared.pid", "config.yml"])

    def test_failed_start_after_last_detached_caddy_exit_cleans_shared_state(self):
        for failure in ("caddy", "port-probe"):
            with self.subTest(failure=failure):
                self.assert_started(self.invoke("3000", "key"))
                row = self.registry()[0]
                history = (self.runtime / "tunnel-history").read_text()
                os.kill(int(row[4]), signal.SIGTERM)
                deadline = time.monotonic() + 5
                while running(int(row[4])) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(running(int(row[4])))
                if failure == "caddy":
                    self.env["FAIL_CADDY"] = "1"
                else:
                    (self.project / "bin/ss").write_text("#!/bin/sh\nexit 1\n")
                result = self.invoke("3001", "other-key")
                self.assertNotEqual(result.returncode, 0)
                expected_error = "Caddy failed to start" if failure == "caddy" else "Could not inspect listening ports"
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(self.registry(), [])
                for name in ("cloudflared/cloudflared.pid", "cloudflared/config.yml", "current-share-url.txt", "current-path-id.txt"):
                    self.assertFalse((self.runtime / name).exists(), name)
                self.assertEqual((self.runtime / "tunnel-history").read_text(), history)
                self.assertFalse(any(running(pid) for pid in self.daemon_pids()))
                self.env.pop("FAIL_CADDY", None)
                (self.project / "bin/ss").write_text("#!/bin/sh\nexit 0\n")

    def test_failed_start_reconciles_pruned_routes_with_live_fallback(self):
        for failure in ("caddy", "port-probe", "tunnel-name", "credentials-file"):
            with self.subTest(failure=failure):
                for key in ("FAIL_CADDY", "TUNNEL_NAME", "TUNNEL_CREDENTIALS_FILE", "TUNNEL_HOSTNAME"):
                    self.env.pop(key, None)
                (self.project / "bin/ss").write_text("#!/bin/sh\nexit 0\n")
                self.assertEqual(self.invoke("stop").returncode, 0)
                self.assert_started(self.invoke("-n", "3000"))
                self.assert_started(self.invoke("3001", "dead-key"))
                fallback, dead = self.registry()
                # Six-field legacy rows must not inherit the rejected hostname.
                (self.runtime / "registry").write_text("\t".join(fallback[:6]) + "\n" + "\t".join(dead) + "\n")
                shared = self.runtime / "cloudflared"
                previous_pid = int((shared / "cloudflared.pid").read_text())
                history = (self.runtime / "tunnel-history").read_text()
                self.assertIn("^/dead-key(/.*)?$", (shared / "config.yml").read_text())
                self.assertEqual((self.runtime / "current-path-id.txt").read_text().strip(), "dead-key")
                os.kill(int(dead[4]), signal.SIGTERM)
                deadline = time.monotonic() + 5
                while running(int(dead[4])) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(running(int(dead[4])))
                self.env["TUNNEL_HOSTNAME"] = "candidate.test"
                if failure == "caddy":
                    self.env["FAIL_CADDY"] = "1"
                elif failure == "port-probe":
                    (self.project / "bin/ss").write_text("#!/bin/sh\nexit 1\n")
                elif failure == "tunnel-name":
                    self.env["TUNNEL_NAME"] = "different-tunnel"
                else:
                    self.env["TUNNEL_CREDENTIALS_FILE"] = "/synthetic/different.json"
                result = self.invoke("3002", "new-key")
                self.assertNotEqual(result.returncode, 0)
                expected_error = {
                    "caddy": "Caddy failed to start",
                    "port-probe": "Could not inspect listening ports",
                    "tunnel-name": "different tunnel or credentials-file",
                    "credentials-file": "different tunnel or credentials-file",
                }[failure]
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(self.registry(), [fallback])
                self.assertTrue(running(int(fallback[4])))
                config = (shared / "config.yml").read_text()
                self.assertIn("tunnel: 'synthetic-tunnel'", config)
                self.assertIn("credentials-file: '/synthetic/never-read.json'", config)
                self.assertIn("hostname: 'example.test'", config)
                self.assertIn(f"service: http://localhost:{fallback[3]}", config)
                self.assertNotIn("path:", config)
                self.assertNotIn(f"service: http://localhost:{dead[3]}", config)
                self.assertNotIn("candidate.test", config)
                self.assertEqual((self.runtime / "current-share-url.txt").read_text().strip(), "https://example.test/")
                self.assertEqual((self.runtime / "current-path-id.txt").read_text().strip(), "")
                self.assertEqual((self.runtime / "tunnel-history").read_text(), history)
                self.assertFalse(running(previous_pid))
                self.assertTrue(running(int((shared / "cloudflared.pid").read_text())))
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 1)
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_legacy_routes_recover_per_instance_before_candidate_overrides(self):
        self.env["TUNNEL_HOSTNAME"] = "fallback.test"
        self.assert_started(self.invoke("-n", "3000"))
        self.env["TUNNEL_HOSTNAME"] = "paths.test"
        self.assert_started(self.invoke("3001", "path-key"))
        self.env["SUBDOMAIN_DOMAIN"] = "legacy.test"
        self.assert_started(self.invoke("-s", "3002", "host-key"))
        self.assert_started(self.invoke("3003", "dead-key"))
        rows = self.registry()
        (self.runtime / "registry").write_text("\n".join("\t".join(row[:6]) for row in rows))
        shared = self.runtime / "cloudflared"
        # The original launcher generated double-quoted hostnames.
        config = (shared / "config.yml").read_text()
        for row in rows:
            config = config.replace(f"hostname: '{row[6]}'", f'hostname: "{row[6]}"')
        (shared / "config.yml").write_text(config)
        history = (self.runtime / "tunnel-history").read_text()
        previous_pid = int((shared / "cloudflared.pid").read_text())
        os.kill(int(rows[-1][4]), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while running(int(rows[-1][4])) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(running(int(rows[-1][4])))
        self.env.update(TUNNEL_HOSTNAME="candidate.test", SUBDOMAIN_DOMAIN="candidate-domain.test", FAIL_CADDY="1")
        result = self.invoke("3004", "new-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Caddy failed to start", result.stderr)
        self.assertEqual(self.registry(), rows[:-1])
        self.assertTrue(all(running(int(row[4])) for row in rows[:-1]))
        config = (shared / "config.yml").read_text()
        for row in rows[:-1]:
            self.assertIn(f"hostname: '{row[6]}'", config)
            self.assertIn(f"service: http://localhost:{row[3]}", config)
        self.assertNotIn("dead-key", config)
        self.assertNotIn("candidate", config)
        self.assertEqual((self.runtime / "current-share-url.txt").read_text().strip(), "https://host-key.legacy.test/")
        self.assertEqual((self.runtime / "current-path-id.txt").read_text().strip(), "host-key")
        self.assertEqual((self.runtime / "tunnel-history").read_text(), history)
        self.assertFalse(running(previous_pid))
        self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)
        self.env.pop("FAIL_CADDY")
        self.assert_started(self.invoke("-s", "3004", "new-key"))
        self.assertEqual(self.registry()[:-1], rows[:-1])
        self.assertEqual(self.registry()[-1][6], "new-key.candidate-domain.test")

    def test_unrecoverable_legacy_routes_preserve_state_atomically(self):
        self.assert_started(self.invoke("3000", "first-key"))
        self.env["TUNNEL_HOSTNAME"] = "second.test"
        self.assert_started(self.invoke("3001", "second-key"))
        rows = self.registry()
        registry = self.runtime / "registry"
        registry.write_text("\n".join("\t".join(row[:6]) for row in rows))
        shared = self.runtime / "cloudflared"
        config_path = shared / "config.yml"
        original_config = config_path.read_text()
        second_rule = f"  - hostname: 'second.test'\n    path: '^/second-key(/.*)?$'\n    service: http://localhost:{rows[1][3]}\n"
        self.assertIn(second_rule, original_config)
        snapshot = {path: path.read_text() for path in (
            registry, shared / "cloudflared.pid", self.runtime / "tunnel-history",
            self.runtime / "current-share-url.txt", self.runtime / "current-path-id.txt",
        )}
        self.env.update(TUNNEL_HOSTNAME="candidate.test", SUBDOMAIN_DOMAIN="candidate-domain.test")
        cases = {
            "missing-config": None,
            "missing-rule": original_config.replace(second_rule, ""),
            "ambiguous-rule": original_config.replace("  - service: http_status:404", second_rule.replace("second.test", "other.test") + "  - service: http_status:404"),
            "duplicate-hostname": original_config.replace("  - hostname: 'second.test'", "  - hostname: 'second.test'\n    hostname: 'other.test'"),
            "late-duplicate-hostname": original_config.replace(second_rule, second_rule + "    hostname: 'other.test'\n"),
            "duplicate-service": original_config.replace(second_rule, second_rule + f"    service: http://localhost:{rows[1][3]}\n"),
            "missing-hostname": original_config.replace("  - hostname: 'second.test'\n    path:", "  - path:"),
            "malformed-hostname": original_config.replace("hostname: 'second.test'", "hostname: 'second.test"),
        }
        for name, config in cases.items():
            with self.subTest(name=name):
                if config is None:
                    config_path.unlink()
                else:
                    config_path.write_text(config)
                result = self.invoke("3002", "new-key")
                self.assertNotEqual(result.returncode, 0)
                expected_error = "Cannot recover shared tunnel identity" if config is None else "Cannot recover hostname"
                self.assertIn(expected_error, result.stderr)
                self.assertIn("Existing runtime state was preserved", result.stderr)
                self.assertNotIn("Starting Caddy", result.stdout)
                for path, content in snapshot.items():
                    self.assertEqual(path.read_text(), content, str(path))
                if config is None:
                    self.assertFalse(config_path.exists())
                else:
                    self.assertEqual(config_path.read_text(), config)
                self.assertTrue(all(running(int(row[4])) for row in rows))
                self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
                self.assertEqual(len(self.daemon_pids("caddy")), 2)
                self.assertEqual(len(self.daemon_pids("cloudflared")), 2)
                self.assertFalse(list(self.runtime.glob("registry.*")))

    def test_group_interrupt_before_daemon_exec_preserves_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_bytes() for path in (
            self.runtime / "registry", shared / "config.yml", shared / "cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        nohup_binary = shutil.which("nohup", path=self.env["PATH"])
        sleep_binary = shutil.which("sleep", path=self.env["PATH"])
        self.assertIsNotNone(nohup_binary)
        self.assertIsNotNone(sleep_binary)
        nohup_stub = self.project / "bin/nohup"
        nohup_stub.write_text(f'''#!/usr/bin/env python3
import os
from pathlib import Path
import signal
import sys
import time
signal.signal(signal.SIGINT, signal.SIG_IGN)
if sys.argv[1] == os.environ['TEST_DELAY_DAEMON']:
    Path(os.environ['TEST_PENDING_PID']).write_text(str(os.getpid()))
    while not Path(os.environ['TEST_RELEASE_EXEC']).exists():
        time.sleep(0.01)
os.execv({nohup_binary!r}, ['nohup', *sys.argv[1:]])
''')
        nohup_stub.chmod(0o755)
        # Observe the parent's actual startup sleep before interrupting its group.
        sleep_stub = self.project / "bin/sleep"
        sleep_stub.write_text(f'''#!/usr/bin/env python3
import os
import sys
if sys.argv[1:] == ['1']:
    with open(os.environ['TEST_START_SLEEPS'], 'a') as record:
        record.write(str(os.getpid()) + '\\n')
os.execv({sleep_binary!r}, ['sleep', *sys.argv[1:]])
''')
        sleep_stub.chmod(0o755)

        def stop_pending(pid):
            if running(pid):
                os.kill(pid, signal.SIGKILL)

        for daemon in ("caddy", "cloudflared"):
            with self.subTest(daemon=daemon):
                pending = self.project / f"{daemon}-pending-pid"
                release = self.project / f"{daemon}-release-exec"
                sleeps = self.project / f"{daemon}-start-sleeps"
                self.env.update(TEST_DELAY_DAEMON=daemon, TEST_PENDING_PID=str(pending),
                                TEST_RELEASE_EXEC=str(release), TEST_START_SLEEPS=str(sleeps))
                previous_pids = set(self.daemon_pids())
                replacement = subprocess.Popen(
                    ["bash", str(self.project / "scripts/tunnel.sh"), "3001", "key"],
                    env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.launchers.append(replacement)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    self.assertIsNone(replacement.poll())
                    if pending.exists() and pending.read_text() and sleeps.exists():
                        if any(running(int(pid)) for pid in sleeps.read_text().splitlines()):
                            break
                    time.sleep(0.01)
                else:
                    self.fail("Candidate did not reach its pre-exec startup sleep")
                pending_pid = int(pending.read_text())
                self.addCleanup(stop_pending, pending_pid)
                self.assertNotIn(pending_pid, self.daemon_pids())
                try:
                    os.killpg(replacement.pid, signal.SIGINT)
                    output, error = replacement.communicate(timeout=10)
                finally:
                    release.write_text("execute")
                self.assertEqual(replacement.returncode, 130, output + error)
                self.assertFalse(running(pending_pid), "Pre-exec child survived launcher cleanup")
                for path, content in snapshot.items():
                    self.assertEqual(path.read_bytes(), content, str(path))
                self.assertTrue(all(running(pid) for pid in previous_pids))
                self.assertFalse(any(running(pid) for pid in set(self.daemon_pids()) - previous_pids))
                self.assertEqual(sorted(path.name for path in shared.iterdir()),
                                 ["cloudflared.log", "cloudflared.pid", "config.yml"])

    def test_signals_before_daemon_pid_capture_preserve_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_text() for path in (
            self.runtime / "registry", shared / "config.yml", shared / "cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        launcher = self.project / "scripts/tunnel.sh"
        original = launcher.read_text()
        self.addCleanup(launcher.write_text, original)
        capture = '  printf -v "$pid_variable" \'%s\' "$!"'
        self.assertEqual(original.count(capture), 1)
        for daemon, variable, config in (
            ("caddy", "CURRENT_CADDY_PID", "${CURRENT_INSTANCE_DIR}/Caddyfile"),
            ("cloudflared", "PENDING_CF_PID", "$CF_CONFIG"),
        ):
            for interruption in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(daemon=daemon, interruption=interruption):
                    # Inject a signal after the child execs, immediately before PID capture.
                    interrupt = f'''  if [[ "$pid_variable" == {variable} ]]; then
    for (( attempt=0; attempt<500; attempt++ )); do
      if is_owned_process "$!" {daemon} "{config}"; then break; fi
      sleep 0.01
    done
    is_owned_process "$!" {daemon} "{config}" || exit 97
    kill -{interruption.name.removeprefix("SIG")} "$$"
  fi
'''
                    launcher.write_text(original.replace(capture, interrupt + capture))
                    previous_pids = set(self.daemon_pids())
                    result = self.invoke("3001", "key")
                    self.assertEqual(result.returncode, 128 + interruption, result.stdout + result.stderr)
                    for path, content in snapshot.items():
                        self.assertEqual(path.read_text(), content, str(path))
                    self.assertTrue(running(int(rows[0][4])))
                    self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
                    new_pids = set(self.daemon_pids()) - previous_pids
                    self.assertEqual(len(new_pids), 1 if daemon == "caddy" else 2)
                    self.assertFalse(any(running(pid) for pid in new_pids))
                    self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 1)
                    self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_interrupted_daemon_start_reconciles_pruned_routes(self):
        launcher = self.project / "scripts/tunnel.sh"
        original = launcher.read_text()
        self.addCleanup(launcher.write_text, original)
        capture = '  printf -v "$pid_variable" \'%s\' "$!"'
        self.assertEqual(original.count(capture), 1)
        for variable, interrupt_cleanup in (
            ("CURRENT_CADDY_PID", False), ("PENDING_CF_PID", False), ("PENDING_CF_PID", True),
        ):
            with self.subTest(variable=variable, interrupt_cleanup=interrupt_cleanup):
                launcher.write_text(original)
                self.assertEqual(self.invoke("stop").returncode, 0)
                self.assert_started(self.invoke("-n", "3000"))
                self.assert_started(self.invoke("3001", "dead-key"))
                fallback, dead = self.registry()
                shared = self.runtime / "cloudflared"
                previous_pid = int((shared / "cloudflared.pid").read_text())
                history = (self.runtime / "tunnel-history").read_text()
                os.kill(int(dead[4]), signal.SIGTERM)
                deadline = time.monotonic() + 5
                while running(int(dead[4])) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(running(int(dead[4])))
                # Optionally interrupt cleanup's replacement launch as well as the candidate.
                daemon = "caddy" if variable == "CURRENT_CADDY_PID" else "cloudflared"
                config = "${CURRENT_INSTANCE_DIR}/Caddyfile" if daemon == "caddy" else "$CF_CONFIG"
                interrupt = f'''  if [[ "$pid_variable" == {variable} && ( "${{TEST_INTERRUPTED:-0}}" == 0 || {int(interrupt_cleanup)} == 1 ) ]]; then
    TEST_INTERRUPTED=1
    for (( attempt=0; attempt<500; attempt++ )); do
      if is_owned_process "$!" {daemon} "{config}"; then break; fi
      sleep 0.01
    done
    is_owned_process "$!" {daemon} "{config}" || exit 97
    kill -TERM "$$"
  fi
'''
                launcher.write_text(original.replace(capture, interrupt + capture))
                result = self.invoke("3002", "new-key")
                self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
                self.assertEqual(self.registry(), [fallback])
                self.assertTrue(running(int(fallback[4])))
                config = (shared / "config.yml").read_text()
                self.assertIn(f"service: http://localhost:{fallback[3]}", config)
                self.assertNotIn("path:", config)
                self.assertEqual((self.runtime / "current-share-url.txt").read_text().strip(), "https://example.test/")
                self.assertEqual((self.runtime / "current-path-id.txt").read_text().strip(), "")
                self.assertEqual((self.runtime / "tunnel-history").read_text(), history)
                self.assertFalse(running(previous_pid))
                self.assertTrue(running(int((shared / "cloudflared.pid").read_text())))
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 1)
                self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_interrupted_cloudflared_replacement_preserves_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_text() for path in (
            shared / "config.yml", shared / "cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        previous_pids = self.daemon_pids("cloudflared")
        replacement = subprocess.Popen(["bash", str(self.project / "scripts/tunnel.sh"), "3001", "key"], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.launchers.append(replacement)
        deadline = time.monotonic() + 5
        while len(self.daemon_pids("cloudflared")) == len(previous_pids) and time.monotonic() < deadline:
            self.assertIsNone(replacement.poll())
            time.sleep(0.01)
        self.assertGreater(len(self.daemon_pids("cloudflared")), len(previous_pids))
        replacement.terminate()
        output, error = replacement.communicate(timeout=10)
        self.assertEqual(replacement.returncode, 143, output + error)
        self.assertEqual(self.registry(), rows)
        self.assertTrue(running(int(rows[0][4])))
        for path, content in snapshot.items():
            self.assertEqual(path.read_text(), content, str(path))
        self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
        self.assertEqual(sum(running(pid) for pid in self.daemon_pids("caddy")), 1)
        self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_process_group_interrupt_during_rollback_preserves_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_bytes() for path in (
            self.runtime / "registry", shared / "config.yml", shared / "cloudflared.pid",
            self.runtime / "tunnel-history", self.runtime / "current-share-url.txt",
            self.runtime / "current-path-id.txt",
        )}
        # Delay candidate shutdown so rollback must wait on a child command.
        (self.project / "bin/cloudflared").write_text(DAEMON.replace(
            "signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))",
            "signal.signal(signal.SIGTERM, lambda *args: (time.sleep(1), sys.exit(0)))",
        ))
        sleep_marker = self.project / "rollback-sleep"
        self.env["TEST_ROLLBACK_SLEEP"] = str(sleep_marker)
        sleep_binary = shutil.which("sleep", path=self.env["PATH"])
        self.assertIsNotNone(sleep_binary)
        sleep_stub = self.project / "bin/sleep"
        sleep_stub.write_text(f'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
marker = Path(os.environ['TEST_ROLLBACK_SLEEP'])
arguments = sys.argv[1:]
if arguments == ['0.1'] and not marker.exists():
    marker.write_text(str(os.getpid()))
    arguments = ['1']
os.execv({sleep_binary!r}, ['sleep', *arguments])
''')
        sleep_stub.chmod(0o755)
        previous_pids = set(self.daemon_pids())
        replacement = subprocess.Popen(
            ["bash", str(self.project / "scripts/tunnel.sh"), "3001", "key"],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.launchers.append(replacement)
        deadline = time.monotonic() + 10
        while len(self.daemon_pids("cloudflared")) < 2 and time.monotonic() < deadline:
            self.assertIsNone(replacement.poll())
            time.sleep(0.01)
        self.assertEqual(len(self.daemon_pids("cloudflared")), 2)
        replacement.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 10
        while not sleep_marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(replacement.poll())
            time.sleep(0.01)
        self.assertTrue(sleep_marker.exists(), "Rollback did not reach process shutdown")
        # Signal the whole isolated group while a cleanup child is running.
        os.killpg(replacement.pid, signal.SIGINT)
        output, error = replacement.communicate(timeout=10)
        self.assertEqual(replacement.returncode, 130, output + error)
        self.assertEqual(self.registry(), rows)
        for path, content in snapshot.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertTrue(all(running(pid) for pid in previous_pids))
        self.assertFalse(any(running(pid) for pid in set(self.daemon_pids()) - previous_pids))
        self.assertEqual(sorted(path.name for path in shared.iterdir()), ["cloudflared.log", "cloudflared.pid", "config.yml"])

    def test_failed_port_probe_preserves_existing_instance(self):
        self.assert_started(self.invoke("3000", "key"))
        rows = self.registry()
        shared_pid = (self.runtime / "cloudflared/cloudflared.pid").read_text()
        history = (self.runtime / "tunnel-history").read_text()
        (self.project / "bin/ss").write_text("#!/bin/sh\nexit 1\n")
        result = self.invoke("3001", "key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not inspect listening ports", result.stderr)
        self.assertEqual(self.registry(), rows)
        self.assertTrue(running(int(rows[0][4])))
        self.assertEqual((self.runtime / "cloudflared/cloudflared.pid").read_text(), shared_pid)
        self.assertTrue(running(int(shared_pid)))
        self.assertEqual((self.runtime / "tunnel-history").read_text(), history)

    def test_failed_shared_config_render_preserves_running_tunnel(self):
        self.assert_started(self.invoke("3000", "key"))
        config = (self.runtime / "cloudflared/config.yml").read_text()
        shared_pid = (self.runtime / "cloudflared/cloudflared.pid").read_text()
        (self.project / "deploy/cloudflared/config.template.yml").unlink()
        result = self.functions('load_config; SOURCE_CF_CONFIG="$CLOUDFLARED_BASE_CONFIG"; read_source_tunnel_values; PATH_ID=key; BACKEND_PORT=3000; ROUTE_HOST="$HOSTNAME_VALUE"; if refresh_cloudflared; then exit 0; else exit 7; fi')
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual((self.runtime / "cloudflared/config.yml").read_text(), config)
        self.assertEqual((self.runtime / "cloudflared/cloudflared.pid").read_text(), shared_pid)
        self.assertTrue(running(int(shared_pid)))
        self.assertEqual(sorted(path.name for path in (self.runtime / "cloudflared").iterdir()), ["cloudflared.log", "cloudflared.pid", "config.yml"])

    def test_foreground_cleanup_preserves_shared_instances(self):
        self.assert_started(self.invoke("3000", "key-one"))
        foreground = self.foreground("3001", "key-two")
        foreground.terminate()
        foreground.communicate(timeout=10)
        rows = self.registry()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "key-one")
        self.assertTrue(running(int(rows[0][4])))
        self.assertTrue(running(int((self.runtime / "cloudflared/cloudflared.pid").read_text())))
        self.assertEqual((self.runtime / "current-share-url.txt").read_text().strip(), "https://example.test/key-one/")
        self.assertEqual((self.runtime / "current-path-id.txt").read_text().strip(), "key-one")

    def test_foreground_reports_shared_cloudflared_exit(self):
        foreground = self.foreground("3000", "key")
        pid = int((self.runtime / "cloudflared/cloudflared.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        output, error = foreground.communicate(timeout=10)
        self.assertNotEqual(foreground.returncode, 0, output + error)
        self.assertIn("cloudflared exited", error)
        self.assertEqual(self.registry(), [])
        self.assertFalse((self.runtime / "current-share-url.txt").exists())
        self.assertFalse(any(running(pid) for pid in self.daemon_pids()))

    def test_foreground_survives_shared_cloudflared_restart(self):
        foreground = self.foreground("3000", "key-one")
        self.assert_started(self.invoke("3001", "key-two"))
        self.assertIsNone(foreground.poll())
        self.assertEqual(len(self.registry()), 2)
        self.assertTrue(running(int((self.runtime / "cloudflared/cloudflared.pid").read_text())))

    def test_replaced_foreground_cannot_stop_replacement(self):
        foreground = self.foreground("3000", "same-key")
        old = self.registry()[0]
        self.assert_started(self.invoke("3001", "same-key"))
        foreground.communicate(timeout=10)
        rows = self.registry()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "3001")
        self.assertNotEqual(rows[0][5], old[5])
        self.assertTrue(running(int(rows[0][4])))

    def test_concurrent_starts_use_distinct_ports(self):
        processes = [subprocess.Popen(["bash", str(self.project / "scripts/tunnel.sh"), port, key], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for port, key in (("3000", "key-one"), ("3001", "key-two"))]
        self.launchers.extend(processes)
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, output + error)
        rows = self.registry()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row[3] for row in rows}), 2)
        self.assertTrue(all(running(int(row[4])) for row in rows))
        self.assertEqual(sum(running(pid) for pid in self.daemon_pids("cloudflared")), 1)

    def test_signal_during_unlock_reacquires_runtime_lock(self):
        launcher = self.project / "scripts/tunnel.sh"
        original = launcher.read_text()
        self.addCleanup(launcher.write_text, original)
        boundary = "flock -u 9; exec 9>&-"
        self.assertEqual(original.count(boundary), 1)
        launcher.write_text(original.replace(boundary, 'flock -u 9; kill -TERM "$$"; exec 9>&-'))
        result = self.functions('''lock_runtime
trap 'lock_runtime; if flock -n "${RUNTIME_DIR}/launcher.lock" true; then fail "Cleanup did not reacquire the runtime lock."; fi; exit 143' TERM
unlock_runtime''')
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)

    def test_unterminated_registry_row_preserves_instance_and_identity(self):
        self.assert_started(self.invoke("3000", "key"))
        row = self.registry()[0]
        registry = self.runtime / "registry"
        registry.write_text(registry.read_text().rstrip("\n"))
        shared = self.runtime / "cloudflared"
        snapshot = {path: path.read_text() for path in (
            shared / "config.yml", shared / "cloudflared.pid", self.runtime / "tunnel-history",
            self.runtime / "current-share-url.txt", self.runtime / "current-path-id.txt",
        )}
        self.env["TUNNEL_NAME"] = "different-tunnel"
        result = self.invoke("3001", "other-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different tunnel", result.stderr)
        self.assertEqual(self.registry(), [row])
        for path, content in snapshot.items():
            self.assertEqual(path.read_text(), content, str(path))
        self.assertTrue(running(int(row[4])))
        self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
        self.env.pop("TUNNEL_NAME")
        registry.write_text(registry.read_text().rstrip("\n"))
        self.assert_started(self.invoke("3001", "other-key"))
        self.assertEqual(self.registry()[0], row)
        self.assertEqual(len(self.registry()), 2)
        self.assertTrue(running(int(row[4])))
        self.assertIn("^/key(/.*)?$", (shared / "config.yml").read_text())

    def test_rejects_switching_shared_tunnel(self):
        self.assert_started(self.invoke("3000", "key"))
        row = self.registry()[0]
        self.env["TUNNEL_NAME"] = "different-tunnel"
        result = self.invoke("3001", "other-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different tunnel", result.stderr)
        self.assertEqual(self.registry(), [row])
        self.assertTrue(running(int(row[4])))

    def test_legacy_home_credentials_allow_start_and_replacement(self):
        self.env["HOME"] = str(self.project / "home")
        legacy_credentials = "~/.cloudflared/synthetic-id.json"
        expanded_credentials = str(self.project / "home/.cloudflared/synthetic-id.json")
        self.source.write_text(self.source.read_text().replace("/synthetic/never-read.json", legacy_credentials))
        self.assert_started(self.invoke("3000", "legacy-key"))
        survivor = self.registry()[0]
        shared = self.runtime / "cloudflared"
        for port, hostname, credentials in (
            ("3001", "candidate.test", legacy_credentials),
            ("3002", "replacement.test", expanded_credentials),
        ):
            with self.subTest(port=port):
                rows = self.registry()
                (self.runtime / "registry").write_text(
                    "\t".join(survivor[:6]) + "\n" +
                    "".join("\t".join(row) + "\n" for row in rows[1:])
                )
                config_path = shared / "config.yml"
                config_path.write_text(config_path.read_text().replace(expanded_credentials, legacy_credentials))
                previous_pid = int((shared / "cloudflared.pid").read_text())
                self.env.update(TUNNEL_HOSTNAME=hostname, TUNNEL_CREDENTIALS_FILE=credentials)
                self.assert_started(self.invoke(port, "new-key"))
                current = self.registry()
                self.assertEqual(len(current), 2)
                self.assertEqual(current[0], survivor)
                self.assertEqual(current[1][2], port)
                self.assertEqual(current[1][6], hostname)
                self.assertTrue(running(int(survivor[4])))
                if len(rows) == 2:
                    self.assertFalse(running(int(rows[1][4])))
                config = config_path.read_text()
                self.assertIn(f"credentials-file: '{expanded_credentials}'", config)
                self.assertIn("hostname: 'example.test'\n    path: '^/legacy-key(/.*)?$'\n" +
                              f"    service: http://localhost:{survivor[3]}", config)
                self.assertIn(f"hostname: '{hostname}'", config)
                self.assertFalse(running(previous_pid))
                self.assertTrue(running(int((shared / "cloudflared.pid").read_text())))

    def test_unrecoverable_shared_identity_preserves_existing_state(self):
        self.assert_started(self.invoke("3000", "key"))
        row = self.registry()[0]
        registry = self.runtime / "registry"
        # Unrecoverable identity must fail before even pruning a stale row.
        registry.write_text(registry.read_text() + f"path\tdead-key\t3002\t9092\t0\t{self.runtime}/instances/dead\texample.test\n")
        shared = self.runtime / "cloudflared"
        config_path = shared / "config.yml"
        original_config = config_path.read_text()
        snapshot = {path: path.read_text() for path in (
            registry, shared / "cloudflared.pid", self.runtime / "tunnel-history",
            self.runtime / "current-share-url.txt", self.runtime / "current-path-id.txt",
        )}
        cases = {
            "missing-config": None,
            "missing-tunnel": original_config.replace("tunnel: 'synthetic-tunnel'\n", ""),
            "empty-tunnel": original_config.replace("tunnel: 'synthetic-tunnel'", "tunnel: ''"),
            "empty-credentials": original_config.replace("credentials-file: '/synthetic/never-read.json'", "credentials-file: ''"),
            "duplicate-tunnel": "tunnel: 'other-tunnel'\n" + original_config,
            "malformed-tunnel": original_config.replace("tunnel: 'synthetic-tunnel'", "tunnel: 'synthetic-tunnel"),
        }
        self.env.update(TUNNEL_NAME="different-tunnel", TUNNEL_CREDENTIALS_FILE="/synthetic/different.json", TUNNEL_HOSTNAME="candidate.test")
        for name, config in cases.items():
            with self.subTest(name=name):
                if config is None:
                    config_path.unlink()
                else:
                    config_path.write_text(config)
                result = self.invoke("3001", "other-key")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot recover shared tunnel identity", result.stderr)
                self.assertIn("Existing runtime state was preserved", result.stderr)
                self.assertNotIn("Starting Caddy", result.stdout)
                for path, content in snapshot.items():
                    self.assertEqual(path.read_text(), content, str(path))
                if config is None:
                    self.assertFalse(config_path.exists())
                else:
                    self.assertEqual(config_path.read_text(), config)
                self.assertTrue(running(int(row[4])))
                self.assertTrue(running(int(snapshot[shared / "cloudflared.pid"])))
                self.assertEqual(len(self.daemon_pids("caddy")), 1)
                self.assertEqual(len(self.daemon_pids("cloudflared")), 1)

    def test_stop_ignores_unrelated_and_invalid_pids(self):
        (self.runtime / "instances/9090").mkdir(parents=True)
        (self.runtime / "cloudflared").mkdir()
        directory = self.runtime / "instances/9090"
        unrelated = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)", "caddy", "--config", str(directory / "Caddyfile"), "cloudflared", "--config", str(self.runtime / "cloudflared/config.yml")])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.terminate)
        directory.joinpath("Caddyfile").write_text("synthetic")
        (self.runtime / "registry").write_text(f"path\tkey\t3000\t9090\t{unrelated.pid}\t{directory}\n")
        (self.runtime / "cloudflared/cloudflared.pid").write_text(str(unrelated.pid))
        result = self.invoke("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(unrelated.poll())
        for pid in ("0", "-1", "oops"):
            self.assertNotEqual(self.functions('is_pid_running "$1"', pid).returncode, 0)

    def test_history_migration_limit_and_failed_selection(self):
        self.runtime.mkdir()
        history = self.runtime / "tunnel-history"
        history.write_text("no-key\\t\\t3000\\thttps://example.test/\\t100\\t200\n")
        result = self.invoke("--list", input="1\ninvalid\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backend port", result.stderr)
        self.assertEqual(history.read_text(), "no-key\\t\\t3000\\thttps://example.test/\\t100\\t200\n")
        result = self.functions('parse_history "$1"; printf "%s|%s|%s|%s|%s|%s" "$HIST_MODE" "$HIST_KEY" "$HIST_PORT" "$HIST_URL" "$HIST_CREATED" "$HIST_USED"', "no-key\t3000\thttps://example.test/\t100\t200")
        self.assertEqual(result.stdout, "no-key||3000|https://example.test/|100|200")
        result = self.functions('MODE=no-key; PATH_ID=""; BACKEND_PORT=3000; add_to_history "https://example.test/"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(history.read_text().split("\t")[4], "100")
        result = self.functions('MODE=path; PATH_ID=key; BACKEND_PORT=3000; for i in {1..12}; do add_to_history "https://example.test/$i/"; done')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(history.read_text().splitlines()), 10)

    def test_caddy_templates_adapt_as_expected(self):
        replacements = {"__BACKEND_PORT__": "18081", "__PATH_ID__": "test-key",
                        "__SUBDOMAIN_HOST__": "test-key.example.test",
                        "__PUBLIC_ROOT__": str(self.project / "public"),
                        "__CACHE_POLICY__": "", "__PREPARATION_HANDLER__": ""}
        for template in sorted((self.project / "deploy/caddy").glob("*.template")):
            for port in ("18080", "443"):
                with self.subTest(template=template.name, port=port):
                    content = template.read_text()
                    for token, value in (replacements | {"__CADDY_PORT__": port}).items():
                        content = content.replace(token, value)
                    config = self.project / "Caddyfile"
                    config.write_text(content)
                    env = os.environ | {"XDG_CONFIG_HOME": str(self.project / "config"), "XDG_DATA_HOME": str(self.project / "data")}
                    result = subprocess.run(["caddy", "adapt", "--config", str(config), "--adapter", "caddyfile", "--validate"], env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    parsed = json.loads(result.stdout)
                    self.assertTrue(parsed["admin"]["disabled"])
                    server = parsed["apps"]["http"]["servers"]["srv0"]
                    self.assertEqual(server["listen"], [f"127.0.0.1:{port}"])
                    self.assertFalse(server.get("tls_connection_policies"))
                    self.assertTrue(server["automatic_https"]["disable"])
                    self.assertNotIn("enabling automatic TLS", result.stderr)
                    self.assertNotIn("enabling automatic HTTP->HTTPS redirects", result.stderr)
                    if template.name == "Caddyfile.template":
                        route = server["routes"][0]
                        self.assertEqual(route["match"][0]["path"], ["/test-key", "/test-key/*"])
                        handlers = route["handle"][0]["routes"][0]["handle"]
                        self.assertEqual(handlers[0]["strip_path_prefix"], "/test-key")
                        self.assertEqual(handlers[1]["headers"]["request"]["set"]["X-Forwarded-Prefix"], ["/test-key"])


if __name__ == "__main__":
    unittest.main()

"""Structured control uses isolated launcher copies and synthetic daemons."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

from test_tunnel import DAEMON, TunnelFixture, running


class ShareTests(TunnelFixture):
    def setUp(self):
        super().setUp()
        self.env["ALIAS_PYTHON"] = sys.executable

    def success(self, *arguments):
        result = self.invoke(*arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def failure(self, *arguments):
        result = self.invoke(*arguments)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        error = json.loads(result.stdout)["error"]
        self.assertTrue(error["code"])
        self.assertTrue(error["message"])
        return error

    def saved_state(self):
        return {path: path.read_bytes() for path in (
            self.runtime / "registry", self.runtime / "cloudflared/config.yml",
            self.runtime / "cloudflared/cloudflared.pid", self.runtime / "tunnel-history",
            self.runtime / "current-share-url.txt", self.runtime / "current-path-id.txt",
        ) if path.exists()}

    def assert_preserved(self, snapshot, pids):
        for path, content in snapshot.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertTrue(all(running(pid) for pid in pids))
        self.assertFalse(list(self.runtime.glob("registry.*")))
        self.assertFalse(list((self.runtime / "cloudflared").glob("refresh.*")))

    def test_empty_listing_and_unknown_id_do_not_create_runtime(self):
        self.source.unlink()
        self.assertEqual(self.success("list-shares"), [])
        self.assertEqual(self.failure("stop-share", "9090.unknown")["code"], "unknown_share")
        self.assertFalse(self.runtime.exists())
        self.runtime.mkdir()
        (self.runtime / "registry").write_text("")
        self.failure("stop-share", "9090.unknown")
        self.assertEqual(list(self.runtime.iterdir()), [self.runtime / "registry"])

    def test_listing_waits_for_legacy_stop_and_returns_empty(self):
        self.success("expose-port", "3000")
        real_flock = shutil.which("flock", path=self.env["PATH"])
        self.assertIsNotNone(real_flock)
        stop_locked = self.project / "stop-locked"
        list_waiting = self.project / "list-waiting"
        release_stop = self.project / "release-stop"
        wrapper = self.project / "bin/flock"
        wrapper.write_text(f'''#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import time
arguments = sys.argv[1:]
if arguments == ['9']:
    if os.environ.get('TEST_STOP_LOCKED'):
        subprocess.run([{real_flock!r}, *arguments], check=True, pass_fds=(9,))
        Path(os.environ['TEST_STOP_LOCKED']).touch()
        release = Path(os.environ['TEST_RELEASE_STOP'])
        deadline = time.monotonic() + 5
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        sys.exit(0 if release.exists() else 99)
    if os.environ.get('TEST_LIST_WAITING'):
        Path(os.environ['TEST_LIST_WAITING']).touch()
os.execv({real_flock!r}, ['flock', *arguments])
''')
        wrapper.chmod(0o755)

        def launch(command, environment):
            process = subprocess.Popen(
                ["bash", str(self.project / "scripts/tunnel.sh"), command],
                env=self.env | environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.launchers.append(process)
            return process

        def wait_for_marker(marker, process):
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                time.sleep(0.01)
            self.assertTrue(marker.exists(), f"Did not reach {marker.name}")

        stopper = launch("stop", {"TEST_STOP_LOCKED": str(stop_locked),
                                  "TEST_RELEASE_STOP": str(release_stop)})
        try:
            wait_for_marker(stop_locked, stopper)
            listing = launch("list-shares", {"TEST_LIST_WAITING": str(list_waiting)})
            wait_for_marker(list_waiting, listing)
            self.assertTrue((self.runtime / "registry").exists())
            self.assertIsNone(listing.poll())
        finally:
            release_stop.touch()
        output, error = stopper.communicate(timeout=10)
        self.assertEqual(stopper.returncode, 0, output + error)
        output, error = listing.communicate(timeout=10)
        self.assertEqual(listing.returncode, 0, output + error)
        self.assertEqual(json.loads(output), [])
        self.assertFalse((self.runtime / "registry").exists())
        self.assertFalse(any(running(pid) for pid in self.daemon_pids()))

    def test_arguments_and_configuration_fail_as_json_before_runtime(self):
        for arguments in (
            ("expose-port",), ("expose-port", "0"), ("expose-port", "65536"),
            ("expose-port", "3000", "positional-key"),
            ("expose-port", "3000", "--key"), ("expose-port", "3000", "--key", ""),
            ("expose-port", "3000", "--key", 'bad"key\n'),
            ("expose-port", "3000", "--url-mode", "unknown"),
            ("expose-port", "3000", "--url-mode"),
            ("expose-port", "3000", "--url-mode", "no-key", "--key", "key"),
            ("expose-port", "3000", "--key", "one", "--key", "two"),
            ("expose-port", "3000", "--url-mode", "path", "--url-mode", "subdomain"),
            ("list-shares", "extra"), ("stop-share",), ("stop-share", "../escape"),
        ):
            with self.subTest(arguments=arguments):
                self.failure(*arguments)
                self.assertFalse(self.runtime.exists())
        (self.project / "cloudflared-alias.conf").write_text("NOT_AN_OPTION=1\n")
        self.assertIn("Unknown config option", self.failure("list-shares")["message"])
        self.assertFalse(self.runtime.exists())

    def test_default_is_keyed_path_detached_and_independent_of_legacy_defaults(self):
        self.env.update(DEFAULT_MODE="no-key", ID_LENGTH="invalid", DETACH="0")
        share = self.success("expose-port", "03000")
        self.assertEqual(share["source"], {"type": "port", "port": 3000})
        self.assertEqual(share["url_mode"], "path")
        self.assertIsNone(share["update_mode"])
        self.assertEqual(share["state"], "active")
        self.assertRegex(share["url"], r"^https://example\.test/[a-f0-9]{32}/$")
        row = self.registry()[0]
        self.assertEqual(share["id"], Path(row[5]).name)
        self.assertEqual(json.loads((Path(row[5]) / "share.json").read_text()), share)
        self.assertTrue(running(int(row[4])))
        self.assertEqual(self.success("list-shares"), [share])

    def test_explicit_modes_and_keys(self):
        for port, mode, key, url in (
            ("3000", "path", "preview", "https://example.test/preview/"),
            ("3001", "subdomain", "preview-host", "https://preview-host.example.test/"),
            ("3002", "no-key", None, "https://example.test/"),
        ):
            arguments = ("--key", key) if key else ()
            share = self.success("expose-port", port, "--url-mode", mode, *arguments)
            self.assertEqual(share["url"], url)
            self.assertEqual(share["url_mode"], mode)
        self.assertEqual(len(self.success("list-shares")), 3)

    def test_generated_subdomain_key_and_legacy_four_character_default(self):
        share = self.success("expose-port", "3000", "--url-mode", "subdomain")
        self.assertRegex(share["url"], r"^https://[a-f0-9]{32}\.example\.test/$")
        self.env["ALIAS_PYTHON"] = "/nonexistent/python"
        self.assert_started(self.invoke("3001"))
        self.assertRegex(self.registry()[-1][1], r"^[a-z0-9]{4}$")

    def test_concurrent_results_are_request_local(self):
        processes = [subprocess.Popen(
            ["bash", str(self.project / "scripts/tunnel.sh"), "expose-port", port],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) for port in ("3000", "3001")]
        self.launchers.extend(processes)
        shares = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, output + error)
            shares.append(json.loads(output))
            self.assertIn("Starting Caddy", error)
        self.assertEqual([share["source"]["port"] for share in shares], [3000, 3001])
        self.assertEqual(len({share["id"] for share in shares}), 2)
        self.assertEqual(len({share["url"] for share in shares}), 2)
        self.assertEqual({share["id"] for share in self.success("list-shares")},
                         {share["id"] for share in shares})

    def test_legacy_listing_recovers_routes_without_migration_or_source_config(self):
        for arguments in (("3000", "old-path"), ("-s", "3001", "old-host"), ("-n", "3002")):
            self.assert_started(self.invoke(*arguments))
        rows = self.registry()
        registry = self.runtime / "registry"
        registry.write_text("\n".join("\t".join(row[:6]) for row in rows))
        self.source.unlink()
        self.env.update(TUNNEL_NAME="wrong", TUNNEL_HOSTNAME="wrong.test")
        (self.runtime / "current-share-url.txt").write_text("not a URL\n")
        (self.runtime / "current-path-id.txt").unlink()
        snapshot = self.saved_state()
        pids = [pid for pid in self.daemon_pids() if running(pid)]
        shares = self.success("list-shares")
        self.assertEqual([share["url"] for share in shares], [
            "https://example.test/old-path/", "https://old-host.example.test/", "https://example.test/",
        ])
        self.assertEqual([share["id"] for share in shares], [Path(row[5]).name for row in rows])
        self.assert_preserved(snapshot, pids)
        self.assertTrue(all(not (Path(row[5]) / "share.json").exists() for row in rows))

    def test_listing_reports_degraded_connector_without_restarting_it(self):
        share = self.success("expose-port", "3000")
        connector = int((self.runtime / "cloudflared/cloudflared.pid").read_text())
        os.kill(connector, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while running(connector) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(running(connector))
        snapshot = self.saved_state()
        self.assertEqual(self.success("list-shares"), [share | {"state": "degraded"}])
        self.assert_preserved(snapshot, self.daemon_pids("caddy"))

    def test_unrecoverable_legacy_listing_returns_only_an_error(self):
        self.assert_started(self.invoke("3000", "key"))
        row = self.registry()[0]
        (self.runtime / "registry").write_text("\t".join(row[:6]))
        (self.runtime / "cloudflared/config.yml").unlink()
        snapshot = self.saved_state()
        self.failure("list-shares")
        self.assert_preserved(snapshot, self.daemon_pids())
        self.assertFalse((self.runtime / "cloudflared/config.yml").exists())

    def test_last_share_can_stop_without_connector_config(self):
        share = self.success("expose-port", "3000")
        (self.runtime / "cloudflared/config.yml").unlink()
        self.assertEqual(self.success("stop-share", share["id"]), share | {"state": "stopped"})
        self.assertEqual(self.success("list-shares"), [])
        self.assertFalse(any(running(pid) for pid in self.daemon_pids()))

    def test_stopping_one_share_and_then_last_preserves_history(self):
        first = self.success("expose-port", "3000", "--key", "first")
        second = self.success("expose-port", "3001", "--key", "second")
        rows = self.registry()
        history = (self.runtime / "tunnel-history").read_bytes()
        self.source.unlink()
        self.env.update(TUNNEL_NAME="wrong", TUNNEL_CREDENTIALS_FILE="/wrong/never-read.json")
        self.assertEqual(self.success("stop-share", first["id"]), first | {"state": "stopped"})
        self.assertFalse(running(int(rows[0][4])))
        self.assertTrue(running(int(rows[1][4])))
        self.assertEqual(self.success("list-shares"), [second])
        config = (self.runtime / "cloudflared/config.yml").read_text()
        self.assertNotIn("^/first", config)
        self.assertIn("^/second", config)
        self.assertEqual(self.success("stop-share", second["id"]), second | {"state": "stopped"})
        self.assertEqual(self.success("list-shares"), [])
        self.assertEqual((self.runtime / "tunnel-history").read_bytes(), history)
        for path in ("cloudflared/config.yml", "cloudflared/cloudflared.pid", "current-share-url.txt", "current-path-id.txt"):
            self.assertFalse((self.runtime / path).exists(), path)
        self.assertFalse(any(running(pid) for pid in self.daemon_pids()))

    def test_stopping_legacy_row_uses_persisted_identity_and_hostname(self):
        self.assert_started(self.invoke("-n", "3000"))
        self.assert_started(self.invoke("3001", "key"))
        rows = self.registry()
        (self.runtime / "registry").write_text("\n".join("\t".join(row[:6]) for row in rows))
        self.source.unlink()
        stopped = self.success("stop-share", Path(rows[1][5]).name)
        self.assertEqual(stopped["url"], "https://example.test/key/")
        self.assertEqual(self.registry(), [rows[0]])
        self.assertTrue(running(int(rows[0][4])))

    def test_unknown_and_unowned_ids_preserve_all_runtime_state(self):
        share = self.success("expose-port", "3000")
        snapshot = self.saved_state()
        self.assertEqual(self.failure("stop-share", "9090.unknown")["code"], "unknown_share")
        self.assert_preserved(snapshot, self.daemon_pids())
        unrelated = subprocess.Popen(["sleep", "30"])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.terminate)
        row = self.registry()[0]
        row[4] = str(unrelated.pid)
        (self.runtime / "registry").write_text("\t".join(row) + "\n")
        snapshot = self.saved_state()
        self.assertEqual(self.failure("stop-share", share["id"])["code"], "unknown_share")
        self.assertEqual(self.success("list-shares"), [])
        self.assert_preserved(snapshot, [unrelated.pid, *self.daemon_pids()])

    def test_replacements_have_new_ids_and_keep_other_shares(self):
        survivor = self.success("expose-port", "3000", "--key", "survivor")
        replaced = self.success("expose-port", "3001", "--key", "replace")
        for port, key in (("3002", "replace"), ("3002", "new-key")):
            old_pid = int(self.registry()[-1][4])
            replacement = self.success("expose-port", port, "--key", key)
            self.assertNotEqual(replacement["id"], replaced["id"])
            self.assertFalse(running(old_pid))
            self.assertEqual(self.success("list-shares"), [survivor, replacement])
            snapshot = self.saved_state()
            self.failure("stop-share", replaced["id"])
            self.assert_preserved(snapshot, [int(row[4]) for row in self.registry()])
            replaced = replacement

    def test_explicit_no_key_replacement_preserves_keyed_shares(self):
        keyed = self.success("expose-port", "3000")
        first = self.success("expose-port", "3001", "--url-mode", "no-key")
        old_pid = int(self.registry()[-1][4])
        second = self.success("expose-port", "3002", "--url-mode", "no-key")
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(running(old_pid))
        self.assertEqual(self.success("list-shares"), [keyed, second])

    def test_failed_exposure_rolls_back_and_returns_json(self):
        share = self.success("expose-port", "3000", "--key", "key")
        snapshot = self.saved_state()
        pids = self.daemon_pids()
        for daemon in ("CADDY", "CLOUDFLARED"):
            self.env[f"FAIL_{daemon}"] = "1"
            error = self.failure("expose-port", "3001", "--key", "key")
            self.assertIn("failed to start", error["message"])
            self.assert_preserved(snapshot, pids)
            self.assertEqual(self.success("list-shares"), [share])
            self.env.pop(f"FAIL_{daemon}")

    def test_failed_stop_rolls_back_before_stopping_target(self):
        shares = [self.success("expose-port", port) for port in ("3000", "3001")]
        snapshot = self.saved_state()
        pids = [pid for pid in self.daemon_pids() if running(pid)]
        self.env["FAIL_CLOUDFLARED"] = "1"
        self.assertIn("failed to refresh", self.failure("stop-share", shares[0]["id"])["message"])
        self.assert_preserved(snapshot, pids)
        self.assertEqual(self.success("list-shares"), shares)

    def test_interrupted_stop_restores_registry_and_connector(self):
        shares = [self.success("expose-port", port) for port in ("3000", "3001")]
        snapshot = self.saved_state()
        pids = [pid for pid in self.daemon_pids() if running(pid)]
        previous_connectors = len(self.daemon_pids("cloudflared"))
        process = subprocess.Popen(
            ["bash", str(self.project / "scripts/tunnel.sh"), "stop-share", shares[0]["id"]],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.launchers.append(process)
        deadline = time.monotonic() + 10
        while len(self.daemon_pids("cloudflared")) == previous_connectors and time.monotonic() < deadline:
            self.assertIsNone(process.poll())
            time.sleep(0.01)
        self.assertGreater(len(self.daemon_pids("cloudflared")), previous_connectors)
        process.terminate()
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, output + error)
        self.assertIn("error", json.loads(output))
        self.assert_preserved(snapshot, pids)
        self.assertEqual(self.success("list-shares"), shares)

    def test_interrupted_last_stop_finishes_committed_cleanup(self):
        marker = self.project / "stopping-caddy"
        self.env["TEST_STOP_MARKER"] = str(marker)
        (self.project / "bin/caddy").write_text(DAEMON.replace(
            "signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))",
            "signal.signal(signal.SIGTERM, lambda *args: "
            "(Path(os.environ['TEST_STOP_MARKER']).write_text('stopping'), time.sleep(0.5), sys.exit(0)))",
        ))
        share = self.success("expose-port", "3000")
        process = subprocess.Popen(
            ["bash", str(self.project / "scripts/tunnel.sh"), "stop-share", share["id"]],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.launchers.append(process)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(process.poll())
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        process.terminate()
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, output + error)
        self.assertIn("error", json.loads(output))
        self.assertEqual(self.registry(), [])
        self.assertEqual(self.success("list-shares"), [])
        self.assertFalse((self.runtime / "cloudflared/cloudflared.pid").exists())
        self.assertFalse(any(running(pid) for pid in self.daemon_pids()))

    def test_python_config_override_and_environment_precedence(self):
        wrapper = self.project / "python wrapper"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        wrapper.chmod(0o755)
        config = self.project / "cloudflared-alias.conf"
        config.write_text(f"ALIAS_PYTHON='{wrapper}'\n")
        self.env.pop("ALIAS_PYTHON")
        self.assertEqual(self.success("expose-port", "3000")["state"], "active")
        config.write_text("ALIAS_PYTHON=/nonexistent/python\n")
        self.env["ALIAS_PYTHON"] = sys.executable
        self.assertEqual(len(self.success("list-shares")), 1)
        self.env.pop("ALIAS_PYTHON")
        self.failure("list-shares")

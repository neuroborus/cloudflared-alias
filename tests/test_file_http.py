"""Real loopback Caddy and manual preparation; no connector or public tunnel."""

from contextlib import contextmanager, ExitStack
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from urllib.parse import quote, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publication
from publication import InvalidRequest, Publication, PublicationError


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class FileHTTPFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alias-file-http-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        shutil.copytree(ROOT / "scripts", self.project / "scripts")
        self.source = self.root / "site"
        self.source.mkdir()
        self.html = self.source / "index.html"
        self.html.write_bytes(b"<h1>original</h1>")
        self.sequence = 0

    def wait_listening(self, process, port, log):
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        with log.open("rb") as output:
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 1024))
            diagnostic = output.read().decode("utf-8", errors="replace")
        for code, message in (("EPERM", "operation not permitted"),
                              ("EACCES", "permission denied"),
                              ("EADDRINUSE", "address already in use")):
            if message in diagnostic.lower():
                print(f"Error [{code}]: Local service did not start", file=sys.stderr)
        if process.poll() is None:
            print("  failureType: 'testTimeoutFailure'", file=sys.stderr)
        self.fail(f"Local service did not start: {diagnostic}")

    @contextmanager
    def serve(self, source=None, update="snapshot", mode="path", *,
              helper_available=True, with_events=False, events_blocked=False):
        self.sequence += 1
        share = Publication(source or self.source, f"http-{self.sequence}",
                            project_root=self.project)
        share.prepare()
        with ExitStack() as cleanup:
            preparation = ""
            event_handler = ""
            if update in ("manual", "live"):
                helper_port = free_port()
                config = share.state_dir / "helper.json"
                config.write_text(json.dumps({"source": str(share.selection.path),
                                              "share_id": share.state_dir.name,
                                              "project_root": str(self.project),
                                              "port": helper_port,
                                              "event_url": ("/test-key" if mode == "path" else "")
                                              + "/__alias/events"}))
                log = share.state_dir / "helper.log"
                output = cleanup.enter_context(log.open("wb"))
                helper = subprocess.Popen([sys.executable, str(self.project / "scripts/publication.py"),
                                           f"serve-{update}", str(config)],
                                          stdin=subprocess.DEVNULL, stdout=output, stderr=output)
                cleanup.callback(stop_process, helper)
                self.wait_listening(helper, helper_port, log)
                if update == "live":
                    event_handler = f"""@events path /__alias/events
                    reverse_proxy @events 127.0.0.1:{helper_port} {{
                        flush_interval -1
                    }}"""
                    if events_blocked:
                        event_handler = '@events path /__alias/events\nrespond @events "Blocked" 403'
                else:
                    preparation = f"""forward_auth 127.0.0.1:{helper_port} {{
                    uri /__alias/prepare
                    @unavailable status 5xx
                    handle_response @unavailable {{
                        error "Publication preparation unavailable" 502
                    }}
                }}"""
            port = free_port()
            name = {"path": "path", "subdomain": "subdomain", "no-key": "nokey"}[mode]
            template = (ROOT / f"deploy/caddy/Caddyfile.files.{name}.template").read_text()
            replacements = {"__CADDY_PORT__": str(port), "__PATH_ID__": "test-key",
                            "__SUBDOMAIN_HOST__": "test-key.example.test",
                            "__PUBLIC_ROOT__": str(share.public_root),
                            "__CACHE_POLICY__": 'header >Cache-Control "no-store"' if update != "snapshot" else "",
                            "__PREPARATION_HANDLER__": preparation,
                            "__EVENT_HANDLER__": event_handler}
            for token, value in replacements.items():
                template = template.replace(token, value)
            config = share.state_dir / "Caddyfile"
            config.write_text(template)
            env = os.environ | {"XDG_CONFIG_HOME": str(share.state_dir / "config"),
                                "XDG_DATA_HOME": str(share.state_dir / "data")}
            log = share.state_dir / "caddy.log"
            output = cleanup.enter_context(log.open("wb"))
            caddy = subprocess.Popen(["caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
                                     env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            cleanup.callback(stop_process, caddy)
            self.wait_listening(caddy, port, log)
            if update in ("manual", "live") and not helper_available:
                stop_process(helper)

            def request(path="/", *, host=None, keyed=True, method="GET"):
                if mode == "path" and keyed:
                    path = "/test-key" + path
                headers = {"Host": host or ("test-key.example.test" if mode == "subdomain" else "example.test")}
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(method, path, headers=headers)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            @contextmanager
            def events(*, last_revision=None, timeout=5):
                path = "/__alias/events"
                if mode == "path":
                    path = "/test-key" + path
                headers = {"Host": "test-key.example.test" if mode == "subdomain" else "example.test"}
                if last_revision is not None:
                    headers["Last-Event-ID"] = last_revision
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
                try:
                    connection.request("GET", path, headers=headers)
                    with connection.getresponse() as response:
                        yield response
                finally:
                    connection.close()

            yield (share, request, events) if with_events else (share, request)


class FileHTTPTests(FileHTTPFixture, unittest.TestCase):

    def test_snapshot_freezes_bytes_and_explicit_republication_refreshes(self):
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"<h1>original</h1>")
                with self.serve(mode=mode) as (share, request):
                    accepted = share.current()
                    self.html.write_bytes(b"<h1>changed</h1>")
                    self.assertEqual(request("/")[::2], (200, b"<h1>original</h1>"))
                    self.assertEqual(request("/index.html?refresh=1")[::2], (200, b"<h1>original</h1>"))
                    self.assertEqual(share.current(), accepted)
                    share.prepare()
                    self.assertEqual(request("/")[::2], (200, b"<h1>changed</h1>"))
                    self.assertNotEqual(share.current(), accepted)

    def test_single_file_url_is_encoded_and_exposes_no_siblings(self):
        selected = self.source / "report ü space.pdf"
        selected.write_bytes(b"%PDF-1.7\nselected")
        (self.source / "secret.txt").write_text("never selected")
        for update in ("snapshot", "manual"):
            for mode in ("path", "subdomain", "no-key"):
                with self.subTest(update=update, mode=mode), self.serve(selected, update, mode) as (_, request):
                    status, headers, body = request("/" + quote(selected.name))
                    self.assertEqual((status, body), (200, selected.read_bytes()))
                    self.assertEqual(headers["Content-Type"], "application/pdf")
                    if update == "manual":
                        self.assertEqual(headers["Cache-Control"], "no-store")
                    for path in ("/", "/index.html", "/secret.txt"):
                        self.assertEqual(request(path)[0], 404, path)
                    status, headers, _ = request("/" + quote(selected.name) + "/?refresh=%23%25")
                    self.assertNotEqual(status, 200)
                    if update == "snapshot" and mode == "path":
                        self.assertEqual(status, 308)
                        self.assertEqual(headers["Location"],
                                         "/test-key/" + quote(selected.name) + "?refresh=%23%25")
                        self.assertEqual(request(headers["Location"], keyed=False)[::2],
                                         (200, selected.read_bytes()))
                    self.assertEqual(request("/" + quote(selected.name), method="HEAD")[::2], (200, b""))

    def test_directory_assets_indexes_mime_types_and_key_rejection(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("example.txt", "static archive")
        assets = {"site.css": (b"h1{}", "text/css"),
                  "app.js": (b"let x = 1", "text/javascript"),
                  "image.svg": (b"<svg/>", "image/svg+xml"),
                  "data.json": (b'{"ok":true}', "application/json"),
                  "archive.zip": (archive.getvalue(), "application/zip")}
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "index.htm").write_bytes(b"nested index")
        for name, (content, _) in assets.items():
            (self.source / name).write_bytes(content)
        (self.source / ".env").write_text("excluded")
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode), self.serve(mode=mode) as (_, request):
                self.assertEqual(request("/nested/")[::2], (200, b"nested index"))
                status, headers, _ = request("/nested")
                self.assertEqual(status, 308)
                prefix = "/test-key" if mode == "path" else ""
                self.assertEqual(urlsplit(headers["Location"]).path, prefix + "/nested/")
                self.assertEqual(urlsplit(request("/nested?refresh=1")[1]["Location"]).query, "refresh=1")
                for name, (content, mime) in assets.items():
                    status, headers, body = request("/" + name)
                    self.assertEqual((status, body), (200, content))
                    actual_mime = headers["Content-Type"].split(";")[0]
                    if name.endswith(".js"):
                        self.assertIn(actual_mime, ("text/javascript", "application/javascript"))
                    else:
                        self.assertEqual(actual_mime, mime)
                self.assertEqual(request("/.env")[0], 404)
                if mode == "path":
                    self.assertEqual(request("/index.html", keyed=False)[0], 404)
                    self.assertEqual(request("/test-key-extra/index.html", keyed=False)[0], 404)
                if mode == "subdomain":
                    self.assertEqual(request("/index.html", host="wrong.example.test")[0], 404)

    def test_manual_current_bytes_additions_deletions_and_no_store(self):
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"old")
                asset = self.source / "fresh.css"
                asset.unlink(missing_ok=True)
                nested = self.source / "new-pages"
                if nested.exists():
                    shutil.rmtree(nested)
                with self.serve(update="manual", mode=mode) as (share, request):
                    self.html.write_bytes(b"new")
                    asset.write_bytes(b"added")
                    nested.mkdir()
                    (nested / "index.htm").write_bytes(b"new directory index")
                    # No watcher prepares changes before their own HTTP request.
                    self.assertEqual((share.public_root / "index.html").read_bytes(), b"old")
                    self.assertFalse((share.public_root / asset.name).exists())
                    for path, expected in (("/", b"new"), ("/fresh.css", b"added"),
                                           ("/new-pages/", b"new directory index")):
                        status, headers, body = request(path)
                        self.assertEqual((status, body), (200, expected))
                        self.assertEqual(headers["Cache-Control"], "no-store")
                    asset.unlink()
                    status, headers, _ = request("/fresh.css")
                    self.assertEqual(status, 404)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertFalse((share.public_root / asset.name).exists())
                    shutil.rmtree(nested)
                    self.assertEqual(request("/new-pages/index.htm")[0], 404)
                    self.assertFalse((share.public_root / nested.name).exists())
                    self.assertEqual(self.html.read_bytes(), b"new")

    def test_manual_standalone_file_edits_deletion_and_recreation(self):
        selected = self.source / "report.pdf"
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                selected.write_bytes(b"%PDF-old")
                with self.serve(selected, "manual", mode) as (share, request):
                    selected.write_bytes(b"%PDF-new")
                    self.assertEqual(request("/report.pdf")[::2], (200, b"%PDF-new"))
                    self.assertEqual(share.current().source_revision,
                                     hashlib.sha256(b"%PDF-new").hexdigest())
                    selected.unlink()
                    self.assertEqual(request("/report.pdf")[0], 404)
                    self.assertIsNone(share.current())
                    selected.write_bytes(b"%PDF-recreated")
                    self.assertEqual(request("/report.pdf")[::2], (200, b"%PDF-recreated"))
                    self.assertIsNotNone(share.current())

    def test_manual_directory_replaced_by_file_removes_accepted_descendants(self):
        docs = self.source / "docs"
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                docs.unlink(missing_ok=True)
                docs.mkdir()
                (docs / "index.html").write_bytes(b"old directory index")
                (docs / "asset.css").write_bytes(b"old asset")
                with self.serve(update="manual", mode=mode) as (share, request):
                    shutil.rmtree(docs)
                    docs.write_bytes(b"regular replacement")
                    self.assertEqual(request("/docs/index.html")[0], 404)
                    self.assertEqual((share.public_root / "docs").read_bytes(), b"regular replacement")
                    self.assertEqual(request("/docs/asset.css")[0], 404)
                    self.assertEqual(request("/docs")[::2], (200, b"regular replacement"))

    def test_manual_directory_replacement_race_keeps_the_safe_subtree(self):
        docs = self.source / "docs"
        docs.mkdir()
        (docs / "index.html").write_bytes(b"safe directory index")
        share = Publication(self.source, "replacement-race", project_root=self.project)
        accepted = share.prepare()
        shutil.rmtree(docs)
        docs.write_bytes(b"regular replacement")
        outside = self.root / "private.txt"
        outside.write_bytes(b"never publish")
        real_open = os.open

        def replace_before_open(name, flags, *args, **kwargs):
            if name == "docs" and flags & os.O_NOFOLLOW and not docs.is_symlink():
                docs.unlink()
                docs.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(publication.os, "open", side_effect=replace_before_open):
            with self.assertRaises(PublicationError):
                share.prepare_request("/docs/index.html")
        self.assertEqual(share.current(), accepted)
        self.assertEqual((share.public_root / "docs/index.html").read_bytes(), b"safe directory index")

    def test_manual_missing_paths_are_rejected_after_accepting_valid_changes(self):
        docs = self.source / "docs"
        docs.mkdir()
        (docs / "index.html").write_bytes(b"old directory index")
        share = Publication(self.source, "missing-paths", project_root=self.project)
        accepted = share.prepare()
        shutil.rmtree(docs)
        docs.write_bytes(b"regular replacement")
        with self.assertRaises(InvalidRequest):
            share.prepare_request("/docs/index.html")
        self.assertEqual((share.public_root / "docs").read_bytes(), b"regular replacement")
        self.assertNotEqual(share.current(), accepted)
        share.prepare_request("/docs")
        docs.unlink()
        with self.assertRaises(InvalidRequest):
            share.prepare_request("/docs")
        self.assertFalse((share.public_root / "docs").exists())
        self.assertEqual(len(list(share.state_dir.glob("generation-*"))), 1)

    def test_manual_helper_failure_serves_accepted_bytes_with_route_guards(self):
        (self.source / "empty-directory").mkdir()
        for failure in ("transport", "server"):
            for mode in ("path", "subdomain", "no-key"):
                with self.subTest(failure=failure, mode=mode):
                    self.html.write_bytes(b"last successful copy")
                    with self.serve(update="manual", mode=mode,
                                    helper_available=failure != "transport") as (share, request):
                        self.html.write_bytes(b"not yet published")
                        if failure == "server":
                            # A genuine preparation exception exercises the
                            # helper's HTTP 500 response, rather than connection refusal.
                            metadata = share.public_root.resolve().parent / "metadata.json"
                            accepted_metadata = metadata.read_bytes()
                            metadata.write_bytes(b"invalid JSON")
                        status, headers, body = request("/index.html")
                        self.assertEqual((status, body), (200, b"last successful copy"))
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        for method in ("GET", "HEAD"):
                            for path in ("/missing.txt", "/metadata.json", "/empty-directory/"):
                                status, headers, _ = request(path, method=method)
                                self.assertEqual(status, 404, (path, method))
                                self.assertEqual(headers["Cache-Control"], "no-store")
                        self.assertEqual(request("/index.html", method="POST")[0], 405)
                        for path in ("/../index.html", "/%2e%2e/index.html",
                                     "/__alias/prepare", "/metadata.json", "/.runtime/private"):
                            self.assertGreaterEqual(request(path)[0], 400, path)
                        if mode == "path":
                            self.assertEqual(request("/index.html", keyed=False)[0], 404)
                        if mode == "subdomain":
                            self.assertEqual(request("/index.html", host="wrong.example.test")[0], 404)
                        if failure == "server":
                            metadata.write_bytes(accepted_metadata)
                            self.assertEqual(request("/index.html")[::2], (200, b"not yet published"))

    def test_file_server_errors_keep_their_status_codes(self):
        for update in ("snapshot", "manual"):
            for mode in ("path", "subdomain", "no-key"):
                with self.subTest(update=update, mode=mode), self.serve(update=update, mode=mode) as (_, request):
                    status, headers, _ = request("/missing.txt")
                    self.assertEqual(status, 404)
                    if update == "manual":
                        self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(request("/index.html", method="POST")[0], 405)

    def test_keyed_directory_redirects_preserve_escaping_and_query(self):
        names = ("a?b", "a#b", "a%b", "a*b", "a[b]")
        for name in names:
            directory = self.source / name
            directory.mkdir()
            (directory / "index.html").write_bytes(name.encode())
        for update, available in (("snapshot", True), ("manual", True), ("manual", False)):
            with self.subTest(update=update, helper_available=available), self.serve(
                    update=update, helper_available=available) as (_, request):
                for query in ("", "?refresh=%23%25"):
                    status, headers, _ = request("/test-key" + query, keyed=False)
                    self.assertEqual(status, 308)
                    self.assertEqual(headers["Location"], "/test-key/" + query)
                for name in names:
                    path = "/" + quote(name, safe="")
                    for query in ("", "?refresh=%23%25"):
                        status, headers, _ = request(path + query)
                        self.assertEqual(status, 308)
                        if update == "manual":
                            self.assertEqual(headers["Cache-Control"], "no-store")
                        location = headers["Location"]
                        self.assertEqual(location, "/test-key" + path + "/" + query)
                        status, headers, body = request(location, keyed=False)
                        self.assertEqual((status, body), (200, name.encode()))
                        self.assertNotIn("Location", headers)

    def test_manual_safe_last_copy_survives_symlink_attacks_and_recovers(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"private bytes")
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"safe copy")
                with self.serve(update="manual", mode=mode) as (share, request):
                    accepted = share.current()
                    self.html.unlink()
                    self.html.symlink_to(outside / "secret.txt")
                    self.assertEqual(request("/")[::2], (200, b"safe copy"))
                    self.assertEqual(share.current(), accepted)
                    self.html.unlink()
                    # Replacing the selected directory cannot escape either.
                    moved = self.root / "moved-site"
                    self.source.rename(moved)
                    self.source.symlink_to(outside, target_is_directory=True)
                    self.assertEqual(request("/index.html")[::2], (200, b"safe copy"))
                    self.assertEqual(request("/secret.txt")[0], 404)
                    self.source.unlink()
                    moved.rename(self.source)
                    self.html.write_bytes(b"recovered")
                    self.assertEqual(request("/")[::2], (200, b"recovered"))
                    self.assertEqual(len(list(share.state_dir.glob("generation-*"))), 1)

    def test_manual_refresh_is_request_local_and_does_not_expand_symlinks(self):
        nested = self.source / "assets"
        nested.mkdir()
        asset = nested / "site.css"
        asset.write_bytes(b"safe css")
        outside = self.root / "private.css"
        outside.write_bytes(b"private css")
        with self.serve(update="manual") as (_, request):
            link = self.source / "unrelated"
            link.symlink_to(outside)
            self.html.write_bytes(b"fresh html")
            self.assertEqual(request("/")[::2], (200, b"fresh html"))
            self.assertEqual(request("/unrelated")[0], 404)
            replacement = self.root / "replacement.css"
            replacement.write_bytes(b"atomic save")
            os.replace(replacement, asset)
            self.assertEqual(request("/assets/site.css")[::2], (200, b"atomic save"))
            moved = self.source / "old-assets"
            nested.rename(moved)
            nested.symlink_to(self.root, target_is_directory=True)
            self.assertEqual(request("/assets/private.css")[0], 404)
            self.assertEqual(request("/assets/site.css")[::2], (200, b"atomic save"))

    def test_traversal_control_routes_and_metadata_are_not_served(self):
        for update in ("snapshot", "manual"):
            for mode in ("path", "subdomain", "no-key"):
                with self.subTest(update=update, mode=mode), self.serve(update=update, mode=mode) as (_, request):
                    for path in ("/../index.html", "/%2e%2e/index.html", "/%2e%2e%2findex.html",
                                 "/__alias", "/__alias/prepare", "/%5f%5falias/prepare",
                                 "/.runtime/private", "/.git/config", "/metadata.json", "/helper.json"):
                        self.assertGreaterEqual(request(path)[0], 400, path)
                    self.assertEqual(request("/index.html", method="POST")[0], 405)

    def test_reserved_source_collision_is_rejected(self):
        reserved = self.source / "__alias"
        reserved.mkdir()
        (reserved / "prepare").write_text("not a control service")
        share = Publication(self.source, "collision", project_root=self.project)
        with self.assertRaisesRegex(PublicationError, "reserved control path"):
            share.prepare()
        with self.assertRaises(PublicationError):
            Publication(reserved / "prepare", "collision-file", project_root=self.project)

    def test_failed_manual_activation_preserves_bytes_and_private_generation(self):
        share = Publication(self.source, "failed-request", project_root=self.project)
        accepted = share.prepare()
        self.html.write_bytes(b"not activated")
        with patch.object(publication.os, "replace", side_effect=OSError("activation failed")):
            with self.assertRaisesRegex(PublicationError, "activation failed"):
                share.prepare_request("/index.html")
        self.assertEqual(share.current(), accepted)
        self.assertEqual((share.public_root / "index.html").read_bytes(), b"<h1>original</h1>")
        self.assertEqual(len(list(share.state_dir.glob("generation-*"))), 1)


if __name__ == "__main__":
    unittest.main()

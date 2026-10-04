"""Official MCP, real Caddy and native publications; only cloudflared is mocked."""

import asyncio
from contextlib import contextmanager, ExitStack
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import threading
import time
from urllib.parse import quote, urlsplit

from test_file_http import free_port
from test_live_events import read_revision
from test_mcp import MCPFixture
from test_reload import BROWSER, injected_config, injected_script, resolve_url, strip_reload
from publication import current_publication


def process_identity(pid):
    """Include Linux start time so cleanup never signals a reused PID."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        return pid, fields[19]
    except FileNotFoundError:
        return None


def source_revision(files):
    manifest = [(name, hashlib.sha256(data).hexdigest()) for name, data in sorted(files.items())]
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class ExposureIntegrationTests(MCPFixture):
    def setUp(self):
        self.owned = {}
        super().setUp()
        caddy = shutil.which("caddy")
        self.assertIsNotNone(caddy, "The pinned Caddy must be on the check's PATH")
        # Record the exact child PID, then exec the real binary, retaining the
        # command/config identity used by the launcher's ownership checks.
        (self.project / "bin/caddy").write_text(f'''#!/usr/bin/env python3
import os
import sys
with open(os.environ["TEST_DAEMON_RECORD"], "a") as record:
    record.write(str(os.getpid()) + "\\tcaddy\\n")
os.execv({caddy!r}, [{caddy!r}, *sys.argv[1:]])
''')
        self.env["CADDY_PORT"] = str(free_port())
        self.page.unlink()
        self.page = self.site / "index.html"
        self.files = {"index.html": b"<h1>initial</h1>", "style.css": b"body { color: red }",
                      "assets/image.svg": b"<svg/>", "nested/index.htm": b"nested page"}
        for name, data in self.files.items():
            path = self.site / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        (self.site / ".env").write_bytes(b"excluded synthetic setting")

    def remember_helpers(self):
        super().remember_helpers()
        for pid in {*self.helpers, *self.daemon_pids()}:
            identity = process_identity(pid)
            if identity is not None:
                self.owned.setdefault(pid, identity)

    def stop_test_processes(self):
        self.remember_helpers()
        try:
            if self.runtime.exists():
                result = self.invoke("stop")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            self.remember_helpers()
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for pid, identity in self.owned.items():
                    if process_identity(pid) == identity:
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
                deadline = time.monotonic() + 5
                while (any(process_identity(pid) == identity for pid, identity in self.owned.items())
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
            self.assertFalse(any(process_identity(pid) == identity
                                 for pid, identity in self.owned.items()), "Owned daemon survived cleanup")

    def run_client(self, body):
        asyncio.run(asyncio.wait_for(body(), timeout=180))

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail(message)

    def publication(self, share):
        return self.runtime / "publications" / share["id"]

    def current(self, share):
        return current_publication(self.publication(share))

    def identities(self, *shares):
        pids = []
        for share in shares:
            instance = self.runtime / "instances" / share["id"]
            pids.append(int((instance / "caddy.pid").read_text()))
            helper = self.publication(share) / "helper.pid"
            if helper.exists():
                pids.append(int(helper.read_text()))
        pids.append(int((self.runtime / "cloudflared/cloudflared.pid").read_text()))
        identities = {pid: process_identity(pid) for pid in pids}
        self.assertTrue(all(identities.values()), identities)
        return identities

    def assert_identities(self, identities):
        self.assertEqual({pid: process_identity(pid) for pid in identities}, identities)

    async def expose(self, client, *, path="site", update="live", mode="path", key="preview"):
        arguments = {"path": path, "update_mode": update, "url_mode": mode}
        if mode != "no-key":
            arguments["key"] = key
        return await self.call(client, "expose_files", arguments)

    def route(self, share):
        url = urlsplit(share["url"])
        base = url.path if share["source"]["type"] != "file" else url.path.rsplit("/", 1)[0] + "/"
        return int(share["id"].split(".", 1)[0]), url.hostname, base

    def request(self, share, path="", *, host=None, keyed=True, method="GET"):
        port, hostname, base = self.route(share)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, (base if keyed else "/") + path.lstrip("/"),
                               headers={"Host": host or hostname})
            with connection.getresponse() as response:
                return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    @contextmanager
    def events(self, share, *, last_revision=None):
        port, hostname, base = self.route(share)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": hostname}
        if last_revision is not None:
            headers["Last-Event-ID"] = last_revision
        try:
            connection.request("GET", base + "__alias/events", headers=headers)
            with connection.getresponse() as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                yield response
        finally:
            connection.close()

    def assert_content(self, share, path, expected):
        status, headers, body = self.request(share, path)
        self.assertEqual(status, 200, body)
        if share["update_mode"] == "live" and Path(path or "index.html").suffix.lower() in {".html", ".htm"}:
            self.assertEqual(injected_config(body), {"revision": self.current(share).revision,
                                                    "events": self.route(share)[2] + "__alias/events"})
            self.assertEqual(strip_reload(body), expected)
        else:
            self.assertEqual(body, expected)
            self.assertNotIn(b"data-alias-reload", body)
        if share["update_mode"] in {"manual", "live"}:
            self.assertEqual(headers["Cache-Control"], "no-store")
        return headers

    def wait_source(self, share, expected):
        self.wait_for(lambda: self.current(share).source_revision == expected,
                      "Native event did not publish the expected source revision")
        return self.current(share)

    def browser(self, body, url):
        import quickjs
        context = quickjs.Context()
        context.add_callable("resolveURL", resolve_url)
        context.eval(BROWSER + f"window.location.href = {json.dumps(url)};")
        context.eval(injected_script(body))
        self.assertEqual(context.eval("browser.connections[0].url"),
                         resolve_url(injected_config(body)["events"], url))
        return context

    def browser_revision(self, browser, revision, index=0):
        data = json.dumps({"revision": revision})
        browser.eval(f"emit({index}, 'revision', {json.dumps(data)});")

    @contextmanager
    def backend(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"path": self.path, "prefix": self.headers.get("X-Forwarded-Prefix")}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_directory_update_and_url_modes_through_mcp_and_caddy(self):
        async def body():
            async with self.client() as client:
                for update in ("snapshot", "manual", "live"):
                    for mode in ("path", "subdomain", "no-key"):
                        with self.subTest(update=update, mode=mode):
                            for name, data in self.files.items():
                                (self.site / name).write_bytes(data)
                            share = await self.expose(client, update=update, mode=mode)
                            self.assertEqual((share["update_mode"], share["url_mode"], share["state"]),
                                             (update, mode, "active"))
                            self.assertEqual(share["source_revision"], source_revision(self.files))
                            identities = self.identities(share)
                            self.assert_content(share, "", self.files["index.html"])
                            for name, data in self.files.items():
                                self.assert_content(share, name, data)
                            for name in (".env", "metadata.json", "helper.json", "__alias/prepare", "%2e%2e/index.html"):
                                self.assertGreaterEqual(self.request(share, name)[0], 400, name)
                            if mode == "path":
                                self.assertEqual(self.request(share, "index.html", keyed=False)[0], 404)
                                self.assertEqual(self.request(share, "preview-extra/index.html", keyed=False)[0], 404)
                                status, headers, _ = self.request(share, "preview?refresh=1", keyed=False)
                                self.assertEqual((status, headers["Location"]), (308, "/preview/?refresh=1"))
                            if mode == "subdomain":
                                self.assertEqual(self.request(share, "index.html", host="wrong.example.test")[0], 404)
                            changed = self.files | {"index.html": b"<h1>updated</h1>", "style.css": b"new CSS"}
                            self.page.write_bytes(changed["index.html"])
                            (self.site / "style.css").write_bytes(changed["style.css"])
                            if update == "manual":
                                self.assertEqual(self.current(share).revision, share["revision"])
                                self.assertEqual((self.publication(share) / "public/index.html").read_bytes(),
                                                 self.files["index.html"])
                            if update == "snapshot":
                                self.assert_content(share, "index.html", self.files["index.html"])
                                self.assert_content(share, "style.css", self.files["style.css"])
                                self.assertEqual(self.current(share).revision, share["revision"])
                                self.assert_identities(identities)
                                old = share
                                share = await self.expose(client, update=update, mode=mode)
                                self.assertNotEqual(share["id"], old["id"])
                                self.assertFalse(self.publication(old).exists())
                                self.assertFalse((self.runtime / "instances" / old["id"] / "caddy.pid").exists())
                            elif update == "live":
                                # No browser or event subscriber has been created.
                                self.wait_source(share, source_revision(changed))
                            self.assert_content(share, "index.html", changed["index.html"])
                            self.assert_content(share, "style.css", changed["style.css"])
                            self.assertEqual(self.current(share).source_revision, source_revision(changed))
                            self.assertEqual(self.page.read_bytes(), changed["index.html"])
                            if update != "snapshot":
                                self.assert_identities(identities)
                            listing = (await self.call(client, "list_shares"))["shares"]
                            self.assertEqual(listing, self.success("list-shares"))
                            self.assertEqual(listing[0]["revision"], self.current(share).revision)
                            await self.call(client, "stop_share", {"id": share["id"]})
                            self.assertFalse(self.publication(share).exists())
                            self.assertEqual(self.success("list-shares"), [])
        self.run_client(body)

    def test_selected_file_boundaries_and_raw_hash_in_all_modes(self):
        selected = self.site / "report ü #.pdf"
        original = b"%PDF-1.7\noriginal\x00\xff"
        changed = b"%PDF-1.7\nmodified\x00\xff"

        async def body():
            async with self.client() as client:
                for update in ("snapshot", "manual", "live"):
                    for mode in ("path", "subdomain", "no-key"):
                        with self.subTest(update=update, mode=mode):
                            selected.write_bytes(original)
                            share = await self.expose(client, path="site/" + selected.name, update=update, mode=mode)
                            filename = quote(selected.name, safe="")
                            self.assertTrue(share["url"].endswith("/" + filename))
                            self.assertEqual(share["source_revision"], hashlib.sha256(original).hexdigest())
                            identities = self.identities(share)
                            self.assertEqual(self.assert_content(share, filename, original)["Content-Type"], "application/pdf")
                            accepted = self.current(share)
                            self.page.write_bytes(b"unselected sibling edit")
                            for name in ("", "index.html", "style.css", "assets/image.svg"):
                                self.assertEqual(self.request(share, name)[0], 404, name)
                            self.assertEqual(self.current(share), accepted)
                            replacement = self.project / "atomic-save"
                            replacement.write_bytes(changed)
                            replacement.replace(selected)
                            if update == "live":
                                self.wait_source(share, hashlib.sha256(changed).hexdigest())
                            expected = original if update == "snapshot" else changed
                            self.assert_content(share, filename, expected)
                            self.assertEqual(self.current(share).source_revision, hashlib.sha256(expected).hexdigest())
                            self.assertEqual([path.name for path in (self.publication(share) / "public").iterdir()],
                                             [selected.name])
                            self.assertEqual(selected.read_bytes(), changed)
                            self.assert_identities(identities)
                            await self.call(client, "stop_share", {"id": share["id"]})
        self.run_client(body)

    def test_native_membership_revisions_sse_ordering_and_browser_lifecycle(self):
        async def body():
            async with self.client() as client:
                for mode in ("path", "subdomain", "no-key"):
                    with self.subTest(mode=mode):
                        for name, data in self.files.items():
                            (self.site / name).write_bytes(data)
                        share = await self.expose(client, mode=mode)
                        identities = self.identities(share)
                        browser = self.browser(self.request(share, "index.html")[2], share["url"] + "index.html")
                        with self.events(share) as stream:
                            initial = read_revision(stream)
                            self.assertEqual(initial, {"revision": share["revision"], "source_revision": source_revision(self.files)})
                            self.browser_revision(browser, initial["revision"])
                            browser.eval("emit(0, 'message', 'heartbeat'); emit(0, 'error', '');")
                            self.assertEqual(browser.eval("browser.reloads"), 0)
                            for edit in (lambda: os.utime(self.page),
                                         lambda: self.page.write_bytes(self.files["index.html"])):
                                generation = os.readlink(self.publication(share) / "public")
                                edit()
                                self.wait_for(lambda: os.readlink(self.publication(share) / "public") != generation,
                                              "Native unchanged-byte event was not processed")
                                self.assertEqual(self.current(share).revision, initial["revision"])
                            replacement = self.project / "editor-save"
                            replacement.write_bytes(b"body { color: tan }")
                            replacement.replace(self.site / "style.css")
                            changed_files = self.files | {"style.css": b"body { color: tan }"}
                            changed = read_revision(stream)
                            self.assertNotEqual(changed["revision"], initial["revision"])
                            self.assertEqual(changed["source_revision"], source_revision(changed_files))
                            self.assertEqual(changed["revision"], self.current(share).revision)
                            self.assert_content(share, "style.css", changed_files["style.css"])
                            self.assertEqual(injected_config(self.request(share, "index.html")[2])["revision"], changed["revision"])
                            self.browser_revision(browser, changed["revision"])
                            self.assertEqual(browser.eval("browser.reloads"), 1)
                            self.assertTrue(browser.eval("browser.connections[0].closed"))
                            added = self.site / "assets/new ü.json"
                            added.write_bytes(b'{"new":true}')
                            changed_files["assets/new ü.json"] = added.read_bytes()
                            member = read_revision(stream)
                            self.assertEqual(member["source_revision"], source_revision(changed_files))
                            self.assertEqual(member["revision"], self.current(share).revision)
                            self.assert_content(share, "assets/" + quote(added.name), added.read_bytes())
                            renamed = added.with_name("renamed.json")
                            added.rename(renamed)
                            changed_files["assets/renamed.json"] = changed_files.pop("assets/new ü.json")
                            renamed_event = read_revision(stream)
                            self.assertEqual(renamed_event["source_revision"], source_revision(changed_files))
                            self.assertEqual(renamed_event["revision"], self.current(share).revision)
                            self.assertNotEqual(renamed_event["source_revision"], member["source_revision"])
                            self.assertEqual(self.request(share, "assets/" + quote(added.name))[0], 404)
                            self.assert_content(share, "assets/renamed.json", renamed.read_bytes())
                            renamed.unlink()
                            del changed_files["assets/renamed.json"]
                            removed = read_revision(stream)
                            self.assertEqual(removed["source_revision"], source_revision(changed_files))
                            self.assertEqual(removed["revision"], changed["revision"])
                            self.assertEqual(removed["revision"], self.current(share).revision)
                            self.assertEqual(self.request(share, "assets/renamed.json")[0], 404)
                        # Miss an update with the event stream closed and the page hidden.
                        fresh_browser = self.browser(self.request(share, "index.html")[2], share["url"])
                        fresh_browser.eval("activate('hidden');")
                        self.page.write_bytes(b"<h1>missed while disconnected</h1>")
                        changed_files["index.html"] = self.page.read_bytes()
                        missed = self.wait_source(share, source_revision(changed_files))
                        self.assert_content(share, "index.html", changed_files["index.html"])
                        with self.events(share, last_revision=removed["revision"]) as stream:
                            reconnect = read_revision(stream)
                            self.assertEqual(reconnect["revision"], missed.revision)
                            fresh_browser.eval("activate('visible'); page('pagehide', true); page('pageshow', true);")
                            self.browser_revision(fresh_browser, reconnect["revision"], index=0)
                            self.assertEqual(fresh_browser.eval("browser.reloads"), 0)  # Superseded callback.
                            self.browser_revision(fresh_browser, reconnect["revision"], index=2)
                            self.assertEqual(fresh_browser.eval("browser.reloads"), 1)
                        loaded = self.browser(self.request(share, "index.html")[2], share["url"])
                        with self.events(share, last_revision=missed.revision) as stream:
                            self.browser_revision(loaded, read_revision(stream)["revision"])
                            self.assertEqual(loaded.eval("browser.reloads"), 0)
                        for name, data in changed_files.items():
                            self.assertEqual((self.site / name).read_bytes(), data)
                        self.assert_identities(identities)
                        await self.call(client, "stop_share", {"id": share["id"]})
        self.run_client(body)

    def test_preparation_failure_retains_bytes_and_recovers_without_restarts(self):
        outside = self.project / "private.txt"
        outside.write_bytes(b"never selected")

        async def body():
            async with self.client() as client:
                for update in ("manual", "live"):
                    for mode in ("path", "subdomain", "no-key"):
                        with self.subTest(update=update, mode=mode):
                            self.page.write_bytes(self.files["index.html"])
                            share = await self.expose(client, update=update, mode=mode)
                            identities = self.identities(share)
                            accepted = self.current(share)
                            with ExitStack() as streams:
                                stream = streams.enter_context(self.events(share)) if update == "live" else None
                                if stream is not None:
                                    self.assertEqual(read_revision(stream)["revision"], accepted.revision)
                                unsafe = self.site / "unsafe.txt" if update == "live" else self.page
                                if update == "manual":
                                    unsafe.unlink()
                                unsafe.symlink_to(outside)
                                if update == "live":
                                    self.page.write_bytes(b"<h1>pending recovery</h1>")
                                else:
                                    self.assert_content(share, "index.html", self.files["index.html"])
                                log = self.publication(share) / "helper.log"
                                self.wait_for(lambda: "Keeping accepted publication" in log.read_text(),
                                              "Preparation did not reject the unsafe source")
                                self.assertEqual(self.current(share), accepted)
                                self.assert_content(share, "index.html", self.files["index.html"])
                                self.assertEqual(self.request(share, "unsafe.txt")[0], 404)
                                # A failed replacement must leave the working route intact too.
                                arguments = {"path": "site", "update_mode": "snapshot", "url_mode": mode}
                                if mode != "no-key":
                                    arguments["key"] = "preview"
                                failure = await self.call(client, "expose_files", arguments, error=True)
                                self.assertIn("preparation failed", failure["error"]["message"].lower())
                                self.assert_identities(identities)
                                self.assertEqual([p.name for p in (self.runtime / "publications").iterdir()], [share["id"]])
                                unsafe.unlink()
                                self.page.write_bytes(b"<h1>pending recovery</h1>")
                                if stream is not None:
                                    recovered = read_revision(stream)
                                    self.assertNotEqual(recovered["revision"], accepted.revision)
                                    self.assertEqual(recovered["source_revision"], source_revision(
                                        self.files | {"index.html": self.page.read_bytes()}))
                                self.assert_content(share, "index.html", self.page.read_bytes())
                                self.assertNotEqual(self.current(share), accepted)
                                self.wait_for(lambda: len(list(self.publication(share).glob("generation-*"))) == 1,
                                              "Private preparation generations were not cleaned")
                                self.assertEqual(outside.read_bytes(), b"never selected")
                                self.assert_identities(identities)
                            await self.call(client, "stop_share", {"id": share["id"]})
        self.run_client(body)

    def test_blocked_sse_routes_do_not_freeze_live_html_or_assets(self):
        # Model an edge/CSP subscription failure at the real Caddy route in
        # this owned project copy, while retaining the real live helper.
        for template in (self.project / "deploy/caddy").glob("Caddyfile.files.*.template"):
            template.write_text(template.read_text().replace("__EVENT_HANDLER__", '''@blocked_events path /__alias/events
            respond @blocked_events "Blocked" 403
            __EVENT_HANDLER__'''))

        async def body():
            async with self.client() as client:
                for mode in ("path", "subdomain", "no-key"):
                    with self.subTest(mode=mode):
                        for name, data in self.files.items():
                            (self.site / name).write_bytes(data)
                        share = await self.expose(client, mode=mode)
                        identities = self.identities(share)
                        status, headers, _ = self.request(share, "__alias/events")
                        self.assertEqual((status, headers["Cache-Control"]), (403, "no-store"))
                        browser = self.browser(self.request(share, "index.html")[2], share["url"])
                        browser.eval("emit(0, 'error', '');")
                        changed = self.files | {"index.html": b"fresh without SSE", "style.css": b"fresh asset"}
                        for name, data in changed.items():
                            (self.site / name).write_bytes(data)
                        self.wait_source(share, source_revision(changed))
                        self.assert_content(share, "index.html", changed["index.html"])
                        self.assert_content(share, "style.css", changed["style.css"])
                        self.assertEqual(browser.eval("browser.reloads"), 0)
                        self.assert_identities(identities)
                        await self.call(client, "stop_share", {"id": share["id"]})
        self.run_client(body)

    def test_port_routes_preserve_backend_paths_and_forwarded_prefix(self):
        async def body():
            with self.backend() as port:
                async with self.client() as client:
                    for mode in ("path", "subdomain", "no-key"):
                        with self.subTest(mode=mode):
                            arguments = {"port": port, "url_mode": mode}
                            if mode != "no-key":
                                arguments["key"] = "backend"
                            share = await self.call(client, "expose_port", arguments)
                            status, _, body = self.request(share, "api?version=1")
                            self.assertEqual(status, 200, body)
                            self.assertEqual(json.loads(body), {"path": "/api?version=1",
                                                               "prefix": "/backend" if mode == "path" else None})
                            if mode == "path":
                                self.assertEqual(self.request(share, "api", keyed=False)[0], 404)
                            if mode == "subdomain":
                                self.assertEqual(self.request(share, "api", host="wrong.example.test")[0], 404)
                            await self.call(client, "stop_share", {"id": share["id"]})
        self.run_client(body)

    def test_mixed_shares_survive_mcp_exit_and_replace_and_stop_independently(self):
        async def body():
            with self.backend() as port, self.backend() as replacement_port, ExitStack() as streams:
                async with self.client() as client:
                    port_share, manual, live, snapshot = await asyncio.gather(
                        self.call(client, "expose_port", {"port": port, "key": "backend"}),
                        self.expose(client, update="manual", mode="subdomain", key="manual"),
                        self.call(client, "expose_files", {"path": "site", "key": "live"}),
                        self.expose(client, update="snapshot", mode="no-key"),
                    )
                    self.assertEqual(live["update_mode"], "live")
                    listing = (await self.call(client, "list_shares"))["shares"]
                    self.assertEqual({share["id"] for share in listing},
                                     {share["id"] for share in (port_share, manual, live, snapshot)})
                    identities = self.identities(port_share, manual, live, snapshot)
                    history = (self.runtime / "tunnel-history").read_bytes()
                    self.assertEqual(len(history.splitlines()), 1)  # Only the port enters history.
                    stream = streams.enter_context(self.events(live))
                    initial = read_revision(stream)
                # The open SSE connection, helpers, Caddy and connector outlive SDK shutdown.
                self.assert_identities(identities)
                self.assertEqual(len(self.success("list-shares")), 4)
                self.page.write_bytes(b"<h1>after MCP shutdown</h1>")
                event = read_revision(stream)
                self.assertNotEqual(event["revision"], initial["revision"])
                self.assert_content(live, "index.html", self.page.read_bytes())
                self.assert_content(manual, "index.html", self.page.read_bytes())
                self.assert_content(snapshot, "index.html", self.files["index.html"])
                self.assertEqual(self.request(port_share, "api")[0], 200)
                self.assertEqual((self.runtime / "tunnel-history").read_bytes(), history)
                self.assert_identities(identities)
                manual_pids = self.identities(manual)
                self.success("stop-share", manual["id"])
                self.assertFalse(self.publication(manual).exists())
                self.assertTrue(all(process_identity(pid) is None for pid in manual_pids))
                self.assert_identities({pid: identity for pid, identity in identities.items() if pid not in manual_pids})
                survivors = self.identities(port_share, live)
                survivors.pop(int((self.runtime / "cloudflared/cloudflared.pid").read_text()))
                async with self.client() as client:
                    republished = await self.expose(client, update="snapshot", mode="no-key")
                    self.assertNotEqual(republished["source_revision"], snapshot["source_revision"])
                    self.assertFalse(self.publication(snapshot).exists())
                    self.assert_content(republished, "index.html", self.page.read_bytes())
                    # Replace files with a real backend, then replace that backend with files.
                    replacement = await self.call(client, "expose_port", {
                        "port": replacement_port, "url_mode": "no-key",
                    })
                    self.assertFalse(self.publication(republished).exists())
                    self.assertEqual(self.request(replacement, "api")[0], 200)
                    new_files = await self.expose(client, mode="no-key")
                    self.assert_content(new_files, "index.html", self.page.read_bytes())
                    self.assert_identities(survivors)
                    failure = await self.call(client, "stop_share", {"id": replacement["id"]}, error=True)
                    self.assertEqual(failure["error"]["code"], "unknown_share")
                    self.assertEqual({share["id"] for share in (await self.call(client, "list_shares"))["shares"]},
                                     {port_share["id"], live["id"], new_files["id"]})
                    await self.call(client, "stop_share", {"id": new_files["id"]})
                    self.assertFalse(self.publication(new_files).exists())
                    self.assert_identities(survivors)
                    await self.call(client, "stop_share", {"id": port_share["id"]})
                    self.assert_content(live, "index.html", self.page.read_bytes())
                    with self.events(live, last_revision=initial["revision"]) as reconnect:
                        self.assertEqual(read_revision(reconnect)["revision"], event["revision"])
                streams.close()  # Close bounded SSE streams before stopping their helper.
                self.success("stop-share", live["id"])
                self.assertEqual(self.success("list-shares"), [])
                self.assertFalse(list((self.runtime / "publications").iterdir()))
                self.assertFalse((self.runtime / "cloudflared/cloudflared.pid").exists())
                self.assertEqual(self.page.read_bytes(), b"<h1>after MCP shutdown</h1>")
                self.assertTrue(all(process_identity(pid) is None for pid in self.owned))
        self.run_client(body)

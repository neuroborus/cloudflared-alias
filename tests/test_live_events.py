"""Real Linux events and bounded loopback SSE streams, without public tunnels."""

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from publication import LivePublication, Publication, PublicationError
from test_file_http import FileHTTPFixture
from test_reload import injected_config, strip_reload


def read_event(response):
    """Read one complete event or heartbeat; the socket bounds every wait."""
    fields = {}
    while True:
        line = response.readline().decode("utf-8").rstrip("\r\n")
        if not line:
            if fields:
                return fields
            raise AssertionError("SSE stream ended without a complete event")
        name, _, value = line.partition(":")
        fields[name] = value.lstrip()


def read_revision(response):
    event = read_event(response)
    if event.get("event") != "revision":
        raise AssertionError(f"Expected a revision event, received {event}")
    result = json.loads(event["data"])
    if event["id"] != result["revision"]:
        raise AssertionError("SSE ID differs from the accepted revision")
    return result


class LiveHTTPTests(FileHTTPFixture, unittest.TestCase):
    def wait_revision(self, share, previous):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = share.current()
            if current != previous:
                return current
            time.sleep(0.02)
        self.fail("Native file event did not activate a new revision")

    def assert_content(self, request, path, expected):
        status, headers, body = request(path)
        if Path(urlsplit(path).path).suffix.lower() in {".html", ".htm"}:
            self.assertRegex(injected_config(body)["revision"], r"^[a-f0-9]{64}$")
            body = strip_reload(body)
        self.assertEqual((status, body), (200, expected))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_html_copies_track_asset_revisions_without_injection_loops(self):
        original = b"<!doctype html><html><body><h1>original</h1></body></html>"
        nested = self.source / "nested"
        nested.mkdir()
        page = nested / "page.HTM"
        page.write_bytes(original)
        self.html.write_bytes(original)
        asset = self.source / "style.css"
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                asset.write_bytes(b"initial asset")
                with self.serve(update="live", mode=mode, with_events=True) as (share, request, events):
                    current = share.current()
                    event_url = ("/test-key" if mode == "path" else "") + "/__alias/events"
                    for path in ("/", "/nested/page.HTM"):
                        status, headers, body = request(path)
                        self.assertEqual(status, 200)
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        self.assertEqual(strip_reload(body), original)
                        self.assertEqual(injected_config(body),
                                         {"revision": current.revision, "events": event_url})
                    asset.write_bytes(b"changed asset")
                    updated = self.wait_revision(share, current)
                    # A refresh without any subscribers has the new embedded
                    # revision even though neither HTML source changed.
                    body = request("/index.html?refresh=1")[2]
                    self.assertEqual(strip_reload(body), original)
                    self.assertEqual(injected_config(body)["revision"], updated.revision)
                    with events() as response:
                        self.assertEqual(read_revision(response)["revision"], updated.revision)
                    self.assertEqual(self.html.read_bytes(), original)
                    self.assertEqual(page.read_bytes(), original)
                    time.sleep(0.3)  # Drain native read events; injection must not watch itself.
                    generation = share._generation()
                    time.sleep(0.3)
                    self.assertEqual(share.current(), updated)
                    self.assertEqual(share._generation(), generation)

    def test_standalone_non_html_remains_current_on_refresh_without_subscriptions(self):
        selected = self.source / "report.pdf"
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                selected.write_bytes(b"%PDF-initial\x00\xff")
                with self.serve(selected, update="live", mode=mode) as (share, request):
                    initial = share.current()
                    self.assert_content(request, "/", selected.read_bytes())
                    selected.write_bytes(b"%PDF-changed\x00\xff")
                    updated = self.wait_revision(share, initial)
                    self.assertEqual(updated.source_revision,
                                     hashlib.sha256(selected.read_bytes()).hexdigest())
                    self.assert_content(request, "/?refresh=1", selected.read_bytes())
                    self.assert_content(request, "/report.pdf?refresh=1", selected.read_bytes())
                    self.assertEqual(request("/")[1]["Content-Type"], "application/pdf")
                    self.assertEqual(request("/report.pdf")[1]["Content-Type"], "application/pdf")
                    self.assertEqual(request("/index.html")[0], 404)

    def test_revision_stream_follows_accepted_bytes_in_every_url_mode(self):
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"original HTML")
                asset = self.source / "style.css"
                asset.write_bytes(b"initial style")
                with self.serve(update="live", mode=mode, with_events=True) as (share, request, events):
                    initial = share.current()
                    with events() as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        self.assertEqual(read_revision(response)["revision"], initial.revision)
                        self.assert_content(request, "/index.html", b"original HTML")
                        self.assert_content(request, "/style.css", b"initial style")
                        self.html.write_bytes(b"changed HTML!")  # Same-length edit.
                        changed = read_revision(response)
                        self.assertNotEqual(changed["revision"], initial.revision)
                        self.assertEqual(changed["revision"], share.current().revision)
                        self.assert_content(request, "/index.html", b"changed HTML!")
                        asset.write_bytes(b"updated style")
                        self.assertNotEqual(read_revision(response)["revision"], changed["revision"])
                        self.assert_content(request, "/style.css", b"updated style")
                    if mode == "path":
                        self.assertEqual(request("/__alias/events", keyed=False)[0], 404)
                    if mode == "subdomain":
                        self.assertEqual(request("/__alias/events", host="wrong.example.test")[0], 404)
                    self.assertEqual(request("/__alias/prepare")[0], 404)
                    self.assertGreaterEqual(request("/../__alias/events")[0], 400)

    def test_disconnected_and_absent_subscriptions_do_not_freeze_content(self):
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"initial")
                asset = self.source / "data.json"
                asset.write_bytes(b'{"version":1}')
                with self.serve(update="live", mode=mode, with_events=True) as (share, request, events):
                    initial = share.current()
                    self.html.write_bytes(b"fresh without subscribers")
                    updated = self.wait_revision(share, initial)
                    self.assert_content(request, "/index.html?refresh=1", b"fresh without subscribers")
                    with events(last_revision=initial.revision) as response:
                        self.assertEqual(read_revision(response)["revision"], updated.revision)
                    asset.write_bytes(b'{"version":2}')
                    missed = self.wait_revision(share, updated)
                    self.assert_content(request, "/data.json", b'{"version":2}')
                    with events(last_revision=updated.revision) as response:
                        self.assertEqual(read_revision(response)["revision"], missed.revision)
                    with events(last_revision=missed.revision) as response:
                        self.assertEqual(read_revision(response)["revision"], missed.revision)

    def test_blocked_event_route_still_publishes_fresh_html_and_assets(self):
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"initial HTML")
                asset = self.source / "style.css"
                asset.write_bytes(b"initial asset")
                with self.serve(update="live", mode=mode, events_blocked=True) as (share, request):
                    status, headers, _ = request("/__alias/events")
                    self.assertEqual(status, 403)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    initial = share.current()
                    self.html.write_bytes(b"HTML with blocked SSE")
                    updated = self.wait_revision(share, initial)
                    self.assert_content(request, "/index.html", b"HTML with blocked SSE")
                    asset.write_bytes(b"asset with blocked SSE")
                    self.wait_revision(share, updated)
                    self.assert_content(request, "/style.css", b"asset with blocked SSE")

    def test_native_membership_events_and_atomic_editor_saves(self):
        with self.serve(update="live", with_events=True) as (share, request, events):
            with events() as response:
                previous = read_revision(response)["revision"]

                def accepted(path, expected):
                    nonlocal previous
                    revision = read_revision(response)["revision"]
                    self.assertNotEqual(revision, previous)
                    previous = revision
                    self.assert_content(request, path, expected)

                nested = self.source / "assets"
                nested.mkdir()
                added = nested / "image.svg"
                added.write_bytes(b"<svg/>")
                accepted("/assets/image.svg", b"<svg/>")
                renamed = nested / "renamed.svg"
                added.rename(renamed)
                accepted("/assets/renamed.svg", b"<svg/>")
                self.assertEqual(request("/assets/image.svg")[0], 404)
                renamed.unlink()
                revision = read_revision(response)["revision"]
                self.assertNotEqual(revision, previous)
                previous = revision
                self.assertEqual(request("/assets/renamed.svg")[0], 404)
                # The replacement is written outside the selection, then moved
                # in atomically, producing one observable content change.
                replacement = self.root / "editor-save"
                replacement.write_bytes(b"atomic replacement")
                replacement.replace(self.html)
                accepted("/index.html", b"atomic replacement")
                moved = self.root / "moved-assets"
                nested.rename(moved)
                (moved / "new.txt").write_bytes(b"moved directory asset")
                moved.rename(nested)
                accepted("/assets/new.txt", b"moved directory asset")
                # Watches must extend to files in directories moved into scope.
                (nested / "new.txt").write_bytes(b"watched after move")
                accepted("/assets/new.txt", b"watched after move")

    def test_failed_preparation_keeps_bytes_and_later_native_event_recovers(self):
        outside = self.root / "private.txt"
        outside.write_bytes(b"never publish")
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                self.html.write_bytes(b"accepted HTML")
                with self.serve(update="live", mode=mode, with_events=True) as (share, request, events):
                    with events() as response:
                        initial = read_revision(response)["revision"]
                        link = self.source / "unsafe.txt"
                        link.symlink_to(outside)
                        self.html.write_bytes(b"pending recovery")
                        deadline = time.monotonic() + 5
                        log = share.state_dir / "helper.log"
                        while ("Keeping accepted publication" not in log.read_text()
                               and time.monotonic() < deadline):
                            time.sleep(0.02)
                        self.assertIn("Keeping accepted publication", log.read_text())
                        self.assertEqual(share.current().revision, initial)
                        self.assert_content(request, "/index.html", b"accepted HTML")
                        self.assertEqual(request("/unsafe.txt")[0], 404)
                        link.unlink()
                        recovered = read_revision(response)
                        self.assertNotEqual(recovered["revision"], initial)
                        self.assert_content(request, "/index.html", b"pending recovery")

    def test_unavailable_event_handler_leaves_static_routes_and_no_store(self):
        asset = self.source / "style.css"
        asset.write_bytes(b"accepted asset")
        for mode in ("path", "subdomain", "no-key"):
            with self.subTest(mode=mode):
                with self.serve(update="live", mode=mode, helper_available=False) as (_, request):
                    status, headers, _ = request("/__alias/events")
                    self.assertEqual(status, 503)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assert_content(request, "/index.html", self.html.read_bytes())
                    self.assert_content(request, "/style.css", b"accepted asset")

    def test_heartbeat_is_a_comment_and_never_prepares_or_revises_content(self):
        with self.serve(update="live", with_events=True) as (share, _, events):
            generation = share._generation()
            accepted = share.current()
            with events(timeout=20) as response:
                self.assertEqual(read_revision(response)["revision"], accepted.revision)
                heartbeat = read_event(response)
                self.assertIn("", heartbeat)  # SSE comment, not a revision event.
                self.assertNotIn("data", heartbeat)
                self.assertEqual(share.current(), accepted)
                self.assertEqual(share._generation(), generation)


class NativeWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alias-native-events-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "file.txt"
        self.file.write_bytes(b"initial")

    async def start(self, source=None):
        share = Publication(source or self.source, "native", project_root=self.project)
        live = LivePublication(share, debounce=0.03)
        self.addAsyncCleanup(live.stop)
        await live.start()
        return share, live

    async def wait_revision(self, live, previous):
        async def changed():
            while live.current == previous:
                await asyncio.sleep(0.01)
            return live.current
        return await asyncio.wait_for(changed(), timeout=5)

    async def close_stream(self, stream):
        # unittest awaits coroutine functions; an async generator's built-in
        # aclose method is not recognized as one by addAsyncCleanup.
        await stream.aclose()

    @contextmanager
    def watch_reads(self):
        opened = []
        real_open = os.open

        def record(name, flags, *args, **kwargs):
            if name == self.file.name:
                opened.append(name)
            return real_open(name, flags, *args, **kwargs)

        with patch("publication.os.open", side_effect=record):
            yield opened

    async def test_standalone_parent_watch_filters_siblings_and_atomic_replacements(self):
        with self.watch_reads() as reads:
            share, live = await self.start(self.file)
            initial = live.current
            count = len(reads)
            sibling = self.source / "sibling.txt"
            sibling.write_bytes(b"unselected")
            await asyncio.sleep(0.2)
            self.assertEqual(len(reads), count)
            self.assertEqual(live.current, initial)
            replacement = self.root / "replacement"
            replacement.write_bytes(b"replacement")
            replacement.replace(self.file)
            current = await self.wait_revision(live, initial)
            self.assertEqual(current.source_revision, hashlib.sha256(b"replacement").hexdigest())
            self.assertEqual((share.public_root / self.file.name).read_bytes(), b"replacement")
            self.assertEqual(list(share.public_root.iterdir()), [share.public_root / self.file.name])
            self.file.unlink()
            await asyncio.sleep(0.2)
            self.assertEqual(live.current, current)
            self.file.write_bytes(b"restored")
            await self.wait_revision(live, current)

    async def test_no_initial_watch_gap_and_no_preparation_from_idle_or_read_events(self):
        share = Publication(self.file, "native", project_root=self.project)
        live = LivePublication(share, debounce=0.03)
        self.addAsyncCleanup(live.stop)
        prepare = share.prepare
        first = True

        def edit_after_copy(**options):
            nonlocal first
            result = prepare(**options)
            if first:
                first = False
                self.file.write_bytes(b"changed before startup completed")
            return result

        with patch.object(share, "prepare", side_effect=edit_after_copy) as calls:
            await live.start()
            await self.wait_revision(live, live.current)
            self.assertEqual((share.public_root / self.file.name).read_bytes(), self.file.read_bytes())
            await asyncio.sleep(0.2)
            count = calls.call_count
            self.file.read_bytes()
            await asyncio.sleep(0.2)
            self.assertEqual(calls.call_count, count)

    async def test_identical_bytes_and_timestamps_do_not_announce_new_revisions(self):
        share, live = await self.start()
        stream = live.events()
        self.addAsyncCleanup(self.close_stream, stream)
        initial = live.current
        self.assertEqual(json.loads((await anext(stream))["data"])["revision"], initial.revision)
        self.file.write_bytes(self.file.read_bytes())
        os.utime(self.file)
        await asyncio.sleep(0.2)
        self.assertEqual(live.current, initial)
        self.assertEqual(share.current(), initial)
        self.assertEqual(next(iter(live.subscribers)).qsize(), 0)
        self.file.write_bytes(b"changed")
        result = await asyncio.wait_for(anext(stream), timeout=5)
        self.assertNotEqual(json.loads(result["data"])["revision"], initial.revision)

    async def test_failed_activation_retries_on_events_and_slow_subscribers_are_bounded(self):
        share, live = await self.start()
        stream = live.events()
        self.addAsyncCleanup(self.close_stream, stream)
        await anext(stream)
        initial = live.current
        with patch.object(share, "prepare", side_effect=PublicationError("injected failure")) as prepare:
            self.file.write_bytes(b"after failure")
            deadline = asyncio.get_running_loop().time() + 5
            while not prepare.called and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(prepare.called)
            self.assertEqual(live.current, initial)
            self.assertEqual(share.current(), initial)
        os.utime(self.file)  # A new native event recovers, without a polling retry.
        updated = await self.wait_revision(live, initial)
        self.file.write_bytes(b"newer")
        latest = await self.wait_revision(live, updated)
        self.assertEqual(next(iter(live.subscribers)).qsize(), 1)
        event = await asyncio.wait_for(anext(stream), timeout=5)
        self.assertEqual(json.loads(event["data"])["revision"], latest.revision)
        await stream.aclose()
        self.assertFalse(live.subscribers)

    async def test_connecting_during_activation_captures_the_latest_accepted_state(self):
        share, live = await self.start()
        activated = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        prepare = share.prepare

        def pause_after_activation(**options):
            result = prepare(**options)
            activated.set()
            if not release.wait(timeout=5):
                raise AssertionError("Preparation was not released")
            return result

        with patch.object(share, "prepare", side_effect=pause_after_activation):
            self.file.write_bytes(b"already activated")
            self.assertTrue(await asyncio.to_thread(activated.wait, 5))
            self.assertEqual((share.public_root / self.file.name).read_bytes(), b"already activated")
            stream = live.events()
            self.addAsyncCleanup(self.close_stream, stream)
            initial = asyncio.create_task(anext(stream))
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(initial.done())
                release.set()
                result = await asyncio.wait_for(initial, timeout=5)
                self.assertEqual(json.loads(result["data"])["revision"], share.current().revision)
            finally:
                release.set()
                initial.cancel()
                await asyncio.gather(initial, return_exceptions=True)

    async def test_directory_root_replacement_restores_recursive_watches(self):
        share, live = await self.start()
        initial = live.current
        moved = self.root / "old-source"
        self.source.rename(moved)
        self.source.mkdir()
        self.file.write_bytes(b"replacement root")
        replaced = await self.wait_revision(live, initial)
        self.assertEqual((share.public_root / self.file.name).read_bytes(), b"replacement root")
        self.file.write_bytes(b"still watched")
        await self.wait_revision(live, replaced)

    async def test_standalone_parent_replacement_rearms_before_file_recreation(self):
        share, live = await self.start(self.file)
        initial = live.current
        observer, worker = live._observer, live._worker
        previous_watch = live._directory_watch
        self.source.rename(self.root / "old-parent")
        self.source.mkdir()

        async def rearmed():
            while live._directory_watch is None or live._directory_watch is previous_watch:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(rearmed(), timeout=5)
        self.assertEqual(live.current, initial)
        self.assertEqual((share.public_root / self.file.name).read_bytes(), b"initial")
        # Recreation after rearming must be detected even when the new parent
        # was empty. Later edits use the same helper and preparation worker.
        self.file.write_bytes(b"new parent file")
        recreated = await self.wait_revision(live, initial)
        self.assertEqual((share.public_root / self.file.name).read_bytes(), b"new parent file")
        self.file.write_bytes(b"subsequent edit")
        await self.wait_revision(live, recreated)
        self.assertEqual((share.public_root / self.file.name).read_bytes(), b"subsequent edit")
        await asyncio.sleep(0.2)  # Drain any remaining notifications from that edit.
        with patch.object(share, "prepare", wraps=share.prepare) as prepare:
            (self.source / "unselected.txt").write_bytes(b"private sibling")
            await asyncio.sleep(0.2)
            prepare.assert_not_called()
        self.assertEqual(list(share.public_root.iterdir()), [share.public_root / self.file.name])
        self.assertIs(live._observer, observer)
        self.assertIs(live._worker, worker)

    async def test_shutdown_joins_owned_watcher_and_preparation_worker(self):
        share, live = await self.start()
        accepted = live.current
        await live.stop()
        self.assertFalse(live._observer.is_alive())
        self.assertFalse(live._observer.emitters)
        self.assertTrue(live._worker.done())
        self.file.write_bytes(b"after stop")
        await asyncio.sleep(0.1)
        self.assertEqual(share.current(), accepted)


if __name__ == "__main__":
    unittest.main()

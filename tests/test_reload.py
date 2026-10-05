"""Execute served reload code in QuickJS; sources and source hashes stay intact."""

import codecs
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publication
from publication import Publication, PublicationError, bundle_revision

SCRIPT = re.compile(br"\n<script data-alias-reload>\n(.*?)</script>\n", re.DOTALL)


def injected_script(body):
    matches = SCRIPT.findall(body)
    if len(matches) != 1:
        raise AssertionError("Expected exactly one injected reload script")
    return matches[0].decode("ascii")


def injected_config(body):
    return json.loads(re.search(r"const config = (\{.*?\});", injected_script(body))[1])


def strip_reload(body):
    injected_script(body)
    return SCRIPT.sub(b"", body)


class ScriptCollector(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._script = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._script = (dict(attrs), [])

    def handle_data(self, data):
        if self._script is not None:
            self._script[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            attrs, chunks = self._script
            self.scripts.append((attrs, "".join(chunks)))
            self._script = None


HTML_CONTEXTS = (
    b'<!doctype html><body><script>const footer = "</body>";</script>',
    b'<!doctype html><body>\n<script>const footer = "preserved"; /* </body> */</script>',
    b'<!doctype html><body>\n<script>const footer = "preserved";</script>\n</body>\n'
    b'<!-- trailing footer </body> -->',
)


class ReloadFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alias-reload-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.root / "site"
        self.source.mkdir()
        self.html = self.source / "index.html"
        self.original = b"<!doctype html><html><body><h1>original</h1></body></html>"
        self.html.write_bytes(self.original)
        self.share = Publication(self.html, "reload", project_root=self.project)


class ReloadPreparationTests(ReloadFixture, unittest.TestCase):
    def test_only_live_html_copies_receive_script_and_original_hash(self):
        snapshot = self.share.prepare()
        served = self.share.public_root / self.html.name
        self.assertEqual(served.read_bytes(), self.original)
        self.share.prepare_request("/index.html")
        self.assertEqual(served.read_bytes(), self.original)  # Manual has no injection.
        live = self.share.prepare(event_url="/preview/__alias/events")
        body = served.read_bytes()
        self.assertEqual(strip_reload(body), self.original)
        self.assertLess(body.index(b"<script data-alias-reload>"), body.index(b"</body>"))
        self.assertEqual(injected_config(body),
                         {"revision": live.revision, "events": "/preview/__alias/events"})
        self.assertEqual(live.source_revision, hashlib.sha256(self.original).hexdigest())
        self.assertEqual(live.source_revision, snapshot.source_revision)
        self.assertNotEqual(live.revision, snapshot.revision)
        self.assertEqual(self.html.read_bytes(), self.original)
        self.assertEqual(list(self.share.public_root.iterdir()), [served])

    def test_body_literals_and_comments_leave_existing_scripts_and_reload_exposed(self):
        for original in HTML_CONTEXTS:
            with self.subTest(original=original):
                self.html.write_bytes(original)
                result = self.share.prepare(event_url="/__alias/events")
                body = (self.share.public_root / "index.html").read_bytes()
                scripts = ScriptCollector(body.decode("ascii")).scripts
                existing = [(attrs, code) for attrs, code in scripts if "data-alias-reload" not in attrs]
                reloads = [code for attrs, code in scripts if "data-alias-reload" in attrs]
                self.assertEqual(existing, ScriptCollector(original.decode("ascii")).scripts)
                self.assertEqual(reloads, ["\n" + injected_script(body)])
                self.assertEqual(strip_reload(body), original)
                self.assertEqual(self.html.read_bytes(), original)
                self.assertEqual(result.source_revision, hashlib.sha256(original).hexdigest())

    def test_body_literals_in_raw_text_and_templates_do_not_capture_injection(self):
        for tag in ("textarea", "title", "style", "xmp", "iframe", "noscript", "template"):
            with self.subTest(tag=tag):
                original = f"<!doctype html><body><{tag}></body></{tag}>".encode("ascii")
                self.html.write_bytes(original)
                self.share.prepare(event_url="/__alias/events")
                body = (self.share.public_root / "index.html").read_bytes()
                self.assertEqual(strip_reload(body), original)
                self.assertTrue(body.startswith(original))

    def test_bundle_hashes_raw_html_and_binary_assets_before_injection(self):
        files = {"index.html": self.original, "nested/page.HTM": b"<h1>fragment</h1>",
                 "style.css": b"h1{}", "image.svg": b"<svg/>",
                 "report.pdf": b"%PDF\x00\xff", "data.json": b'{"ok":true}'}
        for relative, data in files.items():
            path = self.source / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        share = Publication(self.source, "bundle", project_root=self.project)
        result = share.prepare(event_url="/__alias/events")
        expected_manifest = [(name, hashlib.sha256(data).hexdigest())
                             for name, data in sorted(files.items())]
        self.assertEqual(result.source_revision, bundle_revision(expected_manifest))
        metadata = json.loads((share._generation() / "metadata.json").read_text())
        self.assertEqual(metadata["manifest"], [list(entry) for entry in expected_manifest])
        for relative, original in files.items():
            body = (share.public_root / relative).read_bytes()
            if relative.lower().endswith((".html", ".htm")):
                self.assertEqual(strip_reload(body), original)
                self.assertEqual(injected_config(body)["revision"], result.revision)
            else:
                self.assertEqual(body, original)
            self.assertEqual((self.source / relative).read_bytes(), original)

    def test_repeated_copies_and_timestamps_do_not_hash_generated_revisions(self):
        first = self.share.prepare(event_url="/__alias/events")
        served = self.share.public_root / "index.html"
        body = served.read_bytes()
        for _ in range(3):
            os.utime(self.html)
            self.assertEqual(self.share.prepare(event_url="/__alias/events"), first)
            self.assertEqual(served.read_bytes(), body)
        self.html.write_bytes(self.original.replace(b"original", b"modified"))
        changed = self.share.prepare(event_url="/__alias/events")
        self.assertNotEqual(changed.source_revision, first.source_revision)
        self.assertEqual(injected_config(served.read_bytes())["revision"], changed.revision)

    def test_prefix_and_script_changes_only_change_effective_revision(self):
        first = self.share.prepare(event_url="/__alias/events")
        prefixed = self.share.prepare(event_url="/preview/__alias/events")
        self.assertEqual(first.source_revision, prefixed.source_revision)
        self.assertNotEqual(first.revision, prefixed.revision)
        read_text = Path.read_text

        def changed_script(path, *args, **kwargs):
            result = read_text(path, *args, **kwargs)
            return result + "\n// Changed preparation rule.\n" if path.name == "reload.js" else result

        with patch.object(Path, "read_text", new=changed_script):
            changed = self.share.prepare(event_url="/__alias/events")
        self.assertEqual(changed.source_revision, first.source_revision)
        self.assertNotEqual(changed.revision, first.revision)
        self.assertEqual(injected_config((self.share.public_root / "index.html").read_bytes())
                         ["revision"], changed.revision)

    def test_injection_failure_keeps_the_last_successful_generation(self):
        accepted = self.share.prepare(event_url="/__alias/events")
        generation = self.share._generation()
        body = (self.share.public_root / "index.html").read_bytes()
        self.html.write_bytes(b"<h1>pending change</h1>")
        with patch.object(publication, "_inject_reload", side_effect=OSError("injection failed")):
            with self.assertRaisesRegex(PublicationError, "injection failed"):
                self.share.prepare(event_url="/__alias/events")
        self.assertEqual(self.share.current(), accepted)
        self.assertEqual((self.share.public_root / "index.html").read_bytes(), body)
        self.assertEqual(list(self.share.state_dir.glob("generation-*")), [generation])
        self.assertNotEqual(self.share.prepare(event_url="/__alias/events"), accepted)

    def test_injection_preserves_html_encoding_and_fragments(self):
        html = "<body>caf\u00e9</BODY>"
        encodings = ((b"", "utf-8"), (b"", "latin-1"),
                     (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
                     (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"))
        for marker, encoding in encodings:
            with self.subTest(encoding=encoding):
                original = marker + html.encode(encoding)
                self.html.write_bytes(original)
                result = self.share.prepare(event_url="/__alias/events")
                body = (self.share.public_root / "index.html").read_bytes()
                decoded = body[len(marker):].decode(encoding).encode("utf-8")
                self.assertTrue(body.startswith(marker))
                self.assertEqual(strip_reload(decoded), html.encode("utf-8"))
                self.assertEqual(injected_config(decoded)["revision"], result.revision)
                self.assertEqual(self.html.read_bytes(), original)
                self.assertEqual(result.source_revision, hashlib.sha256(original).hexdigest())

    def test_event_paths_must_be_same_origin_and_inside_the_publication_route(self):
        for url in ("https://outside.test/__alias/events", "//outside.test/__alias/events",
                    "__alias/events", "/../__alias/events", "/key/__alias/events?x=1",
                    "/key/__alias/prepare", '/key"</script>/__alias/events'):
            with self.subTest(url=url), self.assertRaises(PublicationError):
                self.share.prepare(event_url=url)
        self.assertFalse(self.share.state_dir.exists())


BROWSER = """
var browser = {connections: [], reloads: 0, attempts: 0, blocked: false};
var windowListeners = {}, documentListeners = {};
function listen(target, name, callback) {
    (target[name] || (target[name] = [])).push(callback);
}
var document = {
    visibilityState: "visible",
    baseURI: "https://example.test/preview/nested/index.html",
    addEventListener(name, callback) { listen(documentListeners, name, callback); }
};
var window = {
    location: {href: "https://example.test/preview/nested/index.html",
               reload() { browser.reloads++; }},
    addEventListener(name, callback) { listen(windowListeners, name, callback); }
};
var URL = function(value, base) { this.href = resolveURL(String(value), String(base)); };
window.EventSource = function(url) {
    browser.attempts++;
    if (browser.blocked) throw new Error("Subscription blocked");
    this.url = resolveURL(String(url), document.baseURI);
    this.closed = false;
    this.listeners = {};
    this.addEventListener = (name, callback) => listen(this.listeners, name, callback);
    this.close = () => { this.closed = true; };
    browser.connections.push(this);
};
function emit(index, name, data) {
    // Also deliver a queued event after close, to exercise superseded handlers.
    (browser.connections[index].listeners[name] || []).forEach(fn => fn({data}));
}
function activate(state) {
    document.visibilityState = state;
    (documentListeners.visibilitychange || []).forEach(fn => fn({}));
}
function page(name, persisted) {
    (windowListeners[name] || []).forEach(fn => fn({persisted}));
}
"""


def resolve_url(value, base):
    """Adapt the browser's URL argument order to urllib's base-first API."""
    return urljoin(base, value)


class BrowserURLTests(unittest.TestCase):
    def test_resolution_uses_browser_argument_order_and_preserves_absolute_urls(self):
        cases = (
            ("/preview/__alias/events", "https://example.test/preview/nested/index.html",
             "https://example.test/preview/__alias/events"),
            ("/__alias/events", "https://opaque.example.test/nested/page.html",
             "https://opaque.example.test/__alias/events"),
            ("/preview/__alias/events", "https://external.test/assets/",
             "https://external.test/preview/__alias/events"),
            ("https://example.test/preview/__alias/events", "https://external.test/assets/",
             "https://example.test/preview/__alias/events"),
        )
        for value, base, expected in cases:
            with self.subTest(value=value, base=base):
                self.assertEqual(resolve_url(value, base), expected)


class BrowserReloadTests(ReloadFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import quickjs  # Pinned quickjs-ng; missing dependencies fail, never skip.
        self.engine = quickjs.Context
        self.loaded = self.share.prepare(event_url="/preview/__alias/events")
        self.body = (self.share.public_root / "index.html").read_bytes()

    def browser(self, before="", body=None):
        context = self.engine()
        # QuickJS has no browser URL APIs; use standard URL resolution in the double.
        context.add_callable("resolveURL", resolve_url)
        context.eval(BROWSER + before)
        context.eval(injected_script(self.body if body is None else body))
        return context

    def revision(self, context, value, index=0):
        data = json.dumps({"revision": value})
        context.eval(f"emit({index}, 'revision', {json.dumps(data)});")

    def test_equal_initial_and_reconnect_revisions_do_not_reload(self):
        context = self.browser()
        self.assertEqual(context.eval("browser.connections[0].url"),
                         "https://example.test/preview/__alias/events")
        self.revision(context, self.loaded.revision)
        context.eval("emit(0, 'error', ''); emit(0, 'open', '');")
        self.revision(context, self.loaded.revision)
        self.assertEqual(context.eval("browser.reloads"), 0)
        self.assertEqual(context.eval("browser.connections.length"), 1)
        self.assertFalse(context.eval("browser.connections[0].closed"))
        self.revision(context, "a" * 64)  # Reconnected stream catches missed changes.
        self.assertEqual(context.eval("browser.reloads"), 1)
        self.assertTrue(context.eval("browser.connections[0].closed"))
        self.revision(context, "b" * 64)
        context.eval("activate('visible'); page('pageshow', true);")
        self.assertEqual(context.eval("browser.reloads"), 1)
        self.assertEqual(context.eval("browser.connections.length"), 1)

    def test_fresh_copy_receives_its_revision_without_an_initial_reload_loop(self):
        context = self.browser()
        self.html.write_bytes(b"<h1>changed</h1>")
        changed = self.share.prepare(event_url="/preview/__alias/events")
        self.revision(context, changed.revision)
        self.assertEqual(context.eval("browser.reloads"), 1)
        fresh = self.browser(body=(self.share.public_root / "index.html").read_bytes())
        self.revision(fresh, changed.revision)
        self.assertEqual(fresh.eval("browser.reloads"), 0)

    def test_root_and_keyed_event_paths_are_used_without_page_relative_resolution(self):
        for event_url in ("/__alias/events", "/release-preview/__alias/events",
                          "/" + "a" * 32 + "/__alias/events"):
            with self.subTest(event_url=event_url):
                loaded = self.share.prepare(event_url=event_url)
                context = self.browser(body=(self.share.public_root / "index.html").read_bytes())
                self.assertEqual(context.eval("browser.connections[0].url"),
                                 "https://example.test" + event_url)
                self.revision(context, loaded.revision)
                self.assertEqual(context.eval("browser.reloads"), 0)

    def test_body_literals_and_comments_preserve_executable_source_and_reload_scripts(self):
        for original in HTML_CONTEXTS:
            with self.subTest(original=original):
                self.html.write_bytes(original)
                loaded = self.share.prepare(event_url="/preview/__alias/events")
                body = (self.share.public_root / "index.html").read_bytes()
                context = self.engine()
                context.add_callable("resolveURL", resolve_url)
                context.eval(BROWSER)
                scripts = ScriptCollector(body.decode("ascii")).scripts
                self.assertEqual(sum("data-alias-reload" in attrs for attrs, _ in scripts), 1)
                for _, code in scripts:
                    context.eval(code)
                self.assertEqual(context.eval("footer"), "</body>" if b'"</body>"' in original else "preserved")
                self.assertEqual(context.eval("browser.connections.length"), 1)
                self.revision(context, loaded.revision)
                self.assertEqual(context.eval("browser.reloads"), 0)
                self.revision(context, "a" * 64)
                self.assertEqual(context.eval("browser.reloads"), 1)

    def test_external_base_cannot_redirect_subscriptions_or_disclose_any_route_key(self):
        external = "https://external.test/assets/"
        self.html.write_text(f'<!doctype html><head><base href="{external}"></head><body>page</body>')
        routes = (("https://example.test/release-preview/nested/page.html", "/release-preview/__alias/events"),
                  ("https://opaque.example.test/nested/page.html", "/__alias/events"),
                  ("https://example.test/nested/page.html", "/__alias/events"))
        for location, event_url in routes:
            with self.subTest(location=location):
                loaded = self.share.prepare(event_url=event_url)
                context = self.browser(
                    f"window.location.href = {json.dumps(location)}; document.baseURI = {json.dumps(external)};",
                    body=(self.share.public_root / "index.html").read_bytes())
                expected = urljoin(location, event_url)
                self.assertEqual(context.eval("browser.connections[0].url"), expected)
                self.revision(context, loaded.revision)
                context.eval("activate('hidden'); activate('visible'); page('pageshow', true);")
                self.assertEqual(context.eval("browser.connections[2].url"), expected)
                self.revision(context, "a" * 64, index=2)
                self.assertEqual(context.eval("browser.reloads"), 1)

    def test_activation_and_back_forward_restore_replace_subscriptions(self):
        context = self.browser()
        context.eval("page('pageshow', false); activate('hidden');")
        self.assertTrue(context.eval("browser.connections[0].closed"))
        self.assertEqual(context.eval("browser.connections.length"), 1)
        context.eval("activate('visible');")
        self.revision(context, "a" * 64, index=0)  # Stale callback must be ignored.
        self.revision(context, self.loaded.revision, index=1)
        self.assertEqual(context.eval("browser.reloads"), 0)
        context.eval("page('pagehide', true); page('pageshow', true);")
        self.assertTrue(context.eval("browser.connections[1].closed"))
        self.assertEqual(context.eval("browser.connections.length"), 3)
        self.revision(context, self.loaded.revision, index=2)
        self.revision(context, "b" * 64, index=2)
        self.assertEqual(context.eval("browser.reloads"), 1)

    def test_hidden_initial_page_subscribes_when_activated(self):
        context = self.browser("document.visibilityState = 'hidden';")
        self.assertEqual(context.eval("browser.connections.length"), 0)
        context.eval("activate('visible');")
        self.revision(context, "a" * 64)
        self.assertEqual(context.eval("browser.reloads"), 1)

    def test_missing_blocked_and_disconnected_event_source_permit_refresh(self):
        missing = self.browser("delete window.EventSource;")
        missing.eval("activate('visible'); page('pageshow', true);")
        self.assertEqual(missing.eval("browser.connections.length"), 0)
        self.assertEqual(missing.eval("browser.reloads"), 0)
        blocked = self.browser("browser.blocked = true;")
        blocked.eval("activate('visible'); page('pageshow', true);")
        self.assertEqual(blocked.eval("browser.connections.length"), 0)
        self.assertEqual(blocked.eval("browser.reloads"), 0)
        blocked.eval("browser.blocked = false; activate('visible');")
        self.revision(blocked, self.loaded.revision)
        self.assertEqual(blocked.eval("browser.reloads"), 0)
        blocked.eval("emit(0, 'error', '');")
        self.assertFalse(blocked.eval("browser.connections[0].closed"))

    def test_heartbeats_and_malformed_events_do_not_reload(self):
        context = self.browser()
        context.eval("emit(0, 'message', 'heartbeat'); emit(0, 'ping', '');")
        for data in ("not JSON", "null", "{}", '{"revision":null}', '{"revision":42}',
                     '{"revision":""}', '{"revision":"invalid"}'):
            context.eval(f"emit(0, 'revision', {json.dumps(data)});")
        self.revision(context, self.loaded.revision)
        self.assertEqual(context.eval("browser.reloads"), 0)


if __name__ == "__main__":
    unittest.main()

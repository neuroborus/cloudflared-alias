"""Bounded preparation uses only synthetic selections and private project state."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publication
from publication import Publication, PublicationError, select_source


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alias-publication-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.root / "site"
        self.source.mkdir()
        self.file = self.source / "index.html"
        self.file.write_bytes(b"<h1>original</h1>")

    def publication(self, source=None, share_id="9090.test", **options):
        return Publication(source or self.source, share_id,
                           project_root=self.project, **options)

    def files(self, share):
        return {path.relative_to(share.public_root).as_posix(): path.read_bytes()
                for path in share.public_root.rglob("*") if path.is_file()}

    def assert_private(self, share):
        generations = list(share.state_dir.glob("generation-*"))
        self.assertEqual(len(generations), 1)
        self.assertEqual(share.public_root.resolve(), generations[0] / "public")
        self.assertTrue((generations[0] / "metadata.json").is_file())
        self.assertFalse((share.public_root / "metadata.json").exists())
        self.assertFalse(list(share.state_dir.glob("**/activate")))

    def test_standalone_file_freezes_only_selected_bytes(self):
        self.file.write_bytes(b"\x89PNG\r\n\x00\xff")
        (self.source / "secret.txt").write_bytes(b"not selected")
        original = self.file.read_bytes()
        share = self.publication(self.file)
        self.assertFalse(share.state_dir.exists())
        self.assertIsNone(share.current())
        result = share.prepare()
        self.assertEqual(share.selection.kind, "file")
        self.assertEqual(result.source_revision, hashlib.sha256(original).hexdigest())
        self.assertEqual(self.files(share), {"index.html": original})
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(share.current(), result)
        self.file.write_bytes(b"new bytes")
        self.file.unlink()
        self.assertEqual(self.files(share), {"index.html": original})
        self.assert_private(share)

    def test_bundle_preserves_assets_and_excludes_metadata_recursively(self):
        assets = self.source / "assets"
        assets.mkdir()
        content = {"index.html": self.file.read_bytes(), "assets/site.css": b"h1{}",
                   "assets/app.js": b"alert(1)", "assets/image.svg": b"<svg/>",
                   "report.pdf": b"%PDF-1.7\x00", "data.json": b'{"ok":true}',
                   "archive.zip": b"PK\x00\xff"}
        for name, data in content.items():
            (self.source / name).write_bytes(data)
        for name in publication.EXCLUDED_NAMES | {".env", ".env.private"}:
            (assets / name).symlink_to(self.project, target_is_directory=True)
        share = self.publication()
        result = share.prepare()
        self.assertEqual(self.files(share), content)
        expected_manifest = [(name, hashlib.sha256(data).hexdigest())
                             for name, data in sorted(content.items())]
        expected = hashlib.sha256(json.dumps(expected_manifest, ensure_ascii=False,
                                             separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(result.source_revision, expected)
        self.assert_private(share)

    def test_root_internal_paths_and_nonregular_files_are_rejected(self):
        runtime = self.project / ".runtime"
        runtime.mkdir()
        (runtime / "private").write_text("secret")
        fifo = self.source / "pipe"
        os.mkfifo(fifo)
        for source in (Path("/"), self.root, self.project, runtime, runtime / "private", fifo):
            with self.subTest(source=source), self.assertRaises(PublicationError):
                self.publication(source)
        for name in ("AGENTS.md", "cloudflared-alias.conf", ".env.local"):
            source = self.source / name
            source.write_text("private")
            with self.subTest(source=source), self.assertRaises(PublicationError):
                self.publication(source)
        self.assertEqual(list(runtime.iterdir()), [runtime / "private"])

    def test_selected_links_and_linked_ancestors_are_rejected(self):
        link = self.root / "linked"
        link.symlink_to(self.source, target_is_directory=True)
        for source in (link, link / "index.html", link / ".." / "site" / "index.html"):
            for spelling in (str(source), "/" + str(source)):
                with self.subTest(source=spelling), self.assertRaises(PublicationError):
                    self.publication(spelling)
        normalized = select_source(self.source / ".." / "site" / "index.html",
                                   project_root=self.project)
        self.assertEqual(normalized.path, self.file)

    def test_equivalent_linux_roots_cannot_publish_launcher_or_ancestors(self):
        for project_root in (self.project, "/" + str(self.project)):
            for source in (self.project, self.root, Path("/")):
                for spelling in (str(source), "/" + str(source), "//" + str(source)):
                    with self.subTest(project_root=project_root, source=spelling):
                        with self.assertRaises(PublicationError):
                            select_source(spelling, project_root=project_root)
            selected = select_source("/" + str(self.file), project_root=project_root)
            self.assertEqual(selected.path, self.file)

    def test_bundle_symlinks_are_rejected_even_when_the_target_is_inside(self):
        share = self.publication()
        accepted = share.prepare()
        for target in (self.file, self.project):
            link = self.source / "link"
            link.symlink_to(target)
            with self.assertRaises(PublicationError):
                share.prepare()
            self.assertEqual(share.current(), accepted)
            self.assertEqual(self.files(share), {"index.html": self.file.read_bytes()})
            self.assert_private(share)
            link.unlink()

    def test_replaced_files_and_directories_cannot_escape_during_open(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"never publish")
        asset = self.source / "asset.txt"
        asset.write_bytes(b"safe")
        nested = self.source / "assets"
        nested.mkdir()
        (nested / "safe").write_bytes(b"also safe")
        share = self.publication()
        accepted = share.prepare()
        accepted_files = self.files(share)
        real_open = os.open
        for entry in (asset, nested):
            moved = self.source / "moved"

            def replace_before_open(name, flags, *args, **kwargs):
                if name == entry.name and "dir_fd" in kwargs and not entry.is_symlink():
                    entry.rename(moved)
                    entry.symlink_to(outside if entry == nested else outside / "secret")
                return real_open(name, flags, *args, **kwargs)

            with patch.object(publication.os, "open", side_effect=replace_before_open):
                with self.assertRaises(PublicationError):
                    share.prepare()
            self.assertEqual(self.files(share), accepted_files)
            self.assertEqual(share.current(), accepted)
            self.assert_private(share)
            entry.unlink()
            moved.rename(entry)

    def test_unstable_read_is_retried_and_hashes_the_accepted_bytes(self):
        share = self.publication(self.file)
        real_read = os.read
        changed = False

        def edit_during_read(descriptor, size):
            nonlocal changed
            data = real_read(descriptor, size)
            if data and not changed:
                self.file.write_bytes(b"accepted replacement")
                changed = True
            return data

        with patch.object(publication.os, "read", side_effect=edit_during_read):
            result = share.prepare()
        self.assertTrue(changed)
        self.assertEqual(self.files(share), {"index.html": b"accepted replacement"})
        self.assertEqual(result.source_revision, hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assert_private(share)

    def test_persistent_instability_is_bounded_and_preserves_last_copy(self):
        share = self.publication(self.file)
        accepted = share.prepare()
        accepted_files = self.files(share)
        real_read = os.read
        changes = 0

        def edit_during_every_read(descriptor, size):
            nonlocal changes
            data = real_read(descriptor, size)
            if data:
                changes += 1
                self.file.write_bytes(f"edit {changes}".encode())
            return data

        with patch.object(publication.os, "read", side_effect=edit_during_every_read):
            with self.assertRaisesRegex(PublicationError, "stabilize"):
                share.prepare()
        self.assertEqual(changes, 3)
        self.assertEqual(share.current(), accepted)
        self.assertEqual(self.files(share), accepted_files)
        self.assert_private(share)
        self.assertNotEqual(share.prepare(), accepted)

    def test_activation_failure_preserves_metadata_and_other_shares(self):
        share = self.publication(self.file)
        other = self.publication(self.file, "9091.other")
        accepted = share.prepare()
        other_accepted = other.prepare()
        self.file.write_bytes(b"new bytes")
        with patch.object(publication.os, "replace", side_effect=OSError("activation failed")):
            with self.assertRaisesRegex(PublicationError, "activation failed"):
                share.prepare()
        self.assertEqual(share.current(), accepted)
        self.assertEqual(self.files(share), {"index.html": b"<h1>original</h1>"})
        self.assertEqual(other.current(), other_accepted)
        self.assert_private(share)
        self.assertNotEqual(share.prepare(), accepted)
        self.assertEqual(self.files(other), {"index.html": b"<h1>original</h1>"})
        self.assert_private(other)

    def test_interruption_after_activation_preserves_the_served_generation(self):
        real_replace = os.replace

        def replace_then_interrupt(source, destination):
            real_replace(source, destination)
            raise KeyboardInterrupt()

        for previously_published in (False, True):
            with self.subTest(previously_published=previously_published):
                share = self.publication(self.file, f"9090.interrupt-{previously_published}")
                self.file.write_bytes(b"old bytes")
                if previously_published:
                    share.prepare()
                self.file.write_bytes(b"accepted replacement")
                with patch.object(publication.os, "replace", side_effect=replace_then_interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        share.prepare()
                self.assertEqual(self.files(share), {"index.html": b"accepted replacement"})
                current = share.current()
                self.assertEqual(current.source_revision,
                                 hashlib.sha256(self.file.read_bytes()).hexdigest())
                active = share.public_root.resolve(strict=True).parent
                self.assertTrue((active / "metadata.json").is_file())
                self.assertEqual(share.prepare(), current)
                self.assertFalse(active.exists())

    def test_revisions_ignore_location_timestamps_and_preparation_metadata(self):
        (self.source / "é.css").write_bytes(b"asset")
        first = self.publication()
        accepted = first.prepare()
        os.utime(self.file, ns=(1, 1))
        self.file.write_bytes(self.file.read_bytes())
        self.assertEqual(first.prepare(), accepted)
        relocated = self.root / "relocated"
        relocated.mkdir()
        for path in self.source.iterdir():
            (relocated / path.name).write_bytes(path.read_bytes())
        second = self.publication(relocated, "9091.copy")
        self.assertEqual(second.prepare(), accepted)
        changed_rules = self.publication(share_id="9092.rules", preparation_version="copy-v2")
        changed = changed_rules.prepare()
        self.assertEqual(changed.source_revision, accepted.source_revision)
        self.assertNotEqual(changed.revision, accepted.revision)

    def test_same_length_edits_and_bundle_membership_change_revisions(self):
        self.file.write_bytes(b"one")
        share = self.publication()
        previous = share.prepare()
        for action in (
            lambda: self.file.write_bytes(b"two"),
            lambda: (self.source / "asset").write_bytes(b"asset"),
            lambda: (self.source / "asset").rename(self.source / "renamed"),
            lambda: (self.source / "renamed").unlink(),
        ):
            action()
            current = share.prepare()
            self.assertNotEqual(current.source_revision, previous.source_revision)
            previous = current
        self.assertEqual(self.files(share), {"index.html": b"two"})
        self.assertEqual(self.file.read_bytes(), b"two")
        self.assert_private(share)

    def test_private_state_rejects_links_and_invalid_ids(self):
        for share_id in ("", "..", "../escape", "/absolute", "bad\nname"):
            with self.subTest(share_id=share_id), self.assertRaises(PublicationError):
                self.publication(share_id=share_id)
        (self.project / ".runtime").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(PublicationError):
            self.publication().prepare()
        self.assertEqual(list(self.source.iterdir()), [self.file])

    def test_deleted_or_retyped_selection_keeps_the_accepted_file(self):
        share = self.publication(self.file)
        accepted = share.prepare()
        self.file.unlink()
        with self.assertRaises(PublicationError):
            share.prepare()
        self.file.mkdir()
        (self.file / "sibling").write_bytes(b"must not expand the selection")
        with self.assertRaisesRegex(PublicationError, "type changed"):
            share.prepare()
        self.assertEqual(self.files(share), {"index.html": b"<h1>original</h1>"})
        self.assertEqual(share.current(), accepted)
        self.assert_private(share)

    def test_metadata_write_failure_does_not_activate_prepared_bytes(self):
        share = self.publication(self.file)
        accepted = share.prepare()
        self.file.write_bytes(b"replacement")
        with patch.object(Path, "write_text", side_effect=OSError("metadata failed")):
            with self.assertRaisesRegex(PublicationError, "metadata failed"):
                share.prepare()
        self.assertEqual(self.files(share), {"index.html": b"<h1>original</h1>"})
        self.assertEqual(share.current(), accepted)
        self.assert_private(share)


if __name__ == "__main__":
    unittest.main()

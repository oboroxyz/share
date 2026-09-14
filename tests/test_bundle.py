import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import share
from share import bundle


class MemoryR2:
    def __init__(self, events):
        self.objects = {}
        self.events = events
        self.fail_upload = None
        self.fail_manifest = False
        self.fail_delete = False

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.events.append(("upload", key))
        self.objects[key] = Path(path).read_bytes()
        if self.fail_upload and key.endswith(self.fail_upload):
            raise RuntimeError("injected upload failure")

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.events.append(("manifest", Key))
        self.objects[Key] = Body
        if self.fail_manifest:
            raise RuntimeError("injected manifest failure")

    def delete_object(self, *, Bucket, Key):
        self.events.append(("delete", Key))
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        for offset in range(0, len(keys), 1000):
            yield {"Contents": [{"Key": key} for key in keys[offset:offset + 1000]]}

    def delete_objects(self, *, Bucket, Delete):
        self.events.append(("batch", Delete["Objects"]))
        if self.fail_delete:
            return {"Errors": [{"Code": "AccessDenied"}]}
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "report"
        self.root.mkdir()
        self.write("index.html", b'<link rel="stylesheet" href="assets/style.css">')
        self.write("assets/style.css", b"body { margin: 1rem }")
        self.events, self.entries = [], {}
        self.s3 = MemoryR2(self.events)
        self.config = {"cloudflare": {"bucket": "test"}, "urls": {"public_base": "https://share.example"}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(share, "load_config", return_value=self.config))
        self.stack.enter_context(patch.object(share, "r2_client", return_value=self.s3))
        self.stack.enter_context(patch.object(share, "cf_client", return_value=object()))
        self.stack.enter_context(patch.object(share, "kv_get", side_effect=lambda c, cf, k: self.entries.get(k)))
        self.stack.enter_context(patch.object(share, "kv_put", side_effect=self.put))
        self.stack.enter_context(patch.object(share, "kv_list", side_effect=lambda c, cf: list(self.entries.values())))
        self.stack.enter_context(patch.object(share, "kv_delete", side_effect=self.delete))
        self.stack.enter_context(patch.object(bundle.shutil, "which", return_value=None))

    def write(self, name, data=b"test"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def put(self, config, cf, key, value):
        self.events.append(("kv_put", key))
        self.entries[key] = value

    def delete(self, config, cf, key):
        self.events.append(("kv_delete", key))
        self.entries.pop(key, None)

    def args(self, **overrides):
        values = dict(directory=str(self.root), name=None, slug=None, entry=None, public=False, exclude=[], dry_run=False)
        return argparse.Namespace(**(values | overrides))

    def upload(self, **overrides):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bundle.cmd_bundle(self.args(**overrides))
        return output.getvalue().strip()

    def test_collection_excludes_hidden_secrets_and_symlinks(self):
        for name in [".env", ".git/config", "assets/.hidden", "secret.pem", "credentials.json", "node_modules/a.js"]:
            self.write(name)
        (self.root / "linked").symlink_to(self.root / "assets", target_is_directory=True)
        (self.root / "link.txt").symlink_to(self.root / "index.html")
        files, skipped = bundle.collect_files(self.root, [])
        self.assertEqual([file.path for file in files], ["assets/style.css", "index.html"])
        self.assertIn(".git", skipped)
        self.assertIn("linked", skipped)
        self.assertIn("link.txt", skipped)

    def test_user_exclusions(self):
        files, skipped = bundle.collect_files(self.root, ["assets/*"])
        self.assertEqual([file.path for file in files], ["index.html"])
        self.assertEqual(skipped, ["assets/style.css"])

    def test_entry_selection(self):
        files, _ = bundle.collect_files(self.root, [])
        self.assertEqual(bundle.choose_entry(files, None), "index.html")
        self.assertEqual(bundle.choose_entry(files, "assets/style.css"), "assets/style.css")
        self.assertIsNone(bundle.choose_entry(files[:1], None))
        for entry in ["../index.html", "/index.html", "./index.html", "missing", "assets//style.css"]:
            with self.subTest(entry=entry), self.assertRaises(bundle.BundleError):
                bundle.choose_entry(files, entry)

    def test_safe_paths(self):
        for path in ["", "../x", "a/../b", "a//b", "a/", "/x", "a\\b", "a\x00b", ".env", "a/.env", "x" * 901]:
            with self.subTest(path=path):
                self.assertFalse(bundle.safe_path(path))
        self.assertTrue(bundle.safe_path("画像/a & b.png"))

    def test_limits(self):
        with patch.object(bundle, "MAX_FILES", 1), self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root, [])
        with patch.object(bundle, "MAX_BYTES", 1), self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root, [])

    def test_empty_and_invalid_root(self):
        with self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root / "index.html", [])
        with self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root / "missing", [])
        with self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root, ["*"])
        linked = Path(self.temp.name) / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(bundle.BundleError):
            bundle.collect_files(linked, [])

    def test_special_files_rejected(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(bundle.BundleError):
            bundle.collect_files(self.root, [])

    def test_snapshot_preserves_bytes_and_sources(self):
        files, _ = bundle.collect_files(self.root, [])
        output = Path(self.temp.name) / "snapshot"
        bundle.snapshot(self.root, files, output)
        for file in files:
            self.assertEqual((self.root / file.path).read_bytes(), (output / file.path).read_bytes())

    def test_snapshot_rejects_changed_file(self):
        files, _ = bundle.collect_files(self.root, [])
        self.write("index.html", b"changed")
        with self.assertRaises(bundle.BundleError):
            bundle.snapshot(self.root, files, Path(self.temp.name) / "snapshot")

    def test_snapshot_rejects_file_symlink_swap(self):
        files, _ = bundle.collect_files(self.root, [])
        (self.root / "index.html").unlink()
        (self.root / "index.html").symlink_to(self.root / "assets/style.css")
        with self.assertRaises(bundle.BundleError):
            bundle.snapshot(self.root, files, Path(self.temp.name) / "snapshot")

    def test_snapshot_rejects_parent_symlink_swap(self):
        files, _ = bundle.collect_files(self.root, [])
        original = self.root / "assets"
        moved = Path(self.temp.name) / "moved"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(bundle.BundleError):
            bundle.snapshot(self.root, files, Path(self.temp.name) / "snapshot")

    def test_dry_run_needs_no_config(self):
        with patch.object(share, "load_config", side_effect=AssertionError("credentials accessed")):
            output = self.upload(dry_run=True)
        self.assertIn("include  index.html", output)
        self.assertIn("2 files", output)
        self.assertEqual(self.events, [])

    def test_upload_is_unlisted_and_publishes_last(self):
        url = self.upload()
        self.assertRegex(url, r"^https://share\.example/[0-9a-f]{32}/$")
        metadata = next(iter(self.entries.values()))
        self.assertFalse(metadata["public"])
        self.assertEqual(metadata["file_count"], 2)
        self.assertEqual(self.events[-2][0], "manifest")
        self.assertEqual(self.events[-1][0], "kv_put")
        prefix = metadata["r2_prefix"]
        manifest = json.loads(self.s3.objects[prefix + ".manifest.json"])
        self.assertEqual(manifest["entry"], "index.html")
        self.assertEqual(self.s3.objects[prefix + "index.html"], (self.root / "index.html").read_bytes())

    def test_public_custom_entry_and_name(self):
        self.upload(public=True, slug="report-v1", entry="assets/style.css", name="My report")
        metadata = self.entries["slug:report-v1"]
        self.assertTrue(metadata["public"])
        self.assertEqual(metadata["entry"], "assets/style.css")
        self.assertEqual(metadata["name"], "My report")

    def test_bundle_never_strips_metadata(self):
        self.write("image.png", b"opaque fixture; not a real image")
        with patch.object(share, "strip_metadata", side_effect=AssertionError("rewrote asset")):
            self.upload()

    def test_duplicate_names_have_independent_storage(self):
        self.assertNotEqual(self.upload(), self.upload())
        self.assertEqual(len({entry["r2_prefix"] for entry in self.entries.values()}), 2)

    def test_existing_or_reserved_slug_does_not_upload(self):
        self.entries["slug:taken"] = {"slug": "taken"}
        for slug in ["taken", "api", "index.html"]:
            with self.subTest(slug=slug), self.assertRaises(bundle.BundleError):
                self.upload(slug=slug)
        self.assertEqual(self.s3.objects, {})

    def test_upload_failure_rolls_back_only_new_prefix(self):
        self.s3.objects["keep/me"] = b"untouched"
        self.s3.fail_upload = "index.html"
        with self.assertRaises(RuntimeError):
            self.upload()
        self.assertEqual(self.s3.objects, {"keep/me": b"untouched"})
        self.assertEqual(self.entries, {})

    def test_manifest_failure_rolls_back(self):
        self.s3.fail_manifest = True
        with self.assertRaises(RuntimeError):
            self.upload()
        self.assertEqual(self.s3.objects, {})
        self.assertEqual(self.entries, {})

    def test_ambiguous_kv_failure_retains_objects(self):
        def uncertain(*args):
            self.put(*args)
            raise TimeoutError("may have committed")
        with patch.object(share, "kv_put", side_effect=uncertain), self.assertRaises(bundle.BundleError):
            self.upload()
        self.assertEqual(len(self.s3.objects), 3)
        self.assertEqual(len(self.entries), 1)

    def test_clipboard_failure_does_not_fail_upload(self):
        with patch.object(bundle.shutil, "which", return_value="/usr/bin/pbcopy"):
            with patch.object(bundle.subprocess, "run", side_effect=OSError("no clipboard")):
                self.assertTrue(self.upload().startswith("https://share.example/"))

    def test_remove_by_url_leaves_local_files_and_other_uploads(self):
        url = self.upload()
        other = self.upload()
        self.events.clear()
        share.cmd_rm(argparse.Namespace(name=url))
        self.assertEqual(len(self.entries), 1)
        self.assertEqual(self.events[0][0], "delete")
        self.assertTrue(self.events[0][1].endswith(".manifest.json"))
        self.assertEqual(self.events[-1][0], "kv_delete")
        self.assertIn("slug:" + other.split("/")[-2], self.entries)
        self.assertTrue((self.root / "index.html").exists())

    def test_remove_is_paginated_and_scoped(self):
        self.upload(slug="many")
        prefix = self.entries["slug:many"]["r2_prefix"]
        self.s3.objects.update({prefix + str(i): b"x" for i in range(1005)})
        self.s3.objects["other/object"] = b"keep"
        share.cmd_rm(argparse.Namespace(name="many"))
        self.assertEqual(self.s3.objects, {"other/object": b"keep"})
        batches = [event[1] for event in self.events if event[0] == "batch"]
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(batch) <= 1000 for batch in batches))

    def test_failed_delete_preserves_kv_for_retry_but_revokes_manifest(self):
        self.upload(slug="retry")
        prefix = self.entries["slug:retry"]["r2_prefix"]
        self.s3.fail_delete = True
        with self.assertRaises(bundle.BundleError):
            share.cmd_rm(argparse.Namespace(name="retry"))
        self.assertIn("slug:retry", self.entries)
        self.assertNotIn(prefix + ".manifest.json", self.s3.objects)
        self.s3.fail_delete = False
        share.cmd_rm(argparse.Namespace(name="retry"))
        self.assertEqual(self.s3.objects, {})
        self.assertEqual(self.entries, {})

    def test_invalid_prefix_cannot_delete_bucket(self):
        for prefix in ["", "/", "__bundles__/", "__bundles__/../", "other/", "__bundles__/" + "a" * 32]:
            with self.subTest(prefix=prefix), self.assertRaises(bundle.BundleError):
                bundle.delete_prefix(self.s3, "test", prefix)
        self.assertEqual(self.events, [])

    def test_shared_bundle_prefix_is_kept(self):
        self.upload(slug="first")
        self.entries["slug:second"] = self.entries["slug:first"] | {"slug": "second"}
        before = dict(self.s3.objects)
        share.cmd_rm(argparse.Namespace(name="first"))
        self.assertEqual(self.s3.objects, before)
        self.assertNotIn("slug:first", self.entries)

    def test_url_resolution_rejects_other_hosts_and_asset_urls(self):
        self.assertEqual(bundle.slug_from_url("https://share.example/abc/?x=1", "https://share.example"), "abc")
        self.assertEqual(bundle.slug_from_url("https://share.example/base/abc", "https://share.example/base"), "abc")
        for url in ["https://other.example/abc/", "https://share.example/abc/a.html", "https://share.example/abc//", "abc", "https://share.example/%2e%2e/"]:
            with self.subTest(url=url):
                self.assertIsNone(bundle.slug_from_url(url, "https://share.example"))

    def test_legacy_shared_object_deletion_stays_safe(self):
        self.entries = {
            "slug:a": {"slug": "a", "name": "report", "r2_key": "legacy/report"},
            "slug:b": {"slug": "b", "name": "report", "r2_key": "legacy/report"},
        }
        self.s3.objects["legacy/report"] = b"keep"
        share.cmd_rm(argparse.Namespace(name="a"))
        self.assertEqual(self.s3.objects, {"legacy/report": b"keep"})
        self.assertNotIn("slug:a", self.entries)

    def test_link_deletion_unchanged(self):
        self.entries["slug:link"] = {"slug": "link", "type": "link", "url": "https://example.com"}
        share.cmd_rm(argparse.Namespace(name="link"))
        self.assertEqual(self.events, [("kv_delete", "slug:link")])

    def test_ls_includes_bundle_without_file_only_fields(self):
        self.upload(slug="listed")
        share.cmd_ls(argparse.Namespace())

    def test_main_dispatch_and_error(self):
        with patch("sys.argv", ["share", "bundle", str(self.root), "--dry-run"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                share.main()
        self.assertIn("include  index.html", output.getvalue())
        with patch("sys.argv", ["share", "bundle", str(self.root / "missing")]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                share.main()
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()

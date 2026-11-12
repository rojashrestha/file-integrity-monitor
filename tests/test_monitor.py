"""Automated tests: python -m unittest discover -s tests -v"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fim_monitor.cli import main
from fim_monitor.monitor import MonitorError, compare, scan, sha256_file


class FileIntegrityMonitorTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.base = Path(self.workspace.name)
        self.watched = self.base / "watched"
        self.watched.mkdir()
        self.baseline = self.base / "private" / "baseline.json"
        (self.watched / "hello.txt").write_text("hello", encoding="utf-8")

    def cli(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(list(arguments))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def init(self):
        return self.cli("init", str(self.watched), "--baseline", str(self.baseline))

    def check(self, *arguments):
        return self.cli("check", str(self.watched), "--baseline", str(self.baseline), *arguments)

    def test_hash_known_value(self):
        self.assertEqual(
            sha256_file(self.watched / "hello.txt"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )

    def test_init_creates_valid_json(self):
        code, output, error = self.init()
        self.assertEqual(code, 0, error)
        self.assertIn("Tracked 1 files", output)
        data = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("hello.txt", data["files"])

    def test_unchanged_files_exit_zero(self):
        self.init()
        code, output, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn("No changes detected", output)

    def test_changed_content_even_when_size_is_same(self):
        self.init()
        (self.watched / "hello.txt").write_text("HELLO", encoding="utf-8")
        code, output, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("[MODIFIED] hello.txt", output)

    def test_added_and_deleted_files(self):
        self.init()
        (self.watched / "hello.txt").unlink()
        (self.watched / "new.txt").write_text("new", encoding="utf-8")
        code, output, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("[DELETED] hello.txt", output)
        self.assertIn("[NEW] new.txt", output)

    def test_nested_folders(self):
        nested = self.watched / "one" / "two"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_text("before", encoding="utf-8")
        self.init()
        (nested / "deep.txt").write_text("after", encoding="utf-8")
        code, output, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("one/two/deep.txt", output)

    def test_machine_readable_json_report(self):
        self.init()
        (self.watched / "new.txt").write_text("new", encoding="utf-8")
        code, output, _ = self.check("--json")
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["added"], ["new.txt"])
        self.assertEqual(report["modified"], [])
        self.assertEqual(report["unchanged_count"], 1)

    def test_cannot_overwrite_without_force(self):
        self.init()
        first = self.baseline.read_text(encoding="utf-8")
        code, _, error = self.init()
        self.assertEqual(code, 2)
        self.assertIn("--force", error)
        self.assertEqual(first, self.baseline.read_text(encoding="utf-8"))
        code, _, _ = self.cli("init", str(self.watched), "--baseline", str(self.baseline), "--force")
        self.assertEqual(code, 0)

    def test_missing_baseline_is_error(self):
        code, _, error = self.check()
        self.assertEqual(code, 2)
        self.assertIn("Run 'init' first", error)

    def test_reject_baseline_inside_watched_folder(self):
        unsafe = self.watched / "baseline.json"
        code, _, error = self.cli("init", str(self.watched), "--baseline", str(unsafe))
        self.assertEqual(code, 2)
        self.assertIn("OUTSIDE", error)
        self.assertFalse(unsafe.exists())

    def test_reject_corrupt_baseline(self):
        self.baseline.parent.mkdir()
        self.baseline.write_text("{this is invalid", encoding="utf-8")
        code, _, error = self.check()
        self.assertEqual(code, 2)
        self.assertIn("Cannot read baseline", error)

    def test_reject_mismatched_root(self):
        self.init()
        other = self.base / "other"
        other.mkdir()
        code, _, error = self.cli("check", str(other), "--baseline", str(self.baseline))
        self.assertEqual(code, 2)
        self.assertIn("different directory", error)

    def test_symlinks_not_scanned(self):
        outside = self.base / "outside.txt"
        outside.write_text("external secret", encoding="utf-8")
        linked = self.watched / "linked.txt"
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks not supported on this platform/account")
        files = scan(self.watched)
        self.assertNotIn("linked.txt", files)
        self.assertEqual(list(files), ["hello.txt"])

    def test_compare_detects_added_deleted_modified(self):
        old = {"a": {"sha256": "1"}, "b": {"sha256": "2"}}
        new = {"a": {"sha256": "3"}, "c": {"sha256": "4"}}
        result = compare(old, new)
        self.assertEqual(result["added"], ["c"])
        self.assertEqual(result["deleted"], ["b"])
        self.assertEqual(result["modified"], ["a"])

    def test_invalid_directory(self):
        code, _, error = self.cli("init", str(self.base / "missing"), "--baseline", str(self.baseline))
        self.assertEqual(code, 2)
        self.assertIn("Not a directory", error)

    def test_watch_rejects_bad_interval(self):
        code, _, error = self.cli("watch", str(self.watched), "--baseline", str(self.baseline), "--interval", "-1")
        self.assertEqual(code, 2)
        self.assertIn("greater than zero", error)


if __name__ == "__main__":
    unittest.main()

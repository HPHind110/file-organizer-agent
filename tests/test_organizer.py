import json
from contextlib import closing
import _winapi
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from organizer import engine
from organizer.planner import PROJECT, STATE, build_plan
from organizer.windows import SafetyError, reader, rename_open

class AgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "inbox"
        self.root.mkdir()

    def put(self, name, data=b"hello"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def plan(self, **kwargs):
        return build_plan(self.root, **kwargs)

    def run_plan(self, plan=None):
        plan = plan or self.plan()
        return engine.apply(plan, plan.token)

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", str(PROJECT / "organize.py"), *map(str, args)],
                                input="", capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def test_name_signatures_categories_and_metadata(self):
        self.put("HÓA_ĐƠN tháng 11.pdf", b"%PDF-1.7")
        self.put("diagram", b"\x89PNG\r\n\x1a\n")
        self.put("backup.tar.gz")
        p = self.plan()
        self.assertEqual({i.target.split("/")[1] for i in p.moves}, {"Finance", "Images", "Archives"})
        snap = next(i.snapshot for i in p.moves if i.source == "backup.tar.gz")
        self.assertEqual(snap["size"], 5)
        self.assertEqual(snap["sha256"], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_review_unknown_conflicting_names_and_headers(self):
        for name in ("lecture_invoice.pdf", "thing.abc", "image.jpg"):
            self.put(name, b"%PDF-")
        self.assertEqual([i.status for i in self.plan().items], ["review"] * 3)

    def test_compound_office_container(self):
        self.put("report.docx", b"PK\x03\x04")
        self.assertEqual(self.plan().moves[0].target, "Organized/Documents/report.docx")

    def test_recursion_and_duplicate_destinations(self):
        self.put("a/File.txt")
        self.put("b/file.TXT", b"different")
        self.assertEqual(self.plan().items, [])
        p = self.plan(recursive=True)
        self.assertEqual(len(p.duplicates[0]), 2)
        self.assertEqual(len(p.moves), 0)
        self.assertTrue(all("same destination" in i.reason for i in p.items))

    def test_existing_destination_and_repeated_scan(self):
        self.put("Organized/Documents/report.txt", b"old")
        self.put("report.txt", b"new")
        p = self.plan(recursive=True)
        self.assertEqual(len(p.items), 1)
        self.assertIsNone(self.run_plan(p))
        self.assertEqual((self.root / p.items[0].target).read_bytes(), b"old")
        self.assertFalse((self.root / STATE).exists())

    def test_reserved_output_is_a_file(self):
        self.put("Organized")
        self.put("report.txt")
        self.assertFalse(self.plan().moves)

    def test_hidden_are_skipped(self):
        self.put(".notes/hidden.txt")
        self.put("~$report.docx")
        self.assertEqual([i.status for i in self.plan(recursive=True).items], ["skip", "skip"])

    def test_custom_config_and_invalid_folder_rule(self):
        config = self.base / "my.json"
        data = {"recursive": True, "categories": {"Coursework": [".hw"]}, "name_rules": []}
        config.write_text(json.dumps(data))
        self.put("week1/a.hw")
        self.assertEqual(self.plan(settings=config).moves[0].target, "Organized/Coursework/a.hw")
        data["categories"] = {"../outside": [".hw"]}
        config.write_text(json.dumps(data))
        with self.assertRaises(SafetyError):
            self.plan(settings=config)

    def test_preview_history_and_dry_run_do_not_write(self):
        path = self.put("note.txt")
        before = (path.stat().st_mtime_ns, self.root.stat().st_mtime_ns)
        a = json.loads(self.cli("plan", self.root, "--json").stdout)
        b = json.loads(self.cli("apply", self.root, "--dry-run", "--json").stdout)
        self.assertEqual(a, b)
        self.assertEqual(json.loads(self.cli("history", self.root, "--json").stdout), [])
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertEqual(before, (path.stat().st_mtime_ns, self.root.stat().st_mtime_ns))

    def test_unapproved_and_outdated_cli_plans_do_nothing(self):
        path = self.put("note.txt")
        p = self.plan()
        path.write_bytes(b"altered")
        self.cli("apply", self.root, "--approve", p.token, code=1)
        self.cli("apply", self.root, code=1)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_stale_same_length_and_timestamp_detected(self):
        path = self.put("a.txt")
        self.put("b.txt")
        p = self.plan()
        s = path.stat()
        path.write_bytes(b"HELLO")
        os.utime(path, ns=(s.st_atime_ns, s.st_mtime_ns))
        result = self.run_plan(p)
        self.assertEqual([r["state"] for r in result["moves"]], ["skipped", "moved"])
        self.assertEqual(path.read_bytes(), b"HELLO")

    def test_locked_and_missing_files_do_not_abort_other_moves(self):
        first = self.put("a.txt")
        missing = self.put("b.txt")
        self.put("c.txt")
        p = self.plan()
        missing.unlink()
        with reader(first, exclusive=True):
            self.assertEqual(self.plan().items[0].status, "review")
            r = self.run_plan(p)
        self.assertEqual([e["state"] for e in r["moves"]], ["skipped", "skipped", "moved"])

    def test_late_destination_is_never_replaced(self):
        self.put("a.txt")
        p = self.plan()
        # Inject the conflict after the last application-level existence check.
        def race(stream, target):
            target.write_bytes(b"someone else's file")
            rename_open(stream, target)
        with patch.object(engine, "rename_open", race):
            result = self.run_plan(p)
        self.assertTrue(result["moves"][0]["detail"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"hello")
        self.assertEqual((self.root / p.moves[0].target).read_bytes(), b"someone else's file")
        result = engine.rollback(self.root, result["id"])
        self.assertTrue(result["moves"][0]["detail"])

    def test_cli_run_history_preview_and_rollback(self):
        path = self.put("Toán Tin/week1.pdf", b"%PDF-sample")
        meta = path.stat()
        p = self.plan(recursive=True)
        result = json.loads(self.cli("apply", self.root, "--recursive", "--approve", p.token, "--json").stdout)
        r = result["run"]
        self.assertEqual(r["moves"][0]["state"], "moved")
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(self.cli("history", self.root, "--json").stdout)[0]["id"], r["id"])
        self.assertEqual(json.loads(self.cli("history", self.root, r["id"], "--json").stdout), r)
        self.cli("rollback", self.root, r["id"], "--dry-run")
        self.assertFalse(path.exists())
        self.cli("rollback", self.root, r["id"], code=1)
        self.cli("rollback", self.root, r["id"], "--yes")
        self.assertEqual(path.read_bytes(), b"%PDF-sample")
        self.assertEqual((meta.st_ino, meta.st_mtime_ns), (path.stat().st_ino, path.stat().st_mtime_ns))
        self.assertEqual(engine.rollback(self.root, r["id"])["moves"][0]["state"], "restored")

    def test_rollback_blocked_then_retry(self):
        original = self.put("a.txt")
        r = self.run_plan()
        original.write_bytes(b"occupied")
        self.cli("rollback", self.root, r["id"], "--yes", code=2)
        self.assertEqual(original.read_bytes(), b"occupied")
        original.rename(self.root / "other.txt")
        self.assertEqual(engine.rollback(self.root, r["id"])["moves"][0]["state"], "restored")

    def test_rollback_leaves_edited_destination_alone(self):
        original = self.put("a.txt")
        r = self.run_plan()
        dest = self.root / r["moves"][0]["dst"]
        dest.write_bytes(b"edited")
        self.assertTrue(engine.rollback(self.root, r["id"])["moves"][0]["detail"])
        self.assertFalse(original.exists())
        self.assertEqual(dest.read_bytes(), b"edited")

    def test_unrecognized_state_directory_preserved(self):
        file = self.put(STATE + "/owner", b"personal data")
        self.put("a.txt")
        with self.assertRaises(SafetyError):
            self.run_plan()
        self.assertEqual(file.read_bytes(), b"personal data")
        self.assertEqual([p.name for p in file.parent.iterdir()], ["owner"])

    def test_parallel_writer_is_refused(self):
        self.put("a.txt")
        with engine.journal(self.root, create=True):
            with self.assertRaises(OSError):
                self.run_plan()
        self.assertTrue((self.root / "a.txt").exists())

    def test_junctions_do_not_escape_root(self):
        outside = self.base / "other"
        outside.mkdir()
        file = outside / "private.txt"
        file.write_bytes(b"private")
        def junction(at):
            try:
                _winapi.CreateJunction(str(outside), str(at))
            except OSError as exc:
                if exc.winerror in (5, 1314):
                    self.skipTest("Windows has denied creation of a temporary junction.")
                raise
        junction(self.root / "linked")
        self.put("note.txt")
        p = self.plan(recursive=True)
        self.assertEqual([i.status for i in p.items], ["skip", "move"])
        junction(self.root / "Organized")
        self.assertEqual(self.run_plan(p)["moves"][0]["state"], "skipped")
        self.assertEqual(list(outside.iterdir()), [file])
        with self.assertRaises(SafetyError):
            build_plan(self.root / "linked")

    def test_existing_windows_junction_is_rejected_read_only(self):
        # Windows normally provides this compatibility junction. Never traverse it.
        path = Path(os.environ["SystemDrive"] + "/Documents and Settings")
        if not path.is_junction():
            self.skipTest("Windows compatibility junction unavailable.")
        with self.assertRaises((SafetyError, PermissionError)):
            build_plan(path)

    def test_late_reparse_flag_stops_moves_before_any_child_is_created(self):
        self.put("a.txt")
        p = self.plan()
        output = self.root / "Organized"
        output.mkdir()
        real_lstat = Path.lstat
        def flagged(path):
            info = real_lstat(path)
            if path == output:
                from types import SimpleNamespace
                return SimpleNamespace(st_file_attributes=info.st_file_attributes | stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, "lstat", flagged):
            row = self.run_plan(p)["moves"][0]
        self.assertEqual(row["state"], "skipped")
        self.assertEqual(list(output.iterdir()), [])
        self.assertTrue((self.root / "a.txt").exists())

    def test_replaced_selected_directory_does_not_get_a_journal(self):
        self.put("a.txt")
        p = self.plan()
        self.root.rename(self.base / "old")
        self.root.mkdir()
        with self.assertRaises(SafetyError):
            self.run_plan(p)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_history_paths_cannot_escape_root(self):
        self.put("a.txt")
        r = self.run_plan()
        with closing(sqlite3.connect(self.root / STATE / engine.DB)) as con, con:
            con.execute("UPDATE moves SET src='../outside.txt'")
        undo = engine.rollback(self.root, r["id"])
        self.assertTrue(undo["moves"][0]["detail"])
        self.assertFalse((self.base / "outside.txt").exists())

    def test_moved_history_cannot_undo_in_another_folder(self):
        self.put("a.txt")
        r = self.run_plan()
        second = self.base / "second"
        shutil.copytree(self.root, second)
        with self.assertRaises(SafetyError):
            engine.rollback(second, r["id"])

    def test_machine_exit_after_move_or_undo(self):
        original = self.put("a.txt")
        self.put("b.txt")
        # Terminate the child after the kernel rename and before status commit.
        program = """
import os, sys
from organizer import engine
from organizer.planner import build_plan
from organizer.windows import rename_open
def stop(stream, dest):
    rename_open(stream, dest)
    os._exit(71)
engine.rename_open = stop
if len(sys.argv) == 2:
    p = build_plan(sys.argv[1])
    engine.apply(p, p.token)
else:
    engine.rollback(sys.argv[1], sys.argv[2])
"""
        def crash(*extra):
            child = subprocess.run([sys.executable, "-B", "-c", program, self.root, *extra], cwd=PROJECT)
            self.assertEqual(child.returncode, 71)
        crash()
        r = engine.inspect_history(self.root)[0]
        self.assertEqual(r["counts"], {"moving": 1, "planned": 1})
        self.assertFalse(original.exists())
        crash(r["id"])
        self.assertEqual(engine.inspect_history(self.root, r["id"])["moves"][0]["state"], "undoing")
        done = engine.rollback(self.root, r["id"])
        self.assertEqual([m["state"] for m in done["moves"]], ["restored", "skipped"])
        self.assertEqual(original.read_bytes(), b"hello")

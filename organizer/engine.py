"""Durable, per-folder move journal and guarded Windows execution."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import uuid

from .planner import OUTPUT, PROJECT, STATE
from .windows import (SafetyError, component, fingerprint, pin_directory, pin_tree,
                      plain, reader, rename_open, select_root)

MARK = b"File Organizer private state v1\n"
DB = "history.sqlite3"

class RenameRefused(SafetyError):
    """The kernel rejected the rename before changing either path."""

SCHEMA = """
CREATE TABLE runs(id TEXT PRIMARY KEY, started TEXT NOT NULL, token TEXT NOT NULL,
                  root_identity TEXT NOT NULL);
CREATE TABLE moves(run TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
                   src TEXT NOT NULL, dst TEXT NOT NULL, snapshot TEXT NOT NULL,
                   state TEXT NOT NULL DEFAULT 'planned', detail TEXT NOT NULL DEFAULT '',
                   PRIMARY KEY(run,seq));
PRAGMA user_version=1;
"""

def identity(root):
    info = root.stat()
    return (info.st_dev, info.st_ino)

@contextmanager
def journal(root, *, create=False, readonly=False, expected_identity=None):
    folder = root / STATE
    with pin_tree(root):
        if expected_identity is not None and identity(root) != expected_identity:
            raise SafetyError("Selected directory changed since planning.")
        new_folder = False
        if create:
            try:
                folder.mkdir()
                new_folder = True
            except FileExistsError:
                pass
        elif not os.path.lexists(folder):
            yield None
            return
        with pin_directory(folder):
            marker = folder / "owner"
            if new_folder:
                with marker.open("xb") as out:
                    out.write(MARK)
                    out.flush()
                    os.fsync(out.fileno())
            if not os.path.lexists(marker):
                raise SafetyError(f"Reserved state directory has no owner marker: {folder}")
            plain(marker, directory=False)
            with reader(marker, exclusive=not readonly) as guard:
                if guard.read(len(MARK) + 1) != MARK:
                    raise SafetyError("Unrecognized state directory.")
                database = folder / DB
                # Reject aliases and unrecognized files rather than treating an
                # existing user folder as writable SQLite storage.
                for path in folder.iterdir():
                    if path.name not in {"owner", DB, DB + "-journal"}:
                        raise SafetyError(f"Unexpected entry in state directory: {path.name}")
                    if plain(path, directory=False).st_nlink != 1:
                        raise SafetyError("Linked state file.")
                if new_folder:
                    with database.open("xb"):
                        pass
                mode = "ro" if readonly else "rw"
                con = sqlite3.connect(database.as_uri() + "?mode=" + mode, uri=True)
                try:
                    con.row_factory = sqlite3.Row
                    con.execute("PRAGMA trusted_schema=OFF")
                    con.execute("PRAGMA temp_store=MEMORY")
                    if new_folder:
                        con.executescript(SCHEMA)
                    if con.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise SafetyError("Unrecognized history format.")
                    yield con
                finally:
                    con.close()

def validated_paths(root, src, dst):
    parts = []
    for value in (src, dst):
        if not isinstance(value, str) or PurePosixPath(value).as_posix() != value:
            raise SafetyError("Invalid recorded path.")
        names = value.split("/")
        for name in names:
            component(name)
        parts.append(names)
    a, b = parts
    if (a[0].casefold() in (STATE, OUTPUT.casefold()) or len(b) != 3
            or b[0] != OUTPUT or b[-1] != a[-1]):
        raise SafetyError("Recorded paths violate the organization layout.")
    source, target = root.joinpath(*a), root.joinpath(*b)
    if any(p == PROJECT or PROJECT in p.parents for p in (source, target)):
        raise SafetyError("Application directory is protected.")
    return source, target

def status(con, run, seq, state, detail=""):
    with con:
        con.execute("UPDATE moves SET state=?, detail=? WHERE run=? AND seq=?",
                    (state, detail, run, seq))

def transfer(root, source, target, snapshot, before, *, make_dirs):
    with ExitStack() as stack:
        for folder in (source.parent, target.parent):
            current = root
            for part in folder.relative_to(root).parts:
                current /= part
                if make_dirs and folder == target.parent:
                    current.mkdir(exist_ok=True)
                stack.enter_context(pin_directory(current))
        with reader(source, moving=True) as stream:
            observed, _ = fingerprint(stream)
            if observed != snapshot:
                raise SafetyError("File identity, modification time, size or contents changed.")
            if os.path.lexists(target):
                raise SafetyError("Destination is occupied.")
            before()  # Commit intent before asking the filesystem to rename.
            try:
                rename_open(stream, target)
            except OSError as exc:
                raise RenameRefused(str(exc)) from exc
            info = plain(target, directory=False)
            if (info.st_dev, info.st_ino, info.st_size) != (
                    observed["device"], observed["inode"], observed["size"]):
                raise SafetyError("Post-move identity check failed. Inspect both paths.")

def get_run(con, run):
    record = con.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone() if con else None
    if not record:
        raise SafetyError(f"Unknown run {run}.")
    return record

def rows(con, run):
    return [dict(row) for row in con.execute(
        "SELECT seq,src,dst,snapshot,state,detail FROM moves WHERE run=? ORDER BY seq", (run,))]

def report(con, run):
    header = dict(get_run(con, run))
    header["moves"] = [{k: v for k, v in row.items() if k != "snapshot"} for row in rows(con, run)]
    return header

def apply(plan, approval):
    if approval != plan.token:
        raise SafetyError("Approval does not match this plan. Preview again.")
    if not plan.moves:
        return None
    root = plan.root
    with journal(root, create=True, expected_identity=plan.identity) as con:
        run = uuid.uuid4().hex
        with con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?)",
                        (run, datetime.now(timezone.utc).isoformat(), approval, json.dumps(plan.identity)))
            con.executemany("INSERT INTO moves(run,seq,src,dst,snapshot) VALUES(?,?,?,?,?)",
                            [(run, n, i.source, i.target, json.dumps(i.snapshot))
                             for n, i in enumerate(plan.moves, 1)])
        for row in rows(con, run):
            seq = row["seq"]
            try:
                source, target = validated_paths(root, row["src"], row["dst"])
                transfer(root, source, target, json.loads(row["snapshot"]),
                         lambda: status(con, run, seq, "moving"), make_dirs=True)
            except RenameRefused as exc:
                status(con, run, seq, "skipped", str(exc))
            except (OSError, SafetyError) as exc:
                # An error after intent might be a verification failure following
                # a successful rename. Preserve uncertainty for rollback.
                with con:
                    con.execute("""UPDATE moves SET detail=?, state=CASE state
                        WHEN 'planned' THEN 'skipped' ELSE state END WHERE run=? AND seq=?""",
                                (str(exc), run, seq))
            else:
                status(con, run, seq, "moved")
        return report(con, run)

def matches(root, path, snapshot):
    with pin_tree(path.parent):
        with reader(path) as stream:
            return fingerprint(stream)[0] == snapshot

def inspect_history(root, run=None):
    root = select_root(root)
    with journal(root, readonly=True) as con:
        if run is not None:
            return report(con, run)
        if con is None:
            return []
        result = []
        for r in con.execute("SELECT * FROM runs ORDER BY started DESC"):
            counts = dict(con.execute("SELECT state,COUNT(*) FROM moves WHERE run=? GROUP BY state",
                                      (r["id"],)))
            result.append(dict(r) | {"counts": counts})
        return result

def rollback(root, run):
    root = select_root(root)
    with journal(root) as con:
        header = get_run(con, run)
        if list(identity(root)) != json.loads(header["root_identity"]):
            raise SafetyError("This history belongs to a different directory.")
        for row in reversed(rows(con, run)):
            seq, state = row["seq"], row["state"]
            if state in {"restored", "skipped"}:
                continue
            if state == "planned":
                status(con, run, seq, "skipped", "Interrupted before move intent.")
                continue
            if state not in {"moving", "moved", "undoing"}:
                raise SafetyError("Unknown journal state.")
            try:
                original, organized = validated_paths(root, row["src"], row["dst"])
                snapshot = json.loads(row["snapshot"])
                with pin_tree(original.parent), pin_tree(organized.parent):
                    if state in {"moving", "undoing"} and not os.path.lexists(organized):
                        if matches(root, original, snapshot):
                            end = "restored" if state == "undoing" else "skipped"
                            status(con, run, seq, end, "Reconciled interrupted operation.")
                            continue
                    transfer(root, organized, original, snapshot,
                             lambda: status(con, run, seq, "undoing"), make_dirs=False)
            except (SafetyError, OSError) as exc:
                with con:
                    con.execute("UPDATE moves SET detail=? WHERE run=? AND seq=?",
                                (str(exc), run, seq))
            else:
                status(con, run, seq, "restored")
        return report(con, run)

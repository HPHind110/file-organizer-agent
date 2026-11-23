"""Read-only observation, classification and deterministic planning."""
from collections import defaultdict
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from .windows import (SafetyError, component, fingerprint, pin_directory,
                      pin_tree, plain, reader, select_root)

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = "Organized"
STATE = ".file-organizer"
PROTECTED = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
EXT = re.compile(r"\.[a-z0-9]+(?:\.[a-z0-9]+)*")

def words(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value).casefold()))

def load_rules(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"recursive", "categories", "name_rules"}:
        raise SafetyError("Settings require recursive, categories and name_rules.")
    if type(data["recursive"]) is not bool or not isinstance(data["categories"], dict):
        raise SafetyError("Invalid recursive flag or categories.")
    index, labels = {}, {}
    def category(name):
        if not isinstance(name, str) or name.startswith("."):
            raise SafetyError("Invalid category name.")
        component(name)
        if labels.setdefault(name.casefold(), name) != name:
            raise SafetyError(f"Categories differ only in case: {name}")
        return name
    def extensions(values):
        if not isinstance(values, list) or not values or not all(
                isinstance(v, str) and EXT.fullmatch(v) for v in values):
            raise SafetyError("Extensions must be lowercase strings such as .pdf.")
        return values
    for name, exts in data["categories"].items():
        category(name)
        for ext in extensions(exts):
            if ext in index:
                raise SafetyError(f"Repeated extension: {ext}")
            index[ext] = name
    rules = data["name_rules"]
    if not isinstance(rules, list):
        raise SafetyError("name_rules must be a list.")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"category", "extensions", "tokens"}:
            raise SafetyError("Invalid name rule.")
        category(rule["category"])
        extensions(rule["extensions"])
        if not isinstance(rule["tokens"], list) or not rule["tokens"] or not all(
                isinstance(t, str) and words(t) for t in rule["tokens"]):
            raise SafetyError("Name rules require non-empty word tokens.")
    return data["recursive"], index, rules

# Several office formats have a ZIP container. The header cannot distinguish them.
SIGNATURES = [(b"%PDF-", {".pdf"}), (b"\x89PNG\r\n\x1a\n", {".png"}),
              (b"\xff\xd8\xff", {".jpg", ".jpeg"}), (b"GIF87a", {".gif"}),
              (b"GIF89a", {".gif"}), (b"PK\x03\x04", {".zip", ".docx", ".xlsx",
                ".pptx", ".odt", ".ods", ".odp", ".epub", ".jar", ".npz"})]

def classify(name, head, index, rules):
    suffixes = Path(name.casefold()).suffixes
    ext = next(("".join(suffixes[i:]) for i in range(len(suffixes))
                if "".join(suffixes[i:]) in index), Path(name.casefold()).suffix)
    evidence = next((kinds for prefix, kinds in SIGNATURES if head.startswith(prefix)), None)
    if evidence and ext and ext not in evidence:
        return None, f"{ext} conflicts with header ({', '.join(sorted(evidence))})"
    if not ext and evidence:
        groups = {index.get(kind) for kind in evidence}
        if len(groups) == 1 and None not in groups:
            return groups.pop(), "recognized content header, no extension"
    base = " " + words(name[:-len(ext)] if ext else name) + " "
    matches = {}
    for rule in rules:
        if ext in rule["extensions"]:
            for token in rule["tokens"]:
                if f" {words(token)} " in base:
                    matches[rule["category"]] = token
    if len(matches) > 1:
        return None, "conflicting name rules: " + ", ".join(sorted(matches))
    if matches:
        label, token = next(iter(matches.items()))
        return label, f"filename contains {token!r} and extension is {ext}"
    if ext in index:
        return index[ext], f"extension {ext}" + (" agrees with header" if evidence else "")
    return None, f"unmapped extension {ext or '(none)'}"

@dataclass
class Item:
    source: str
    status: str
    reason: str
    target: str | None = None
    snapshot: dict | None = None

@dataclass
class Plan:
    root: Path
    identity: tuple
    items: list
    duplicates: list

    def payload(self):
        return {"root": str(self.root), "identity": self.identity,
                "items": [asdict(i) for i in self.items], "duplicates": self.duplicates}

    @property
    def token(self):
        encoded = json.dumps(self.payload(), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    @property
    def moves(self):
        return [i for i in self.items if i.status == "move"]

def build_plan(root, settings=PROJECT / "settings.json", recursive=None):
    root = select_root(root)
    if root == PROJECT or PROJECT in root.parents:
        raise SafetyError("Select a data folder outside the application.")
    default, index, rules = load_rules(settings)
    recurse = default if recursive is None else recursive
    items, names, targets = [], defaultdict(list), defaultdict(list)

    def visit(folder):
        with pin_directory(folder):
            for path in sorted(folder.iterdir(), key=lambda p: (p.name.casefold(), p.name)):
                rel = path.relative_to(root).as_posix()
                if folder == root and path.name.casefold() in {OUTPUT.casefold(), STATE}:
                    continue
                if path == PROJECT:
                    continue
                try:
                    component(path.name)
                    info = path.lstat()
                    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        items.append(Item(rel, "skip", "link or reparse point"))
                    elif path.name.startswith((".", "~$")) or info.st_file_attributes & PROTECTED:
                        items.append(Item(rel, "skip", "hidden, system or temporary entry"))
                    elif stat.S_ISDIR(info.st_mode):
                        if recurse:
                            visit(path)
                    elif stat.S_ISREG(info.st_mode):
                        names[path.name.casefold()].append(rel)
                        with reader(path) as stream:
                            snapshot, head = fingerprint(stream)
                        label, why = classify(path.name, head, index, rules)
                        dest = (Path(OUTPUT) / label / path.name).as_posix() if label else None
                        item = Item(rel, "move" if dest else "review", why, dest, snapshot)
                        items.append(item)
                        if dest:
                            targets[dest.casefold()].append(item)
                    else:
                        items.append(Item(rel, "skip", "special filesystem entry"))
                except (OSError, SafetyError) as exc:
                    items.append(Item(rel, "review", str(exc)))

    with pin_tree(root):
        root_info = root.stat()
        visit(root)
        for group in targets.values():
            for item in group:
                dest = root / item.target
                try:
                    for folder in (root / OUTPUT, dest.parent):
                        if os.path.lexists(folder):
                            plain(folder, directory=True)
                    if len(group) > 1:
                        raise SafetyError("multiple sources propose the same destination")
                    if os.path.lexists(dest):
                        raise SafetyError("destination already exists")
                except (SafetyError, OSError) as exc:
                    item.status, item.reason = "review", str(exc)
    return Plan(root, (root_info.st_dev, root_info.st_ino), items,
                [g for g in names.values() if len(g) > 1])

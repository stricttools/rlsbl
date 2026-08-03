#!/usr/bin/env python3
"""Route direct stdlib effect calls in rlsbl/ through rlsbl.effects.

One-shot codemod for the effect-chokepoint convergence (Phase 6.1).  It walks
the production package with ``ast``, rewrites each banned call's *callee*
in place (arguments are untouched, so behavior is preserved exactly), and
inserts the ``effects`` import where a file gained its first call.

Usage:
    python3 scripts/route_effects_calls.py --surface process [--dry-run] [PATH ...]
    python3 scripts/route_effects_calls.py --surface fs
    python3 scripts/route_effects_calls.py --surface net

Surfaces are separate so each one can land as its own reviewable commit.
Files that need a hand-written treatment (late-bound imports, aliasing) are
listed in SKIP_FILES and must be converted manually.
"""

import argparse
import ast
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RLSBL_DIR = os.path.join(REPO_ROOT, "rlsbl")

# (module, attr) -> effects function name
PROCESS_MAP = {
    ("subprocess", "run"): "run",
}
FS_MAP = {
    ("os", "replace"): "replace",
    ("os", "rename"): "rename",
    ("os", "makedirs"): "makedirs",
    ("os", "mkdir"): "mkdir",
    ("os", "unlink"): "remove",
    ("os", "remove"): "remove",
    ("os", "rmdir"): "rmdir",
    ("os", "removedirs"): "removedirs",
    ("os", "chmod"): "chmod",
    ("shutil", "rmtree"): "rmtree",
    ("shutil", "copy2"): "copy_file",
    ("shutil", "copytree"): "copytree",
}
NET_MAP = {
    ("request", "urlopen"): "urlopen",
    ("urllib", "urlopen"): "urlopen",
}

SURFACES = {"process": PROCESS_MAP, "fs": FS_MAP, "net": NET_MAP}

# Hand-converted: these late-bind the stdlib module through a package
# namespace so tests can patch it, which the mechanical rewrite would break.
SKIP_FILES = {
    "rlsbl/effects.py",
    "rlsbl/commands/release/hooks.py",
    "rlsbl/commands/release/validate.py",
}

WRITE_MODE_CHARS = ("w", "a", "x")


def production_files(paths):
    if paths:
        for p in paths:
            yield os.path.abspath(p)
        return
    for dirpath, dirnames, filenames in os.walk(RLSBL_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("templates", "__pycache__")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def module_aliases(tree):
    """Map local names to the stdlib module they stand for."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                leaf = alias.name.split(".")[-1]
                root = alias.name.split(".")[0]
                aliases[alias.asname or root] = leaf if alias.asname else root
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def collect_edits(tree, mapping, include_open):
    """Return [(lineno, col, end_lineno, end_col, new_text)] for one file."""
    aliases = module_aliases(tree)
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name):
                base_name = aliases.get(base.id, base.id)
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            else:
                continue
            target = mapping.get((base_name, func.attr))
            if target:
                edits.append(
                    (func.lineno, func.col_offset, func.end_lineno,
                     func.end_col_offset, f"effects.{target}")
                )
        elif include_open and isinstance(func, ast.Name) and func.id == "open":
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if isinstance(mode, str) and any(c in mode for c in WRITE_MODE_CHARS):
                edits.append(
                    (func.lineno, func.col_offset, func.end_lineno,
                     func.end_col_offset, "effects.open_write")
                )
    return edits


def apply_edits(lines, edits):
    """Apply callee replacements back-to-front so offsets stay valid."""
    for lineno, col, end_lineno, end_col, new_text in sorted(edits, reverse=True):
        if lineno != end_lineno:
            raise RuntimeError(f"multi-line callee at line {lineno}")
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:col] + new_text + line[end_col:]
    return lines


def import_statement(path):
    """Build the relative ``from ... import effects`` line for *path*."""
    rel = os.path.relpath(path, RLSBL_DIR)
    # Package nesting below rlsbl/.  A package's __init__.py resolves relative
    # imports against that same package, so it needs no separate adjustment.
    depth = len(rel.split(os.sep)) - 1
    return "from " + "." * (depth + 1) + " import effects"


def insert_import(lines, statement):
    """Insert *statement* after the file's existing import block."""
    if any(line.strip() == statement for line in lines):
        return lines
    tree = ast.parse("".join(lines))
    last = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node
    if last is None:
        # No module-level imports (rare): put it after the docstring.
        idx = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
            getattr(tree.body[0], "value", None), ast.Constant
        ):
            idx = tree.body[0].end_lineno
        lines.insert(idx, statement + "\n")
        return lines
    lines.insert(last.end_lineno, statement + "\n")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--surface", required=True, choices=sorted(SURFACES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    mapping = SURFACES[args.surface]
    include_open = args.surface == "fs"
    total = 0
    touched = 0

    for path in production_files(args.paths):
        rel = os.path.relpath(path, REPO_ROOT)
        if rel in SKIP_FILES:
            continue
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
        edits = collect_edits(tree, mapping, include_open)
        if not edits:
            continue
        lines = source.splitlines(keepends=True)
        lines = apply_edits(lines, edits)
        lines = insert_import(lines, import_statement(path))
        new_source = "".join(lines)
        ast.parse(new_source, filename=path)  # syntax guard
        total += len(edits)
        touched += 1
        print(f"{rel}: {len(edits)} call(s)")
        if not args.dry_run:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_source)

    print(f"\n{args.surface}: {total} call(s) in {touched} file(s)"
          f"{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

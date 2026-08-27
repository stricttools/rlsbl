#!/usr/bin/env python3
"""AST sweep: find target-name string literals used in feature-support decisions.

The target registry (``rlsbl.targets.TARGETS``) is the single authority for
what a release target supports.  Every place outside the targets package that
tests a target NAME to decide behaviour ("if the target is npm, run npm
test") is a duplicate of that authority and drifts from it.

This script enumerates those places.  It is AST-based, not grep-based: a bare
occurrence of the string ``"go"`` in a docstring or an error message is not a
finding, while ``if name == "go":`` is.

Finding categories:

``compare``
    A comparison (``==``, ``!=``, ``in``, ``not in``) with a target-name
    literal on either side, or inside the literal collection of a membership
    test.  This is the classic hand-rolled dispatch.

``dispatch_key``
    A dict/set/list/tuple display whose elements are *predominantly* target
    names (see ``NAME_DENSITY``), i.e. a hand-listed aggregate that should
    have been derived by iterating the registry.

``subscript``
    ``SOMETHING["npm"]`` -- a direct index by a target-name literal.

``match_case``
    A ``match`` statement case pattern that is a target-name literal.

Known blind spots (shared with the permanent guard in
``tests/test_target_literal_guard.py``):

- Indirection: a name stored in a module constant and compared later
  (``NPM = "npm"`` ... ``if n == NPM``) is invisible.
- f-strings and computed names (``f"{lang}"``) are invisible.
- Non-Python surfaces: JSON config, YAML workflow templates, and Jinja-ish
  scaffold templates are not scanned.
- A dict display below the density threshold whose keys happen to be target
  names is reported as ``compare``-free and may be missed.

Usage::

    python3 scripts/sweep_target_name_literals.py            # human table
    python3 scripts/sweep_target_name_literals.py --json     # machine readable
    python3 scripts/sweep_target_name_literals.py --markdown # work-list body

Exit status is 0 always: this is an inventory tool, not a check.  The
permanent enforcement is ``tests/test_target_literal_guard.py``.
"""

import argparse
import ast
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from rlsbl.targets import TARGETS  # noqa: E402

TARGET_NAMES = frozenset(TARGETS.keys())

# A collection display counts as a hand-listed target aggregate when at least
# this fraction of its string elements are target names AND at least two are.
NAME_DENSITY = 0.5


def _is_target_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in TARGET_NAMES


def _string_elements(node):
    """Return the string constants of a collection display, or None."""
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return None
    out = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            return None
    return out


def _dict_string_keys(node):
    if not isinstance(node, ast.Dict):
        return None
    out = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(k.value)
        else:
            return None
    return out


def _dense(names):
    hits = [n for n in names if n in TARGET_NAMES]
    if len(hits) < 2:
        return False
    return len(hits) / len(names) >= NAME_DENSITY


class Sweeper(ast.NodeVisitor):
    def __init__(self, relpath, source):
        self.relpath = relpath
        self.lines = source.splitlines()
        self.findings = []
        self._reported = set()

    def _add(self, node, category, names, note=""):
        key = (node.lineno, node.col_offset, category)
        if key in self._reported:
            return
        self._reported.add(key)
        line = self.lines[node.lineno - 1].strip() if node.lineno - 1 < len(self.lines) else ""
        self.findings.append(
            {
                "file": self.relpath,
                "line": node.lineno,
                "category": category,
                "names": sorted(set(names)),
                "source": line,
                "note": note,
            }
        )

    def visit_Compare(self, node):
        found = []
        if _is_target_literal(node.left):
            found.append(node.left.value)
        for comparator in node.comparators:
            if _is_target_literal(comparator):
                found.append(comparator.value)
            elts = _string_elements(comparator)
            if elts:
                found.extend(n for n in elts if n in TARGET_NAMES)
            keys = _dict_string_keys(comparator)
            if keys:
                found.extend(n for n in keys if n in TARGET_NAMES)
        if found:
            self._add(node, "compare", found)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if _is_target_literal(node.slice):
            self._add(node, "subscript", [node.slice.value])
        self.generic_visit(node)

    def visit_Dict(self, node):
        keys = _dict_string_keys(node)
        if keys and _dense(keys):
            self._add(node, "dispatch_key", [k for k in keys if k in TARGET_NAMES],
                      note="dict display keyed by target names")
        self.generic_visit(node)

    def _collection(self, node):
        elts = _string_elements(node)
        if elts and _dense(elts):
            self._add(node, "dispatch_key", [e for e in elts if e in TARGET_NAMES],
                      note=f"{type(node).__name__.lower()} display of target names")
        self.generic_visit(node)

    visit_Set = _collection
    visit_List = _collection
    visit_Tuple = _collection

    def visit_match_case(self, node):
        for sub in ast.walk(node.pattern):
            if isinstance(sub, ast.MatchValue) and _is_target_literal(sub.value):
                self._add(node, "match_case", [sub.value.value])
        self.generic_visit(node)


def sweep(root):
    """Return every finding under *root*, sorted by file then line."""
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git"}]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, REPO_ROOT)
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError as e:
                print(f"warning: could not parse {rel}: {e}", file=sys.stderr)
                continue
            sw = Sweeper(rel, source)
            sw.visit(tree)
            findings.extend(sw.findings)
    findings.sort(key=lambda f: (f["file"], f["line"]))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.join(REPO_ROOT, "rlsbl"))
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--expect", type=int, default=None,
                    help="assert this many findings; exit 2 on mismatch")
    args = ap.parse_args()

    findings = sweep(args.root)

    if args.as_json:
        print(json.dumps(findings, indent=2))
    elif args.markdown:
        by_file = {}
        for f in findings:
            by_file.setdefault(f["file"], []).append(f)
        for path, group in by_file.items():
            print(f"\n### `{path}`\n")
            print("| Line | Kind | Names | Source |")
            print("| --- | --- | --- | --- |")
            for f in group:
                src = f["source"].replace("|", "\\|")
                print(f"| {f['line']} | {f['category']} | {', '.join(f['names'])} | `{src}` |")
    else:
        for f in findings:
            print(f"{f['file']}:{f['line']}  [{f['category']}]  {','.join(f['names'])}")
            print(f"    {f['source']}")
        print(f"\ntotal findings: {len(findings)}", file=sys.stderr)

    if args.expect is not None and len(findings) != args.expect:
        print(f"expected {args.expect} findings, got {len(findings)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

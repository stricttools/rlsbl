#!/usr/bin/env python3
"""Restore frontmatter descriptions on selfdoc-generated pages.

selfdoc 0.4.5 has two regressions that this script works around:

1. Module pages (docs/rlsbl-*.md) get a generic
   "API reference for X -- auto-generated documentation covering public
   functions, classes, and type signatures." description that overwrites
   the bespoke description sourced from the module-level docstring. This
   script restores the source module's first docstring line as the page
   description.

2. CLI pages (docs/cli-*.md) get a description computed as chelp[:155],
   then wrapped in double quotes. selfdoc's own frontmatter parser keeps
   the surrounding quotes when measuring length, so descriptions that
   were exactly 155 chars parse as 157 chars and trip SEO010. This script
   trims any over-length CLI page description to the last sentence
   boundary at or below 153 chars so the parsed length stays at most 155.

The script is idempotent: running it again on already-restored files is
a no-op for module pages (description already matches docstring) and
deterministic for CLI pages (trim is stable).

Usage: scripts/restore-module-descriptions.py [docs_dir]

Defaults to ./docs relative to the project root.
"""

from __future__ import annotations

import ast
import os
import re
import stat
import sys
import tempfile

GENERIC_PATTERN = re.compile(
    r"^API reference for (the )?[\w.]+( module)? — auto-generated documentation "
    r"covering public functions, classes, and type signatures\.$"
)

# Max chars for the value inside the description string (excluding the
# wrapping quotes). selfdoc's parser counts quotes, so 153 + 2 = 155.
MAX_DESC_VALUE = 153


def find_project_root(start: str) -> str:
    """Walk up from ``start`` to find the directory containing rlsbl/."""
    cur = os.path.abspath(start)
    while cur != "/":
        if os.path.isdir(os.path.join(cur, "rlsbl")):
            return cur
        cur = os.path.dirname(cur)
    raise RuntimeError(f"could not find project root containing rlsbl/ from {start}")


def module_path_for_doc(project_root: str, doc_basename: str) -> str | None:
    """Map docs/rlsbl-foo-bar.md to rlsbl/foo/bar.py or rlsbl/foo/bar/__init__.py.

    Returns None if no source file exists.
    """
    # Strip 'rlsbl-' prefix and '.md' suffix; convert '-' separators to '/'.
    stem = doc_basename[len("rlsbl-"):-len(".md")]
    parts = stem.split("-")
    rel_module = "/".join(parts)
    candidates = [
        os.path.join(project_root, "rlsbl", f"{rel_module}.py"),
        os.path.join(project_root, "rlsbl", rel_module, "__init__.py"),
    ]
    # Also handle the rlsbl.md root page -> rlsbl/__init__.py
    if not parts or parts == [""]:
        candidates.insert(0, os.path.join(project_root, "rlsbl", "__init__.py"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def extract_module_docstring(py_path: str) -> str | None:
    """Return the first line of the module-level docstring, or None."""
    try:
        with open(py_path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    doc = ast.get_docstring(tree, clean=True)
    if not doc:
        return None
    # Use only the first paragraph (up to first blank line) and collapse
    # newlines so it fits on one frontmatter line. Module docstrings in
    # this project are typically a single sentence already.
    first_para = doc.split("\n\n", 1)[0]
    one_line = " ".join(first_para.split())
    return one_line or None


def parse_frontmatter(text: str) -> tuple[list[str], int, int] | None:
    """Locate frontmatter block. Returns (lines, start_idx, end_idx) or None.

    start_idx is the index of the opening '---' line, end_idx of the closing.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines, 0, idx
    return None


def read_description_value(fm_lines: list[str], start: int, end: int) -> tuple[int, str] | None:
    """Find the description line. Returns (line_index, raw_value) or None.

    raw_value is the value as it appears after 'description:', with the
    leading whitespace stripped but quotes preserved.
    """
    for i in range(start + 1, end):
        line = fm_lines[i]
        stripped = line.lstrip()
        if stripped.startswith("description:"):
            value = stripped[len("description:"):].lstrip()
            return i, value
    return None


def strip_quotes(value: str) -> str:
    """Strip a single layer of matching surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def quote_value(value: str) -> str:
    """Quote a description value for YAML, escaping any embedded quotes."""
    # Prefer double quotes; escape any existing double quotes.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def trim_to_sentence_boundary(value: str, max_len: int, min_len: int = 120) -> str:
    """Trim ``value`` to at most ``max_len`` chars, ending at a sentence boundary.

    Prefers a sentence boundary in ``[min_len, max_len]``. Falls back to a
    word boundary in that range, then to the last sentence/word boundary
    below ``min_len``, and finally to a hard cut.
    """
    if len(value) <= max_len:
        return value
    candidate = value[:max_len]
    # Look for sentence-ending punctuation followed by space/end.
    sentence_ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", candidate)]
    # Prefer a sentence boundary in the SEO sweet spot [min_len, max_len].
    sweet_sentence = [p for p in sentence_ends if p >= min_len]
    if sweet_sentence:
        return candidate[:sweet_sentence[-1]].rstrip()
    # Else prefer a word boundary in the sweet spot.
    sweet_word = -1
    for i in range(min_len, len(candidate)):
        if candidate[i] == " ":
            sweet_word = i
    if sweet_word > 0:
        return candidate[:sweet_word].rstrip()
    # Else fall back to last sentence boundary below min_len, if any.
    if sentence_ends and sentence_ends[-1] >= 40:
        return candidate[:sentence_ends[-1]].rstrip()
    # Last word boundary anywhere.
    space = candidate.rfind(" ")
    if space >= 40:
        return candidate[:space].rstrip()
    # Hard cut.
    return candidate.rstrip()


def write_atomic_readonly(path: str, text: str) -> None:
    """Mirror selfdoc's atomic write: tmp file, chmod 0o444, replace."""
    if os.path.isfile(path):
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_description(doc_path: str, new_value: str) -> bool:
    """Replace the description value in the page's frontmatter.

    Returns True if the file was modified, False otherwise.
    """
    with open(doc_path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = parse_frontmatter(text)
    if parsed is None:
        return False
    lines, start, end = parsed
    found = read_description_value(lines, start, end)
    if found is None:
        return False
    line_idx, current_raw = found
    new_raw = quote_value(new_value)
    if current_raw == new_raw:
        return False
    # Preserve leading indentation of the original line.
    orig_line = lines[line_idx]
    indent = orig_line[: len(orig_line) - len(orig_line.lstrip())]
    lines[line_idx] = f"{indent}description: {new_raw}"
    new_text = "\n".join(lines)
    write_atomic_readonly(doc_path, new_text)
    return True


def process_module_page(project_root: str, doc_path: str) -> str:
    """Apply Task 2c restoration to a single rlsbl-*.md page.

    Returns one of: 'updated', 'noop', 'no-source', 'no-docstring', 'skipped'.
    """
    with open(doc_path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = parse_frontmatter(text)
    if parsed is None:
        return "skipped"
    lines, start, end = parsed
    found = read_description_value(lines, start, end)
    if found is None:
        return "skipped"
    _, raw = found
    current = strip_quotes(raw)
    # Only restore when the current description matches the generic template
    # OR when no description has been set yet.
    if not GENERIC_PATTERN.match(current):
        return "skipped"
    src = module_path_for_doc(project_root, os.path.basename(doc_path))
    if src is None:
        return "no-source"
    docstring = extract_module_docstring(src)
    if not docstring:
        return "no-docstring"
    # Selfdoc's own parser counts quotes; clamp to MAX_DESC_VALUE.
    docstring = trim_to_sentence_boundary(docstring, MAX_DESC_VALUE)
    changed = update_description(doc_path, docstring)
    return "updated" if changed else "noop"


def process_cli_page(doc_path: str) -> str:
    """Apply CLI-page SEO010 workaround.

    Returns 'updated', 'noop', or 'skipped'.
    """
    with open(doc_path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = parse_frontmatter(text)
    if parsed is None:
        return "skipped"
    lines, start, end = parsed
    found = read_description_value(lines, start, end)
    if found is None:
        return "skipped"
    _, raw = found
    value = strip_quotes(raw)
    if len(value) <= MAX_DESC_VALUE:
        return "noop"
    trimmed = trim_to_sentence_boundary(value, MAX_DESC_VALUE)
    changed = update_description(doc_path, trimmed)
    return "updated" if changed else "noop"


def main(argv: list[str]) -> int:
    docs_dir = argv[1] if len(argv) > 1 else None
    if docs_dir is None:
        project_root = find_project_root(os.getcwd())
        docs_dir = os.path.join(project_root, "docs")
    else:
        docs_dir = os.path.abspath(docs_dir)
        project_root = find_project_root(docs_dir)

    if not os.path.isdir(docs_dir):
        print(f"error: docs directory not found: {docs_dir}", file=sys.stderr)
        return 2

    module_counts = {"updated": 0, "noop": 0, "no-source": 0, "no-docstring": 0, "skipped": 0}
    cli_counts = {"updated": 0, "noop": 0, "skipped": 0}

    for entry in sorted(os.listdir(docs_dir)):
        if not entry.endswith(".md"):
            continue
        full = os.path.join(docs_dir, entry)
        if entry.startswith("rlsbl-") or entry == "rlsbl.md":
            result = process_module_page(project_root, full)
            module_counts[result] = module_counts.get(result, 0) + 1
            if result == "updated":
                print(f"  module: {entry}")
            elif result in ("no-source", "no-docstring"):
                print(f"  module: {entry} -> {result}")
        elif entry.startswith("cli-"):
            result = process_cli_page(full)
            cli_counts[result] = cli_counts.get(result, 0) + 1
            if result == "updated":
                print(f"  cli: {entry}")

    print("\nModule pages:")
    for k, v in module_counts.items():
        print(f"  {k}: {v}")
    print("\nCLI pages:")
    for k, v in cli_counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

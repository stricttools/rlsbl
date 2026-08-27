"""Rename a Go module path across a repository.

``rlsbl rewrite go-module-path --from-module <old> --to-module <new>`` moves a
module and everything under it onto a new path:

* every ``go.mod`` in the repository has its module-path TOKENS rewritten --
  the ``module`` directive itself, and any ``require`` / ``replace`` /
  ``exclude`` / ``retract`` reference to the old path from a nested module;
* every Go import site under the old path is rewritten, located by the
  tree-sitter import scanner (:func:`rlsbl.lint.go_ast.scan_imports`) and
  rewritten **line-anchored** -- only on the exact line the parser reported an
  import spec, and only inside that spec's quoted literal.

Containment is never a bare ``startswith``.  Both halves ask
:mod:`rlsbl.module_paths`, so a neighbouring module whose path merely begins
with the same letters (``github.com/o/foobar`` beside ``github.com/o/foo``) is
left alone.

What is deliberately NOT rewritten
----------------------------------

* **Comments.**  ``//`` text in a ``go.mod`` and anything outside an import
  spec in a ``.go`` file are prose; rewriting them would make the occurrence
  counts describe something other than the code being moved.  Grep for the old
  path after the rename to catch documentation.
* **Any file that is not a ``go.mod`` or a ``.go``.**  READMEs, CI workflows
  and generated code are outside this command's scope, on purpose: it renames
  a module, it does not sweep a repository for a string.
* **``vendor/``.**  Vendored trees are third-party copies, not this module.
"""

import os
import re
import sys
from dataclasses import dataclass

from ... import effects
from ...lint.go_ast import scan_imports
from ...lint.utils import walk_source_files
from ...module_paths import GO_SEP, go_import_under_module, rewrite_module_prefix
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile

#: Path components that take a file out of the walk, on top of the linter's
#: own exclusions.  A vendored tree is a third-party copy, not this module.
_EXCLUDED_COMPONENTS = frozenset({"vendor"})

#: Characters that continue a module-path token.  A match must not be preceded
#: by one (or it sits inside a longer path) and must not be followed by one
#: (or it is a DIFFERENT module that merely shares a prefix).  ``/`` is in the
#: "before" set but not the "after" set: ``.../foo/v2`` continues the same
#: module, while ``x/github.com/o/foo`` is a different token entirely.
_TOKEN_BEFORE = r"(?<![A-Za-z0-9._/\-])"
_TOKEN_AFTER = r"(?![A-Za-z0-9._\-])"


class GoModuleRewriteError(Exception):
    """A hard error in the module-path rewrite (bad input, count mismatch)."""


@dataclass(frozen=True)
class FileRewrite:
    """One file's pending rewrite, as observed."""

    path: str          # absolute path
    rel: str           # path relative to the project root
    kind: str          # "go.mod" or "go source"
    occurrences: int
    sites: tuple[str, ...]   # human-readable per-occurrence lines


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def validate_module_paths(old, new):
    """Reject module paths that cannot be renamed between."""
    for label, value in (("--from-module", old), ("--to-module", new)):
        if not value or not value.strip():
            raise GoModuleRewriteError(f"{label} must name a module path")
        if value != value.strip() or any(c.isspace() for c in value):
            raise GoModuleRewriteError(
                f"{label} must not contain whitespace: {value!r}"
            )
    if old == new:
        raise GoModuleRewriteError(
            "--from-module and --to-module are the same path; nothing to rename"
        )


# ---------------------------------------------------------------------------
# go.mod
# ---------------------------------------------------------------------------


def _token_pattern(old):
    return re.compile(_TOKEN_BEFORE + re.escape(old) + _TOKEN_AFTER)


def _excluded(path, root):
    """True when *path* sits under an excluded path component."""
    rel = os.path.relpath(path, str(root))
    return any(part in _EXCLUDED_COMPONENTS for part in rel.split(os.sep))


def find_go_mod_files(root):
    """Every ``go.mod`` in the tree, absolute, sorted."""
    found = walk_source_files(str(root), ("go.mod",), [])
    return sorted(
        p for p in found
        if os.path.basename(p) == "go.mod" and not _excluded(p, root)
    )


def rewrite_go_mod_text(text, old, new):
    """Rewrite module-path tokens in ``go.mod`` *text*.

    Returns ``(new_text, occurrences, sites)``.  Only the code portion of each
    line is touched: anything after ``//`` is a comment and is left verbatim.
    """
    pattern = _token_pattern(old)
    out = []
    occurrences = 0
    sites = []
    for lineno, raw in enumerate(text.splitlines(keepends=True), start=1):
        stripped = raw.rstrip("\r\n")
        eol = raw[len(stripped):]
        comment_at = stripped.find("//")
        code = stripped if comment_at < 0 else stripped[:comment_at]
        comment = "" if comment_at < 0 else stripped[comment_at:]

        hits = list(pattern.finditer(code))
        if hits:
            occurrences += len(hits)
            for hit in hits:
                token = _token_at(code, hit.start())
                sites.append(
                    f"line {lineno}: {token} -> "
                    f"{rewrite_module_prefix(token, old, new, sep=GO_SEP)}"
                )
            code = pattern.sub(lambda _m: new, code)
        out.append(code + comment + eol)
    return "".join(out), occurrences, tuple(sites)


def _token_at(text, start):
    """The whole module-path token beginning at *start* in *text*."""
    end = start
    while end < len(text) and (text[end].isalnum() or text[end] in "._/-~"):
        end += 1
    return text[start:end]


def declared_modules(go_mod_paths):
    """``{abs go.mod path: declared module path}`` for every readable go.mod."""
    declared = {}
    for path in go_mod_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.split("//", 1)[0].strip()
                    if line.startswith("module ") or line == "module":
                        parts = line.split(None, 1)
                        if len(parts) == 2 and parts[1].strip():
                            declared[path] = parts[1].strip()
                        break
        except (OSError, UnicodeDecodeError):
            continue
    return declared


# ---------------------------------------------------------------------------
# Go source files
# ---------------------------------------------------------------------------


def rewrite_go_source_text(text, sites, old, new):
    """Rewrite the import literals *sites* names, line-anchored.

    Args:
        text: the file's full contents.
        sites: ``[(import_path, line_number)]`` as the tree-sitter scanner
            reported them, restricted to imports under *old*.

    Returns ``(new_text, occurrences, descriptions)``.  An import spec whose
    quoted literal is not present on the reported line is a hard error: the
    parser and the text disagree, and guessing where to write is exactly the
    failure this command must not have.
    """
    lines = text.splitlines(keepends=True)
    occurrences = 0
    descriptions = []
    by_line = {}
    for import_path, lineno in sites:
        by_line.setdefault(lineno, []).append(import_path)

    for lineno in sorted(by_line):
        if lineno < 1 or lineno > len(lines):
            raise GoModuleRewriteError(
                f"import spec reported on line {lineno}, which the file does "
                f"not have ({len(lines)} lines)"
            )
        line = lines[lineno - 1]
        for import_path in by_line[lineno]:
            literal = f'"{import_path}"'
            if literal not in line:
                raise GoModuleRewriteError(
                    f"line {lineno} does not contain the import literal "
                    f"{literal} the parser reported"
                )
            new_path = rewrite_module_prefix(import_path, old, new, sep=GO_SEP)
            line = line.replace(literal, f'"{new_path}"', 1)
            occurrences += 1
            descriptions.append(f"line {lineno}: {import_path} -> {new_path}")
        lines[lineno - 1] = line

    return "".join(lines), occurrences, tuple(descriptions)


def scan_go_source(path, old):
    """Import sites in *path* that are under module *old*."""
    return [
        (import_path, lineno)
        for import_path, _fp, lineno in scan_imports(path)
        if go_import_under_module(import_path, old)
    ]


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def observe_file(path, root, old, new):
    """Observe one file, returning a :class:`FileRewrite` or None."""
    rel = os.path.relpath(path, str(root))
    try:
        text = _read(path)
    except (OSError, UnicodeDecodeError):
        return None

    if os.path.basename(path) == "go.mod":
        _new_text, count, sites = rewrite_go_mod_text(text, old, new)
        kind = "go.mod"
    else:
        found = scan_go_source(path, old)
        if not found:
            return None
        _new_text, count, sites = rewrite_go_source_text(text, found, old, new)
        kind = "go source"

    if count == 0:
        return None
    return FileRewrite(
        path=path, rel=rel, kind=kind, occurrences=count, sites=sites,
    )


def recompute(rewrite, old, new):
    """Re-derive a file's rewrite from disk.  Returns ``(new_text, count)``."""
    text = _read(rewrite.path)
    if rewrite.kind == "go.mod":
        new_text, count, _ = rewrite_go_mod_text(text, old, new)
    else:
        new_text, count, _ = rewrite_go_source_text(
            text, scan_go_source(rewrite.path, old), old, new
        )
    return new_text, count


def observe(root, old, new):
    """Build the whole preview: one item per file that would change.

    A sweep that finds NOTHING is a hard error, not an empty plan. The
    overwhelmingly likely cause is a mistyped ``--from-module``, and a command
    that answers a typo with "nothing to do, exit 0" teaches the operator to
    believe a rename happened when none did.

    A repository that does not itself DECLARE the module is fine and is
    reported as such: renaming a dependency's module path across a consumer
    (upstream moved, the imports must follow) is the same sweep with no
    ``module`` directive in it.
    """
    go_mods = find_go_mod_files(root)
    declared = declared_modules(go_mods)
    owns = old in set(declared.values())

    sources = sorted(
        p for p in walk_source_files(str(root), (".go",), [])
        if not _excluded(p, root)
    )

    items = []
    for path in [*go_mods, *sources]:
        found = observe_file(path, root, old, new)
        if found is None:
            continue
        items.append(
            VerdictItem(
                key=found.rel,
                state="rewrite",
                summary=(
                    f"{found.occurrences} occurrence"
                    f"{'' if found.occurrences == 1 else 's'} "
                    f"in this {found.kind}"
                ),
                facts=found.sites,
                actions=(
                    f"apply would rewrite {found.occurrences} occurrence"
                    f"{'' if found.occurrences == 1 else 's'} here.",
                ),
                data=found,
            )
        )

    if not items:
        found_modules = sorted(set(declared.values()))
        listing = (
            ", ".join(found_modules) if found_modules
            else "(no go.mod declares a module)"
        )
        raise GoModuleRewriteError(
            f"nothing references '{old}' anywhere in this repository -- no "
            f"go.mod token and no import site. Check --from-module for a "
            f"typo. Module paths declared here: {listing}."
        )

    total = sum(item.data.occurrences for item in items)
    summary_facts = ()
    if not owns:
        summary_facts = (
            f"no go.mod here declares '{old}': this repository CONSUMES the "
            f"module rather than owning it, so only references are rewritten.",
        )
    return Preview((
        *items,
        VerdictItem(
            key="(total)",
            state="summary",
            summary=(
                f"{total} occurrence{'' if total == 1 else 's'} across "
                f"{len(items)} file{'' if len(items) == 1 else 's'}: "
                f"{old} -> {new}"
            ),
            facts=summary_facts,
        ),
    ))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_item(item, old, new):
    """Write one file, refusing when its count moved since the preview."""
    found = item.data
    if found is None:
        return  # the summary / nothing-to-do items carry no file
    new_text, count = recompute(found, old, new)
    if count != found.occurrences:
        raise GoModuleRewriteError(
            f"{found.rel}: the preview counted {found.occurrences} "
            f"occurrence(s) but the file now has {count}. The working tree "
            f"changed between the preview and the apply; nothing further has "
            f"been written. Re-run with --dry-run, read the plan, and apply "
            f"again."
        )
    effects.atomic_write_text(found.path, new_text, preserve_mode=True)
    print(f"  {found.rel}: rewrote {count} occurrence(s)")


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def cmd_go_module_path(flags, project_root):
    """``rlsbl rewrite go-module-path`` -- rename a Go module across the repo.

    ``flags["from-module"]`` / ``flags["to-module"]`` -- the module paths.
    ``flags["dry-run"]``                             -- plan only.
    """
    old = flags["from-module"]
    new = flags["to-module"]
    dry_run = bool(flags.get("dry-run", False))

    try:
        validate_module_paths(old, new)
    except GoModuleRewriteError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    reconciler = Reconciler(
        observe=lambda: observe(project_root, old, new),
        apply_item=lambda item: apply_item(item, old, new),
        show_keys=True,
    )
    try:
        preview = reconcile(reconciler, dry_run=dry_run)
    except GoModuleRewriteError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        changed = [i for i in preview.items if i.data is not None]
        print(f"Renamed {old} -> {new} across {len(changed)} file(s).")

"""Rename a Go module path across a repository.

``rlsbl rewrite go-module-path --from-module <old> --to-module <new>`` moves a
module and everything under it onto a new path:

* every ``go.mod`` in the repository has its module-path TOKENS rewritten --
  the ``module`` directive itself, and any ``require`` / ``replace`` /
  ``exclude`` / ``retract`` reference to the old path from a nested module;
* every Go import site under the old path is rewritten, located by the
  tree-sitter import scanner (:func:`rlsbl.lint.go_ast.scan_imports`) and
  rewritten **line-scoped** -- only on the exact line the parser reported an
  import spec, and only inside that spec's quoted literal.

Containment is never a bare ``startswith``.  Both halves ask
:mod:`rlsbl.module_paths`, so a neighbouring module whose path merely begins
with the same letters (``github.com/o/foobar`` beside ``github.com/o/foo``) is
left alone.

What is deliberately NOT rewritten
----------------------------------

* **Anything git ignores.**  The sweep reads exactly what ``git ls-files
  --cached --others --exclude-standard`` lists (see
  :func:`rlsbl.lint.utils.walk_source_files`): tracked files plus untracked
  files that are not ignored.  A gitignored third-party clone is somebody
  else's module, and a directory outside any git work tree is refused.

* **Comments.**  ``//`` text in a ``go.mod`` and anything outside an import
  spec in a ``.go`` file are prose; rewriting them would make the occurrence
  counts describe something other than the code being moved.  Grep for the old
  path after the rename to catch documentation.
* **Any file that is not a ``go.mod``, a ``.go``, or a committed strictcli
  schema dump.**  READMEs, CI workflows and generated code are outside this
  command's scope, on purpose: it renames a module, it does not sweep a
  repository for a string.  The one exception is
  ``.strictcli/schema.json``, whose ``project_id`` IS the module path for a
  strictcli-based Go app: that single line is rewritten, because rlsbl already
  writes this file during a release and a dump left on the old identity makes
  the next one refuse.
* **``vendor/``, ``.git/``, and the project's scratch directories.**  A
  vendored tree is a third-party copy, not this module; ``.git`` is the
  repository's own storage; and ``experiments/`` and ``screenshots/`` hold
  disposable artifacts (see :mod:`rlsbl.scratch_dirs`), which the shared walker
  prunes from every walk.  Every other directory is visited, INCLUDING
  ``build``, ``dist``,
  ``static``, ``public``, ``assets`` and ``node_modules``: those are a
  linter's build-output exclusions, and each of them is also a perfectly
  ordinary Go package directory (``internal/assets``, ``cmd/build``,
  ``web/static``).  Pruning them here skipped real packages silently and left
  a tree that does not compile, so this command passes its own exclusion set
  to the shared walker rather than inheriting the linters' one.
"""

import json
import os
import re
import sys
from dataclasses import dataclass

from ... import effects
from ...lint.go_ast import scan_imports
from ...lint.tree_walk import SourceParseError
from ...lint.utils import SourceWalkError, walk_source_files
from ...module_paths import GO_SEP, go_import_under_module, rewrite_module_prefix
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile
from .abort import already_written

#: Path components that take a file out of the walk.  A vendored tree is a
#: third-party copy, not this module.
_EXCLUDED_COMPONENTS = frozenset({"vendor"})

#: Directory names the sweep never descends into, on top of the scratch
#: directories :func:`~rlsbl.lint.utils.walk_source_files` always prunes.
#: Passed explicitly so this command does not inherit the linters'
#: build-output exclusions, which are ordinary Go package names (see the
#: module docstring).
_WALK_EXCLUDED_DIRS = frozenset({*_EXCLUDED_COMPONENTS, ".git"})

#: Characters that continue a module-path token.  A match must not be preceded
#: by one (or it sits inside a longer path) and must not be followed by one
#: (or it is a DIFFERENT module that merely shares a prefix).  ``/`` is in the
#: "before" set but not the "after" set: ``.../foo/v2`` continues the same
#: module, while ``x/github.com/o/foo`` is a different token entirely.
_TOKEN_BEFORE = r"(?<![A-Za-z0-9._/\-])"
_TOKEN_AFTER = r"(?![A-Za-z0-9._\-])"

#: The committed strictcli schema dump, relative to the module that owns it.
#: A strictcli-based app dumps its whole CLI surface here, and for a Go app the
#: document's ``project_id`` is the module path -- so a rename that skips this
#: file leaves a dump claiming the old identity, and the next
#: ``--dump-schema`` refuses to overwrite a schema "belonging to" another
#: project.  rlsbl runs that dump itself at the release's schema-dump step.
_SCHEMA_DUMP_DIR = ".strictcli"
_SCHEMA_DUMP_NAME = "schema.json"

#: The top-level ``project_id`` member of a canonically-encoded strictcli
#: schema dump: two spaces of indent (depth 1), the key, and a JSON string
#: literal.  Pinned at exactly two spaces and at the start of a line, like the
#: release's own ``version`` patch, so a ``project_id`` nested deeper -- a flag
#: NAMED project_id, a nested object carrying one -- can never match, and the
#: match can never land inside another string's contents.
_SCHEMA_PROJECT_ID_LINE = re.compile(
    r'^  "project_id": "((?:[^"\\]|\\.)*)"(,?)$', re.MULTILINE,
)


class GoModuleRewriteError(Exception):
    """A hard error in the module-path rewrite (bad input, count mismatch)."""


@dataclass(frozen=True)
class FileRewrite:
    """One file's pending rewrite, as observed."""

    path: str          # absolute path
    rel: str           # path relative to the project root
    kind: str          # "go.mod", "go source" or "strictcli schema dump"
    occurrences: int
    sites: tuple[str, ...]   # human-readable per-occurrence lines


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def validate_module_paths(old, new):
    """Reject module paths that cannot be renamed between.

    A supplied-but-empty path is not refused here: that is one class with one
    message, and the CLI boundary owns it (``rlsbl._refuse_empty_flags``), so
    ``--from-module ""`` reads the same as every other empty flag value in
    rlsbl rather than a message only this command uses.
    """
    for label, value in (("--from-module", old), ("--to-module", new)):
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
    found = walk_source_files(
        str(root), ("go.mod",), [], excluded_dir_names=_WALK_EXCLUDED_DIRS,
    )
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
# The committed strictcli schema dump
# ---------------------------------------------------------------------------


#: What the plan calls this file, and what ``recompute`` dispatches on.
SCHEMA_DUMP_KIND = "strictcli schema dump"


def find_schema_dumps(root):
    """Every committed strictcli schema dump in the tree, absolute, sorted."""
    found = walk_source_files(
        str(root), (_SCHEMA_DUMP_NAME,), [],
        excluded_dir_names=_WALK_EXCLUDED_DIRS,
    )
    return sorted(
        p for p in found
        if os.path.basename(p) == _SCHEMA_DUMP_NAME
        and os.path.basename(os.path.dirname(p)) == _SCHEMA_DUMP_DIR
        and not _excluded(p, root)
    )


def rewrite_schema_project_id(text, old, new):
    """Rewrite a strictcli schema dump's ``project_id``, line-scoped.

    Returns ``(new_text, occurrences, sites)`` -- one occurrence at most, since
    a document declares one identity.  The patch is TEXTUAL for the same reason
    the release's ``version`` patch is: strictcli writes this file in its own
    canonical encoding, and a decode/re-encode round trip through
    ``json.dumps`` silently produces a different document (``ensure_ascii``
    alone rewrites every non-ASCII character in every help string).

    A dump whose ``project_id`` is not under *old* is not this module's, and is
    reported as nothing to do: a Python or TypeScript app's ``project_id`` is a
    bare distribution name, and a neighbouring Go module's merely starts with
    the same letters.
    """
    match = _SCHEMA_PROJECT_ID_LINE.search(text)
    if match is None:
        return text, 0, ()
    try:
        current = json.loads(f'"{match.group(1)}"')
    except ValueError:
        return text, 0, ()
    if not go_import_under_module(current, old):
        return text, 0, ()

    renamed = rewrite_module_prefix(current, old, new, sep=GO_SEP)
    lineno = text.count("\n", 0, match.start()) + 1
    replacement = (
        f'  "project_id": {json.dumps(renamed, ensure_ascii=False)}'
        f"{match.group(2)}"
    )
    return (
        text[:match.start()] + replacement + text[match.end():],
        1,
        (f"line {lineno}: project_id {current} -> {renamed}",),
    )


# ---------------------------------------------------------------------------
# Go source files
# ---------------------------------------------------------------------------


#: The delimiters Go's two string literal forms use.  An import spec is
#: written in one of them, and the scanner reports the path without either, so
#: the rewrite looks for both and preserves whichever the file used.
_QUOTES = ('"', "`")


def _literal_on_line(line, import_path):
    """The quoted literal for *import_path* as it appears on *line*, or None."""
    for quote in _QUOTES:
        literal = f"{quote}{import_path}{quote}"
        if literal in line:
            return quote, literal
    return None


def rewrite_go_source_text(text, sites, old, new):
    """Rewrite the import literals *sites* names, line-scoped.

    Args:
        text: the file's full contents.
        sites: ``[(import_path, line_number)]`` as the tree-sitter scanner
            reported them, restricted to imports under *old*.

    Returns ``(new_text, occurrences, descriptions)``.  An import spec whose
    quoted literal is not present on the reported line -- in EITHER of Go's
    two string forms -- is a hard error: the parser and the text disagree, and
    guessing where to write is exactly the failure this command must not have.
    The form the file used is preserved: a raw-string import stays a raw
    string.
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
            located = _literal_on_line(line, import_path)
            if located is None:
                raise GoModuleRewriteError(
                    f"line {lineno} does not contain the import literal for "
                    f"{import_path} the parser reported, in either quote form"
                )
            quote, literal = located
            new_path = rewrite_module_prefix(import_path, old, new, sep=GO_SEP)
            line = line.replace(literal, f"{quote}{new_path}{quote}", 1)
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


def _is_schema_dump(path):
    """True when *path* is a committed strictcli schema dump."""
    return (
        os.path.basename(path) == _SCHEMA_DUMP_NAME
        and os.path.basename(os.path.dirname(path)) == _SCHEMA_DUMP_DIR
    )


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
    elif _is_schema_dump(path):
        _new_text, count, sites = rewrite_schema_project_id(text, old, new)
        kind = SCHEMA_DUMP_KIND
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
    elif rewrite.kind == SCHEMA_DUMP_KIND:
        new_text, count, _ = rewrite_schema_project_id(text, old, new)
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
        p for p in walk_source_files(
            str(root), (".go",), [], excluded_dir_names=_WALK_EXCLUDED_DIRS,
        )
        if not _excluded(p, root)
    )

    schema_dumps = find_schema_dumps(root)

    items = []
    for path in [*go_mods, *sources, *schema_dumps]:
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


def apply_item(item, old, new, applied=None):
    """Write one file, refusing when its count moved since the preview.

    *applied* is an optional list the caller passes through every item; each
    successful write appends its key, so an abort can name what is already on
    disk.
    """
    found = item.data
    if found is None:
        return  # the summary / nothing-to-do items carry no file
    try:
        new_text, count = recompute(found, old, new)
    except (OSError, UnicodeDecodeError) as exc:
        raise GoModuleRewriteError(
            f"{found.rel}: the file the plan named could not be read at apply "
            f"time ({exc}). It was there when the preview ran, so the working "
            f"tree changed underneath the plan. "
            f"{already_written(applied)}"
            f"Re-run with --dry-run: the re-plan reads the tree as it is now."
        ) from exc
    if count != found.occurrences:
        raise GoModuleRewriteError(
            f"{found.rel}: the preview counted {found.occurrences} "
            f"occurrence(s) but the file now has {count}. The working tree "
            f"changed between the preview and the apply; nothing further has "
            f"been written. "
            f"{already_written(applied)}"
            f"Re-run with --dry-run, read the plan, and apply again -- a "
            f"re-run re-plans from the tree as it is now."
        )
    effects.atomic_write_text(found.path, new_text, preserve_mode=True)
    if applied is not None:
        applied.append(found.rel)
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

    applied = []
    reconciler = Reconciler(
        observe=lambda: observe(project_root, old, new),
        apply_item=lambda item: apply_item(item, old, new, applied=applied),
        show_keys=True,
    )
    try:
        preview = reconcile(reconciler, dry_run=dry_run)
    except (GoModuleRewriteError, SourceParseError, SourceWalkError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        changed = [i for i in preview.items if i.data is not None]
        print(f"Renamed {old} -> {new} across {len(changed)} file(s).")

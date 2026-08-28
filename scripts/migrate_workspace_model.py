#!/usr/bin/env python3
"""Migrate one workspace.toml to the ownership model, then anchor its releases.

The ownership model changed three things about a workspace's declaration, and
each is a mechanical edit an operator would otherwise make by hand in every
repository:

  1. **The root member is declared.** Every workspace names the member that owns
     the repository root (``path = "."``, named ``root``), so no tracked file
     stands outside the ownership model. Its KIND is a per-repository decision
     with no default -- a dev node whose root files need no changelog coverage,
     or a member of a named releasable whose root files get coverage under it --
     so the operator states it: ``--root-dev-node``, or ``--root-releasable
     <name> --tag-format <format>``.
  2. **``watch`` keys are gone.** Territory is derived from declared member
     paths; every ``watch = [...]`` line is deleted.
  3. **The mirror destination moved onto the releasable.** A member-level
     ``subtree_remote`` moves to that member's ``[[releasables]]`` entry.

Everything else the loader refuses is an operator decision this script will not
make: which member owns the root when two claim it, which releasable a mirrored
member belongs to, which tag scheme a root releasable's history already uses. It
edits the file raw (tomlkit, comments and key order preserved) because the
loader itself refuses the old model and so cannot be used to read one, then
loads the result and prints any remaining loader error verbatim as the
operator's residue list.

Finally it runs the release-anchor backfill (``backfill_release_anchors.py``) in
the same repository, which is idempotent and commits its own writes.

Usage:
    uv run python scripts/migrate_workspace_model.py --root-dev-node --dry-run
    uv run python scripts/migrate_workspace_model.py --repo PATH --root-dev-node
    uv run python scripts/migrate_workspace_model.py \\
        --root-releasable monorepo --tag-format 'v{version}'

Exit status: 0 when the migrated workspace loads cleanly, 1 when it loads with
residue an operator must resolve (or the backfill reports foreign tags), 2 when
the migration was refused before writing anything.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass, field

import tomlkit
from tomlkit.items import AoT

# Import rlsbl from the repository this script ships in, not from whatever is
# installed: the model this migrates TO is this checkout's, and the installed
# rlsbl in a fleet repository is by definition the version that predates it.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_ROOT)

from rlsbl.errors import WorkspaceError  # noqa: E402
from rlsbl.ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH  # noqa: E402
from rlsbl.utils import commit_files  # noqa: E402
from rlsbl.workspace import (  # noqa: E402
    LAST_IMPLICIT_MODE_VERSION,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    is_root_path,
    load_releasables,
    load_workspace,
)
from rlsbl import effects  # noqa: E402


class MigrationError(Exception):
    """The migration was refused: nothing has been written."""


# ---------------------------------------------------------------------------
# The operator's declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootKind:
    """What kind of member owns the repository root, as the operator states it.

    Exactly one of the two kinds, never a default: whether the files at a
    repository's root ship to users is a per-repository fact, and a wrong guess
    either exempts released files from changelog coverage or demands coverage
    for files that never ship.
    """

    dev_node: bool
    releasable: str | None = None
    tag_format: str | None = None

    def __post_init__(self):
        if self.dev_node and self.releasable:
            raise MigrationError(
                "the root member is one kind or the other: --root-dev-node and "
                "--root-releasable cannot both be given."
            )
        if not self.dev_node and not self.releasable:
            raise MigrationError(
                "the root member's kind must be declared. Every workspace "
                "declares the repository root as a member, and it is either:\n"
                "  --root-dev-node                  a dev node -- root files "
                "are exempt from changelog coverage\n"
                "  --root-releasable <name> --tag-format <fmt>\n"
                "                                   a member of a named "
                "releasable -- root files get changelog coverage\n"
                "There is no default: which one a repository wants depends on "
                "whether its root files ship to users."
            )
        if self.releasable and not self.tag_format:
            raise MigrationError(
                "--tag-format is required with --root-releasable. A releasable "
                "that owns the repository root must never inherit a default tag "
                'format: pass "v{version}" for bare version tags, or '
                '"{name}@v{version}" for the workspace scheme. Only the '
                "repository's existing tags say which."
            )
        if self.dev_node and self.tag_format:
            raise MigrationError(
                "--tag-format belongs to --root-releasable: a dev-node root "
                "member is never released, so it has no tag scheme."
            )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class Edit:
    """One mechanical change, named where the operator can see it in the file."""

    kind: str
    where: str
    detail: str

    def render(self) -> str:
        return f"  {self.where}: {self.detail}"


@dataclass
class Plan:
    """The migrated document plus the list of edits that produced it."""

    repo: str
    path: str
    doc: object
    edits: list[Edit] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.edits)

    def add(self, kind, where, detail):
        self.edits.append(Edit(kind=kind, where=where, detail=detail))


def _member_label(index, table) -> str:
    name = table.get("name") or os.path.basename(str(table.get("path", "")))
    return f"projects[{index}] ('{name}')"


def _releasable_label(index, table) -> str:
    return f"releasables[{index}] ('{table.get('name')}')"


def _as_table_list(value):
    """The tables in a ``[[section]]``, or an empty list for anything else."""
    if isinstance(value, AoT):
        return list(value)
    if isinstance(value, list):
        return [item for item in value if hasattr(item, "get")]
    return []


class Section:
    """One ``[[section]]`` of the document, converted only when written to.

    ``releasables = []`` and ``projects = []`` are how an empty section is
    spelled, because an empty array-of-tables renders as nothing at all -- and a
    workspace whose ``releasables`` line disappears becomes an implicit-mode
    workspace the loader refuses. So the inline array is rewritten as an
    array-of-tables at the moment a table is actually appended to it, and left
    exactly as written (in place, in its original position) when it is not.
    """

    def __init__(self, doc, key):
        self.doc = doc
        self.key = key
        existing = doc.get(key)
        if existing is not None and not isinstance(existing, AoT):
            if len(_as_table_list(existing)) > 0:
                raise MigrationError(
                    f"workspace.toml declares '{key}' as "
                    f"{type(existing).__name__}, not an array of tables "
                    f"([[{key}]]). Fix the file by hand: the migration edits "
                    f"tables, and will not rewrite a section it cannot recognize."
                )
        self._aot = existing if isinstance(existing, AoT) else None

    def tables(self):
        if self._aot is not None:
            return list(self._aot)
        return _as_table_list(self.doc.get(self.key))

    def append(self, table):
        if self._aot is None:
            if self.key in self.doc:
                del self.doc[self.key]
            self._aot = tomlkit.aot()
            self.doc.add(self.key, self._aot)
        self._aot.append(table)


def _new_table(pairs):
    """A fresh tomlkit table, spaced like the tables around it."""
    table = tomlkit.table()
    for key, value in pairs:
        table.add(key, value)
    table.trivia.indent = "\n"
    return table


# ---------------------------------------------------------------------------
# Edit 1: the root member
# ---------------------------------------------------------------------------


def _find_root_member(members):
    """``(index, table)`` of the member declaring the repository root, or None.

    Every spelling of the root counts (``.``, ``""``, ``./``) -- they are one
    directory, and the loader normalizes them to one member path.
    """
    for i, table in enumerate(members):
        if is_root_path(str(table.get("path", ""))):
            return i, table
    return None


def _plan_root_member(plan, projects, releasables, kind):
    members = projects.tables()
    found = _find_root_member(members)

    if found is None:
        pairs = [("path", ROOT_MEMBER_PATH), ("name", ROOT_MEMBER_NAME)]
        if kind.dev_node:
            # A dev-node root member is both: dev_only (nothing user-facing may
            # depend on it) and outside every releasable (it is never released).
            pairs += [("dev_only", True), ("releasable", False)]
            what = "a dev node"
        else:
            pairs += [("releasable", kind.releasable)]
            what = f"a member of releasable '{kind.releasable}'"
        projects.append(_new_table(pairs))
        plan.add(
            "add-root-member",
            f"projects[{len(members)}] ('{ROOT_MEMBER_NAME}')",
            f'add the root member (path = ".", name = "{ROOT_MEMBER_NAME}") as '
            f"{what}",
        )
    else:
        index, table = found
        label = _member_label(index, table)
        if str(table.get("path")) != ROOT_MEMBER_PATH:
            spelled = table.get("path")
            table["path"] = ROOT_MEMBER_PATH
            plan.add(
                "spell-root-path",
                label,
                f"rewrite path '{spelled}' as the canonical '{ROOT_MEMBER_PATH}'",
            )
        if table.get("name") != ROOT_MEMBER_NAME:
            previous = table.get("name")
            table["name"] = ROOT_MEMBER_NAME
            plan.add(
                "name-root-member",
                label,
                f"rename '{previous}' to the reserved '{ROOT_MEMBER_NAME}' -- job "
                f"keys, router filters and check regexes are derived from it, so "
                f"its spelling cannot vary from repository to repository",
            )
        _plan_root_kind(plan, label, table, kind)

    if not kind.dev_node:
        _plan_root_releasable_entry(plan, releasables, kind)


def _plan_root_kind(plan, label, table, kind):
    """Complete an already-declared root member to the kind the operator named."""
    declared = table.get("releasable", None)
    if kind.dev_node:
        if isinstance(declared, str):
            raise MigrationError(
                f"{label} is the root member and declares "
                f"releasable = \"{declared}\", but --root-dev-node was passed. "
                f"A dev-node root member stands outside every releasable, so the "
                f"two contradict. Re-run with "
                f"--root-releasable {declared} --tag-format <fmt> to keep the "
                f"declaration in the file, or delete the releasable key by hand "
                f"if the root member really is a dev node -- which of the two "
                f"this repository wants is your decision."
            )
        if declared is None:
            table["releasable"] = False
            plan.add(
                "root-kind",
                label,
                "add releasable = false -- the root member stands outside every "
                "releasable",
            )
        if not table.get("dev_only", False):
            table["dev_only"] = True
            plan.add(
                "root-kind",
                label,
                "add dev_only = true -- root files need no changelog coverage",
            )
        return

    if declared is False:
        raise MigrationError(
            f"{label} is the root member and declares releasable = false, but "
            f"--root-releasable {kind.releasable} was passed. A member outside "
            f"every releasable and a member of one contradict. Re-run with "
            f"--root-dev-node to keep the declaration in the file, or decide "
            f"that the root files ship under '{kind.releasable}' and delete the "
            f"releasable = false line by hand."
        )
    if isinstance(declared, str) and declared != kind.releasable:
        raise MigrationError(
            f"{label} is the root member and declares "
            f"releasable = \"{declared}\", but --root-releasable "
            f"{kind.releasable} was passed. The two name different releasables, "
            f"and which one owns the repository root is your decision -- re-run "
            f"naming the one this workspace means."
        )
    if declared is None:
        table["releasable"] = kind.releasable
        plan.add(
            "root-kind",
            label,
            f"add releasable = \"{kind.releasable}\" -- root files get changelog "
            f"coverage under it",
        )


def _plan_root_releasable_entry(plan, releasables, kind):
    """The root member's releasable exists and declares its tag format."""
    entries = releasables.tables()
    for i, table in enumerate(entries):
        if table.get("name") != kind.releasable:
            continue
        declared = table.get("tag_format", None)
        if declared is None:
            table["tag_format"] = kind.tag_format
            plan.add(
                "root-tag-format",
                _releasable_label(i, table),
                f"add tag_format = \"{kind.tag_format}\" -- a releasable owning "
                f"the repository root never inherits a default",
            )
        elif str(declared) != kind.tag_format:
            raise MigrationError(
                f"releasables[{i}] ('{kind.releasable}') already declares "
                f"tag_format = \"{declared}\", but --tag-format "
                f"\"{kind.tag_format}\" was passed. Only this repository's "
                f"existing tags say which scheme its history uses, so the "
                f"script will not overwrite one with the other -- re-run naming "
                f"the scheme the tags already follow, or change the line by hand."
            )
        return

    releasables.append(
        _new_table([("name", kind.releasable), ("tag_format", kind.tag_format)])
    )
    plan.add(
        "add-releasable",
        f"releasables[{len(entries)}] ('{kind.releasable}')",
        f"add the releasable that owns the root member, with "
        f"tag_format = \"{kind.tag_format}\"",
    )


# ---------------------------------------------------------------------------
# Edit 2: the watch key
# ---------------------------------------------------------------------------


def _plan_watch_keys(plan, projects):
    """Delete every ``watch`` key: territory is derived, never enumerated."""
    for i, table in enumerate(projects.tables()):
        if "watch" not in table:
            continue
        declared = list(table["watch"])
        del table["watch"]
        plan.add(
            "drop-watch",
            _member_label(i, table),
            f"delete the watch key ({declared}) -- every file belongs to the "
            f"member with the most specific declared path, and the root member "
            f"owns everything no other member claims",
        )


# ---------------------------------------------------------------------------
# Edit 3: the mirror destination
# ---------------------------------------------------------------------------


def _plan_mirror_remotes(plan, projects, releasables):
    """Move each member's ``subtree_remote`` onto the releasable it belongs to.

    A mirror carries one subtree's whole history, its tags and its GitHub
    Releases, and the unit that owns a version, a changelog and a tag scheme is
    the releasable -- so the destination is declared there. Which releasable a
    mirrored member should belong to, when it belongs to none, is a decision
    this pass refuses to make.
    """
    members = projects.tables()
    entries = releasables.tables()

    for i, table in enumerate(members):
        if "subtree_remote" not in table:
            continue
        remote = str(table["subtree_remote"])
        label = _member_label(i, table)
        rel_name = table.get("releasable", None)

        if not isinstance(rel_name, str):
            stated = (
                "declares releasable = false"
                if rel_name is False
                else "declares no releasable"
            )
            raise MigrationError(
                f"{label} declares subtree_remote = \"{remote}\" but {stated}, "
                f"so there is no releasable to move the mirror's destination "
                f"onto. Two ways out, and which one this repository wants is "
                f"your decision: give this member a releasable of its own (a "
                f"[[releasables]] entry with its name, its tag format and this "
                f"member as its only member -- creating one is not a mechanical "
                f"edit, so the script will not invent it), or delete the "
                f"subtree_remote line because the mirror is retired."
            )

        target = None
        for j, rel_table in enumerate(entries):
            if rel_table.get("name") == rel_name:
                target = (j, rel_table)
                break
        if target is None:
            raise MigrationError(
                f"{label} declares subtree_remote = \"{remote}\" and "
                f"releasable = \"{rel_name}\", but no [[releasables]] entry is "
                f"named '{rel_name}'. The mirror's destination belongs on that "
                f"entry, and creating it -- with the tag format its existing "
                f"tags already follow -- is your decision, not a mechanical "
                f"edit."
            )

        siblings = [
            str(m.get("name") or os.path.basename(str(m.get("path", ""))))
            for m in members
            if m.get("releasable") == rel_name
        ]
        if len(siblings) > 1:
            raise MigrationError(
                f"{label} declares subtree_remote = \"{remote}\", but its "
                f"releasable '{rel_name}' has {len(siblings)} members "
                f"({', '.join(siblings)}). A mirror is the standalone repository "
                f"ONE subtree is split into, so a mirrored releasable has "
                f"exactly one member -- moving the destination there would write "
                f"a workspace the loader refuses. Either split the releasable so "
                f"the mirrored member owns one of its own, or drop the mirror; "
                f"which one is your decision."
            )

        j, rel_table = target
        declared = rel_table.get("subtree_remote", None)
        if declared is None:
            rel_table["subtree_remote"] = remote
            detail = (
                f"move the mirror destination \"{remote}\" onto "
                f"releasables[{j}] ('{rel_name}'), where the version, the "
                f"changelog and the tag scheme already are"
            )
        elif str(declared) != remote:
            raise MigrationError(
                f"{label} declares subtree_remote = \"{remote}\" but its "
                f"releasable '{rel_name}' already declares "
                f"subtree_remote = \"{declared}\". Two destinations for one "
                f"mirror, and only you know which repository this subtree is "
                f"actually mirrored into -- resolve it by hand."
            )
        else:
            detail = (
                f"delete the mirror destination \"{remote}\" -- "
                f"releasables[{j}] ('{rel_name}') already declares it"
            )
        del table["subtree_remote"]
        plan.add("relocate-mirror", label, detail)


# ---------------------------------------------------------------------------
# Building and rendering
# ---------------------------------------------------------------------------


def workspace_file(repo) -> str:
    return os.path.join(repo, WORKSPACE_DIR, WORKSPACE_FILE)


def build_plan(repo: str, kind: RootKind) -> Plan:
    """Read the workspace and produce the migrated document plus its edit list.

    Nothing is written here: every refusal below happens before a byte changes.
    """
    path = workspace_file(repo)
    if not os.path.isfile(path):
        raise MigrationError(
            f"no workspace found: {path} does not exist. This migration applies "
            f"to monorepo workspaces; a standalone repository has nothing to "
            f"migrate."
        )
    with open(path, encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())

    if doc.get("releasables") is None:
        raise MigrationError(
            "workspace.toml has no [[releasables]] section: this is an "
            "implicit-mode workspace, and implicit mode is not migrated here. "
            "Converting one is a design decision per repository -- which "
            "releasables exist, which members belong to each, and what each one "
            "is tagged as -- not a mechanical edit. Take the deferred path: pin "
            f"rlsbl to {LAST_IMPLICIT_MODE_VERSION}, the last version that reads "
            "an implicit-mode workspace, and file a todo in this repository to "
            "convert it on its own schedule."
        )

    plan = Plan(repo=repo, path=path, doc=doc)
    releasables = Section(doc, "releasables")
    projects = Section(doc, "projects")

    _plan_root_member(plan, projects, releasables, kind)
    _plan_watch_keys(plan, projects)
    _plan_mirror_remotes(plan, projects, releasables)
    return plan


def render_plan(plan: Plan, out=None) -> None:
    """Print every edit, per file, in the order they were made."""
    out = sys.stdout if out is None else out
    print(f"Repository: {plan.repo}", file=out)
    print(f"  {os.path.relpath(plan.path, plan.repo)}", file=out)
    if not plan.edits:
        print("    (nothing to migrate: the workspace model is already declared)",
              file=out)
        return
    for edit in plan.edits:
        print(edit.render(), file=out)


def commit_message(plan: Plan) -> str:
    """One commit per run, naming the edits it carries."""
    counts = {}
    for edit in plan.edits:
        counts[edit.kind] = counts.get(edit.kind, 0) + 1
    parts = [f"{kind} x{n}" if n > 1 else kind for kind, n in sorted(counts.items())]
    return "Migrate workspace.toml to the ownership model: " + ", ".join(parts)


def apply_plan(plan: Plan) -> None:
    """Write the migrated document over the workspace file."""
    effects.atomic_write_text(plan.path, tomlkit.dumps(plan.doc))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(repo: str, out) -> int:
    """Load the migrated workspace; print any loader error verbatim.

    A remaining error is exactly the residue the script could not decide, so it
    is reported as the operator's list rather than paraphrased.
    """
    try:
        projects = load_workspace(repo)
        load_releasables(repo, projects)
    except (WorkspaceError, OSError, ValueError) as exc:
        print("\nOPERATOR INPUT REQUIRED: the migrated workspace does not load:",
              file=out)
        print(f"\n{exc}\n", file=out)
        print(
            "The mechanical edits above are complete; this is the part only you "
            "can decide.",
            file=out,
        )
        return 1
    print("\nThe migrated workspace loads: members and releasables both.", file=out)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    repo: str,
    *,
    root_kind: RootKind,
    dry_run: bool,
    auto_commit: bool,
    backfill: bool,
    use_gh: bool = True,
    out=None,
) -> int:
    out = sys.stdout if out is None else out
    try:
        plan = build_plan(repo, root_kind)
    except MigrationError as exc:
        print(f"error: {exc}", file=out)
        return 2

    render_plan(plan, out=out)

    if dry_run:
        print("\n--dry-run: nothing written.", file=out)
    elif plan.changed:
        apply_plan(plan)
        print(f"\nWrote {os.path.relpath(plan.path, repo)}.", file=out)
        if auto_commit:
            commit_files(
                commit_message(plan),
                [os.path.relpath(plan.path, repo)],
                autogenerated=True,
                cwd=repo,
            )

    status = 0
    if not plan.changed or not dry_run:
        # The file on disk is the migrated file (either it just got written, or
        # it already was one), so loading it answers for the real thing.
        status = verify(repo, out)
        loads = status == 0
    else:
        print(
            "\nThe loader verification runs on the real pass: under --dry-run "
            "the migrated file was never written.",
            file=out,
        )
        loads = False

    if backfill:
        if not loads:
            # The backfill enumerates a workspace's release scopes through the
            # loader, so it has nothing to read until the workspace loads.
            print(
                "\nThe release-anchor backfill is skipped: it reads the "
                "workspace through the loader, which this file does not pass "
                "yet. Re-run this pass once the residue above is resolved -- "
                "both halves are idempotent.",
                file=out,
            )
            return status
        print("\n--- release-anchor backfill ---\n", file=out)
        status = (
            _load_backfill().run(
                repo, dry_run=dry_run, use_gh=use_gh, auto_commit=auto_commit,
                out=out,
            )
            or status
        )
    return status


def _load_backfill():
    """The anchor backfill, imported from this checkout's scripts directory.

    In-process rather than as a subprocess: the pass already exposes exactly the
    entry this one needs (``run(repo, dry_run=..., out=...)``), it already
    imports rlsbl from this same checkout, and running it in-process keeps one
    output stream, one exit status, and one dry-run decision instead of two.
    """
    name = "backfill_release_anchors"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_SCRIPT_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate one workspace.toml to the ownership model and backfill its "
            "release anchors."
        ),
    )
    parser.add_argument(
        "--repo", default=".", help="repository to operate on (default: cwd)"
    )
    kind_group = parser.add_mutually_exclusive_group(required=True)
    kind_group.add_argument(
        "--root-dev-node",
        action="store_true",
        help=(
            "the root member is a dev node: files at the repository root that no "
            "other member claims need no changelog coverage"
        ),
    )
    kind_group.add_argument(
        "--root-releasable",
        metavar="NAME",
        help=(
            "the root member belongs to this releasable: files at the repository "
            "root that no other member claims get changelog coverage under it"
        ),
    )
    parser.add_argument(
        "--tag-format",
        metavar="FORMAT",
        help=(
            "required with --root-releasable: the tag format that releasable "
            "uses, e.g. 'v{version}' or '{name}@v{version}'"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the full plan and write nothing",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="write the migrated file but leave it uncommitted",
    )
    parser.add_argument(
        "--no-gh",
        action="store_true",
        help="passed to the anchor backfill: skip its GitHub Release lookups",
    )
    args = parser.parse_args(argv)

    try:
        kind = RootKind(
            dev_node=args.root_dev_node,
            releasable=args.root_releasable,
            tag_format=args.tag_format,
        )
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2
    return run(
        repo,
        root_kind=kind,
        dry_run=args.dry_run,
        auto_commit=not args.no_commit,
        backfill=True,
        use_gh=not args.no_gh,
    )


if __name__ == "__main__":
    sys.exit(main())

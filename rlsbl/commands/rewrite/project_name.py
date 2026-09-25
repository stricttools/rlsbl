"""Rename a standalone project's published identity.

``rlsbl rewrite project-name --from <old> --to <new>`` renames the identities
rlsbl owns, and only those:

* **The package name** in every target manifest whose target renames it itself
  (``package_rename = "manifest-field"`` on the target protocol), located
  through the targets' configured paths. Nothing else in the manifest is
  touched: not repository or homepage URLs, and not command names (npm ``bin``,
  PyPI ``[project.scripts]``), which the closing message lists instead.
* **The Go module path**, for a Go target: the current path's last element is
  replaced by ``--to``, and the move is performed by the module-path rewrite
  (:mod:`rlsbl.commands.rewrite.go_module_path`). The new path is derived only
  when that last element equals ``--from``.
* **The transition record**: one ``identity-transition`` event per changed
  identity. A ``package-name`` event from ``--from`` to ``--to``, and for a Go
  target a ``go-module-path`` event from the LAST PUBLISHED module path -- read
  from ``go.mod`` at the latest release's commit, which the release archives
  record -- to the new one. A latest release whose archive is marked
  unrecoverable records no commit to read it at, and is refused. The effective version is the version the next
  release ships, decided by the release flow's own
  :func:`~rlsbl.commands.release.validate.next_release_version`.

It moves no source directory and rewrites no source code beyond the Go import
sites the module-path rewrite owns. It contacts no registry, renames no
repository, deprecates nothing, and writes no changelog entry: the closing
message lists those steps for the caller.

Every refusal happens before anything is written. The rename is committed as
one commit and the record as a second, so a crash between them is completed by
re-running: a manifest that already declares ``--to`` counts as renamed, and
an event already recorded is not appended again.
"""

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

from ... import effects
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile
from ...targets import TARGETS
from ...targets.base import (
    PACKAGE_RENAME_GO_MODULE_PATH,
    PACKAGE_RENAME_MANIFEST_FIELD,
    PACKAGE_RENAME_UNSUPPORTED,
)
from . import go_module_path as gmp
from .abort import already_written

#: The facets this command records, as the transition-record schema spells them.
PACKAGE_NAME_FACET = "package-name"
GO_MODULE_FACET = "go-module-path"


class ProjectRenameError(Exception):
    """A refusal: something the caller must change before the rename can run."""


@dataclass
class ManifestStep:
    """One manifest-field target's manifest, as observed."""

    target: str
    rel: str
    plan: object  # rlsbl.targets.base.ManifestRenamePlan


@dataclass
class GoStep:
    """The Go module path move, as observed."""

    current: str
    new: str
    rel_dir: str

    @property
    def needed(self):
        return self.current != self.new


@dataclass
class RenamePlan:
    """Everything the rename will do, derived before anything is written."""

    root: str
    old: str
    new: str
    effective_version: str
    manifests: list = field(default_factory=list)
    renamed_already: list = field(default_factory=list)
    go: GoStep | None = None
    events: list = field(default_factory=list)
    recorded_already: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    source_dirs: list = field(default_factory=list)
    install_paths: list = field(default_factory=list)
    command_names: list = field(default_factory=list)
    record_path: str = ""


def rename_commit_message(old, new):
    """The rename commit's message, which a re-run looks the commit up by."""
    return f"rewrite: rename project {old} -> {new}"


# ---------------------------------------------------------------------------
# Observation: every refusal, before anything is written
# ---------------------------------------------------------------------------


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _refuse_in_workspace(root):
    from ...workspace import find_workspace_root

    ws_root = find_workspace_root(root)
    if ws_root is not None:
        raise ProjectRenameError(
            f"this project is inside a monorepo workspace ({ws_root}), and "
            f"`rlsbl rewrite project-name` renames a standalone project. In a "
            f"workspace, rename the releasable with `rlsbl monorepo "
            f"rename-releasable <old> <new>` instead."
        )


def _refuse_invalid_to(targets, old, new):
    if old == new:
        raise ProjectRenameError(
            f"--from and --to are the same name ('{old}'); there is nothing to "
            f"rename. Pass the new name as --to."
        )
    problems = []
    for name, (target, _path) in targets.items():
        if target.package_rename != PACKAGE_RENAME_UNSUPPORTED:
            for problem in target.package_name_problems(new):
                problems.append(f"  {name}: {problem}")
    if problems:
        raise ProjectRenameError(
            f"--to '{new}' is not a valid package name for every target this "
            f"project publishes to:\n" + "\n".join(problems) + "\n"
            f"Re-run with a --to that every one of them accepts."
        )


def _observe_names(root, targets, ctx, old, new, plan):
    """Classify every target's declared name; refuse on any mismatch."""
    mismatched = []
    unsupported_old = []
    go_declared = None
    for name, (target, path) in targets.items():
        if target.package_rename == PACKAGE_RENAME_MANIFEST_FIELD:
            step = target.package_rename_plan(path, old, new)
            rel = _rel(root, step.path)
            if step.occurrences:
                plan.manifests.append(ManifestStep(name, rel, step))
            elif step.current_name == new:
                plan.renamed_already.append(f"{rel} already declares \"{new}\"")
            else:
                mismatched.append((f"{rel} ({target.package_name_field})", step.current_name))
        elif target.package_rename == PACKAGE_RENAME_GO_MODULE_PATH:
            from ...utils import read_go_module_path

            module = read_go_module_path(path)
            if module is None:
                raise ProjectRenameError(
                    f"the Go target at {_rel(root, path) or '.'} has no readable "
                    f"go.mod module directive. Fix go.mod and re-run."
                )
            go_declared = (module, _rel(root, path))
        else:
            declared = target.read_name(path, ctx)
            if declared == old:
                unsupported_old.append(f"  {name}: {target.package_name_field}")

    if mismatched:
        declared = sorted({str(n) for _where, n in mismatched})
        lines = "\n".join(f"  {where} declares \"{n}\"" for where, n in mismatched)
        hint = (
            f"Re-run with --from {declared[0]}."
            if len(declared) == 1 else
            "Re-run with --from set to the name these manifests declare, after "
            "making them agree."
        )
        raise ProjectRenameError(
            f"--from '{old}' does not match the package name the manifests "
            f"declare:\n{lines}\n{hint}"
        )

    if go_declared is not None:
        module, rel_dir = go_declared
        last = module.rsplit("/", 1)[-1]
        if last == old:
            plan.go = GoStep(module, module[: -len(old)] + new, rel_dir)
        elif last == new:
            plan.go = GoStep(module, module, rel_dir)
        else:
            raise ProjectRenameError(
                f"go.mod declares module {module}, whose last element '{last}' "
                f"is not --from '{old}', so the new module path cannot be "
                f"derived from it. If '{last}' is the project's name, re-run "
                f"with --from {last}. Otherwise move the module by hand with "
                f"`rlsbl rewrite go-module-path --from-module {module} "
                f"--to-module <a path ending in /{new}>`, commit that, and "
                f"re-run."
            )

    if unsupported_old:
        raise ProjectRenameError(
            f"rlsbl does not rename the package name of these targets, and "
            f"each still declares '{old}':\n" + "\n".join(unsupported_old) + "\n"
            f"Edit each by hand to '{new}', commit, and re-run."
        )

    if not plan.manifests and not plan.renamed_already and plan.go is None:
        raise ProjectRenameError(
            "no target of this project has a package name rlsbl renames "
            "(npm, PyPI, or a Go module). Rename the project by hand."
        )


def _read_release_file(root):
    from ...errors import ReleaseFileError
    from ...release_file import get_release_file_path, read_release_file

    path = get_release_file_path(root)
    rel = _rel(root, path)
    if not os.path.isfile(path):
        raise ProjectRenameError(
            f"there is no release file at {rel}, so the version the new names "
            f"take effect from cannot be derived. Run `rlsbl release init`, "
            f"set bump and description in {rel}, commit it, and re-run."
        )
    try:
        return read_release_file(path)
    except ReleaseFileError as exc:
        raise ProjectRenameError(
            f"{rel} cannot be read, so the version the new names take effect "
            f"from cannot be derived: {exc}. Fix {rel}, commit it, and re-run."
        ) from exc


def _effective_version(root, targets, release_config):
    """The version the next release ships: the release flow's own decision."""
    from ..release.validate import is_first_release, next_release_version
    from ...release_file import get_releases_dir
    from ...release_record import version_is_archived
    from ...utils import local_tag_state

    registry = release_config.include[0] if release_config.include else None
    if registry not in targets:
        raise ProjectRenameError(
            f"the release file's include list starts with {registry!r}, which "
            f"is not one of this project's targets ({', '.join(targets)}). "
            f"Fix include in .rlsbl/releases/unreleased.toml and re-run."
        )
    target, path = targets[registry]
    current = target.read_version(path)
    released_before = version_is_archived(get_releases_dir(root), current)
    tag_state = local_tag_state(target.tag_format(current), cwd=root)
    version, _bump = next_release_version(
        current, release_config.bump, release_config.preid,
        first_release=is_first_release(released_before, tag_state),
    )
    return version


def _published_module_path(root, rel_dir, plan):
    """The module path the latest release published, or None (with a note)."""
    from ...release_file import get_releases_dir
    from ...release_record import latest_release_fact, read_entry
    from ...utils import run

    releases_dir = get_releases_dir(root)
    fact = latest_release_fact(releases_dir, cwd=root)
    if fact.version is None:
        plan.notes.append(
            "no go-module-path event: this project has never released, so no "
            "Go module path was ever published."
        )
        return None
    if fact.unrecoverable:
        # The refusal names no command: nothing can establish honestly which
        # commit an unrecoverable release shipped from.
        raise ProjectRenameError(
            f"the latest release, {fact.version}, is marked unrecoverable in "
            f"its archive, so rlsbl's record cannot establish the Go module "
            f"path that release published, and the go-module-path event this "
            f"rename must record cannot be written."
        )
    entry = read_entry(releases_dir, fact.version, cwd=root)
    go_mod = "go.mod" if rel_dir in ("", ".") else f"{rel_dir}/go.mod"
    try:
        text = run("git", ["show", f"{entry.candidate_sha}:{go_mod}"], cwd=root)
    except subprocess.CalledProcessError:
        plan.notes.append(
            f"no go-module-path event: the latest release, {fact.version}, "
            f"had no {go_mod}, so it published no Go module."
        )
        return None
    for line in text.splitlines():
        stripped = line.split("//", 1)[0].strip()
        if stripped.startswith("module "):
            return stripped[len("module "):].strip()
    plan.notes.append(
        f"no go-module-path event: {go_mod} at the latest release, "
        f"{fact.version}, declares no module."
    )
    return None


def _plan_events(root, plan, has_manifest_target):
    from ...transition_record import (
        KIND_IDENTITY_TRANSITION,
        IdentityTransitionEvent,
        TransitionRecordError,
        read_events,
        repository_transition_record_path,
    )

    plan.record_path = repository_transition_record_path(root)
    wanted = []
    if has_manifest_target:
        wanted.append((PACKAGE_NAME_FACET, plan.old, plan.new))
    if plan.go is not None:
        published = _published_module_path(root, plan.go.rel_dir, plan)
        if published is not None and published == plan.go.new:
            plan.notes.append(
                f"no go-module-path event: the latest release already "
                f"published {published}."
            )
        elif published is not None:
            wanted.append((GO_MODULE_FACET, published, plan.go.new))

    try:
        existing = read_events(plan.record_path, kinds=[KIND_IDENTITY_TRANSITION])
    except TransitionRecordError as exc:
        raise ProjectRenameError(
            f"{_rel(root, plan.record_path)} cannot be read ({exc}). Repair the "
            f"line it names and re-run."
        ) from exc
    recorded = {(e.facet, e.old, e.new) for e in existing}
    for facet, old, new in wanted:
        if (facet, old, new) in recorded:
            plan.recorded_already.append(f"{facet} {old} -> {new}")
            continue
        plan.events.append(IdentityTransitionEvent(
            facet=facet, old=old, new=new,
            effective_version=plan.effective_version,
        ))


def _plan_remaining_steps(root, config, targets, plan):
    """Source directories and command names that still carry the old name."""
    old = plan.old
    for pipeline in (config.get("pipelines") or {}).values():
        if not isinstance(pipeline, dict):
            continue
        for entry in pipeline.get("install_paths") or []:
            parts = [p for p in str(entry).split("/") if p not in ("", ".")]
            if old in parts:
                plan.install_paths.append(entry)
                plan.source_dirs.append("/".join(parts))

    for _name, (target, path) in targets.items():
        for full in target.package_source_dirs(path, old):
            plan.source_dirs.append(_rel(root, full))
        for manifest, entry in target.package_command_names(path, old):
            plan.command_names.append(
                f"{_rel(root, manifest)} {entry} still names the old command"
            )


def prepare(root, old, new):
    """Derive the whole rename, refusing before anything is written."""
    from ...commands.release.validate import blocking_dirty_paths
    from ...member_context import resolve_member_context

    root = str(root)
    _refuse_in_workspace(root)

    member = resolve_member_context(root)
    targets = {
        name: (TARGETS[name], path) for name, path in member.target_paths.items()
    }
    _refuse_invalid_to(targets, old, new)

    dirty = blocking_dirty_paths(cwd=root)
    if dirty:
        raise ProjectRenameError(
            "the working tree has uncommitted changes:\n"
            + "\n".join(f"  {p}" for p in dirty)
            + "\nCommit them (safegit commit) or remove them, then re-run."
        )

    release_config = _read_release_file(root)
    plan = RenamePlan(
        root=root, old=old, new=new,
        effective_version=_effective_version(root, targets, release_config),
    )
    ctx = SimpleNamespace(config=member.config, project_root=root)
    _observe_names(root, targets, ctx, old, new, plan)
    has_manifest_target = any(
        t.package_rename == PACKAGE_RENAME_MANIFEST_FIELD for t, _p in targets.values()
    )
    _plan_events(root, plan, has_manifest_target)
    _plan_remaining_steps(root, member.config, targets, plan)
    return plan


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


def _event_summary(event):
    return (
        f"{event.facet} {event.old} -> {event.new}, "
        f"effective {event.effective_version}"
    )


def observe(plan):
    """The per-file, per-event plan. Runs under the no-writes guard."""
    items = []
    for step in plan.manifests:
        items.append(VerdictItem(
            key=step.rel, state="rewrite",
            summary=f"1 occurrence in this {step.target} manifest",
            facts=(f'package name "{plan.old}" -> "{plan.new}"',),
            actions=("apply would rewrite 1 occurrence here.",),
            data=("manifest", step),
        ))
    for line in plan.renamed_already:
        items.append(VerdictItem(
            key=line.split(" ", 1)[0], state="already_renamed", summary=line,
        ))
    go_files = 0
    if plan.go is not None and plan.go.needed:
        for item in gmp.observe(plan.root, plan.go.current, plan.go.new).items:
            if item.data is None:
                continue
            go_files += 1
            items.append(VerdictItem(
                key=item.key, state=item.state, summary=item.summary,
                facts=item.facts, actions=item.actions, data=("module-path-file", item),
            ))
    elif plan.go is not None:
        items.append(VerdictItem(
            key="(go module)", state="already_renamed",
            summary=f"go.mod already declares {plan.go.new}",
        ))
    rewrites = len(plan.manifests) + go_files
    if rewrites:
        items.append(VerdictItem(
            key="(rename commit)", state="commit",
            summary=f"commit the {rewrites} rewritten file(s) as one commit: "
                    f"'{rename_commit_message(plan.old, plan.new)}'",
            data=("commit-rename", None),
        ))
    record_rel = _rel(plan.root, plan.record_path)
    for event in plan.events:
        items.append(VerdictItem(
            key=f"{record_rel} {event.facet}", state="record",
            summary=_event_summary(event),
        ))
    for line in plan.recorded_already:
        items.append(VerdictItem(
            key=f"{record_rel} {line.split(' ', 1)[0]}", state="already_recorded",
            summary=line,
        ))
    for i, note in enumerate(plan.notes, start=1):
        items.append(VerdictItem(key=f"(note {i})", state="note", summary=note))
    if plan.events:
        items.append(VerdictItem(
            key="(record commit)", state="commit",
            summary=f"append {len(plan.events)} identity-transition event(s) "
                    f"to {record_rel} and commit them",
            data=("record", None),
        ))
    items.append(VerdictItem(
        key="(total)", state="summary",
        summary=f"{rewrites} file(s) to rewrite and {len(plan.events)} event(s) "
                f"to record: {plan.old} -> {plan.new}, effective "
                f"{plan.effective_version}",
    ))
    return Preview(items)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def record_identity_transitions(root, record_path, events, old, new):
    """Append the events to the transition record and commit them."""
    from ...transition_record import append_events
    from ...utils import commit_files_if_changed

    append_events(record_path, events)
    commit_files_if_changed(
        f"rewrite: record the {old} -> {new} identity transitions",
        [_rel(root, record_path)],
        skip_message="identity transitions already committed; nothing to commit.",
        cwd=root,
    )


def _apply_manifest(step, plan, applied):
    target = TARGETS[step.target]
    fresh = target.package_rename_plan(
        os.path.dirname(step.plan.path), plan.old, plan.new,
    )
    if fresh.occurrences != step.plan.occurrences:
        raise ProjectRenameError(
            f"{step.rel}: the preview counted {step.plan.occurrences} "
            f"occurrence(s) but the file now has {fresh.occurrences}. The "
            f"working tree changed between the preview and the apply. "
            f"{already_written(applied)}"
            f"Re-run with --dry-run: the re-plan reads the tree as it is now."
        )
    effects.atomic_write_text(step.plan.path, fresh.new_text, preserve_mode=True)
    applied.append(step.rel)
    print(f"  {step.rel}: rewrote 1 occurrence")


def apply_item(item, plan, applied):
    from ...utils import commit_files_if_changed

    if item.data is None:
        return
    kind, payload = item.data
    if kind == "manifest":
        _apply_manifest(payload, plan, applied)
    elif kind == "module-path-file":
        gmp.apply_item(payload, plan.go.current, plan.go.new, applied=applied)
    elif kind == "commit-rename":
        # Not autogenerated: the rename changes the published identity, so
        # changelog coverage asks for the breaking entry the closing message
        # prints. Only the record commit below is bookkeeping.
        commit_files_if_changed(
            rename_commit_message(plan.old, plan.new), list(applied),
            skip_message="rename already committed; nothing to commit.",
            autogenerated=False,
            cwd=plan.root,
        )
    elif kind == "record":
        record_identity_transitions(
            plan.root, plan.record_path, plan.events, plan.old, plan.new,
        )


# ---------------------------------------------------------------------------
# The closing message
# ---------------------------------------------------------------------------


def _rename_commit(root, old, new):
    """The rename commit's hash, found by its message; None before it exists."""
    from ...utils import run

    try:
        out = run("git", [
            "log", "-1", "--format=%H", "--fixed-strings",
            f"--grep={rename_commit_message(old, new)}",
        ], cwd=root)
    except subprocess.CalledProcessError:
        return None
    return out.strip() or None


def remaining_steps(plan, rename_sha):
    """What the caller does next, in order."""
    steps = []
    if plan.source_dirs:
        steps.append(
            "Move the source directories whose paths carry the old name: "
            + ", ".join(plan.source_dirs) + "."
        )
        steps.append("Update the code that imports them to the new paths.")
    else:
        steps.append(
            f"No source directory named '{plan.old}' was found; move any "
            f"whose path carries the old name, and update the code that "
            f"imports it."
        )
    if plan.install_paths:
        steps.append(
            "Update install_paths in .rlsbl/config.json: "
            + ", ".join(plan.install_paths) + "."
        )
    steps.append(
        "Run `rlsbl scaffold` to regenerate the files that follow the source "
        "layout and the module path."
    )
    for line in plan.command_names:
        steps.append(f"Decide the command name: {line}.")
    steps.append(
        f"Rename the GitHub repository to match '{plan.new}' (rlsbl does not "
        f"rename repositories)."
    )
    description = (
        f"Renamed from {plan.old} to {plan.new}: releases from "
        f"{plan.effective_version} are published as {plan.new}"
    )
    if plan.go is not None:
        description += f", and the Go module path is {plan.go.new}"
    commits = rename_sha[:12] if rename_sha else "<the rename commit>"
    steps.append(
        "Add a breaking changelog entry for the rename: rlsbl changelog add "
        f"--commits {commits} --type breaking --description "
        f"{shlex.quote(description + '.')}"
    )
    return steps


def _print_closing(plan, *, dry_run):
    rename_sha = None if dry_run else _rename_commit(plan.root, plan.old, plan.new)
    if not dry_run:
        for event in plan.events:
            print(f"Recorded {_event_summary(event)}.")
        for line in plan.recorded_already:
            print(f"Already recorded: {line}.")
        for note in plan.notes:
            print(f"Note: {note}")
    print("\nRemaining steps, in order:")
    for i, step in enumerate(remaining_steps(plan, rename_sha), start=1):
        print(f"  {i}. {step}")


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def cmd_project_name(flags, project_root):
    """``rlsbl rewrite project-name`` -- rename a standalone project's identity.

    ``flags["from"]`` / ``flags["to"]`` -- the package names.
    ``flags["dry-run"]``                -- plan only.
    """
    from ..release.validate import ReleaseValidationError
    from ...errors import RlsblError
    from ...lint.tree_walk import SourceParseError
    from ...lint.utils import SourceWalkError

    old = flags["from"]
    new = flags["to"]
    dry_run = bool(flags.get("dry-run", False))
    handled = (
        ProjectRenameError, gmp.GoModuleRewriteError, SourceParseError,
        SourceWalkError, RlsblError, ReleaseValidationError,
    )

    try:
        plan = prepare(project_root, old, new)
    except handled as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    applied = []
    reconciler = Reconciler(
        observe=lambda: observe(plan),
        apply_item=lambda item: apply_item(item, plan, applied),
        show_keys=True,
    )
    try:
        reconcile(reconciler, dry_run=dry_run)
    except handled as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not dry_run:
        print(f"Renamed {old} -> {new} in {len(applied)} file(s).")
    _print_closing(plan, dry_run=dry_run)

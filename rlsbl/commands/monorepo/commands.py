"""Monorepo workspace management commands: init, add, remove, list, status, outdated, and check-names."""

import os
import sys
import time

from ...ownership import OwnershipScope
from ...utils import commit_files
from ...workspace import find_workspace_root, load_workspace, save_workspace, WorkspaceProject, WORKSPACE_DIR, WORKSPACE_FILE
from ...workspace_graph import WorkspaceGraph
from ...targets import detect_targets, resolve_releasable_config_dir, TARGETS, TargetEntry


def _cmd_init(flags, project_root):
    root_dir = str(project_root)
    ws_file = os.path.join(root_dir, WORKSPACE_DIR, WORKSPACE_FILE)
    if os.path.isfile(ws_file):
        print("Error: Workspace already initialized.", file=sys.stderr)
        sys.exit(1)

    from ...ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH
    from ...workspace import Releasable

    # Every workspace has a root member, and its KIND is a decision only the
    # operator can make: a dev node whose root files need no changelog
    # coverage, or a member of a named releasable whose root files do.
    root_releasable = flags.get("root-releasable") or None
    root_tag_format = flags.get("root-tag-format") or None
    root_dev_node = bool(flags.get("root-dev-node"))

    if not root_dev_node and not root_releasable:
        print(
            "Error: the root member's kind must be declared. Every workspace "
            "declares the repository root as a member, and it is either:\n"
            "  --root-dev-node                  a dev node -- root files are "
            "exempt from changelog coverage\n"
            "  --root-releasable <name> --tag-format <fmt>\n"
            "                                   a member of a named releasable "
            "-- root files get changelog coverage,\n"
            "                                   and the releasable's tags use "
            "the format you name\n"
            "There is no default: which one a repository wants depends on "
            "whether its root files ship to users.",
            file=sys.stderr,
        )
        sys.exit(1)

    if root_releasable and not root_tag_format:
        print(
            "Error: --tag-format is required with --root-releasable. A "
            "releasable that owns the repository root must never inherit a "
            "default tag format: pass \"v{version}\" for bare version tags, or "
            "\"{name}@v{version}\" for the workspace scheme.",
            file=sys.stderr,
        )
        sys.exit(1)

    root_config = None
    if root_releasable:
        root_config = _root_releasable_config(
            root_dir,
            flags.get("root-publish-mode") or None,
            flags.get("root-publish-gate-check-regex") or None,
        )

    root_member = {"path": ROOT_MEMBER_PATH, "name": ROOT_MEMBER_NAME}
    if root_releasable:
        root_member["releasable"] = root_releasable
        releasables = [Releasable(name=root_releasable, tag_format=root_tag_format)]
    else:
        # A dev-node root member is both: dev_only (nothing user-facing may
        # depend on it) and outside every releasable (it is never released).
        root_member["dev_only"] = True
        root_member["releasable"] = False
        releasables = []

    ws_dir = os.path.join(root_dir, WORKSPACE_DIR)
    ws_dir_existed = os.path.isdir(ws_dir)
    save_workspace(root_dir, [root_member], releasables=releasables)
    written = [os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)]
    if root_config is not None:
        written.append(_write_root_releasable_config(root_dir, root_releasable, root_config))
    print("Initialized monorepo workspace in .rlsbl-monorepo/")
    if root_releasable:
        print(
            f"Root member '{ROOT_MEMBER_NAME}' belongs to releasable "
            f"'{root_releasable}' (tag format: {root_tag_format}). Its config "
            f"is {written[-1]}."
        )
    else:
        print(f"Root member '{ROOT_MEMBER_NAME}' is a dev node.")

    if not flags.get("auto-commit", True):
        print(
            "Skipped commit (--no-auto-commit). Run "
            f"`safegit commit -- {' '.join(written)}` manually."
        )
        return

    # The commit is the init's last step, and a failed one is fatal: an
    # uncommitted workspace.toml is a workspace nothing records, which the
    # next init refuses as "already initialized". Everything this command
    # wrote is removed again, so a re-run starts from where this one did.
    try:
        commit_files("monorepo: init workspace", written, cwd=root_dir)
    except Exception as exc:  # noqa: BLE001 -- every failure stops the init
        detail = (getattr(exc, "stderr", None) or "").strip()
        _remove_init_output(root_dir, written, ws_dir if not ws_dir_existed else None)
        print(
            f"Error: committing the workspace failed ({detail or exc}), so the "
            f"workspace is not initialized: {', '.join(written)} "
            "were removed again. Fix what the commit reports, then re-run "
            "this `rlsbl monorepo init`.",
            file=sys.stderr,
        )
        sys.exit(1)


def _root_releasable_config(root_dir, publish_mode, gate_regex):
    """The config a root releasable starts with, or a refusal naming the flag.

    A workspace's root member has no ``.rlsbl/`` of its own (the
    ``root-rlsbl-conflict`` check refuses one beside ``.rlsbl-monorepo/``), and
    ``rlsbl scaffold`` does not scaffold the workspace root, so its config
    lives in its releasable's ``config.json`` and this init writes it. The
    publish mode has no default, as everywhere else. A root that publishes
    (publish mode "ci" and a detected release target) gets its targets and
    their default pipelines, and must name the check runs publishing waits
    for: its CI is hand-authored, so rlsbl cannot infer them.
    """
    import re

    from ...config import PUBLISH_MODES
    from ...commands.init_cmd import default_pipeline_entry
    from ...pipelines import PIPELINE_TYPES

    if publish_mode not in PUBLISH_MODES:
        print(
            "Error: --publish-mode is required with --root-releasable, and "
            f"must be one of {sorted(PUBLISH_MODES)}: \"ci\" publishes the "
            "root member's targets from CI, \"none\" publishes nothing. "
            "There is no default.",
            file=sys.stderr,
        )
        sys.exit(1)
    if gate_regex is not None:
        try:
            re.compile(gate_regex)
        except re.error as exc:
            print(
                f"Error: --publish-gate-check-regex {gate_regex!r} is not a "
                f"valid regular expression ({exc}).",
                file=sys.stderr,
            )
            sys.exit(1)

    config = {"publish_mode": publish_mode}
    if publish_mode == "ci":
        entries = [e for e in detect_targets(root_dir) if e.name in TARGETS]
        names = list(dict.fromkeys(e.name for e in entries))
        if names:
            config["targets"] = names
            pipelines = {}
            for entry in entries:
                if entry.name in PIPELINE_TYPES and entry.name not in pipelines:
                    pipelines[entry.name] = default_pipeline_entry(
                        entry.name, lambda path=entry.path: path,
                    )
            if pipelines:
                config["pipelines"] = pipelines
            if gate_regex is None:
                from .publish_inline import ROOT_GATE_REGEX_EXAMPLE

                print(
                    "Error: --publish-gate-check-regex is required with "
                    "--root-releasable and --publish-mode ci when the root "
                    f"publishes (release targets detected at the root: "
                    f"{', '.join(names)}). Publishing waits for the root "
                    "package's CI check runs on the release commit, and that "
                    "CI is hand-authored, so rlsbl cannot infer their names. "
                    "Pass the regex that matches them, e.g. "
                    f"--publish-gate-check-regex '{ROOT_GATE_REGEX_EXAMPLE}'.",
                    file=sys.stderr,
                )
                sys.exit(1)
    if gate_regex is not None:
        config["publish_gate_check_regex"] = gate_regex
    return config


def _write_root_releasable_config(root_dir, releasable, config):
    """Write the root releasable's ``config.json``; return its relative path."""
    import json

    from ...workspace import get_releasable_dir
    from ... import effects

    rel_dir = get_releasable_dir(root_dir, releasable)
    effects.makedirs(rel_dir, exist_ok=True)
    path = os.path.join(rel_dir, "config.json")
    effects.atomic_write_text(path, json.dumps(config, indent=2) + "\n")
    return os.path.relpath(path, root_dir)


def _remove_init_output(root_dir, written, created_ws_dir):
    """Remove what a failed `monorepo init` wrote.

    *created_ws_dir* is the ``.rlsbl-monorepo/`` directory when this init
    created it (removed whole), else None (only the written files go).
    """
    from ...saferm import saferm_delete

    reason = "rolling back a monorepo init whose commit failed"
    if created_ws_dir is not None:
        saferm_delete(created_ws_dir, description=reason, recursive=True)
        return
    for rel in written:
        saferm_delete(os.path.join(root_dir, rel), description=reason, skip_missing=True)


def _create_releasable(name, tag_format_flag, target_entries, path):
    """The Releasable an add naming an undeclared group creates.

    Its ``tag_format`` is written out explicitly, either as the operator stated
    it (``--tag-format``) or as the member's primary target implies -- the same
    derivation ``monorepo absorb`` uses for its auto-singleton
    (:func:`rlsbl.tag_glob.derive_releasable_tag_format`), so a member's targets
    imply one format with one answer whichever command creates the releasable.

    The root member's releasable is never created here: a workspace this
    command can load already declares a root member, so an add naming ``.`` is
    refused as a path the workspace already claims. Creating it is
    ``monorepo init --root-releasable``'s job, and that is where the format is
    required to be stated.
    """
    from ...errors import MixedTagSchemeError
    from ...tag_glob import derive_releasable_tag_format
    from ...workspace import Releasable

    if tag_format_flag:
        return Releasable(name=name, tag_format=tag_format_flag)

    try:
        tag_format = derive_releasable_tag_format(
            target_entries, name, path, subject=f"member dir '{path}'",
        )
    except MixedTagSchemeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    return Releasable(name=name, tag_format=tag_format)


def _cmd_add(args, flags, project_root, dry_run=False):
    if not args:
        print("Error: Usage: rlsbl monorepo add <path> [--name <name>]", file=sys.stderr)
        sys.exit(1)

    path = args[0]
    from ...ownership import member_path_problem

    problem = member_path_problem(path)
    if problem is not None:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)

    # A member path is repository-relative, so it is resolved from the
    # workspace root, never from wherever the command happens to run.
    root = find_workspace_root(str(project_root))
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)
    abs_path = os.path.join(root, path)
    if not os.path.isdir(abs_path):
        print(
            f"Error: '{path}' is not a directory under the workspace root "
            f"{root}.",
            file=sys.stderr,
        )
        sys.exit(1)

    explicit_target = flags.get("target")
    if explicit_target:
        if explicit_target not in TARGETS:
            print(f"Error: Unknown target '{explicit_target}'.", file=sys.stderr)
            valid = ", ".join(sorted(TARGETS))
            print(f"Valid targets: {valid}", file=sys.stderr)
            sys.exit(1)
        target_entries = [TargetEntry(name=explicit_target, path=abs_path)]
    else:
        target_entries = detect_targets(abs_path)
        if not target_entries:
            print(f"Error: No release target detected in '{path}'. Initialize a project first.", file=sys.stderr)
            print("Hint: create a project manifest (e.g., package.json, pyproject.toml, go.mod, version.json) in the directory.", file=sys.stderr)
            sys.exit(1)

    from ...ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH
    from ...workspace import is_root_path

    adding_root = is_root_path(path)
    if adding_root:
        path = ROOT_MEMBER_PATH
        name = flags.get("name") or ROOT_MEMBER_NAME
        if name != ROOT_MEMBER_NAME:
            print(
                f"Error: the root member (path \".\") is named "
                f"'{ROOT_MEMBER_NAME}' and nothing else -- job keys, router "
                f"filters and check regexes are derived from that name. Drop "
                f"--name, or pass --name {ROOT_MEMBER_NAME}.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        name = flags.get("name") or os.path.basename(path.rstrip("/"))
        if name == ROOT_MEMBER_NAME:
            print(
                f"Error: '{ROOT_MEMBER_NAME}' is reserved for the member that "
                f"owns the repository root (path \".\"). Choose a different "
                f"--name for the member at '{path}'.",
                file=sys.stderr,
            )
            sys.exit(1)

    depends_on_raw = flags.get("depends-on")
    library_raw = flags.get("library")
    dev_only_raw = flags.get("dev_only")
    releasable_raw = flags.get("releasable")
    registry_name = flags.get("registry-name") or ""
    tag_format_flag = flags.get("tag-format") or ""

    # Parse --library as boolean
    library = None
    if library_raw is not None:
        if library_raw == "true":
            library = True
        elif library_raw == "false":
            library = False
        else:
            print(f"Error: --library must be 'true' or 'false', got '{library_raw}'.", file=sys.stderr)
            sys.exit(1)

    # Parse --dev-only as boolean
    dev_only = None
    if dev_only_raw is not None:
        if dev_only_raw == "true":
            dev_only = True
        elif dev_only_raw == "false":
            dev_only = False
        else:
            print(f"Error: --dev-only must be 'true' or 'false', got '{dev_only_raw}'.", file=sys.stderr)
            sys.exit(1)

    # Parse --releasable as string name or "false"
    releasable_value = None  # None means "not set" (omit from project)
    if releasable_raw is not None:
        if releasable_raw == "false":
            releasable_value = False
        elif releasable_raw:
            releasable_value = releasable_raw
        # Empty string means flag not passed (default="")

    projects = load_workspace(root)

    for proj in projects:
        if proj["path"] == path:
            print(f"Error: Project at '{path}' already exists in workspace.", file=sys.stderr)
            sys.exit(1)
        if proj["name"] == name:
            print(f"Error: Project named '{name}' already exists in workspace.", file=sys.stderr)
            sys.exit(1)

    # Every workspace declares its releasables, so --releasable is required
    from ...workspace import load_releasables
    if releasable_value is None:
        print(
            "Error: --releasable is required: every workspace declares its "
            "releasables in [[releasables]]. "
            "Use --releasable <name> or --releasable false.",
            file=sys.stderr,
        )
        sys.exit(1)

    # A releasable this add NAMES but the workspace does not declare is created
    # here, as the auto-singleton `monorepo absorb` creates for an arriving
    # member: one [[releasables]] entry, with its tag_format written out
    # explicitly rather than inherited by accident. A name the workspace already
    # declares is joined, and that releasable already owns its format.
    releasables = None
    created_releasable = None
    if isinstance(releasable_value, str):
        releasables = load_releasables(root, projects)
        defined_names = {r.name for r in releasables}
        if releasable_value in defined_names:
            if tag_format_flag:
                declared = next(
                    r for r in releasables if r.name == releasable_value
                ).effective_tag_format
                print(
                    f"Error: --tag-format applies only to the releasable this "
                    f"command creates. Releasable '{releasable_value}' already "
                    f"exists and declares its own tag format ('{declared}'); "
                    f"change it in workspace.toml if it is wrong.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            created_releasable = _create_releasable(
                releasable_value, tag_format_flag, target_entries, path,
            )
            releasables = list(releasables) + [created_releasable]
    elif tag_format_flag:
        print(
            "Error: --tag-format is the format of the releasable this command "
            "creates, and --releasable false creates none -- the member opts "
            "out of versioning entirely. Drop --tag-format, or name the "
            "releasable this member belongs to.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate --depends-on against existing project names
    depends_on = None
    if depends_on_raw:
        depends_on = [d.strip() for d in depends_on_raw.split(",")]
        existing_names = {proj["name"] for proj in projects}
        for dep_name in depends_on:
            if dep_name not in existing_names:
                print(f"Error: Dependency '{dep_name}' does not exist in workspace.", file=sys.stderr)
                sys.exit(1)

    project = {"path": path, "name": name}
    if depends_on:
        project["depends_on"] = depends_on
    if library is True:
        project["library"] = True
    if dev_only is True:
        project["dev_only"] = True
    if releasable_value is not None:
        project["releasable"] = releasable_value
    if registry_name:
        project["registry_name"] = registry_name

    # The sync this add runs refuses a root publisher that names no check
    # regex; refuse before anything is written, with the same fix.
    from .publish_inline import root_publisher_check_regex_problem

    problem = root_publisher_check_regex_problem(root)
    if problem is not None:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)

    # Honest plan boundary: validation above has fully run. In dry-run we report
    # exactly what would happen and make ZERO mutations -- no workspace write, no
    # scaffold, no sync. Anything below this point mutates state.
    if dry_run:
        print(f"Would add project '{name}' at {path}")
        print(f"  workspace.toml entry: {project}")
        if created_releasable is not None:
            print(
                f"  Would create releasable '{created_releasable.name}' "
                f"(tag format: {created_releasable.tag_format})"
            )
        project_rlsbl = os.path.join(abs_path, ".rlsbl", "config.json")
        if not os.path.exists(project_rlsbl):
            print(f"  Would scaffold '{name}' (no .rlsbl/config.json present)")
        else:
            print(f"  Would skip scaffold ('{name}' already scaffolded)")
        print("  Would run: rlsbl monorepo sync (regenerate CI workflows)")
        return

    no_commit = not flags.get("auto-commit", True)
    ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)
    ws_path = os.path.join(root, ws_file)

    # The registration is written first, because `rlsbl scaffold` in a
    # directory the workspace does not declare writes a standalone project's
    # layout instead of a member's. Its bytes before this command are kept so
    # a failed scaffold or sync restores them: the member is never left
    # registered without the scaffold and the sync it depends on.
    with open(ws_path, "r", encoding="utf-8", newline="") as f:
        workspace_before = f.read()

    projects.append(project)
    # ``releasables`` is the full desired list only when this add created one;
    # otherwise it stays None so the existing section is preserved untouched.
    save_workspace(
        root, projects, releasables=releasables if created_releasable else None,
    )
    print(f"Added project '{name}' at {path}")
    if created_releasable is not None:
        print(
            f"Created releasable '{created_releasable.name}' "
            f"(tag format: {created_releasable.tag_format}). Its state "
            f"directory is scaffolded by the `rlsbl monorepo sync` below."
        )

    registered = False
    try:
        # Auto-scaffold if not already scaffolded
        project_rlsbl = os.path.join(abs_path, ".rlsbl", "config.json")
        if not os.path.exists(project_rlsbl):
            print(f"Scaffolding {name}...")
            # -P: suppress CWD injection from ``python -m`` run in a foreign dir
            # (a root module shadowing a stdlib/dep name would break rlsbl imports).
            # No confirmation-skip flag: `scaffold` is `mutating` but not
            # `consequential`, so strictcli never prompts for it.
            cmd = [sys.executable, "-P", "-m", "rlsbl", "scaffold"]
            if explicit_target:
                cmd.extend(["--target", explicit_target])
            if no_commit:
                cmd.append("--no-auto-commit")
            _run_add_child(cmd, abs_path, f"`rlsbl scaffold` in {path}", name)

        # Sync CI workflows
        sync_cmd = [sys.executable, "-P", "-m", "rlsbl", "monorepo", "sync"]
        if no_commit:
            sync_cmd.append("--no-auto-commit")
        _run_add_child(sync_cmd, root, "`rlsbl monorepo sync`", name)

        if no_commit:
            print(f"Skipped commit (--no-auto-commit). Run `safegit commit -- {ws_file}` manually.")
        else:
            try:
                commit_files(f"monorepo: add {name}", [ws_file], cwd=root)
            except Exception as exc:  # noqa: BLE001 -- every failure stops the add
                detail = (getattr(exc, "stderr", None) or "").strip()
                _add_step_failed(
                    f"committing {ws_file} failed ({detail or exc})", name,
                )
        registered = True
    finally:
        if not registered:
            effects.atomic_write_text(ws_path, workspace_before, preserve_mode=True)


def _run_add_child(cmd, cwd, what, name):
    """Run one of `monorepo add`'s child commands; stop the add if it fails.

    The child's own output streams through, so its error is already printed
    when this refuses. The caller restores workspace.toml.
    """
    result = effects.run(cmd, cwd=cwd, check=False)
    if effects.unsettled(result) or result.returncode == 0:
        return
    _add_step_failed(
        f"{what} failed (exit {result.returncode}; its error is above)", name,
    )


def _add_step_failed(failure, name):
    """Stop `monorepo add` after a failed step; the caller restores workspace.toml."""
    print(
        f"Error: {failure}, so member '{name}' is not added: "
        f"{os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)} is restored to what it "
        f"was before this command. Files the scaffold and the sync already "
        f"wrote stay, and register nothing. Fix what it reports, then re-run "
        f"this `rlsbl monorepo add`.",
        file=sys.stderr,
    )
    sys.exit(1)


def _cmd_remove(args, flags, project_root):
    if not args:
        print("Error: Usage: rlsbl monorepo remove <path>", file=sys.stderr)
        sys.exit(1)

    path = args[0]

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    # A member path has one spelling (the one workspace.toml holds), so the
    # match is exact: `web/` is not the member at `web`.
    new_projects = [p for p in projects if p["path"] != path]

    if len(new_projects) == len(projects):
        known = ", ".join(f"'{p['path']}' ({p['name']})" for p in projects)
        print(
            f"Error: no member at '{path}'. The workspace's members, by path: "
            f"{known or '(none)'}. Pass one of those paths as written.",
            file=sys.stderr,
        )
        sys.exit(1)

    save_workspace(root, new_projects)
    print(f"Removed project at {path}")


#: The member flags a listing renders, each with the word it is shown as.
#: Derived facts (``dev_node``) are deliberately absent: the row shows what
#: the member DECLARES, and "dev-only outside every releasable" is already
#: readable from the two columns beside each other.
_LISTED_MEMBER_FLAGS = (
    ("library", "library"),
    ("dev_only", "dev-only"),
    ("test_only", "test-only"),
)


def _listed_releasable(proj):
    """How a member's releasable membership renders in a listing."""
    value = proj.get("releasable")
    if value is False:
        return "false"
    if isinstance(value, str) and value:
        return value
    return "--"


def _cmd_list(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    if not projects:
        print("No projects in workspace.")
        return

    # Name and path alone answer almost nothing: which releasable a member is
    # versioned under, and what kind of member it is, are the two facts every
    # other monorepo command branches on.
    rows = []
    for proj in projects:
        flags_shown = ", ".join(
            word for key, word in _LISTED_MEMBER_FLAGS if proj.get(key)
        )
        rows.append((
            proj["name"], proj["path"], _listed_releasable(proj), flags_shown,
        ))

    headers = ("Name", "Path", "Releasable", "Flags")
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in rows))
        for i in range(3)
    ]

    def _render(cells):
        padded = "  ".join(cells[i].ljust(widths[i]) for i in range(3))
        return f"{padded}  {cells[3]}".rstrip()

    print(_render(headers))
    for row in rows:
        print(_render(row))


def _latest_release_for_row(changes_dir, tag_glob):
    """The release a status row reports, from that row's own RELEASE RECORD.

    Returns ``(fact, release commit)``: the project's latest archived release as a
    displayable fact (annotated when this checkout does not contain it), and
    the highest archived release this checkout DOES contain, which is what
    bounds the coverage range. The two differ exactly when the checkout
    predates a release, and the table shows both rather than collapsing them.

    This used to be ``git tag -l <glob> --sort=-v:refname``, which reported
    whatever the tag namespace happened to hold.
    """
    from ...release_record import (
        latest_release_fact,
        nearest_release_commit,
        releases_dir_for_changes_dir,
    )

    releases_dir = releases_dir_for_changes_dir(changes_dir)
    return (
        latest_release_fact(releases_dir, tag_glob=tag_glob),
        nearest_release_commit(releases_dir, tag_glob=tag_glob),
    )


def _coverage_column(release_commit, changes_dir, scope):
    """Return the Coverage-column string for one status row.

    Real JSONL coverage: the commits since *release commit* -- the release record entry for
    the highest archived release this checkout contains -- scoped to the row's
    members via *scope* (an :class:`~rlsbl.ownership.OwnershipScope`, which
    carries the whole member list), minus the exempt ones, cross-referenced
    against the row's
    ``unreleased.jsonl``. Rendered ``covered/tracked`` with a
    ``(N exempted)`` suffix, matching ``rlsbl status``.

    This column used to count CHANGELOG.md bullet lines above the last version
    heading. CHANGELOG.md is regenerated from the JSONL at release time, so
    that count described the *previous* release's prose, never whether the
    current unreleased commits had entries -- and it read as "documented" when
    no entry existed at all.

    ``"no changelog"`` when the changes directory is missing.
    """
    if not os.path.isdir(changes_dir):
        return "no changelog"

    from ...changelog.files import read_unreleased
    from ...changelog.resolve import _git_log_hashes, resolve_hashes
    from ...changelog.validate import filter_exempt_commits
    from ...git_util import filter_commits_for_scope

    from ...upstream import history_exclusions

    range_spec = f"{release_commit.candidate_sha}..HEAD" if release_commit else "HEAD"
    # In a fork, upstream's history is not ours to cover -- the same
    # exclusion the coverage check applies.
    commits = _git_log_hashes([range_spec, *history_exclusions()])
    # Scope first, then exempt -- the order the authoritative coverage check
    # uses, so an unrelated package's changelog churn is never counted here.
    if scope is not None:
        in_scope = filter_commits_for_scope(
            set(commits), scope, operation="monorepo status coverage",
        )
        commits = [c for c in commits if c in in_scope]
    non_exempt, _stats = filter_exempt_commits(commits)
    exempted = len(commits) - len(non_exempt)

    all_hashes = []
    for entry in read_unreleased(changes_dir):
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)
    covered_shas = {full for full in resolved.values() if full is not None}

    covered = sum(1 for c in non_exempt if c in covered_shas)
    suffix = f" ({exempted} exempted)" if exempted else ""
    return f"{covered}/{len(non_exempt)}{suffix}"


def _suppressed_member_version(root, proj, releasable_name, releasable_config_dir):
    """The releasable's version when *proj* is a publish-suppressed member, else None.

    "Publish-suppressed" is the member's EFFECTIVE ``publish_mode`` -- the
    releasable-level config merged with the member's own -- which is the same
    resolution ``version-consistency`` performs before it decides to read the
    version file instead of the manifest. Both questions therefore get the same
    answer from the same place.

    None means the ordinary manifest reading applies: a published member, a
    member outside every releasable, a releasable with no version file, or a
    config that cannot answer what its publish mode is (which is the
    ``version-consistency`` check's finding to report, not this table's).
    """
    if not releasable_name:
        return None

    from ...member_context import resolve_member_context
    from ...workspace import read_releasable_version

    try:
        member = resolve_member_context(
            os.path.join(root, proj["path"]),
            releasable_config_dir=releasable_config_dir,
        )
        if member.publish_mode != "none":
            return None
        return read_releasable_version(root, releasable_name) or None
    except Exception:
        return None


def _cmd_status_explicit(root, projects):
    """Render per-releasable status rows for a workspace.

    One row per releasable (version, tag, coverage, member count + names),
    plus one row for each standalone project not belonging to any releasable.
    Tag globs come from the shared resolver so releasable members resolve
    their releasable's tag_format instead of a per-member glob.
    """
    from ...workspace import (
        get_releasable_changes_dir,
        load_releasables,
        members_of,
        read_releasable_version,
    )
    from ...changelog.files import (
        get_changes_dir,
        refuse_non_releasable_member_changes,
    )
    from ...tag_glob import resolve_monorepo_tag_glob

    # The per-project rows below fall back to a member's OWN changes dir. A
    # member outside every releasable has no changelog for that fallback to
    # read, so the state is refused rather than rendered as a coverage figure
    # nothing will ever finalize.
    refuse_non_releasable_member_changes(
        root, projects, operation="monorepo status",
    )

    releasables = load_releasables(root, projects)
    rows = []  # (name, kind, version, tag, coverage, members)
    claimed = set()

    for rel in releasables:
        members = members_of(rel.name, projects)
        for m in members:
            claimed.add(m["name"])
        try:
            version = read_releasable_version(root, rel.name) or "?"
        except Exception:
            version = "?"
        tag_glob = resolve_monorepo_tag_glob(None, root, releasable=rel)
        rel_changes = get_releasable_changes_dir(root, rel.name)
        fact, release_commit = _latest_release_for_row(rel_changes, tag_glob)
        coverage = _coverage_column(
            release_commit, rel_changes,
            OwnershipScope.for_releasable(projects, members, rel.name),
        )
        member_names = ", ".join(m["name"] for m in members)
        members_col = f"{len(members)} ({member_names})" if members else "0"
        rows.append((rel.name, "releasable", str(version), fact.label(), coverage, members_col))

    for proj in projects:
        if proj["name"] in claimed:
            continue
        name = proj["name"]
        path = proj["path"]
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        version = "?"
        if target_entries and target_entries[0].name in TARGETS:
            try:
                version = TARGETS[target_entries[0].name].read_version(target_entries[0].path)
            except Exception:
                version = "?"
        tag_glob = resolve_monorepo_tag_glob(proj, root, releasable=None)
        proj_changes = get_changes_dir(os.path.join(root, path))
        fact, release_commit = _latest_release_for_row(proj_changes, tag_glob)
        coverage = _coverage_column(
            release_commit, proj_changes,
            OwnershipScope.for_member(projects, proj),
        )
        rows.append((name, "project", str(version), fact.label(), coverage, "-"))

    headers = ("Name", "Kind", "Version", "Released", "Coverage", "Members")
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(len(headers)):
            widths[i] = max(widths[i], len(str(row[i])))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def _cmd_status(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)

    if not projects:
        print("No projects in workspace.")
        return

    # Every workspace declares its releasables, so the per-releasable summary
    # (versions, tags, coverage, members) is always rendered first. The rich
    # per-project table below still follows it: the two answer different
    # questions -- what is released, and what each member is -- and the
    # per-project columns (target, path, deps, remote) exist nowhere else.
    _cmd_status_explicit(root, projects)
    print()

    # Build dependency graph
    graph = WorkspaceGraph(root, projects)

    # Releasable membership, for the column display
    from ...tag_glob import resolve_monorepo_tag_glob
    from ...workspace import (
        load_releasables,
        mirror_remote_for,
        resolve_releasable_for_project,
    )
    releasable_map = {}  # project name -> releasable name
    releasables = load_releasables(root, projects)
    for proj in projects:
        rel = resolve_releasable_for_project(proj, releasables)
        releasable_map[proj["name"]] = rel.name if rel else ""

    rows = []
    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect targets
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        target_names = [e.name for e in target_entries]
        target_display = ", ".join(target_names) if target_names else "none"

        # Read version (use first target -- one version per project).
        #
        # A publish-suppressed member is the exception: it publishes nothing,
        # so nothing bumps its manifest and the `version-consistency` check
        # deliberately never reads it -- it passes such a member on the
        # releasable's version file alone. Reporting the manifest here showed
        # the member frozen at whatever version it was created with while its
        # releasable shipped release after release, so the row reports the same
        # authority the check uses, annotated so a reader knows which file
        # answered.
        version = "?"
        first_target_name = target_entries[0].name if target_entries else None
        suppressed_version = _suppressed_member_version(
            root, proj, releasable_map.get(name, ""), rel_dir,
        )
        if suppressed_version is not None:
            version = f"{suppressed_version} (version file)"
        elif first_target_name and first_target_name in TARGETS:
            try:
                version = TARGETS[first_target_name].read_version(target_entries[0].path)
            except Exception:
                version = "?"

        # The tag glob names this package's tag scheme; the release itself
        # comes from its RELEASE RECORD. Releasable members read the releasable's
        # changes dir -- and therefore its archives -- not the package's.
        tag_glob = resolve_monorepo_tag_glob(
            proj, root, releasable=resolve_releasable_for_project(proj, releasables),
        )

        from ...changelog.files import get_changes_dir
        _cl_changes_dir = None
        _cl_rel_name = releasable_map.get(name, "")
        if _cl_rel_name:
            from ...workspace import get_releasable_changes_dir
            _cl_changes_dir = get_releasable_changes_dir(root, _cl_rel_name)
        _changes_dir = _cl_changes_dir or get_changes_dir(os.path.join(root, path))
        fact, release_commit = _latest_release_for_row(_changes_dir, tag_glob)
        # The scope answers the same question the changes dir was chosen by: a
        # releasable member's coverage is read against its RELEASABLE's state,
        # whose own directory no member path claims. Scoping it as a bare member
        # would drop every commit that touched only that state, and this row's
        # "(N exempted)" would disagree with the releasable table's above.
        if _cl_rel_name:
            _scope = OwnershipScope.for_releasable(projects, [proj], _cl_rel_name)
        else:
            _scope = OwnershipScope.for_member(projects, proj)
        coverage_str = _coverage_column(release_commit, _changes_dir, _scope)

        # Dependency counts
        deps_count = graph.dep_count(name)
        rdeps_count = graph.rdep_count(name)
        deps_str = str(deps_count) if deps_count else "0"
        rdeps_str = str(rdeps_count) if rdeps_count else "0"

        # Library flag
        library_str = "yes" if proj.get("library", False) else ""

        # Dev-only flag
        dev_only_str = "yes" if proj.dev_only else ""

        # Subtree remote -- declared by the releasable this member belongs to
        remote = mirror_remote_for(proj, releasables)
        remote_str = remote if remote else "-"

        # Releasable membership
        releasable_str = releasable_map.get(name, "")

        rows.append((name, path, target_display, version, fact.label(), coverage_str, library_str, dev_only_str, deps_str, rdeps_str, remote_str, releasable_str))

    # Determine which dynamic columns to show
    any_library = any(row[6] != "" for row in rows)
    any_dev_only = any(row[7] != "" for row in rows)
    any_deps = any(row[8] != "0" for row in rows)
    any_rdeps = any(row[9] != "0" for row in rows)
    any_remote = any(row[10] != "-" for row in rows)
    any_releasable = any(row[11] != "" for row in rows)

    # Calculate column widths
    base_headers = ("Project", "Path", "Target", "Version", "Released", "Coverage")
    if any_releasable:
        base_headers = base_headers + ("Releasable",)
    if any_library:
        base_headers = base_headers + ("Library",)
    if any_dev_only:
        base_headers = base_headers + ("DevOnly",)
    if any_deps:
        base_headers = base_headers + ("Deps",)
    if any_rdeps:
        base_headers = base_headers + ("Rdeps",)
    if any_remote:
        base_headers = base_headers + ("Remote",)
    headers = base_headers

    # Build display rows matching the dynamic header order
    display_rows = []
    for row in rows:
        cells = list(row[:6])  # base columns: name, path, target, version, tag, unreleased
        if any_releasable:
            cells.append(row[11])
        if any_library:
            cells.append(row[6])
        if any_dev_only:
            cells.append(row[7])
        if any_deps:
            cells.append(row[8])
        if any_rdeps:
            cells.append(row[9])
        if any_remote:
            cells.append(row[10])
        display_rows.append(tuple(cells))

    widths = [len(h) for h in headers]
    for cells in display_rows:
        for i in range(len(headers)):
            widths[i] = max(widths[i], len(cells[i]))

    # Print header
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)

    # Print rows
    for cells in display_rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))
        print(line)


# Canonical definitions live in rlsbl.constraints; imported here for
# backward compatibility with callers that import via commands.monorepo.
from ...constraints import _evaluate_constraint, _parse_version_tuple  # noqa: F401
from ... import effects


def _cmd_outdated(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    graph = WorkspaceGraph(root, projects)

    # Build a lookup: project name -> (target_name, target_path) for version reading
    project_version_info = {}
    for proj in projects:
        name = proj["name"]
        path = proj["path"]
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        if target_entries and target_entries[0].name in TARGETS:
            project_version_info[name] = (target_entries[0].name, target_entries[0].path)

    rows = []
    for proj in projects:
        name = proj["name"]
        deps = graph.dependencies(name)
        for dep in deps:
            # Read the dependency's current version
            current_version = "?"
            if dep.name in project_version_info:
                target_name, target_path = project_version_info[dep.name]
                try:
                    current_version = TARGETS[target_name].read_version(target_path)
                except Exception:
                    current_version = "?"

            # Determine status
            if dep.dep_type == "workspace":
                status = "workspace"
            elif dep.dep_type == "path":
                status = "path"
            elif dep.dep_type == "explicit":
                status = "explicit"
            else:
                status = _evaluate_constraint(dep.constraint, current_version)

            constraint_display = "(explicit)" if dep.dep_type == "explicit" else dep.constraint
            rows.append((name, dep.name, constraint_display, current_version, status))

    if not rows:
        print("No intra-workspace dependencies found.")
        return

    # Print table
    headers = ("Project", "Dependency", "Constraint", "Current", "Status")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)

    for row in rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        print(line)


def _cmd_release_order(flags, project_root):
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from ...workspace_graph import CycleError

    graph = WorkspaceGraph(root, projects)
    project_names = [p["name"] for p in projects]

    try:
        order = graph.topological_order()
    except CycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    all_independent = all(graph.dep_count(p) == 0 for p in project_names)

    if all_independent:
        print("All projects are independent (no intra-workspace dependencies).")
        print()
        for name in sorted(project_names):
            print(f"  {name}")
    else:
        print("Release order (leaves first):")
        print()
        for i, name in enumerate(order, 1):
            print(f"  {i}. {name}")


def _cmd_check_names(args, flags, project_root):
    target = flags.get("target")
    if not target:
        print("Error: --target is required. Usage: rlsbl monorepo check-names --target <npm|pypi|go>", file=sys.stderr)
        sys.exit(1)

    prefix = flags.get("prefix", "")
    suffix = flags.get("suffix", "")
    delay_ms = int(flags.get("delay", "200"))

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    from ..check import _check_single_name, _format_table_row, summary_line

    from ...workspace import project_is_dev_only

    # A dev node publishes nothing, so it has no registry identity to check --
    # and every workspace has at least one (its root member, when that member
    # is a dev node). Asking a registry about it is pointless contact.
    projects = [p for p in projects if not project_is_dev_only(p)]
    if not projects:
        print("No publishable projects in workspace.")
        return

    rows = []
    offline = False
    for i, proj in enumerate(projects):
        # A project's registry_name IS its registry identity: use it verbatim,
        # bypassing prefix/suffix. Only fall back to prefix+name+suffix when no
        # registry_name is declared.
        registry_name = proj.registry_name if isinstance(proj, WorkspaceProject) else proj.get("registry_name", "")
        if registry_name:
            checked_name = registry_name
        else:
            checked_name = prefix + proj["name"] + suffix
        result = _check_single_name(checked_name, target)
        table_row = _format_table_row(result)
        rows.append({
            "project": proj["name"],
            "checked_name": checked_name,
            "status": table_row["status"],
        })
        # An offline check (go) asks no registry, so there is no rate limit to respect.
        offline = bool(result.get("offline"))
        if i < len(projects) - 1 and not offline:
            time.sleep(delay_ms / 1000)

    # Compute column widths
    proj_width = max(len("Project"), max(len(r["project"]) for r in rows))
    name_width = max(len("Checked Name"), max(len(r["checked_name"]) for r in rows))
    status_width = max(len("Status"), max(len(r["status"]) for r in rows))

    header = f"{'Project':<{proj_width}}  {'Checked Name':<{name_width}}  {'Status':<{status_width}}"
    print(header)
    for row in rows:
        line = f"{row['project']:<{proj_width}}  {row['checked_name']:<{name_width}}  {row['status']:<{status_width}}"
        print(line)

    print(f"\n{summary_line(rows)}")

    # Batch context note: the delay only applies to networked checks.
    if not offline:
        msg = f"Checked with {delay_ms}ms delay between names."
        if delay_ms == 200:
            msg += " Increase --delay if rate limited."
        print(msg)

"""Nested-member checks (tags: workspace, preflight): a member's tools leave the members inside it alone.

Checks: nested-member-runner-exclusion, nested-member-upload-contents.
"""

import os

from ..check_context import WorkspaceCheckContext


def runner_exclusion_problems(workspace_root):
    """One finding per member whose test runner would collect a nested member."""
    from ..nested_exclusions import missing_exclusions, nested_paths_inside
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from ..workspace import load_workspace, nested_member_dirs

    root = str(workspace_root)
    problems = []
    # The whole member list, read from the workspace: a release's preflight
    # hands each member a context listing that member alone.
    for proj in load_workspace(root):
        proj_dir = os.path.join(root, proj["path"])
        nested = nested_member_dirs(proj_dir)
        if not nested:
            continue
        entries = detect_targets(
            proj_dir, releasable_config_dir=resolve_releasable_config_dir(proj, root),
        )
        seen = set()
        for entry in entries:
            target = TARGETS.get(entry.name)
            if target is None:
                continue
            key = (target.scratch_test_exclusion, os.path.realpath(entry.path))
            if key in seen:
                continue
            seen.add(key)
            nested_rel = nested_paths_inside(entry.path, nested)
            found = missing_exclusions(
                target.scratch_test_exclusion, entry.path, nested_rel,
            )
            if found is None:
                continue
            config_file, missing = found
            config_rel = os.path.relpath(config_file, root).replace(os.sep, "/")
            problems.append(
                f"{proj['name']}: its {entry.name} test runner would collect "
                f"the members nested inside it; {config_rel} must exclude "
                f"{', '.join(missing)}. Run `rlsbl scaffold` in {proj['path']} "
                f"to write them (a file rlsbl cannot write into, such as a "
                f"pytest.ini or a deno.jsonc, takes them by hand). pytest "
                f"resolves --ignore against the directory it starts in: run it "
                f"from {proj['path']}, as rlsbl and CI do."
            )
    return problems


def register_nested_checks(app):
    """Register the nested-member checks on *app*."""
    _register_upload_contents(app)

    @app.error_check("nested-member-runner-exclusion")
    def check_nested_member_runner_exclusion(ctx, reporter):
        """A member's test runner must exclude every member nested inside it."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        problems = runner_exclusion_problems(ctx.workspace_root)
        if not problems:
            return reporter.passed(
                "every member's test runner excludes the members nested inside it"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} member(s) whose test runner would collect a nested member"
        )


# ---------------------------------------------------------------------------
# What a member's upload carries
# ---------------------------------------------------------------------------

#: Directories the Go module zip never carries (golang.org/x/mod/zip).
_GO_ZIP_EXCLUDED_DIRS = frozenset({"vendor", ".git", ".hg", ".svn", ".bzr"})


def npm_pack_files(package_dir, *, timeout=120):
    """The repo-agnostic paths `npm pack` would put in *package_dir*'s tarball.

    Listed with ``--dry-run --ignore-scripts --offline``: nothing is packed,
    no lifecycle script runs, and nothing is fetched. npm still writes its
    cache and logs, so it is pointed at a throwaway cache directory.
    """
    import json
    import tempfile

    from .. import effects

    with tempfile.TemporaryDirectory(prefix="rlsbl-npm-pack-") as cache:
        env = dict(os.environ, npm_config_cache=cache)
        result = effects.run(
            ["npm", "pack", "--dry-run", "--json", "--ignore-scripts",
             "--offline", "--logs-max=0"],
            cwd=package_dir, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"`npm pack --dry-run` failed in {package_dir}: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    listing = json.loads(result.stdout)
    return [entry["path"] for entry in listing[0]["files"]]


def go_module_zip_files(module_dir):
    """The tracked files the Go module zip of *module_dir* would carry.

    Go's rule: a subdirectory holding its own ``go.mod`` is another module and
    is left out, and so are ``vendor/`` and version-control directories. The
    proxy builds the zip from the tagged commit, so only tracked files count.
    """
    from ..ldflags_symbols import git_tracked_files

    tracked = git_tracked_files(module_dir)
    nested_modules = {
        os.path.dirname(rel) for rel in tracked
        if os.path.basename(rel) == "go.mod" and os.path.dirname(rel)
    }
    files = []
    for rel in tracked:
        parts = rel.split("/")
        if any(part in _GO_ZIP_EXCLUDED_DIRS for part in parts[:-1]):
            continue
        if any(rel.startswith(d + "/") for d in nested_modules):
            continue
        files.append(rel)
    return files


def upload_content_problems(workspace_root):
    """One finding per file a member's npm or Go upload would take from a nested member."""
    from ..ownership import member_name, owner_of
    from ..targets import detect_targets, resolve_releasable_config_dir
    from ..workspace import load_workspace, nested_member_dirs

    root = os.path.realpath(str(workspace_root))
    projects = load_workspace(root)
    problems = []
    for proj in projects:
        proj_dir = os.path.join(root, proj["path"])
        if not nested_member_dirs(proj_dir):
            continue
        entries = detect_targets(
            proj_dir, releasable_config_dir=resolve_releasable_config_dir(proj, root),
        )
        for entry in entries:
            if entry.name == "npm":
                listed = npm_pack_files(entry.path)
                label = "`npm pack`"
                remedy = (
                    'List only the member\'s own files in package.json\'s "files" '
                    "field (or exclude the nested member in .npmignore)."
                )
            elif entry.name == "go":
                listed = go_module_zip_files(entry.path)
                label = "the Go module zip"
                remedy = (
                    "The module proxy keeps a zip permanently. Move the nested "
                    "member out of the module's directory, or give it a go.mod "
                    "of its own, which Go leaves out of the zip."
                )
            else:
                continue
            base = os.path.relpath(os.path.realpath(entry.path), root).replace(os.sep, "/")
            for rel in listed:
                path = rel if base == "." else f"{base}/{rel}"
                owner = owner_of(path, projects)
                if owner is None or member_name(owner) == member_name(proj):
                    continue
                problems.append(
                    f"{proj['name']}: {label} would ship {path}, which member "
                    f"'{member_name(owner)}' owns. {remedy}"
                )
    return problems


def _register_upload_contents(app):
    @app.error_check("nested-member-upload-contents")
    def check_nested_member_upload_contents(ctx, reporter):
        """A member's npm package and Go module zip carry no nested member's files."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        try:
            problems = upload_content_problems(ctx.workspace_root)
        except (RuntimeError, ValueError, OSError) as exc:
            reporter.error(str(exc))
            return reporter.found("an upload's contents could not be listed")
        if not problems:
            return reporter.passed(
                "no npm package or Go module zip carries a nested member's files"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} file(s) a member's upload would take from a nested member"
        )

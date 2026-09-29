"""Nested-member checks (tags: workspace, preflight): a member's tools leave the members inside it alone.

Checks: nested-member-runner-exclusion, nested-member-upload-contents,
nested-member-uv-sources.
"""

import os

from ..check_context import WorkspaceCheckContext
from ..errors import ConfigError


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
                f"{', '.join(missing)}. "
                + _runner_exclusion_fix(proj, target, config_rel, missing)
                + f" pytest resolves --ignore against the directory it starts "
                f"in: run it from {proj['path']}, as rlsbl and CI do."
            )
    return problems


def _runner_exclusion_fix(proj, target, config_rel, missing):
    """How to write *missing* into *config_rel*, as the operator can do it.

    ``rlsbl scaffold`` writes them for a member, but it does not run at the
    workspace root, so the root member's entries are written by hand.
    """
    from ..ownership import is_root_path
    from ..scratch_dirs import DENO_CONFIG_EXCLUDE, PYTEST_NORECURSEDIRS

    if not is_root_path(proj["path"]):
        return (
            f"Run `rlsbl scaffold` in {proj['path']} to write them (a file "
            f"rlsbl cannot write into, such as a pytest.ini or a deno.jsonc, "
            f"takes them by hand)."
        )
    if target.scratch_test_exclusion == PYTEST_NORECURSEDIRS:
        where = "addopts under [tool.pytest.ini_options]"
        if not config_rel.endswith("pyproject.toml"):
            where = "addopts under [pytest]"
    elif target.scratch_test_exclusion == DENO_CONFIG_EXCLUDE:
        where = "the top-level exclude list"
    else:
        where = "the runner's exclusions"
    return (
        f"`rlsbl scaffold` does not run at the workspace root, so write them "
        f"by hand: add {' '.join(missing)} to {where} in {config_rel}."
    )


def register_nested_checks(app):
    """Register the nested-member checks on *app*."""
    _register_upload_contents(app)
    _register_uv_sources(app)

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


def _uploads(proj, root):
    """Does *proj* publish an upload at all?

    A member outside every releasable is never released, and one whose
    effective config sets ``publish_mode: "none"`` is released without
    publishing, so neither has an upload to carry anyone's files. A config
    whose ``publish_mode`` cannot be read is treated as publishing: the
    ``publish-mode`` check refuses it on its own.
    """
    from ..config import ConfigError, read_project_config, suppresses_publish
    from ..targets import resolve_releasable_config_dir

    if not isinstance(proj.get("releasable"), str):
        return False
    config = read_project_config(
        os.path.join(root, proj["path"]),
        releasable_config_dir=resolve_releasable_config_dir(proj, root),
    )
    try:
        return not suppresses_publish(config)
    except ConfigError:
        return True


def upload_content_problems(workspace_root):
    """One finding per file a member's upload would take from a nested member.

    Each target answers what its own upload carries
    (:meth:`~rlsbl.targets.base.BaseTarget.offline_upload_listing`); a target
    that cannot list it offline -- a Python sdist has to be built -- answers
    None and is checked in CI instead.
    """
    from ..ownership import member_name, owner_of
    from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from ..workspace import load_workspace, nested_member_dirs

    root = os.path.realpath(str(workspace_root))
    projects = load_workspace(root)
    problems = []
    for proj in projects:
        proj_dir = os.path.join(root, proj["path"])
        if not nested_member_dirs(proj_dir) or not _uploads(proj, root):
            continue
        entries = detect_targets(
            proj_dir, releasable_config_dir=resolve_releasable_config_dir(proj, root),
        )
        for entry in entries:
            target = TARGETS.get(entry.name)
            listing = target.offline_upload_listing(entry.path) if target else None
            if listing is None:
                continue
            base = os.path.relpath(os.path.realpath(entry.path), root).replace(os.sep, "/")
            for rel in listing.files:
                path = rel if base == "." else f"{base}/{rel}"
                owner = owner_of(path, projects)
                if owner is None or member_name(owner) == member_name(proj):
                    continue
                problems.append(
                    f"{proj['name']}: {listing.label} would ship {path}, which "
                    f"member '{member_name(owner)}' owns. {listing.fix}"
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
        except (ConfigError, ValueError, OSError) as exc:
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


# ---------------------------------------------------------------------------
# uv workspace sources
# ---------------------------------------------------------------------------


def _workspace_sources(pyproject):
    """Names declared ``{ workspace = true }`` in *pyproject*'s [tool.uv.sources]."""
    import tomllib

    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if not isinstance(sources, dict):
        return []
    return sorted(
        name for name, spec in sources.items()
        if isinstance(spec, dict) and spec.get("workspace") is True
    )


def uv_sources_problems(workspace_root):
    """One finding per nested member declaring a uv workspace source itself."""
    from ..ownership import member_path, nested_member_paths
    from ..uv_workspace import find_uv_workspace_root
    from ..workspace import load_workspace

    root = str(workspace_root)
    projects = load_workspace(root)
    nested = {
        path
        for proj in projects if member_path(proj)
        for path in nested_member_paths(proj, projects)
    }
    problems = []
    for proj in projects:
        if member_path(proj) not in nested:
            continue
        pyproject = os.path.join(root, proj["path"], "pyproject.toml")
        names = _workspace_sources(pyproject)
        if not names:
            continue
        uv_root = find_uv_workspace_root(os.path.join(root, proj["path"])) or root
        root_pyproject = os.path.join(uv_root, "pyproject.toml")
        problems.append(
            f"{proj['name']}: {proj['path']}/pyproject.toml declares "
            f"{', '.join(names)} as `{{ workspace = true }}` in "
            f"[tool.uv.sources], but uv refuses a workspace source declared in "
            f"a member nested inside another member (\"references a workspace "
            f"in `tool.uv.sources` ... but is not a workspace member\"). Move "
            f"the entries to [tool.uv.sources] in {root_pyproject}, where uv "
            f"resolves them for every member."
        )
    return problems


def _register_uv_sources(app):
    @app.error_check("nested-member-uv-sources")
    def check_nested_member_uv_sources(ctx, reporter):
        """A nested member declares no uv workspace source of its own."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        problems = uv_sources_problems(ctx.workspace_root)
        if not problems:
            return reporter.passed(
                "no nested member declares a uv workspace source of its own"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} nested member(s) declaring uv workspace sources"
        )

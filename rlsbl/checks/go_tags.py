"""Go tag rules (tags: workspace/project, preflight): what a Go tag may name.

Checks: path-tag-format-go-member, go-module-major-suffix.

A Go tag is the published artifact -- the module proxy resolves it and caches
the answer permanently -- so both rules are refused before anything is tagged,
not repaired afterwards.
"""

import os
import re

from ..check_context import WorkspaceCheckContext

#: A Go path tag format, once its name is filled in: ``<path>/v{version}``.
_PATH_TAG_PATTERN = re.compile(r"^(?P<path>[^{}]+)/v\{version\}$")

#: A module path's major-version suffix: a last element ``v2``, ``v3``, ...
_MAJOR_SUFFIX = re.compile(r"^(?P<base>.+)/(?P<major>v(?:[2-9]|[1-9][0-9]+))$")


def go_path_of_tag_format(tag_format, name):
    """The module directory a path-style *tag_format* tags, or None.

    ``gfx/v{version}`` tags the Go module at ``gfx``; ``{name}@v{version}``
    and ``v{version}`` are not path tags at all.
    """
    from ..tag_glob import TagScheme

    match = _PATH_TAG_PATTERN.match(TagScheme.from_format(tag_format, name).pattern)
    return match.group("path") if match else None


def _go_member_paths(ctx, members):
    """The declared paths of *members* that carry a Go target."""
    from . import targets_for_check
    from ..targets import detect_targets, resolve_releasable_config_dir

    scope = targets_for_check("path-tag-format-go-member")
    paths = []
    for proj in members:
        proj_dir = os.path.join(str(ctx.workspace_root), proj["path"])
        entries = detect_targets(
            proj_dir,
            releasable_config_dir=resolve_releasable_config_dir(
                proj, ctx.workspace_root,
            ),
        )
        if any(entry.name in scope for entry in entries):
            paths.append(proj["path"])
    return paths


def path_tag_format_problems(ctx):
    """One refusal per releasable whose path-style tag_format names no Go member of its own.

    The members come from the workspace itself rather than ``ctx.projects``: a
    release's preflight hands each member a context listing that member alone,
    and the question is about the releasable's whole member list.
    """
    from ..workspace import load_workspace, members_of

    all_projects = load_workspace(str(ctx.workspace_root))
    problems = []
    for rel in ctx.releasables:
        fmt = rel.effective_tag_format
        path = go_path_of_tag_format(fmt, rel.name)
        if path is None:
            continue
        go_paths = _go_member_paths(ctx, members_of(rel.name, all_projects))
        if path in go_paths:
            continue
        alternatives = "'{name}@v{version}'"
        if go_paths:
            alternatives += (
                ", or the path of one of its Go members: "
                + ", ".join(f"'{p}/v{{version}}'" for p in sorted(go_paths))
            )
        problems.append(
            f"releasable '{rel.name}' declares tag_format '{fmt}', a Go "
            f"module-proxy tag for path '{path}', but no Go member of "
            f"'{rel.name}' lives at '{path}'. A Go tag publishes the module at "
            f"the path it names, permanently, so this one would publish a "
            f"version of a module '{rel.name}' does not own, or of none. Set "
            f"tag_format in the releasable's [[releasables]] entry to "
            f"{alternatives}."
        )
    return problems


def major_suffix_problems(repo_root, module_dirs):
    """One refusal per go.mod whose module path ends in a major-version suffix."""
    from ..utils import read_go_module_path

    problems = []
    for directory in module_dirs:
        module = read_go_module_path(directory)
        if not module:
            continue
        match = _MAJOR_SUFFIX.match(module)
        if match is None:
            continue
        rel = os.path.relpath(directory, repo_root).replace(os.sep, "/")
        base = match.group("base")
        major = match.group("major")
        problems.append(
            f"{rel}/go.mod declares module '{module}', whose trailing /{major} "
            f"marks major version {major[1:]}: Go resolves only {major}.x.y "
            f"versions of it. A major version of 2 or higher is refused before "
            f"anything is tagged, because a Go tag is permanent and a stable "
            f"major is never released by accident. Drop the suffix with "
            f"`rlsbl rewrite go-module-path --from-module {module} --to-module "
            f"{base}`."
        )
    return problems


def register_go_tag_checks(app):
    """Register the Go tag checks on *app*."""

    @app.error_check("path-tag-format-go-member")
    def check_path_tag_format_go_member(ctx, reporter):
        """A path-style tag_format must name the path of one of its releasable's Go members."""
        if not isinstance(ctx, WorkspaceCheckContext) or not ctx.releasables:
            return reporter.skipped("not a workspace with releasables")
        problems = path_tag_format_problems(ctx)
        if not problems:
            return reporter.passed(
                "every path-style tag_format names a Go member of its releasable"
            )
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} releasable(s) tagging a path none of their Go members has"
        )

    @app.error_check("go-module-major-suffix")
    def check_go_module_major_suffix(ctx, reporter):
        """No go.mod module path may end in a major-version suffix."""
        from .project import _go_module_dirs, _repo_root

        module_dirs = _go_module_dirs(ctx, "go-module-major-suffix")
        if not module_dirs:
            return reporter.skipped("no Go target detected")
        problems = major_suffix_problems(_repo_root(ctx), module_dirs)
        if not problems:
            return reporter.passed("no module path carries a major-version suffix")
        for problem in problems:
            reporter.error(problem)
        return reporter.found(
            f"{len(problems)} module path(s) with a major-version suffix"
        )

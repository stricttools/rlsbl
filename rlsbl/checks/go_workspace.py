"""Go modules that depend on each other inside one workspace (tags: workspace, preflight).

Checks: go-workspace-require-current, go-workspace-replace.

rlsbl does not bump a module's ``require`` line when a sibling module
releases: the dependent's ``go.sum`` needs the new version's hash, which
exists only once the tag reaches the module proxy. So a require below the
sibling's latest release is refused, naming the ``go get`` that raises it.
Development across modules uses a committed ``go.work``; a ``replace`` into
the workspace is refused, because ``go install module@version`` rejects a
module that carries one and its consumers never see it.
"""

import os

from ..check_context import WorkspaceCheckContext


def _go_members(ctx):
    """``[(member, module path, go.mod path)]`` for every member with a go.mod."""
    from ..go_identity import read_module_line
    from ..workspace import load_workspace

    root = str(ctx.workspace_root)
    found = []
    for proj in load_workspace(root):
        go_mod = os.path.join(root, proj["path"], "go.mod")
        if not os.path.isfile(go_mod):
            continue
        module = read_module_line(go_mod)
        if module:
            found.append((proj, module, go_mod))
    return found


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _version_key(version):
    from ..release_file import archive_sort_key

    return archive_sort_key(version.lstrip("v"))


def require_problems(ctx):
    """One finding per require naming a sibling module below its latest release."""
    from ..go_mod import parse_directives
    from ..release_file import is_release_version
    from ..release_record import latest_released_version
    from ..workspace import get_releasable_dir

    root = str(ctx.workspace_root)
    members = _go_members(ctx)
    by_module = {module: proj for proj, module, _ in members}
    problems = []
    for proj, _module, go_mod in members:
        requires, _replaces = parse_directives(_read(go_mod))
        for req in requires:
            sibling = by_module.get(req.path)
            if sibling is None or sibling["name"] == proj["name"]:
                continue
            releasable = sibling.get("releasable")
            if not isinstance(releasable, str) or not releasable:
                continue
            releases = os.path.join(get_releasable_dir(root, releasable), "releases")
            latest = latest_released_version(releases) if os.path.isdir(releases) else None
            if latest is None:
                continue
            required = req.version.lstrip("v")
            if is_release_version(required) and _version_key(required) >= _version_key(latest):
                continue
            problems.append(
                f"{proj['name']}: {proj['path']}/go.mod requires {req.path} "
                f"{req.version} (line {req.line}), below that module's latest "
                f"release, v{latest}. rlsbl does not raise it for you -- go.sum "
                f"needs the new version's hash, which exists only once the tag "
                f"reaches the module proxy. Run `go get {req.path}@v{latest}` in "
                f"{proj['path']}, then commit go.mod and go.sum."
            )
    return problems


def replace_problems(ctx):
    """One finding per replace pointing into the workspace."""
    from ..go_mod import parse_directives

    root = os.path.realpath(str(ctx.workspace_root))
    members = _go_members(ctx)
    dirs = sorted({
        os.path.relpath(os.path.dirname(os.path.realpath(g)), root).replace(os.sep, "/")
        for _p, _m, g in members
    })
    problems = []
    for proj, _module, go_mod in members:
        if not isinstance(proj.get("releasable"), str):
            continue
        _requires, replaces = parse_directives(_read(go_mod))
        module_dir = os.path.dirname(os.path.realpath(go_mod))
        for rep in replaces:
            if not rep.is_local:
                continue
            target = os.path.realpath(os.path.join(module_dir, rep.new_path))
            if not (target == root or target.startswith(root + os.sep)):
                continue
            use = " ".join(d if d != "." else "." for d in dirs)
            problems.append(
                f"{proj['name']}: {proj['path']}/go.mod replaces {rep.old_path} "
                f"with {rep.new_path} (line {rep.line}), a directory in this "
                f"workspace. `go install {rep.old_path}@<version>` rejects a "
                f"module carrying a replace, and its consumers never see one. "
                f"Develop across the workspace's modules with a committed "
                f"go.work instead: run `go work init` and `go work use {use}` at "
                f"the repository root and commit go.work, then run "
                f"`go mod edit -dropreplace={rep.old_path}` in {proj['path']}."
            )
    return problems


def register_go_workspace_checks(app):
    """Register the Go workspace checks on *app*."""

    @app.error_check("go-workspace-require-current")
    def check_go_workspace_require_current(ctx, reporter):
        """No Go member requires a sibling module below its latest release."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        problems = require_problems(ctx)
        if not problems:
            return reporter.passed("every sibling module requirement is current")
        for problem in problems:
            reporter.error(problem)
        return reporter.found(f"{len(problems)} stale sibling module requirement(s)")

    @app.error_check("go-workspace-replace")
    def check_go_workspace_replace(ctx, reporter):
        """No releasable Go member replaces a module with a workspace directory."""
        if not isinstance(ctx, WorkspaceCheckContext) or ctx.workspace_root is None:
            return reporter.skipped("not a workspace")
        problems = replace_problems(ctx)
        if not problems:
            return reporter.passed("no Go member replaces a module with a workspace directory")
        for problem in problems:
            reporter.error(problem)
        return reporter.found(f"{len(problems)} replace directive(s) into the workspace")

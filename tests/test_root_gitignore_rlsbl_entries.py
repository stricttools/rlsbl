"""`monorepo sync` gives the workspace root's .gitignore rlsbl's entries.

``rlsbl scaffold`` writes rlsbl's run-state entries (locks, in-progress
release state, scrub results) into a project's ``.gitignore``, and it does not
run at the workspace root. The root's ``.gitignore`` never got them, so a
scrub's ``scrub-result.json`` under ``.rlsbl-monorepo/releasables/`` showed as
untracked at the root. The sync, which maintains the root, now adds the
missing entries and keeps every line already there.
"""

import subprocess

from conftest import capture_all_checks

from rlsbl.commands.monorepo.commands import _cmd_init
from rlsbl.commands.monorepo.sync import _cmd_sync


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=root, check=check, capture_output=True, text=True,
        timeout=30,
    )


def test_sync_adds_the_entries_and_commits_them(mock_git_repo):
    root = mock_git_repo
    (root / ".gitignore").write_text("my-own-entry\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "own gitignore")
    _cmd_init({"root-dev-node": True}, project_root=root)

    _cmd_sync({}, project_root=root)

    lines = (root / ".gitignore").read_text().splitlines()
    assert lines[0] == "my-own-entry"
    assert ".rlsbl-monorepo/releasables/*/releases/scrub-result.json" in lines
    assert ".rlsbl-monorepo/releasables/*/releases/in-progress.json" in lines
    assert _git(root, "status", "--porcelain", "--", ".gitignore").stdout == ""

    scrub = root / ".rlsbl-monorepo" / "releasables" / "app" / "releases" / "scrub-result.json"
    scrub.parent.mkdir(parents=True)
    scrub.write_text("{}\n")
    assert _git(root, "check-ignore", "-q", str(scrub), check=False).returncode == 0

    before = (root / ".gitignore").read_text()
    _cmd_sync({}, project_root=root)
    assert (root / ".gitignore").read_text() == before


def test_the_root_member_passes_the_gitignore_staleness_check(mock_git_repo):
    from rlsbl.context import create_context
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    root = mock_git_repo
    _cmd_init({"root-dev-node": True}, project_root=root)
    _cmd_sync({}, project_root=root)

    projects = load_workspace(str(root))
    base = create_context(root, workspace_root=root)
    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config=base.config,
        projects=projects, graph=None,
    )
    result = capture_all_checks()["scaffold-gitignore-stale"](ctx)
    assert result.kind == "passed", result

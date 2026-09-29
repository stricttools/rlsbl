"""go-module-identity's printed rewrites, executed as printed, in a workspace with nested modules.

Each moved module gets a fix of its own. Renaming a parent used to rename
the nested module too, so running the parent's printed command left the nested
module's printed command naming a path that no longer existed anywhere.
"""

import re
import shlex

from conftest import make_nested_workspace, run_git

OLD = "github.com/example/old"
NEW = "github.com/example/nested"


def _check(root):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    return app._check_defs["go-module-identity"].impl(ctx)


def test_each_printed_rewrite_runs_and_the_check_clears(tmp_path, monkeypatch):
    import rlsbl

    root = tmp_path / "ws"
    make_nested_workspace(root, "go", commit=False)
    # Every module still carries the repository's previous identity.
    for member in ("draw", "draw/cmd", "kernel", "kernel/vulkan"):
        for name in ("go.mod",) + tuple(
            p.name for p in (root / member).iterdir() if p.suffix == ".go"
        ):
            path = root / member / name
            path.write_text(path.read_text().replace(NEW, OLD))
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "remote", "add", "origin", "git@github.com:example/nested.git")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "moved repository")

    result = _check(root)
    assert result.status == "fail"
    commands = re.findall(
        r"rlsbl rewrite go-module-path --from-module \S+ --to-module [^\s`]+",
        " ".join(p.text for p in result.problems),
    )
    assert len(commands) == 4, commands

    monkeypatch.chdir(root)
    for command in commands:
        ran = rlsbl.app.test(shlex.split(command)[1:])
        assert ran.exit_code == 0, (command, ran.stderr)

    assert _check(root).status == "pass", [p.text for p in _check(root).problems]

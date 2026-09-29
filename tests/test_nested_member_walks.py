"""A member's own files exclude the members nested inside it.

Every walk over one member's sources -- library lint, ruff, the ldflags check,
the dependency import scan -- reads the member's own territory and nothing a
nested member owns: a nested member's files are that member's, exactly as the
root member's walks already leave every other member alone.
"""

import os

import pytest

from conftest import make_nested_workspace, run_git
from rlsbl.checks._common import _sibling_exclude_dirs


def nested_member_paths(member, members):
    from rlsbl.ownership import nested_member_paths as authority

    return authority(member, members)


def _member(projects, name):
    return next(p for p in projects if p["name"] == name)


# ---------------------------------------------------------------------------
# The authority
# ---------------------------------------------------------------------------


class TestNestedMemberPaths:
    def test_a_parent_lists_its_nested_members(self, tmp_path):
        projects = make_nested_workspace(tmp_path / "ws", "go", commit=False)
        assert nested_member_paths(_member(projects, "draw"), projects) == [
            "draw/cmd",
        ]
        assert nested_member_paths(_member(projects, "kernel"), projects) == [
            "kernel/vulkan",
        ]

    def test_a_leaf_lists_none(self, tmp_path):
        projects = make_nested_workspace(tmp_path / "ws", "go", commit=False)
        assert nested_member_paths(_member(projects, "drawcmd"), projects) == []

    def test_the_root_member_lists_every_other_member(self, tmp_path):
        projects = make_nested_workspace(tmp_path / "ws", "python", commit=False)
        assert nested_member_paths(_member(projects, "root"), projects) == [
            "sdk", "sdk/npm", "sdk/python",
        ]

    def test_a_sibling_sharing_a_name_prefix_is_not_nested(self, tmp_path):
        projects = [
            {"path": ".", "name": "root"},
            {"path": "draw", "name": "draw"},
            {"path": "drawing", "name": "drawing"},
        ]
        assert nested_member_paths(projects[1], projects) == []

    def test_the_dependency_scan_excludes_the_same_directories(self, tmp_path):
        root = tmp_path / "ws"
        projects = make_nested_workspace(root, "go", commit=False)
        assert _sibling_exclude_dirs(str(root), "draw", projects) == [
            os.path.realpath(root / "draw" / "cmd"),
        ]


# ---------------------------------------------------------------------------
# library-lint
# ---------------------------------------------------------------------------


class TestLibraryLint:
    def test_a_parent_is_not_linted_on_its_nested_members_files(self, tmp_path):
        from rlsbl.lint import lint_library

        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        results = lint_library(str(root / "draw"))
        offending = [r for r in results if "cmd" in r.file.split(os.sep)]
        assert offending == [], offending

    def test_the_nested_member_itself_is_still_linted(self, tmp_path):
        from rlsbl.lint import lint_library

        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        results = lint_library(str(root / "draw" / "cmd"))
        assert any("func main()" in r.message for r in results)

    def test_the_root_member_lints_no_other_member(self, tmp_path):
        from rlsbl.lint import lint_library

        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        (root / "go.mod").write_text("module github.com/example/nested\n\ngo 1.22\n")
        (root / "tool.go").write_text("package nested\n\nfunc F() int { return 1 }\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "root module")
        results = lint_library(str(root))
        assert [r for r in results if os.sep + "cmd" + os.sep in r.file] == []


# ---------------------------------------------------------------------------
# ldflags-symbol
# ---------------------------------------------------------------------------


def _goreleaser(target):
    return (
        "version: 2\n\nbuilds:\n  - main: .\n    ldflags:\n"
        f"      - -s -w -X {target}={{{{.Version}}}}\n"
    )


class TestLdflagsSymbol:
    def test_a_nested_modules_build_file_is_not_the_parents(self, tmp_path):
        from rlsbl.ldflags_symbols import evaluate_ldflags_symbols

        root = tmp_path / "ws"
        make_nested_workspace(root, "go", commit=False)
        (root / "draw" / "cmd" / ".goreleaser.yml").write_text(
            _goreleaser("main.Missing")
        )
        run_git(root, "init", "-q")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "fixture")
        verdict = evaluate_ldflags_symbols([str(root / "draw")])
        assert verdict.problems == []
        assert not any("cmd/.goreleaser.yml" in n for n in verdict.notes)
        # ...and the child's own evaluation still sees it.
        child = evaluate_ldflags_symbols([str(root / "draw" / "cmd")])
        assert child.problems

    def test_a_parents_flag_is_not_verified_by_a_nested_modules_symbol(
        self, tmp_path,
    ):
        from rlsbl.ldflags_symbols import evaluate_ldflags_symbols

        root = tmp_path / "ws"
        make_nested_workspace(root, "go", commit=False)
        # The parent's build injects into a package directory that exists only
        # inside the nested module.
        (root / "draw" / ".goreleaser.yml").write_text(
            "version: 2\n\nbuilds:\n  - main: ./cmd\n    ldflags:\n"
            "      - -s -w -X main.version={{.Version}}\n"
        )
        (root / "draw" / "cmd" / "version.go").write_text(
            "package main\n\nvar version string\n\nfunc v() string { return version }\n"
        )
        run_git(root, "init", "-q")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "fixture")
        verdict = evaluate_ldflags_symbols([str(root / "draw")])
        assert verdict.verified == 0


# ---------------------------------------------------------------------------
# ruff-lint
# ---------------------------------------------------------------------------


def test_ruff_lint_excludes_nested_members(tmp_path, monkeypatch):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    calls = []

    class _Done:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["ruff", "--version"]:
            return _Done("ruff 0.15.20")
        return _Done("[]")

    monkeypatch.setattr("rlsbl.effects.run", fake_run)
    monkeypatch.setattr("rlsbl.utils.require_tool", lambda *a, **k: True)
    ctx = WorkspaceCheckContext(
        project_root=root / "sdk",
        workspace_root=root,
        config={"publish_mode": "ci"},
        projects=load_workspace(str(root)),
    )
    app._check_defs["ruff-lint"].impl(ctx)
    check = next(c for c in calls if c[:2] == ["ruff", "check"])
    assert "--extend-exclude" in check
    excluded = check[check.index("--extend-exclude") + 1].split(",")
    assert sorted(excluded) == [
        str(root / "sdk" / "npm"), str(root / "sdk" / "python"),
    ]


# ---------------------------------------------------------------------------
# The Go import scan: the longest module path owns an import
# ---------------------------------------------------------------------------


class TestGoImportAttribution:
    @pytest.fixture
    def module_map(self):
        prefix = "github.com/example/nested"
        return {
            "draw": f"{prefix}/draw",
            "drawcmd": f"{prefix}/draw/cmd",
            "kernel": f"{prefix}/kernel",
            "vulkan": f"{prefix}/kernel/vulkan",
        }

    def _scan(self, root, member, module_map):
        from rlsbl.import_scanners import GoImportScanner

        return GoImportScanner().scan(
            str(root / member), set(module_map), module_path_map=module_map,
        )

    def test_an_import_of_a_nested_module_is_the_nested_modules(
        self, tmp_path, module_map,
    ):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go", commit=False)
        (root / "draw" / "uses.go").write_text(
            'package draw\n\nimport "github.com/example/nested/kernel/vulkan"\n\n'
            "func G() int { return vulkan.F() }\n"
        )
        run_git(root, "init", "-q")
        names = {i.package_name for i in self._scan(root, "draw", module_map)}
        assert names == {"vulkan"}

    def test_an_import_of_the_own_modules_package_is_no_dependency(
        self, tmp_path, module_map,
    ):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go", commit=False)
        sub = root / "draw" / "cmd" / "sub"
        sub.mkdir()
        (sub / "sub.go").write_text("package sub\n\nfunc S() int { return 1 }\n")
        (root / "draw" / "cmd" / "uses.go").write_text(
            'package main\n\nimport "github.com/example/nested/draw/cmd/sub"\n\n'
            "func g() int { return sub.S() }\n"
        )
        run_git(root, "init", "-q")
        names = [i.package_name for i in self._scan(root, "draw/cmd", module_map)]
        # draw/cmd's own sub-package is not an import of draw; its real
        # import of draw still is.
        assert names.count("draw") == 1


def test_ruff_lint_excludes_nested_members_with_a_one_member_context(
    tmp_path, monkeypatch,
):
    """A release's preflight lists only the member itself in ctx.projects."""
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    calls = []

    class _Done:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Done("ruff 0.15.20" if cmd[:2] == ["ruff", "--version"] else "[]")

    monkeypatch.setattr("rlsbl.effects.run", fake_run)
    monkeypatch.setattr("rlsbl.utils.require_tool", lambda *a, **k: True)
    sdk = next(p for p in load_workspace(str(root)) if p["name"] == "sdk")
    ctx = WorkspaceCheckContext(
        project_root=root / "sdk", workspace_root=root,
        config={"publish_mode": "ci"}, projects=[sdk],
    )
    app._check_defs["ruff-lint"].impl(ctx)
    check = next(c for c in calls if c[:2] == ["ruff", "check"])
    assert "--extend-exclude" in check

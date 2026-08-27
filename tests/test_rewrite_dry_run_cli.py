"""``rlsbl rewrite ... --dry-run``, driven through the real CLI.

The unit tests for both rewrite commands call their ``cmd_*`` entry points
directly. That bypasses strictcli's dispatch, so no effects handle is minted,
the preview machinery's observe screen is not in the loop, and the
``no_writes`` guard around observation is never actually exercised.

That gap is exactly what ``tests/test_absorb_dry_run_cli.py`` was written for
after a preview died on an unallowlisted read. These tests go through
``rlsbl.app.test([...])`` so the effects handle, the recording and the observe
allowlist the real app was built with are all the real ones:

* the preview must render a plan and exit 0;
* the tree must be byte-identical afterwards;
* an apply through the same dispatch must actually write.

Nothing below the dispatch is replaced.
"""

import os
import textwrap

import pytest

import rlsbl

OLD = "github.com/o/foo"
NEW = "github.com/n/qux"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _snapshot(root):
    """Every file's bytes, keyed by path relative to *root*."""
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root)] = open(full, "rb").read()
    return out


def _make_project(root):
    """An rlsbl project (a .rlsbl/ marker is what find_project_root needs)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".rlsbl").mkdir()
    (root / ".rlsbl" / "config.json").write_text("{}\n")
    return root


# ---------------------------------------------------------------------------
# go-module-path
# ---------------------------------------------------------------------------


@pytest.fixture
def go_project(tmp_path):
    root = _make_project(tmp_path / "goproj")
    _write(root, "go.mod", f"module {OLD}\n\ngo 1.22\n")
    _write(root, "main.go", (
        "package main\n"
        "\n"
        f'import "{OLD}/internal/svc"\n'
        "\n"
        "func main() { _ = svc.X }\n"
    ))
    _write(root, "internal/svc/svc.go", "package svc\n\nvar X = 1\n")
    return root


class TestGoModulePathThroughTheCli:
    def test_dry_run_renders_the_plan_and_writes_nothing(
        self, go_project, monkeypatch, capsys
    ):
        before = _snapshot(go_project)
        monkeypatch.chdir(go_project)

        result = rlsbl.app.test([
            "--dry-run", "rewrite", "go-module-path",
            "--from-module", OLD, "--to-module", NEW,
        ])

        assert result.exit_code == 0, (
            f"the preview failed before it could report anything; "
            f"stderr was:\n{result.stderr}"
        )
        assert "go.mod: rewrite" in result.stdout, result.stdout
        assert "main.go: rewrite" in result.stdout, result.stdout
        assert f"{OLD} -> {NEW}" in result.stdout, result.stdout
        assert _snapshot(go_project) == before

    def test_apply_through_the_same_dispatch_writes(
        self, go_project, monkeypatch
    ):
        monkeypatch.chdir(go_project)
        result = rlsbl.app.test([
            "rewrite", "go-module-path",
            "--from-module", OLD, "--to-module", NEW,
        ])
        assert result.exit_code == 0, result.stderr
        assert (go_project / "go.mod").read_text().startswith(f"module {NEW}")
        assert f'"{NEW}/internal/svc"' in (go_project / "main.go").read_text()

    def test_a_typo_exits_one_and_writes_nothing(self, go_project, monkeypatch):
        before = _snapshot(go_project)
        monkeypatch.chdir(go_project)
        result = rlsbl.app.test([
            "--dry-run", "rewrite", "go-module-path",
            "--from-module", "github.com/nobody/here", "--to-module", NEW,
        ])
        assert result.exit_code == 1
        assert "typo" in result.stderr, result.stderr
        assert _snapshot(go_project) == before

    def test_both_module_flags_are_required(self, go_project, monkeypatch):
        monkeypatch.chdir(go_project)
        result = rlsbl.app.test([
            "rewrite", "go-module-path", "--from-module", OLD,
        ])
        assert result.exit_code == 1
        assert "to-module" in result.stderr, result.stderr

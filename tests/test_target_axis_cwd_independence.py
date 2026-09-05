"""The support axes are per-target facts, not facts about the current directory.

Two axes used to probe ``dev_install_command(".")`` -- the PROCESS CWD. The go
target reads ``go.mod`` and ``.rlsbl/config.json`` when its argument is a Go
project, and hard-errors when the go pipeline declares no ``install_paths``. So
in such a repository the axis could not be answered at all, and since
``rlsbl.targets.introspect`` asserts every target answers every axis AT IMPORT,
every rlsbl invocation from that directory died before it parsed a single
argument:

    RuntimeError: target 'go' cannot answer the 'supports_dev_install' support
    axis: GoIntrospectError: the go pipeline in .rlsbl/config.json does not
    declare 'install_paths' ...

That is the error-siting rule broken twice over: a per-target fact answered
from wherever the operator happened to be standing, and a hard error about one
project's configuration fired inside registration code that runs everywhere.
The remedy belongs to ``rlsbl dev install``, which is the command that has a
project directory and something to install from it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.introspect import target_axis_answers


@pytest.fixture
def go_project_without_install_paths(tmp_path):
    """A Go repo whose go pipeline declares no ``install_paths``.

    Two main packages, so no declaration could be derived either -- this is
    exactly the configuration ``rlsbl dev install`` must refuse, and exactly
    the one no axis may consult.
    """
    repo = tmp_path / "gotool"
    (repo / ".rlsbl").mkdir(parents=True)
    (repo / "cmd" / "one").mkdir(parents=True)
    (repo / "cmd" / "two").mkdir(parents=True)
    (repo / "go.mod").write_text(
        "module github.com/example/gotool\n\ngo 1.22\n", encoding="utf-8",
    )
    for name in ("one", "two"):
        (repo / "cmd" / name / "main.go").write_text(
            "package main\n\nfunc main() {}\n", encoding="utf-8",
        )
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({
            "publish_mode": "ci",
            "targets": ["go"],
            "pipelines": {"go": {"type": "go", "local": True}},
        }) + "\n",
        encoding="utf-8",
    )
    return repo


class TestEnumerationDoesNotConsultTheCwd:
    def test_every_target_answers_every_axis_from_such_a_directory(
        self, go_project_without_install_paths, monkeypatch,
    ):
        """The assertion that runs at import must hold from ANY directory."""
        monkeypatch.chdir(go_project_without_install_paths)
        answers = target_axis_answers()
        assert answers["go"]["supports_dev_install"] is True

    def test_no_axis_answer_moves_with_the_current_directory(
        self, go_project_without_install_paths, monkeypatch, tmp_path,
    ):
        """A per-target fact that moves with the cwd is not a per-target fact.

        Every target, every axis -- so an axis added later that reads the
        directory it is standing in is reported here rather than in a bug.
        """
        neutral = tmp_path / "elsewhere"
        neutral.mkdir()
        monkeypatch.chdir(neutral)
        outside = target_axis_answers()
        monkeypatch.chdir(go_project_without_install_paths)
        inside = target_axis_answers()
        moved = {
            f"{target}.{axis}"
            for target, row in inside.items()
            for axis, value in row.items()
            if outside[target][axis] != value
        }
        assert moved == set(), f"axes answered from the cwd: {sorted(moved)}"

    def test_importing_rlsbl_from_that_directory_succeeds(
        self, go_project_without_install_paths,
    ):
        """The observed symptom: every invocation died before parsing argv.

        A subprocess, because the death is at module import and this process
        imported rlsbl long ago. The interpreter is this one and the module
        path is this checkout, so nothing is fetched or installed.
        """
        repo_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [repo_root, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        result = subprocess.run(
            [sys.executable, "-c", "import rlsbl"],
            cwd=str(go_project_without_install_paths),
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestTheRemedyFiresWhereItIsConsumed:
    def test_dev_install_names_install_paths(
        self, go_project_without_install_paths, monkeypatch,
    ):
        """`rlsbl dev install` still refuses, and still names the declaration."""
        import rlsbl

        monkeypatch.chdir(go_project_without_install_paths)
        result = rlsbl.app.test(["dev", "install", "--target", "global"])

        assert result.exit_code == 1, result.stdout
        assert "install_paths" in result.stderr, result.stderr
        assert "./cmd/one" in result.stderr, result.stderr

    def test_the_target_still_refuses_when_asked_about_that_project(
        self, go_project_without_install_paths,
    ):
        """The refusal itself is untouched: only its siting moved."""
        from rlsbl.go_introspect import GoIntrospectError

        with pytest.raises(GoIntrospectError, match="install_paths"):
            TARGETS["go"].dev_install_command(
                str(go_project_without_install_paths)
            )

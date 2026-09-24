"""The ``go-toolchain-declared`` check: a Go target's go.mod carries a
``toolchain`` line.

``actions/setup-go`` reads go.mod's ``toolchain`` line first and the ``go``
directive only when there is none, and the ``go`` directive is the floor
CONSUMERS need, not the Go the project is developed and tested with. A module
with no ``toolchain`` line therefore has its CI test on the oldest Go it
supports while every developer runs a newer one. The check asks only whether
the line is present: it never compares it with the Go installed on the machine
running it (a release-blocking check reads only what the repository owns).
"""

import os
import shutil
import subprocess

import pytest

from rlsbl import app
from rlsbl.checks import CHECK_TARGETS

from conftest import make_ctx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = "go-toolchain-declared"


def _go_project(root, go_mod):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".rlsbl").mkdir()
    (root / ".rlsbl" / "config.json").write_text(
        '{"publish_mode": "ci", "targets": ["go"]}\n')
    (root / "go.mod").write_text(go_mod)
    (root / "VERSION").write_text("0.1.0\n")
    return root


def _run(root):
    return app._check_defs[CHECK].impl(make_ctx(root))


def _text(result):
    return " ".join(p.text for p in result.problems)


def test_a_go_mod_without_a_toolchain_line_is_refused(tmp_path):
    root = _go_project(tmp_path / "mod", "module example.com/m\n\ngo 1.23\n")
    result = _run(root)
    assert result.status == "fail", result
    text = _text(result)
    assert "go.mod declares no toolchain line" in text
    assert "go mod edit -toolchain=<version>" in text


def test_a_toolchain_line_passes_whatever_its_version(tmp_path):
    # Presence only: an old toolchain is not compared with the machine's Go.
    root = _go_project(
        tmp_path / "mod", "module example.com/m\n\ngo 1.21\n\ntoolchain go1.21.0\n")
    assert _run(root).status == "pass"


def test_a_commented_out_toolchain_line_does_not_count(tmp_path):
    root = _go_project(
        tmp_path / "mod", "module example.com/m\n\ngo 1.23\n// toolchain go1.26.6\n")
    assert _run(root).status == "fail"


@pytest.mark.skipif(shutil.which("go") is None, reason="needs the go command")
def test_the_named_remedy_clears_the_refusal(tmp_path):
    root = _go_project(tmp_path / "mod", "module example.com/m\n\ngo 1.23\n")
    assert _run(root).status == "fail"
    subprocess.run(
        ["go", "mod", "edit", "-toolchain=go1.26.6"], cwd=root, check=True,
        env={**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod"},
    )
    assert _run(root).status == "pass"


def test_a_project_with_no_go_target_skips(tmp_path):
    root = tmp_path / "py"
    root.mkdir()
    (root / ".rlsbl").mkdir()
    (root / ".rlsbl" / "config.json").write_text(
        '{"publish_mode": "ci", "targets": ["pypi"]}\n')
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n')
    assert _run(root).status == "skip"


class TestRegistration:
    def test_it_blocks_a_release_and_reads_only_the_repository(self):
        import tomllib

        with open(os.path.join(REPO_ROOT, "rlsbl", "data", "checks.toml"), "rb") as f:
            meta = tomllib.load(f)["checks"][CHECK]
        assert meta["severity"] == "error"
        assert "preflight" in meta["tags"]
        assert "project" in meta["tags"]
        assert "release" not in meta["tags"]
        assert meta["needs_network"] is False
        assert meta["pure"] is True

    def test_it_is_scoped_to_the_go_target(self):
        assert CHECK_TARGETS[CHECK] == frozenset({"go"})

    def test_it_has_a_row_in_the_docs_check_reference(self):
        text = open(os.path.join(REPO_ROOT, ".stricttools", "docs", "checks.md"),
                    encoding="utf-8").read()
        assert f"| `{CHECK}` |" in text

    def test_it_is_in_the_expected_checks_roster(self):
        from test_doctor_checks_migration import EXPECTED_CHECKS

        assert CHECK in EXPECTED_CHECKS

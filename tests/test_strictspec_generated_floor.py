"""Tests for the ``strictspec-generated-floor`` check.

The failure class, which broke a release attempt: a generated validator calls
``strictspec.require_runtime_version(GENERATED_BY)`` at IMPORT and that pairing
is exact. The repo's own lock resolves the strictspec that generated the file,
so every local test passes -- but a consumer installing the published artifact
resolves whatever the DECLARED floor allows, and an older strictspec makes the
validator's import raise.

``dep-floors`` cannot see this: it compares major.minor, and it compares the
lock rather than the generated code.
"""

import json

import pytest

from rlsbl import app
from rlsbl.strictspec_floor import (
    declared_floor,
    evaluate_strictspec_floor,
    parse_version,
    read_generated_by,
)

from conftest import make_ctx


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _project(root, *, floor="strictspec>=0.2.3", generated=("0.2.3",),
             lang="python", missing_output=False):
    root.mkdir(parents=True, exist_ok=True)
    deps = [] if floor is None else [floor]
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "consumer"\n'
        'version = "0.1.0"\n'
        f"dependencies = {json.dumps(deps)}\n"
    )
    lines = []
    gen_dir = root / "pkg" / "strictspec_gen"
    gen_dir.mkdir(parents=True, exist_ok=True)
    for index, stamp in enumerate(generated):
        output = f"pkg/strictspec_gen/v{index}_validator.py"
        lines.append("[[schemas]]")
        lines.append(f'path = ".strictspec/v{index}.schema.toml"')
        lines.append("  [[schemas.targets]]")
        lines.append(f'  lang    = "{lang}"')
        lines.append(f'  output  = "{output}"')
        if not missing_output:
            body = "import strictspec\n"
            if stamp is not None:
                body += f'GENERATED_BY = "{stamp}"\n'
            body += "strictspec.require_runtime_version(GENERATED_BY)\n"
            (root / output).write_text(body)
    (root / "strictspec.toml").write_text(
        "format_version = 1\n\n" + "\n".join(lines) + "\n"
    )
    return root


def _run(root):
    ctx = make_ctx(root, config={"publish_mode": "ci"})
    return app._check_defs["strictspec-generated-floor"].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Version handling at full patch precision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("0.2.3", (0, 2, 3)),
    ("v0.2.3", (0, 2, 3)),
    ("1.4", (1, 4, 0)),
    ("2", (2, 0, 0)),
    ("not a version", None),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("constraint,expected", [
    (">=0.2.3", (0, 2, 3)),
    (">=0.2.3,<0.3", (0, 2, 3)),
    ("==0.2.4", (0, 2, 4)),
    ("<0.3", None),
    ("", None),
])
def test_declared_floor(constraint, expected):
    assert declared_floor(constraint) == expected


def test_read_generated_by(tmp_path):
    path = tmp_path / "v.py"
    path.write_text('# comment\nGENERATED_BY = "0.9.1"\n')
    assert read_generated_by(path) == "0.9.1"


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


class TestFloorComparison:
    def test_a_floor_at_the_generated_release_passes(self, tmp_path):
        _project(tmp_path, floor="strictspec>=0.2.3", generated=("0.2.3",))
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_floor_one_patch_behind_errors(self, tmp_path):
        """The exact shape dep-floors cannot see: a PATCH-level difference."""
        _project(tmp_path, floor="strictspec>=0.2.3", generated=("0.2.4",))
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "0.2.4" in text
        assert '"strictspec>=0.2.4"' in text

    def test_a_floor_ahead_of_the_generated_release_passes(self, tmp_path):
        _project(tmp_path, floor="strictspec>=0.3.0", generated=("0.2.3",))
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_the_highest_generated_by_decides(self, tmp_path):
        _project(
            tmp_path, floor="strictspec>=0.2.3",
            generated=("0.2.3", "0.2.9", "0.2.1"),
        )
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "0.2.9" in _text(result)

    def test_a_dependency_with_no_floor_errors(self, tmp_path):
        _project(tmp_path, floor="strictspec", generated=("0.2.3",))
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "no version floor" in _text(result)

    def test_an_undeclared_dependency_errors(self, tmp_path):
        _project(tmp_path, floor=None, generated=("0.2.3",))
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "no manifest section declares strictspec" in _text(result)

    def test_a_floor_in_a_dependency_group_is_read(self, tmp_path):
        _project(tmp_path, floor=None, generated=("0.2.3",))
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "c"\nversion = "0.1.0"\ndependencies = []\n\n'
            '[dependency-groups]\ndev = ["strictspec>=0.2.3"]\n'
        )
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_generated_file_without_the_constant_errors(self, tmp_path):
        _project(tmp_path, generated=(None,))
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "no GENERATED_BY" in _text(result)

    def test_a_non_python_target_without_the_constant_is_a_note(self, tmp_path):
        _project(tmp_path, generated=(None,), lang="go")
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_declared_but_ungenerated_output_is_a_note(self, tmp_path):
        _project(tmp_path, generated=("0.2.3",), missing_output=True)
        result = _run(tmp_path)
        assert result.status == "pass"
        assert "not generated yet" in result.message


class TestApplicability:
    def test_a_project_without_a_manifest_skips(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "c"\nversion = "0.1.0"\ndependencies = []\n'
        )
        result = _run(tmp_path)
        assert result.status == "skip"
        assert "strictspec.toml" in result.message

    def test_a_manifest_with_no_targets_skips(self, tmp_path):
        (tmp_path / "strictspec.toml").write_text("format_version = 1\n")
        result = _run(tmp_path)
        assert result.status == "skip"

    def test_a_missing_pyproject_is_reported(self, tmp_path):
        _project(tmp_path, generated=("0.2.3",))
        (tmp_path / "pyproject.toml").unlink()
        verdict = evaluate_strictspec_floor(tmp_path)
        assert not verdict.ok
        assert "no readable pyproject.toml" in verdict.problems[0]

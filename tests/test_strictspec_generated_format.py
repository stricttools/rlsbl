"""Tests for the ``strictspec-generated-format`` check.

A strictspec-generated validator declares the SHAPE it was written to as an
integer ``GENERATED_CODE_FORMAT`` and calls
``strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)``
at import. Every runtime declares the inclusive range of formats it reads, so
the pairing holds across releases and the only thing that breaks it is a
committed validator whose format the runtime does not read. The remedy is
always regeneration.

``GENERATED_BY`` is informational. Nothing here derives a dependency floor from
it, and a declared ``strictspec`` floor far below it is not this check's
business -- that check was what the format declaration replaced.
"""

import json
from pathlib import Path

import strictspec

from rlsbl import app
from rlsbl.strictspec_floor import (
    accepted_format_range,
    declared_targets,
    evaluate_strictspec_generated_format,
    read_generated_code_format,
)

from conftest import make_ctx


MIN_FORMAT, MAX_FORMAT = strictspec.MIN_GENERATED_CODE_FORMAT, strictspec.MAX_GENERATED_CODE_FORMAT


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _project(root, *, floor="strictspec>=0.3.0", formats=(MIN_FORMAT,),
             generated_by="0.3.0", lang="python", missing_output=False):
    """A project declaring one validator per entry in ``formats``.

    A ``None`` entry writes a validator carrying no ``GENERATED_CODE_FORMAT``
    at all -- generated before the declaration existed, which the runtime
    refuses rather than reading as format 1.
    """
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
    for index, fmt in enumerate(formats):
        output = f"pkg/strictspec_gen/v{index}_validator.py"
        lines.append("[[schemas]]")
        lines.append(f'path = ".strictspec/v{index}.schema.toml"')
        lines.append("  [[schemas.targets]]")
        lines.append(f'  lang    = "{lang}"')
        lines.append(f'  output  = "{output}"')
        if not missing_output:
            body = "import strictspec\n"
            body += f'GENERATED_BY = "{generated_by}"\n'
            if fmt is not None:
                body += f"GENERATED_CODE_FORMAT = {fmt}\n"
                body += (
                    "strictspec.require_generated_code_format("
                    "GENERATED_CODE_FORMAT, GENERATED_BY)\n"
                )
            (root / output).write_text(body)
    (root / "strictspec.toml").write_text(
        "format_version = 1\n\n" + "\n".join(lines) + "\n"
    )
    return root


def _run(root):
    ctx = make_ctx(root, config={"publish_mode": "ci"})
    return app._check_defs["strictspec-generated-format"].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Reading the two sides
# ---------------------------------------------------------------------------


def test_accepted_format_range_is_the_runtimes_own():
    assert accepted_format_range() == (MIN_FORMAT, MAX_FORMAT)


def test_read_generated_code_format(tmp_path):
    path = tmp_path / "v.py"
    path.write_text('# comment\nGENERATED_BY = "0.3.0"\nGENERATED_CODE_FORMAT = 4\n')
    assert read_generated_code_format(path) == 4


def test_read_generated_code_format_absent(tmp_path):
    path = tmp_path / "v.py"
    path.write_text('GENERATED_BY = "0.2.5"\n')
    assert read_generated_code_format(path) is None


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


class TestFormatComparison:
    def test_a_format_the_runtime_reads_passes(self, tmp_path):
        _project(tmp_path, formats=(MAX_FORMAT,))
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_format_above_the_range_errors(self, tmp_path):
        _project(tmp_path, formats=(MAX_FORMAT + 1,))
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "pkg/strictspec_gen/v0_validator.py" in text
        assert str(MAX_FORMAT + 1) in text
        assert "regenerate with `strictspec gen`" in text

    def test_a_format_below_the_range_errors(self, tmp_path):
        _project(tmp_path, formats=(MIN_FORMAT - 1,))
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "pkg/strictspec_gen/v0_validator.py" in _text(result)

    def test_a_validator_predating_the_declaration_errors(self, tmp_path):
        _project(tmp_path, formats=(None,))
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "no GENERATED_CODE_FORMAT" in text
        assert "regenerate with `strictspec gen`" in text

    def test_every_unreadable_validator_is_named(self, tmp_path):
        _project(tmp_path, formats=(MAX_FORMAT, MAX_FORMAT + 1, None))
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "v1_validator.py" in text
        assert "v2_validator.py" in text
        assert "v0_validator.py" not in text

    def test_no_dependency_floor_is_derived_from_the_release_stamp(self, tmp_path):
        """``GENERATED_BY`` is information, and nothing reads it as a floor.

        A validator stamped far ahead of the declared floor used to be an
        error. Pairing is on the format now, so the same project passes.
        """
        _project(
            tmp_path,
            floor="strictspec>=0.1.0",
            generated_by="9.9.9",
            formats=(MAX_FORMAT,),
        )
        result = _run(tmp_path)
        assert result.status == "pass"
        assert "9.9.9" not in result.message

    def test_a_project_declaring_no_strictspec_at_all_still_passes(self, tmp_path):
        _project(tmp_path, floor=None, formats=(MAX_FORMAT,))
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_non_python_target_without_the_constant_is_a_note(self, tmp_path):
        _project(tmp_path, formats=(None,), lang="go")
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_declared_but_ungenerated_output_is_a_note(self, tmp_path):
        _project(tmp_path, formats=(MAX_FORMAT,), missing_output=True)
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

    def test_a_manifest_declaring_only_ungenerated_outputs_passes(self, tmp_path):
        _project(tmp_path, formats=(MAX_FORMAT, MAX_FORMAT), missing_output=True)
        verdict = evaluate_strictspec_generated_format(tmp_path)
        assert verdict.ok


class TestThisRepositorysCommittedValidators:
    """The pairing this repository ships, read off disk.

    Every test above builds a fixture project. This one reads rlsbl's own
    committed validators against the strictspec the environment resolves,
    which is the pairing an installer performs: a committed validator whose
    format the installed runtime does not read makes `rlsbl status`,
    `rlsbl unreleased` and `rlsbl changelog add` raise on import.
    """

    def test_every_committed_validator_declares_a_format_the_runtime_reads(self):
        root = Path(__file__).resolve().parent.parent
        targets = declared_targets(root)
        assert targets, "strictspec.toml declares no generation targets"

        low, high = accepted_format_range()
        formats = {}
        for output, lang in targets:
            if lang != "python":
                continue
            path = root / output
            assert path.is_file(), f"{output} is declared but not generated"
            formats[output] = read_generated_code_format(path)

        assert formats, "the manifest declares no python validator"
        unread = {
            output: fmt
            for output, fmt in formats.items()
            if fmt is None or not low <= fmt <= high
        }
        assert not unread, (
            f"the installed strictspec {strictspec.__version__} reads "
            f"generated-code formats {low}..{high}, but these validators "
            f"declare otherwise: {unread}. Regenerate with "
            f"`strictspec gen --manifest strictspec.toml`."
        )

    def test_the_predicate_agrees_with_the_range(self):
        """The check's verdict is the runtime's own, not a second opinion."""
        root = Path(__file__).resolve().parent.parent
        verdict = evaluate_strictspec_generated_format(root)
        assert verdict.ok, verdict.problems

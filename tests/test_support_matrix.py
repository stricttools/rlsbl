"""The committed support matrix, its completeness assertions, and its freshness check.

``rlsbl/data/support-matrix.json`` is the machine-readable answer to "what does
every release target support". The docs directives read it instead of importing
rlsbl, so a stale file would silently ship wrong documentation -- hence the
regenerate-and-compare check, and hence the completeness assertions that make a
missing answer an import-time error rather than a blank cell.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from strictcli import ErrorReporter, WarnReporter

from rlsbl.checks import CHECK_TARGETS
from rlsbl.targets import TARGETS
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.introspect import (
    AXIS_NAMES,
    MATRIX_FORMAT_VERSION,
    MATRIX_RELPATH,
    TARGET_AXES,
    TargetAxis,
    assert_axis_inventory_is_complete,
    assert_every_target_answers_every_axis,
    build_matrix,
    declared_support_surfaces,
    matrix_path,
    render_matrix,
    target_axis_answers,
    write_matrix,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED = REPO_ROOT / MATRIX_RELPATH


# ---------------------------------------------------------------------------
# The artifact itself
# ---------------------------------------------------------------------------


class TestTheCommittedArtifact:

    def test_it_exists_and_is_json(self):
        assert COMMITTED.is_file(), f"{MATRIX_RELPATH} is not committed"
        json.loads(COMMITTED.read_text(encoding="utf-8"))

    def test_it_matches_a_fresh_regeneration(self):
        assert COMMITTED.read_text(encoding="utf-8") == render_matrix(), (
            f"{MATRIX_RELPATH} is stale; regenerate it with "
            f"`uv run python scripts/generate_support_matrix.py`"
        )

    def test_serialization_is_deterministic(self):
        assert render_matrix() == render_matrix()

    def test_it_carries_a_format_version(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert doc["format_version"] == MATRIX_FORMAT_VERSION

    def test_every_registered_target_has_a_row(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert set(doc["targets"]) == set(TARGETS)

    def test_every_axis_appears_in_every_row(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        for name, row in doc["targets"].items():
            assert set(row) == set(AXIS_NAMES), f"target '{name}' row is short"

    def test_every_registered_check_has_a_scope(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert set(doc["checks"]) == set(CHECK_TARGETS)

    def test_it_carries_every_docs_table(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert set(doc["tables"]) == {"axes", "targets", "feature_matrix", "pipelines"}
        for table in doc["tables"].values():
            assert table["headers"]
            assert table["rows"]
            for row in table["rows"]:
                assert len(row) == len(table["headers"])


class TestWriteMatrix:

    def test_writing_into_an_empty_tree_reports_a_change(self, tmp_path):
        assert write_matrix(str(tmp_path)) is True
        assert (tmp_path / MATRIX_RELPATH).read_text(encoding="utf-8") == render_matrix()

    def test_rewriting_an_identical_file_reports_no_change(self, tmp_path):
        write_matrix(str(tmp_path))
        assert write_matrix(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Completeness, in both directions
# ---------------------------------------------------------------------------


class _MinimalTarget(BaseTarget):
    """A well-formed target: inherits every axis answer from the base."""

    @property
    def name(self):
        return "minimal"


class _AxisBlindTarget:
    """A target that answers nothing -- not even ``ecosystem``."""

    name = "axis-blind"


class TestCompletenessOfTheTargetDirection:

    def test_the_live_registry_answers_every_axis(self):
        answers = target_axis_answers()
        assert set(answers) == set(TARGETS)
        for row in answers.values():
            assert set(row) == set(AXIS_NAMES)

    def test_a_well_formed_synthetic_target_is_accepted(self):
        assert_every_target_answers_every_axis(registry={"minimal": _MinimalTarget()})

    def test_a_target_that_cannot_answer_an_axis_is_an_error(self):
        with pytest.raises(RuntimeError) as exc:
            assert_every_target_answers_every_axis(
                registry={"axis-blind": _AxisBlindTarget()}
            )
        assert "axis-blind" in str(exc.value)
        assert "support axis" in str(exc.value)

    def test_the_error_names_the_axis_that_went_unanswered(self):
        class _NoEcosystem(_MinimalTarget):
            @property
            def ecosystem(self):
                raise AttributeError("no ecosystem here")

        with pytest.raises(RuntimeError, match="'ecosystem' support axis"):
            assert_every_target_answers_every_axis(registry={"x": _NoEcosystem()})

    def test_an_unserializable_answer_is_an_error(self):
        axis = TargetAxis("weird", "returns something JSON cannot hold", lambda t: object())
        with pytest.raises(RuntimeError, match="cannot serialize"):
            assert_every_target_answers_every_axis(
                registry={"minimal": _MinimalTarget()}, axes=(axis,)
            )


class TestCompletenessOfTheAxisDirection:

    def test_the_live_inventory_covers_every_declared_support_surface(self):
        assert declared_support_surfaces() <= set(AXIS_NAMES)

    def test_a_new_support_surface_without_an_axis_is_an_error(self):
        class _WithANewAxis(BaseTarget):
            @property
            def supports_telepathy(self):
                return False

        with pytest.raises(RuntimeError, match="supports_telepathy"):
            assert_axis_inventory_is_complete(cls=_WithANewAxis)

    def test_the_error_points_at_the_inventory_and_the_regeneration(self):
        class _WithANewAxis(BaseTarget):
            @property
            def provides_teleportation(self):
                return False

        with pytest.raises(RuntimeError) as exc:
            assert_axis_inventory_is_complete(cls=_WithANewAxis)
        assert "TARGET_AXES" in str(exc.value)
        assert "generate_support_matrix" in str(exc.value)

    def test_every_axis_name_is_unique(self):
        assert len(AXIS_NAMES) == len(set(AXIS_NAMES))

    def test_every_axis_states_what_it_means(self):
        for axis in TARGET_AXES:
            assert axis.doc.strip(), axis.name


class TestTheNewlyDerivedSupportProperties:
    """The registry-derived sets now read a property instead of comparing types."""

    def test_version_query_support_matches_the_override(self):
        for name, target in TARGETS.items():
            expected = (
                type(target).query_latest_version is not BaseTarget.query_latest_version
            )
            assert target.supports_version_query is expected, name

    def test_name_claim_support_matches_the_override(self):
        for name, target in TARGETS.items():
            expected = (
                type(target).claim_placeholder is not BaseTarget.claim_placeholder
            )
            assert target.supports_name_claim is expected, name

    def test_yank_support_matches_the_override(self):
        for name, target in TARGETS.items():
            expected = type(target).yank is not BaseTarget.yank
            assert target.supports_yank is expected, name

    def test_the_base_answers_no_to_all_three(self):
        base = _MinimalTarget()
        assert base.supports_version_query is False
        assert base.supports_name_claim is False
        assert base.supports_yank is False


# ---------------------------------------------------------------------------
# The freshness check
# ---------------------------------------------------------------------------


def _project_check(name):
    """Return a runnable version of a registered project check."""
    from rlsbl.checks.project import register_project_checks

    mock_app = MagicMock()
    checks = {}

    def _make_capture(reporter_cls):
        def capture_check(check_name):
            def decorator(fn):
                def run(ctx):
                    return fn(ctx, reporter_cls())
                checks[check_name] = run
                return fn
            return decorator
        return capture_check

    mock_app.error_check = _make_capture(ErrorReporter)
    mock_app.warn_check = _make_capture(WarnReporter)
    register_project_checks(mock_app)
    return checks[name]


def _ctx(root):
    from rlsbl.context import ProjectContext

    return ProjectContext(project_root=root, workspace_root=None, config={})


class TestFreshnessCheck:

    def test_it_skips_where_the_artifact_does_not_live(self, tmp_path):
        outcome = _project_check("target-matrix-fresh")(_ctx(tmp_path))
        assert outcome.status == "skip"

    def test_a_freshly_written_artifact_passes(self, tmp_path):
        write_matrix(str(tmp_path))
        outcome = _project_check("target-matrix-fresh")(_ctx(tmp_path))
        assert outcome.status == "pass", outcome

    def test_a_tampered_artifact_fails(self, tmp_path):
        write_matrix(str(tmp_path))
        path = Path(matrix_path(str(tmp_path)))
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["targets"]["plain"]["provides_ci_templates"] = True
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        outcome = _project_check("target-matrix-fresh")(_ctx(tmp_path))
        assert outcome.status == "fail"
        assert any("regenerate" in p.text.lower() for p in outcome.problems), outcome

    def test_an_empty_artifact_fails(self, tmp_path):
        path = Path(matrix_path(str(tmp_path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        outcome = _project_check("target-matrix-fresh")(_ctx(tmp_path))
        assert outcome.status == "fail"

    def test_the_real_repo_is_fresh(self):
        outcome = _project_check("target-matrix-fresh")(_ctx(REPO_ROOT))
        assert outcome.status == "pass", outcome


class TestFourPlaceRegistration:
    """A new check is registered in four places; none of them may be missed.

    A fifth place exists outside this class's reach: the EXPECTED_CHECKS
    roster in tests/test_doctor_checks_migration.py, whose equality test
    fails loudly when a check lands in checks.toml without joining it.
    """

    def test_it_is_in_the_checks_metadata_registry(self):
        import tomllib

        with open(REPO_ROOT / "rlsbl" / "data" / "checks.toml", "rb") as f:
            checks = tomllib.load(f)["checks"]
        meta = checks["target-matrix-fresh"]
        assert meta["severity"] == "error"
        assert "project" in meta["tags"]
        assert "preflight" in meta["tags"]

    def test_it_is_in_the_check_to_target_matrix(self):
        assert CHECK_TARGETS["target-matrix-fresh"] is None

    def test_it_has_a_row_in_the_docs_check_reference(self):
        text = (REPO_ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        assert "| `target-matrix-fresh` |" in text

    def test_the_matrix_records_its_own_scope(self):
        doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert doc["checks"]["target-matrix-fresh"]["kind"] == "universal"


def test_build_matrix_needs_no_arguments():
    """The generator asks the registries; it is handed nothing."""
    assert build_matrix()["targets"]

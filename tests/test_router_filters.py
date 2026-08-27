"""Router path filters: what the generator emits, and whether we can read it.

Two halves that have to agree exactly, and used not to:

- :class:`rlsbl.router_filters.RouterFilters` derives each member's patterns
  from the workspace (ownership territories, dependency territories, root
  manifests and lockfiles, tool machinery, and the root member's negated
  excludes);
- :func:`rlsbl.router_filters.matches_filter` reads them the way
  ``dorny/paths-filter`` does, which the release flow relies on when it refuses
  a candidate whose push window could only produce skipped jobs.

The reader's conformance is not asserted from the action's README. It is
replayed from ``tests/data/paths_filter_verdicts.json``, whose every verdict
was produced by running the pinned action's own ``src/filter.ts`` -- see
``scripts/capture_paths_filter_verdicts.js``.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from strictcli import ErrorReporter, WarnReporter

from conftest import make_workspace
from rlsbl.checks import CHECK_TARGETS
from rlsbl.router_filters import (
    MATCH_EVERYTHING,
    PREDICATE_QUANTIFIER,
    ROUTER_HEADER,
    ROUTER_WORKFLOW_PATH,
    RouterFilters,
    any_path_matches,
    matches_filter,
    root_trigger_files,
)
from rlsbl.workspace import load_releasables, load_workspace
from routerharness import generate_router


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "data" / "paths_filter_verdicts.json"
CAPTURE_SCRIPT = "scripts/capture_paths_filter_verdicts.js"


def _fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _cases():
    return [(case["name"], case) for case in _fixture()["cases"]]


class TestMatcherConformance:
    """The Python matcher answers what the real action answered."""

    @pytest.mark.parametrize("name,case", _cases(), ids=lambda v: v if isinstance(v, str) else "")
    def test_it_reproduces_every_captured_verdict(self, name, case):
        patterns = case["patterns"]
        for path, expected in case["verdicts"].items():
            assert matches_filter(path, patterns) is expected, (
                f"{name}: {path!r} against {patterns} -- "
                f"dorny/paths-filter said {expected}, rlsbl says "
                f"{matches_filter(path, patterns)}"
            )

    def test_the_corpus_covers_negation(self):
        """A corpus of only positive patterns would prove nothing about excludes."""
        assert any(case["has_negation"] for _n, case in _cases())

    def test_any_path_matches_is_the_per_path_question(self):
        """The diff-level helper asks the whole filter once per path.

        Never ``any(pattern matches path)`` over the cross product: that reads
        an exclude as an independent way to match.
        """
        patterns = [MATCH_EVERYTHING, "!pkg/**"]
        assert any_path_matches(["README.md", "pkg/a.py"], patterns)
        assert not any_path_matches(["pkg/a.py", "pkg/b.py"], patterns)


class TestQuantifierNecessity:
    """Why the router declares ``predicate-quantifier`` at all.

    The captured fixture holds both answers for every case. Under the action's
    DEFAULT quantifier a negated pattern matches everything outside itself, so
    the root member's ``**`` + ``!other/**`` would match the very paths it
    excludes. These assertions are the recorded evidence for that, not a claim
    about it.
    """

    def test_negation_free_filters_read_the_same_under_either_quantifier(self):
        for name, case in _cases():
            if case["has_negation"]:
                continue
            assert case["verdicts"] == case["verdicts_default_quantifier"], name

    def test_every_negated_filter_reads_differently_under_the_default(self):
        for name, case in _cases():
            if not case["has_negation"]:
                continue
            assert case["verdicts"] != case["verdicts_default_quantifier"], (
                f"{name}: the default quantifier gave the same answers, so the "
                f"fixture no longer demonstrates why {PREDICATE_QUANTIFIER} is "
                f"declared"
            )

    def test_the_generated_router_declares_it(self):
        content = generate_router([{"name": "core", "path": "core", "_ci_docs": []}])
        assert f"predicate-quantifier: {PREDICATE_QUANTIFIER}" in content


class TestFixtureToPinLinkage:
    """The fixture is only evidence about the version it was captured from."""

    def test_it_records_the_version_it_was_captured_from(self):
        doc = _fixture()
        assert doc["action"] == "dorny/paths-filter"
        assert doc["action_version"]
        assert doc["action_tarball_sha256"]
        assert doc["generator"] == CAPTURE_SCRIPT

    def test_it_matches_the_pinned_action_version(self):
        from rlsbl.action_versions import get_action_version

        pinned = get_action_version("dorny/paths-filter")
        recorded = _fixture()["action_version"]
        assert recorded == pinned, (
            f"the captured verdicts came from dorny/paths-filter@{recorded} but "
            f"the workflows now pin @{pinned}. The matcher's conformance is "
            f"unproven against the pinned version: re-capture with "
            f"`node {CAPTURE_SCRIPT}` and commit the result."
        )

    def test_it_records_the_quantifier_the_router_declares(self):
        assert _fixture()["predicate_quantifier"] == PREDICATE_QUANTIFIER

    def test_the_capture_script_is_committed_and_executable(self):
        script = REPO_ROOT / CAPTURE_SCRIPT
        assert script.is_file()
        assert os.access(script, os.X_OK)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _member(name, path, **extra):
    return {"name": name, "path": path, **extra}


class TestOwnershipTerritories:

    def test_a_member_owns_its_declared_path(self, tmp_path):
        members = [_member("root", "."), _member("core", "packages/core")]
        filters = RouterFilters(tmp_path, members)
        assert "packages/core/**" in filters.patterns_for(members[1])

    def test_the_root_member_matches_everything_minus_the_others(self, tmp_path):
        members = [
            _member("root", "."),
            _member("core", "packages/core"),
            _member("cli", "packages/cli"),
        ]
        patterns = RouterFilters(tmp_path, members).patterns_for(members[0])
        assert patterns[0] == MATCH_EVERYTHING
        assert "!packages/core/**" in patterns
        assert "!packages/cli/**" in patterns

    def test_the_root_members_filter_reads_as_the_residual(self, tmp_path):
        members = [_member("root", "."), _member("core", "packages/core")]
        patterns = RouterFilters(tmp_path, members).patterns_for(members[0])
        assert matches_filter("README.md", patterns)
        assert matches_filter("docs/guide.md", patterns)
        assert not matches_filter("packages/core/src/index.ts", patterns)

    def test_the_root_member_keeps_a_territory_it_depends_on(self, tmp_path):
        """An exclude would cut the root member off from its own dependency."""
        members = [
            _member("root", ".", depends_on=["core"]),
            _member("core", "packages/core"),
            _member("cli", "packages/cli"),
        ]
        patterns = RouterFilters(tmp_path, members).patterns_for(members[0])
        assert "!packages/core/**" not in patterns
        assert "!packages/cli/**" in patterns
        assert matches_filter("packages/core/src/index.ts", patterns)


class TestDependencyTerritories:

    def test_a_dependency_edge_appears_in_the_dependents_filter(self, tmp_path):
        members = [
            _member("root", "."),
            _member("app", "apps/web", depends_on=["core"]),
            _member("core", "packages/core"),
        ]
        filters = RouterFilters(tmp_path, members)
        app = filters.patterns_for(members[1])
        assert "packages/core/**" in app
        assert matches_filter("packages/core/src/index.ts", app)
        # ...and the edge is directed.
        assert "apps/web/**" not in filters.patterns_for(members[2])

    def test_dependency_territories_are_transitive(self, tmp_path):
        members = [
            _member("root", "."),
            _member("app", "apps/web", depends_on=["mid"]),
            _member("mid", "packages/mid", depends_on=["core"]),
            _member("core", "packages/core"),
        ]
        app = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert "packages/mid/**" in app
        assert "packages/core/**" in app

    def test_a_dependency_on_the_root_member_widens_to_everything(self, tmp_path):
        """The root's territory is the residual; it has no positive spelling.

        Importing the root's own excludes would cancel the dependent's own
        territory (an exclude is final), so the dependent widens instead.
        Over-triggering is safe; under-triggering deadlocks a release on a
        skipped job.
        """
        members = [
            _member("root", "."),
            _member("app", "apps/web", depends_on=["root"]),
        ]
        app = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert MATCH_EVERYTHING in app
        assert not any(p.startswith("!") for p in app)
        assert matches_filter("anything/at/all.txt", app)

    def test_a_missing_dependency_is_a_hard_error(self, tmp_path):
        """Construction never swallows a graph failure into a narrower filter."""
        from rlsbl.errors import WorkspaceError

        members = [_member("root", "."), _member("app", "apps/web", depends_on=["ghost"])]
        with pytest.raises(WorkspaceError):
            RouterFilters(tmp_path, members)


class TestBuiltInTriggers:

    def test_root_manifests_and_lockfiles_trigger_every_member(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "uv.lock").write_text("")
        members = [_member("root", "."), _member("core", "packages/core")]
        core = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert "pyproject.toml" in core
        assert "uv.lock" in core
        assert matches_filter("uv.lock", core)

    def test_absent_root_files_are_not_emitted(self, tmp_path):
        members = [_member("root", "."), _member("core", "packages/core")]
        core = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert "go.sum" not in core
        assert root_trigger_files(tmp_path) == []

    def test_a_nested_manifest_is_not_a_root_trigger(self, tmp_path):
        (tmp_path / "packages" / "core").mkdir(parents=True)
        (tmp_path / "packages" / "core" / "go.mod").write_text("module x\n")
        members = [_member("root", "."), _member("cli", "packages/cli")]
        cli = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert "go.mod" not in cli

    def test_the_router_itself_reruns_everything(self, tmp_path):
        members = [_member("root", "."), _member("core", "packages/core")]
        core = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert ROUTER_WORKFLOW_PATH in core
        assert matches_filter(ROUTER_WORKFLOW_PATH, core)

    def test_the_root_member_needs_no_built_ins(self, tmp_path):
        """``**`` already covers them and no exclude can take them away."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        members = [_member("root", "."), _member("core", "packages/core")]
        root = RouterFilters(tmp_path, members).patterns_for(members[0])
        assert root == [MATCH_EVERYTHING, "!packages/core/**"]
        assert matches_filter("pyproject.toml", root)
        assert matches_filter(ROUTER_WORKFLOW_PATH, root)


class TestDeterminism:

    def test_the_pattern_order_is_stable(self, tmp_path):
        members = [
            _member("root", "."),
            _member("app", "apps/web", depends_on=["core", "mid"]),
            _member("mid", "packages/mid"),
            _member("core", "packages/core"),
        ]
        first = RouterFilters(tmp_path, members).patterns_for(members[1])
        second = RouterFilters(tmp_path, members).patterns_for(members[1])
        assert first == second
        assert first == sorted(set(first), key=first.index), "no duplicates"

    def test_the_block_renders_one_entry_per_member(self, tmp_path):
        members = [_member("root", "."), _member("core", "packages/core")]
        block = RouterFilters(tmp_path, members).filters_block()
        assert block.startswith("root:\n")
        assert "core:\n" in block
        assert block.endswith("\n")


# ---------------------------------------------------------------------------
# The freshness check
# ---------------------------------------------------------------------------


def _workspace_check(name):
    """Return a runnable version of a registered workspace check."""
    from rlsbl.checks.workspace import register_workspace_checks

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
    register_workspace_checks(mock_app)
    return checks[name]


def _wctx(root):
    from rlsbl.check_context import WorkspaceCheckContext

    projects = load_workspace(str(root))
    return WorkspaceCheckContext(
        project_root=Path(root),
        workspace_root=Path(root),
        config={},
        projects=projects,
        graph=None,
        releasables=load_releasables(str(root), projects),
    )


def _write_router(root, members, releasables=None):
    """Write a router whose filters block is the fresh derivation."""
    projects = [dict(m.to_dict() if hasattr(m, "to_dict") else m) for m in members]
    for proj in projects:
        proj["_ci_docs"] = []
    content = generate_router(projects, releasables=releasables, root=str(root))
    wf = Path(root) / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ci-router.yml").write_text(content, encoding="utf-8")
    return wf / "ci-router.yml"


@pytest.fixture
def router_workspace(tmp_path):
    make_workspace(str(tmp_path), [
        {"path": "packages/core", "name": "core"},
        {"path": "packages/cli", "name": "cli", "depends_on": ["core"]},
    ])
    members = load_workspace(str(tmp_path))
    _write_router(tmp_path, members, load_releasables(str(tmp_path), members))
    return tmp_path


class TestRouterFiltersFreshCheck:

    def test_it_skips_outside_a_workspace(self, tmp_path):
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=tmp_path, workspace_root=None, config={})
        assert _workspace_check("router-filters-fresh")(ctx).status == "skip"

    def test_it_skips_when_no_router_exists(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "packages/core", "name": "core"}])
        outcome = _workspace_check("router-filters-fresh")(_wctx(tmp_path))
        assert outcome.status == "skip"

    def test_a_freshly_generated_router_passes(self, router_workspace):
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "pass", outcome

    def test_a_stale_block_fails(self, router_workspace):
        router = router_workspace / ".github" / "workflows" / "ci-router.yml"
        router.write_text(
            router.read_text().replace("- 'packages/core/**'", "- 'packages/nope/**'"),
            encoding="utf-8",
        )
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "fail"
        assert any("regenerate" in p.text.lower() for p in outcome.problems), outcome

    def test_a_new_dependency_edge_makes_it_stale(self, router_workspace):
        """The exact drift the check exists for: the graph moved, the router did not."""
        from rlsbl.workspace import save_workspace

        members = load_workspace(str(router_workspace))
        for member in members:
            if member["name"] == "core":
                member["depends_on"] = ["cli"]
        save_workspace(
            str(router_workspace), members,
            releasables=load_releasables(str(router_workspace), members),
        )
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "fail"

    def test_a_downgraded_quantifier_fails(self, router_workspace):
        router = router_workspace / ".github" / "workflows" / "ci-router.yml"
        router.write_text(
            router.read_text().replace(
                f"predicate-quantifier: {PREDICATE_QUANTIFIER}",
                "predicate-quantifier: some",
            ),
            encoding="utf-8",
        )
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "fail"
        assert any("quantifier" in p.text for p in outcome.problems), outcome

    def test_it_skips_a_router_it_did_not_generate(self, router_workspace):
        """Freshness compares what sync wrote against what sync would write.

        A hand-authored ci-router.yml has no generated form to be stale
        against, and calling it stale would be a claim about a file rlsbl
        does not own.
        """
        router = router_workspace / ".github" / "workflows" / "ci-router.yml"
        router.write_text(
            "name: CI Router\non:\n  push:\njobs:\n  detect:\n"
            "    runs-on: ubuntu-latest\n    steps: []\n",
            encoding="utf-8",
        )
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "skip", outcome

    def test_a_generated_router_with_no_filters_block_fails(self, router_workspace):
        """It claims to be generated, so its filters block must be readable."""

        router = router_workspace / ".github" / "workflows" / "ci-router.yml"
        router.write_text(
            f"{ROUTER_HEADER}\njobs:\n  detect:\n    steps: []\n",
            encoding="utf-8",
        )
        outcome = _workspace_check("router-filters-fresh")(_wctx(router_workspace))
        assert outcome.status == "fail"

    def test_it_derives_from_the_whole_workspace_not_the_context(self, router_workspace):
        """A context carrying one member (the releasable preflight) must not
        make a fresh router look stale."""
        from rlsbl.check_context import WorkspaceCheckContext

        members = load_workspace(str(router_workspace))
        partial = WorkspaceCheckContext(
            project_root=router_workspace,
            workspace_root=router_workspace,
            config={},
            projects=[members[-1]],
            graph=None,
            releasables=[],
        )
        outcome = _workspace_check("router-filters-fresh")(partial)
        assert outcome.status == "pass", outcome


class TestFivePlaceRegistration:
    """A new check is registered in five places; none of them may be missed."""

    def test_it_is_in_the_checks_metadata_registry(self):
        import tomllib

        with open(REPO_ROOT / "rlsbl" / "data" / "checks.toml", "rb") as f:
            checks = tomllib.load(f)["checks"]
        meta = checks["router-filters-fresh"]
        assert meta["severity"] == "error"
        assert "workspace" in meta["tags"]
        assert "preflight" in meta["tags"]

    def test_it_is_in_the_check_to_target_matrix(self):
        assert CHECK_TARGETS["router-filters-fresh"] == "workspace"

    def test_it_has_a_row_in_the_docs_check_reference(self):
        text = (REPO_ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        assert "| `router-filters-fresh` |" in text

    def test_it_is_in_the_expected_checks_roster(self):
        from test_doctor_checks_migration import EXPECTED_CHECKS

        assert "router-filters-fresh" in EXPECTED_CHECKS

    def test_it_is_registered_on_the_app(self):
        from rlsbl import app

        assert "router-filters-fresh" in app._check_defs

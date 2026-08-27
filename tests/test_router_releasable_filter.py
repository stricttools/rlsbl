"""The CI router's paths filter includes each project's releasable artifact.

A monorepo release commit that touches ONLY the releasable's metadata --
inevitable on a FIRST release, where writing the version is a no-op -- left the
project's CI job skipped by the router's paths filter. The publish gate refuses
to treat a skipped check as passing, and every recovery path (re-run CI,
`release retry`, re-dispatch publish) re-evaluates the same filter on the same
commit, so the release deadlocked with no sanctioned way out.

The releasable's finalized ``CHANGELOG.md`` is now part of the project's filter:
a release commit IS a change to the released project. The changes JSONL is
deliberately NOT included -- `rlsbl changelog add` writes it between releases
and must not spend CI minutes per entry.
"""

from ruamel.yaml import YAML

from routerharness import generate_router
from rlsbl.workspace_types import Releasable, WorkspaceProject


def _releasable(name):
    return Releasable(name=name, tag_format="{name}@v{version}")


def _project(name, path, releasable=None, depends_on=None):
    data = {"name": name, "path": path, "_ci_docs": []}
    if releasable is not None:
        data["releasable"] = releasable
    if depends_on is not None:
        data["depends_on"] = depends_on
    return WorkspaceProject(data)


def _filters(content):
    """Return the dorny/paths-filter filters block of the generated router."""
    parsed = YAML(typ="safe").load(content)
    for step in parsed["jobs"]["detect"]["steps"]:
        if step.get("id") == "changes":
            return step["with"]["filters"]
    raise AssertionError("no paths-filter step in the generated router")


ARTIFACT = ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md"


class TestReleasableFinalizeArtifactInFilter:
    def test_member_filter_carries_the_artifact(self):
        content = generate_router(
            [_project("core", "core", releasable="alpha")],
            releasables=[_releasable("alpha")],
        )
        filters = _filters(content)
        assert "core:" in filters
        assert f"- '{ARTIFACT}'" in filters
        assert "- 'core/**'" in filters

    def test_artifact_is_added_alongside_derived_territories(self):
        """The artifact joins a filter that already carries a dependency's territory."""
        content = generate_router(
            [
                _project("core", "core", releasable="alpha",
                         depends_on=["shared"]),
                _project("shared", "shared", releasable="alpha"),
            ],
            releasables=[_releasable("alpha")],
        )
        filters = _filters(content)
        assert "- 'core/**'" in filters
        assert "- 'shared/**'" in filters
        assert f"- '{ARTIFACT}'" in filters

    def test_every_member_of_the_releasable_gets_it(self):
        content = generate_router(
            [
                _project("core", "core", releasable="alpha"),
                _project("cli", "cli", releasable="alpha"),
            ],
            releasables=[_releasable("alpha")],
        )
        filters = _filters(content)
        assert filters.count(f"- '{ARTIFACT}'") == 2

    def test_changes_jsonl_is_not_in_the_filter(self):
        """changelog add must not trigger CI on every entry."""
        content = generate_router(
            [_project("core", "core", releasable="alpha")],
            releasables=[_releasable("alpha")],
        )
        assert "changes/unreleased.jsonl" not in _filters(content)
        assert "releasables/alpha/**" not in _filters(content)

    def test_non_releasable_member_gets_no_artifact(self):
        """releasable = false -> territory and machinery only, no artifact."""
        content = generate_router(
            [_project("docs", "docs", releasable=False)],
            releasables=[_releasable("alpha")],
        )
        filters = _filters(content)
        assert "- 'docs/**'" in filters
        assert ARTIFACT not in filters

    def test_implicit_mode_gets_no_artifact(self):
        """No releasables (implicit monorepo) -> no finalize artifact to anchor on."""
        filters = _filters(generate_router([_project("core", "core")], releasables=None))
        assert "- 'core/**'" in filters
        assert ".rlsbl-monorepo/" not in filters


class TestFirstReleaseTriggersCI:
    """The deadlock scenario, at the filter level.

    A first release's push touches nothing under the project's own path: the
    version file already holds the initial version, so the only changed files
    are the releasable's finalized changelog artifacts.
    """

    FIRST_RELEASE_CHANGED_FILES = [
        ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md",
        ".rlsbl-monorepo/releasables/alpha/changes/1.0.0.jsonl",
        ".rlsbl-monorepo/releasables/alpha/changes/unreleased.jsonl",
        ".rlsbl-monorepo/releasables/alpha/version",
    ]

    def _patterns_for(self, filters, project):
        """Extract one project's glob list out of the filters block."""
        patterns, in_project = [], False
        for line in filters.splitlines():
            if line.startswith(f"{project}:"):
                in_project = True
                continue
            if in_project:
                if not line.startswith("  - "):
                    break
                patterns.append(line.strip()[3:].strip("'"))
        return patterns

    def test_release_commit_matches_the_project_filter(self):
        from rlsbl.router_filters import matches_filter

        content = generate_router(
            [_project("core", "core", releasable="alpha")],
            releasables=[_releasable("alpha")],
        )
        patterns = self._patterns_for(_filters(content), "core")
        assert patterns, "expected a multi-pattern filter for a releasable member"

        matched = [
            f for f in self.FIRST_RELEASE_CHANGED_FILES
            if matches_filter(f, patterns)
        ]
        assert matched == [".rlsbl-monorepo/releasables/alpha/CHANGELOG.md"], matched

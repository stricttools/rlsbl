"""Purity is verified by execution, not asserted in a comment.

The rule (``rlsbl/data/checks.toml``, and ``docs/checks.md``): a pure check
starts only allowlisted read-only programs.  The allowlist is
``rlsbl/observe_allowlist.py`` and its standard is "no user-visible mutation".

So the test is not a source grep.  It runs every pure-declared check for real,
against a real git repository, with an observer wrapped around the effects
chokepoint, and fails on any spawn whose argv matches no allowlist prefix.
"""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from githarness import commit_file, git, init_repo
from rlsbl import effects
from rlsbl import observe_allowlist as oa


CHECKS_TOML = Path(__file__).resolve().parent.parent / "rlsbl" / "data" / "checks.toml"


def _check_defs():
    with open(CHECKS_TOML, "rb") as f:
        return tomllib.load(f)["checks"]


def _pure_names():
    return sorted(n for n, m in _check_defs().items() if m.get("pure"))


def _matches_allowlist(argv):
    for entry in oa.OBSERVE_ALLOWLIST:
        prefix = entry.argv
        if len(prefix) <= len(argv) and tuple(argv[: len(prefix)]) == prefix:
            return True
    return False


# ---------------------------------------------------------------------------
# The dependency graph has no impure edge into the pure partition
# ---------------------------------------------------------------------------


class TestNoCascade:
    """A pure check depending on an impure one is listed, never executed.

    strictcli's ``pure_only`` partition lists a pure check whenever one of its
    dependencies is listed, so an impure dependency silently removes a pure
    check from every preview.  Zero such edges exist; this keeps it that way.
    """

    def test_no_pure_check_depends_on_an_impure_one(self):
        defs = _check_defs()
        offenders = []
        for name, meta in defs.items():
            if not meta.get("pure"):
                continue
            for dep in meta.get("depends_on") or []:
                dep_meta = defs.get(dep)
                if dep_meta is None:
                    continue
                if not dep_meta.get("pure") or dep_meta.get("needs_network"):
                    offenders.append(f"{name} -> {dep}")
        assert not offenders, (
            "a pure check whose dependency is listed under --dry-run is itself "
            f"listed, so it silently vanishes from every preview: {offenders}"
        )

    def test_no_pure_check_needs_the_network_without_saying_so(self):
        """pure + needs_network is legal but currently unused -- if that
        changes, the pure_only partition stops covering every pure check and
        the purity sweep below must be widened."""
        defs = _check_defs()
        both = [n for n, m in defs.items() if m.get("pure") and m.get("needs_network")]
        assert not both, (
            f"{both} are pure AND need network, so `pure_only` will not run "
            "them and TestPureChecksOnlySpawnObserves no longer sweeps them"
        )


# ---------------------------------------------------------------------------
# The reclassification is recorded, not accidental
# ---------------------------------------------------------------------------


class TestTheReclassificationIsDeclared:

    @pytest.mark.parametrize("name", [
        # spawn only read-only local git; impure under the older
        # "starts no program" rule
        "changelog-hashes",
        "changelog-range",
        "changelog-coverage",
        "changelog-orphans",
        "changelog-batch-entries",
        "prepush-changelog-coverage",
        "prepush-gitignore-guard",
        "workspace-unregistered",
        "go-companion-tags",
        # was declared pure while spawning -- misdeclared then, legitimate now
        "config-schema",
    ])
    def test_check_is_declared_pure(self, name):
        assert _check_defs()[name]["pure"] is True

    @pytest.mark.parametrize("name", [
        "ruff-lint",            # rewrites files with --fix, and is a linter
        "workspace-unbuildable",  # uv sync materializes an environment
        "test-suite",
        "test-suite-workspace",
        "library-lint",           # reaches gradle/detekt on JVM projects
        "maven-central-metadata",  # generates a POM via gradlew
    ])
    def test_writing_tool_check_stays_impure(self, name):
        assert _check_defs()[name]["pure"] is False

    def test_the_data_file_states_the_rule(self):
        text = CHECKS_TOML.read_text(encoding="utf-8")
        assert "observe_allowlist" in text, (
            "the purity rule must name the allowlist it is defined against"
        )
        assert "allowlisted read-only programs" in text

    def test_the_docs_page_states_the_same_rule(self):
        page = (
            Path(__file__).resolve().parent.parent / "docs" / "checks.md"
        ).read_text(encoding="utf-8")
        assert "starts only read-only programs on the observe allowlist" in page, (
            "the docs page and the data file are the two statement sites and "
            "must not disagree"
        )
        assert "@app.check()" not in page, (
            "stale decorator name: checks are registered with "
            "@app.error_check / @app.warn_check"
        )


# ---------------------------------------------------------------------------
# The sweep: run them and watch what they start
# ---------------------------------------------------------------------------


@pytest.fixture
def purity_project(tmp_path):
    """A real project with git history, a tag, a changelog and a manifest.

    Rich enough that the pure checks do real work rather than skipping on the
    first missing file -- a sweep over checks that all skip proves nothing.
    """
    repo = tmp_path / "purityproj"
    repo.mkdir()
    init_repo(repo)

    (repo / "pyproject.toml").write_text(
        '[project]\nname = "purityproj"\nversion = "0.1.0"\n'
        'description = "fixture"\nrequires-python = ">=3.11"\n'
        'dependencies = []\n',
        encoding="utf-8",
    )
    (repo / "purityproj").mkdir()
    (repo / "purityproj" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8",
    )
    rlsbl_dir = repo / ".rlsbl"
    (rlsbl_dir / "changes").mkdir(parents=True)
    (rlsbl_dir / "config.json").write_text(
        json.dumps({
            "publish_mode": "none",
            "targets": ["pypi"],
            "changelog_format_version_enforced": True,
        }),
        encoding="utf-8",
    )
    (rlsbl_dir / "changes" / "unreleased.jsonl").write_text("", encoding="utf-8")
    commit_file(repo, "README.md", "# purityproj\n", "initial")
    git(repo, "tag", "v0.1.0")

    # One covered commit after the tag, so the changelog checks have a real
    # unreleased range to resolve instead of an empty one.
    (repo / "purityproj" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "purityproj/extra.py")
    git(repo, "commit", "-q", "-m", "add extra")
    sha = git(repo, "rev-parse", "HEAD")
    (rlsbl_dir / "changes" / "unreleased.jsonl").write_text(
        json.dumps({
            "format_version": 1,
            "id": "01JPURITYPROJ0000000000000",
            "commits": [sha],
            "user_facing": True,
            "description": "A fixture entry.",
            "type": "feature",
        }) + "\n",
        encoding="utf-8",
    )
    return repo


class _SpawnObserver:
    """Wraps the effects chokepoint and records every argv it is asked to run."""

    def __init__(self):
        self.argvs = []

    def install(self, monkeypatch):
        real_run, real_spawn = effects.run, effects.spawn

        def watched_run(argv, **kwargs):
            rendered = (
                ["/bin/sh", "-c", argv] if kwargs.get("shell") else list(argv)
            )
            self.argvs.append(rendered)
            return real_run(argv, **kwargs)

        def watched_spawn(argv, **kwargs):
            self.argvs.append(list(argv))
            return real_spawn(argv, **kwargs)

        monkeypatch.setattr(effects, "run", watched_run)
        monkeypatch.setattr(effects, "spawn", watched_spawn)
        return self

    @property
    def violations(self):
        return [a for a in self.argvs if not _matches_allowlist(a)]


class TestPureChecksOnlySpawnObserves:

    def test_the_whole_pure_partition_spawns_only_observes(
        self, purity_project, monkeypatch,
    ):
        import rlsbl
        from rlsbl.context import ProjectContext

        monkeypatch.chdir(purity_project)
        rlsbl.app.reset_check_provider_cache()
        observer = _SpawnObserver().install(monkeypatch)
        try:
            ctx = ProjectContext(
                project_root=Path(str(purity_project)),
                workspace_root=None,
                config=json.loads(
                    (purity_project / ".rlsbl" / "config.json").read_text()
                ),
            )
            results, impure_listed, _exit = rlsbl.app.run_checks(
                ctx, run_all=True, pure_only=True,
            )
        finally:
            rlsbl.app.reset_check_provider_cache()

        assert results, "the sweep executed nothing -- it would be vacuous"
        assert not observer.violations, (
            "a pure check started a program that is not on the observe "
            "allowlist, so it would really run (or be refused) under "
            f"--dry-run: {observer.violations}"
        )
        # Every declared-pure check either ran or was skipped by scope; none
        # may have been listed as impure.
        assert not (set(_pure_names()) & set(impure_listed)), (
            f"pure checks landed in the impure partition: "
            f"{sorted(set(_pure_names()) & set(impure_listed))}"
        )

    def test_the_observer_would_notice_a_violation(self, purity_project, monkeypatch):
        """The sweep is not vacuously green."""
        observer = _SpawnObserver().install(monkeypatch)
        try:
            effects.run(
                ["git", "status", "--porcelain"],
                cwd=str(purity_project), capture_output=True, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        assert observer.violations == [["git", "status", "--porcelain"]], (
            "the bare `git status` form is not allowlisted (it takes "
            "index.lock); the observer must report it"
        )

    def test_the_sweep_really_ran_read_only_git(self, purity_project, monkeypatch):
        """Checks that spawn git must actually have spawned it in the sweep."""
        import rlsbl
        from rlsbl.context import ProjectContext

        monkeypatch.chdir(purity_project)
        rlsbl.app.reset_check_provider_cache()
        observer = _SpawnObserver().install(monkeypatch)
        try:
            ctx = ProjectContext(
                project_root=Path(str(purity_project)),
                workspace_root=None,
                config=json.loads(
                    (purity_project / ".rlsbl" / "config.json").read_text()
                ),
            )
            rlsbl.app.run_checks(ctx, run_all=True, pure_only=True)
        finally:
            rlsbl.app.reset_check_provider_cache()

        assert any(a and a[0] == "git" for a in observer.argvs), (
            "no pure check spawned git at all -- the fixture is skipping "
            "everything and the sweep proves nothing"
        )

"""Tests for the hotfix release type."""

import json
import os
import subprocess
import time

import pytest

from conftest import run_git as _run_git, git_head as _git_head, make_commit as _make_commit
from rlsbl.changelog.generate import generate_version_section
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import validate_unreleased
from rlsbl.utils import bump_version


class TestBumpVersionHotfix:
    """bump_version with hotfix produces the same result as patch."""

    def test_hotfix_bumps_patch(self):
        assert bump_version("1.2.3", "hotfix") == "1.2.4"

    def test_hotfix_same_as_patch(self):
        assert bump_version("0.5.0", "hotfix") == bump_version("0.5.0", "patch")

    def test_hotfix_with_prerelease(self):
        assert bump_version("1.0.0-beta.1", "hotfix") == "1.0.1"


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Create a git repo with an initial commit and a baseline version tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit
    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    # Create a baseline version tag so <tag>..HEAD works
    _run_git(repo, "tag", "v0.0.0")

    # Set up .rlsbl/changes
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    return repo


class TestValidateUnreleasedHotfix:
    """Hotfix exempts user_facing check but forbids user-facing entries."""

    def test_hotfix_no_user_facing_passes(self, git_repo):
        sha = _make_commit(git_repo)
        changes_dir = str(git_repo / ".rlsbl" / "changes")
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write(json.dumps({"commits": [sha], "user_facing": False}) + "\n")

        result = validate_unreleased(
            changes_dir, config={}, bump_type="hotfix",
        )
        assert result["passed"] is True
        assert result["checks"]["user_facing"] == (True, [])

    def test_hotfix_with_user_facing_fails(self, git_repo):
        sha = _make_commit(git_repo)
        changes_dir = str(git_repo / ".rlsbl" / "changes")
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write(json.dumps({
                "commits": [sha],
                "user_facing": True,
                "description": "A feature",
                "type": "feature",
            }) + "\n")

        result = validate_unreleased(
            changes_dir, config={}, bump_type="hotfix",
        )
        assert result["passed"] is False
        assert "hotfix_no_user_facing" in result["checks"]
        passed, details = result["checks"]["hotfix_no_user_facing"]
        assert passed is False
        assert "hotfix releases must not have user-facing entries" in details[0]

    def test_patch_no_user_facing_still_fails(self, git_repo):
        sha = _make_commit(git_repo)
        changes_dir = str(git_repo / ".rlsbl" / "changes")
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write(json.dumps({"commits": [sha], "user_facing": False}) + "\n")

        result = validate_unreleased(
            changes_dir, config={}, bump_type="patch",
        )
        assert result["passed"] is False
        passed, _ = result["checks"]["user_facing"]
        assert passed is False

    def test_none_bump_type_no_user_facing_still_fails(self, git_repo):
        """Default (None) bump_type does not exempt user_facing check."""
        sha = _make_commit(git_repo)
        changes_dir = str(git_repo / ".rlsbl" / "changes")
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write(json.dumps({"commits": [sha], "user_facing": False}) + "\n")

        result = validate_unreleased(
            changes_dir, config={}, bump_type=None,
        )
        assert result["passed"] is False
        passed, _ = result["checks"]["user_facing"]
        assert passed is False


class TestGenerateVersionSectionHotfix:
    """generate_version_section renders ### Hotfix for hotfix releases."""

    def test_hotfix_with_description_renders_hotfix_section(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            description="Fix memory leak in connection pool",
            bump_type="hotfix",
        )
        assert "### Hotfix" in md
        assert "- Fix memory leak in connection pool" in md
        assert "No user-facing changes." not in md

    def test_hotfix_without_description_renders_no_user_facing(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            bump_type="hotfix",
        )
        assert "### Hotfix" not in md
        assert "- No user-facing changes." in md

    def test_none_bump_type_no_user_facing_renders_standard(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section("2.0.0", entries)
        assert "## 2.0.0" in md
        assert "- No user-facing changes." in md
        assert "### Hotfix" not in md

    def test_hotfix_with_description_and_context(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            description="Fix timeout bug",
            context="Root cause was a race condition",
            bump_type="hotfix",
        )
        assert "### Hotfix" in md
        assert "- Fix timeout bug" in md
        assert "Root cause was a race condition" in md
        assert "No user-facing changes." not in md


class TestValidBumpTypesConsolidated:
    """VALID_BUMP_TYPES is imported from release_file, not re-declared."""

    def test_validate_py_imports_from_release_file(self):
        import rlsbl.commands.release.validate as validate_mod
        from rlsbl.release_file import VALID_BUMP_TYPES as canonical
        assert validate_mod.VALID_BUMP_TYPES is canonical

    def test_init_py_imports_from_release_file(self):
        import rlsbl.commands.release as release_mod
        from rlsbl.release_file import VALID_BUMP_TYPES as canonical
        assert release_mod.VALID_BUMP_TYPES is canonical

    def test_hotfix_in_valid_bump_types(self):
        from rlsbl.release_file import VALID_BUMP_TYPES
        assert "hotfix" in VALID_BUMP_TYPES


class TestReleaseInitTemplateIncludesHotfix:
    """Release init template mentions hotfix."""

    def test_standalone_release_init(self, tmp_path, monkeypatch):
        """release_init.py template includes 'hotfix' in the bump comment."""
        import tomlkit

        # We can check the source directly -- the comment is generated
        # by the release_init module
        from rlsbl.commands import release_init
        import inspect

        source = inspect.getsource(release_init)
        assert "patch, minor, major, hotfix, or prerelease" in source

    def test_batch_release_init(self):
        """batch_release_init.py template includes 'hotfix' in the bump comment."""
        from rlsbl.commands.monorepo import batch_release_init
        import inspect

        source = inspect.getsource(batch_release_init)
        assert "patch, minor, major, hotfix, or prerelease" in source

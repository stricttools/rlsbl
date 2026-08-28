"""Tests for the infra release type (formerly "hotfix")."""

import json
import os

import pytest

from githarness import record_release
import tomlkit

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.generate import (
    _read_release_metadata_full,
    generate_changelog,
    generate_version_section,
)
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import validate_unreleased
from rlsbl.errors import ReleaseFileError
from rlsbl.utils import bump_version


class TestBumpVersionInfra:
    """bump_version with infra produces the same result as patch."""

    def test_infra_bumps_patch(self):
        assert bump_version("1.2.3", "infra") == "1.2.4"

    def test_infra_same_as_patch(self):
        assert bump_version("0.5.0", "infra") == bump_version("0.5.0", "patch")

    def test_infra_with_prerelease(self):
        assert bump_version("1.0.0-beta.1", "infra") == "1.0.1"

    def test_legacy_hotfix_bump_type_is_rejected(self):
        """The legacy "hotfix" bump type is no longer recognized."""
        with pytest.raises(Exception):
            bump_version("1.2.3", "hotfix")


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
    record_release(repo, "v0.0.0")

    # Set up .rlsbl/changes
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    return repo


class TestValidateUnreleasedInfra:
    """Infra exempts user_facing check but forbids user-facing entries."""

    def test_infra_no_user_facing_passes(self, git_repo):
        sha = _make_commit(git_repo)
        changes_dir = str(git_repo / ".rlsbl" / "changes")
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w") as f:
            f.write(json.dumps({"commits": [sha], "user_facing": False}) + "\n")

        result = validate_unreleased(
            changes_dir, config={}, bump_type="infra",
        )
        assert result["passed"] is True
        assert result["checks"]["user_facing"] == (True, [])

    def test_infra_with_user_facing_fails(self, git_repo):
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
            changes_dir, config={}, bump_type="infra",
        )
        assert result["passed"] is False
        assert "infra_no_user_facing" in result["checks"]
        passed, details = result["checks"]["infra_no_user_facing"]
        assert passed is False
        assert "infra releases must not have user-facing entries" in details[0]

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


class TestGenerateVersionSectionInfra:
    """generate_version_section renders ### Infrastructure for infra releases."""

    def test_infra_with_description_renders_infrastructure_section(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            description="Fix memory leak in connection pool",
            bump_type="infra",
        )
        assert "### Infrastructure" in md
        assert "- Fix memory leak in connection pool" in md
        assert "No user-facing changes." not in md

    def test_infra_without_description_renders_no_user_facing(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            bump_type="infra",
        )
        assert "### Infrastructure" not in md
        assert "- No user-facing changes." in md

    def test_none_bump_type_no_user_facing_renders_standard(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section("2.0.0", entries)
        assert "## 2.0.0" in md
        assert "- No user-facing changes." in md
        assert "### Infrastructure" not in md

    def test_infra_with_description_and_context(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "1.0.1", entries,
            description="Fix timeout bug",
            context="Root cause was a race condition",
            bump_type="infra",
        )
        assert "### Infrastructure" in md
        assert "- Fix timeout bug" in md
        assert "Root cause was a race condition" in md
        assert "No user-facing changes." not in md


class TestInfraChangelogAssembly:
    """The CHANGELOG.md ASSEMBLER must emit an infra release's section.

    The renderer above was always correct; the assembler
    (``generate_changelog``) skipped the section whenever unreleased.jsonl had
    no entries -- which is exactly the shape of every infra release, since
    user-facing entries are forbidden for that bump type. The result was that
    every infra release fleet-wide shipped a CHANGELOG.md with no ``## X.Y.Z``
    heading at all.
    """

    def _changes_dir(self, tmp_path):
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        return changes

    def test_prerelease_assembly_emits_infra_section(self, tmp_path, monkeypatch):
        """The pre-finalization call (empty unreleased.jsonl + version_override)
        renders the infra section that gets committed as CHANGELOG.md."""
        monkeypatch.chdir(tmp_path)
        changes = self._changes_dir(tmp_path)
        (changes / "unreleased.jsonl").write_text("")
        (changes / "0.14.0.jsonl").write_text(
            '{"commits":["aaa"],"user_facing":true,'
            '"description":"A feature","type":"feature"}\n'
        )

        content = generate_changelog(
            str(tmp_path),
            version_override="0.14.1",
            description="Re-publish after a broken artifact scan.",
            bump_type="infra",
        )

        assert "## 0.14.1" in content
        section = content[content.index("## 0.14.1"):content.index("## 0.14.0")]
        assert "### Infrastructure" in section
        assert "- Re-publish after a broken artifact scan." in section

    def test_finalized_infra_version_regenerates_from_archive(
        self, tmp_path, monkeypatch,
    ):
        """The heal path: a FINALIZED infra release (0-byte ``x.y.z.jsonl``)
        regenerates its section from the archived release file, so a repo whose
        CHANGELOG.md lost the section is repaired by ``changelog generate``."""
        monkeypatch.chdir(tmp_path)
        changes = self._changes_dir(tmp_path)
        (changes / "unreleased.jsonl").write_text("")
        (changes / "0.1.1.jsonl").write_text("")
        (changes / "0.1.0.jsonl").write_text(
            '{"commits":["aaa"],"user_facing":true,'
            '"description":"Initial","type":"feature"}\n'
        )
        releases = tmp_path / ".rlsbl" / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.document()
        doc.add("bump", "infra")
        doc.add("description", "Scaffold refresh.")
        (releases / "v0.1.1.toml").write_text(tomlkit.dumps(doc))

        content = generate_changelog(str(tmp_path))

        assert "## 0.1.1" in content
        section = content[content.index("## 0.1.1"):content.index("## 0.1.0")]
        assert "### Infrastructure" in section
        assert "- Scaffold refresh." in section


class TestLegacyHotfixArchiveHardError:
    """Regenerating from an archived release file with the legacy "hotfix"
    bump value is a hard error, not a silent mis-render."""

    def _write_archive(self, tmp_path, version, bump):
        releases = tmp_path / ".rlsbl" / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.document()
        doc.add("bump", bump)
        doc.add("description", "Some release")
        (releases / f"v{version}.toml").write_text(tomlkit.dumps(doc))
        return str(releases)

    def test_legacy_hotfix_value_raises(self, tmp_path):
        releases = self._write_archive(tmp_path, "1.2.3", "hotfix")
        with pytest.raises(ReleaseFileError) as exc:
            _read_release_metadata_full(
                str(tmp_path), "1.2.3", releases_dir=releases,
            )
        msg = str(exc.value)
        assert "v1.2.3.toml" in msg
        assert "hotfix" in msg
        assert "infra" in msg

    def test_infra_value_is_accepted(self, tmp_path):
        releases = self._write_archive(tmp_path, "1.2.3", "infra")
        desc, ctx, bump = _read_release_metadata_full(
            str(tmp_path), "1.2.3", releases_dir=releases,
        )
        assert bump == "infra"
        assert desc == "Some release"


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

    def test_infra_in_valid_bump_types(self):
        from rlsbl.release_file import VALID_BUMP_TYPES
        assert "infra" in VALID_BUMP_TYPES

    def test_legacy_hotfix_not_in_valid_bump_types(self):
        from rlsbl.release_file import VALID_BUMP_TYPES
        assert "hotfix" not in VALID_BUMP_TYPES


class TestReleaseInitTemplateIncludesInfra:
    """Release init template mentions infra."""

    def test_standalone_release_init(self):
        """release_init.py template includes 'infra' in the bump comment."""
        from rlsbl.commands import release_init
        import inspect

        source = inspect.getsource(release_init)
        assert "patch, minor, major, infra, or prerelease" in source

    def test_batch_release_init(self):
        """batch_release_init.py template includes 'infra' in the bump comment."""
        from rlsbl.commands.monorepo import batch_release_init
        import inspect

        source = inspect.getsource(batch_release_init)
        assert "patch, minor, major, infra, or prerelease" in source

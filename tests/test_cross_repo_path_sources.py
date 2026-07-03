"""Tests for the cross-repo-path-sources check and its release-time guard.

A committed pyproject.toml must not declare [tool.uv.sources] entries whose
``path`` resolves outside the repository root. In-repo path sources and
``workspace = true`` sources are legal. Relative paths are resolved against
the pyproject.toml's directory (uv's resolution rule), so ``../sibling``
in a repo-root pyproject.toml is a cross-repo source.
"""

import pytest

from rlsbl import app
from rlsbl.checks.project import find_cross_repo_path_sources
from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _abort_on_cross_repo_sources,
)
from rlsbl.context import ProjectContext


def _write_pyproject(root, sources_block):
    content = (
        '[project]\n'
        'name = "testpkg"\n'
        'version = "0.1.0"\n'
        'dependencies = ["strictcli"]\n'
    )
    if sources_block is not None:
        content += "\n[tool.uv.sources]\n" + sources_block
    (root / "pyproject.toml").write_text(content)


def _ctx(root, workspace_root=None):
    return ProjectContext(
        project_root=root, workspace_root=workspace_root, config={},
    )


def _run_check(root, workspace_root=None):
    return app._check_defs["cross-repo-path-sources"].impl(
        _ctx(root, workspace_root=workspace_root)
    )


class TestFindCrossRepoPathSources:
    """Unit tests for the shared detection function."""

    def test_absolute_cross_repo_path(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        other = tmp_path / "other" / "python"
        other.mkdir(parents=True)
        _write_pyproject(repo, f'strictcli = {{ path = "{other}", editable = true }}\n')

        offenders = find_cross_repo_path_sources(str(repo))
        assert len(offenders) == 1
        pkg, declared, resolved = offenders[0]
        assert pkg == "strictcli"
        assert declared == str(other)

    def test_relative_parent_path(self, tmp_path):
        """../sibling resolved against the pyproject's directory is cross-repo."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "orxtra").mkdir()
        _write_pyproject(repo, 'orxtra = { path = "../orxtra", editable = true }\n')

        offenders = find_cross_repo_path_sources(str(repo))
        assert len(offenders) == 1
        assert offenders[0][0] == "orxtra"
        assert offenders[0][1] == "../orxtra"

    def test_in_repo_path_is_legal(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "packages" / "sub").mkdir(parents=True)
        _write_pyproject(repo, 'sub = { path = "packages/sub", editable = true }\n')

        assert find_cross_repo_path_sources(str(repo)) == []

    def test_workspace_source_is_legal(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, 'sibling = { workspace = true }\n')

        assert find_cross_repo_path_sources(str(repo)) == []

    def test_no_sources_table(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, None)

        assert find_cross_repo_path_sources(str(repo)) == []

    def test_no_pyproject(self, tmp_path):
        assert find_cross_repo_path_sources(str(tmp_path)) == []

    def test_boundary_root_allows_monorepo_sibling(self, tmp_path):
        """A member's ../sibling path is in-repo when the boundary is the workspace root."""
        ws = tmp_path / "mono"
        member = ws / "packages" / "a"
        sibling = ws / "packages" / "b"
        member.mkdir(parents=True)
        sibling.mkdir(parents=True)
        _write_pyproject(member, 'b = { path = "../b", editable = true }\n')

        assert find_cross_repo_path_sources(str(member), boundary_root=str(ws)) == []
        # Without the workspace boundary, the same source is cross-repo.
        assert len(find_cross_repo_path_sources(str(member))) == 1

    def test_non_string_path_reported_not_crash(self, tmp_path):
        """A non-string TOML path value is reported as invalid, not a TypeError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, 'dep = { path = 123 }\n')

        offenders = find_cross_repo_path_sources(str(repo))
        assert len(offenders) == 1
        pkg, declared, resolved = offenders[0]
        assert pkg == "dep"
        assert declared == "123"
        assert "invalid path value for source 'dep'" in resolved
        assert "pyproject.toml" in resolved

    def test_table_path_reported_not_crash(self, tmp_path):
        """A table path value is equally invalid and reported, not crashed on."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, 'dep = { path = { nested = true } }\n')

        offenders = find_cross_repo_path_sources(str(repo))
        assert len(offenders) == 1
        assert offenders[0][0] == "dep"
        assert "invalid path value for source 'dep'" in offenders[0][2]

    def test_list_of_sources(self, tmp_path):
        """uv allows a list of marker-gated sources; each entry is checked."""
        repo = tmp_path / "repo"
        repo.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        _write_pyproject(
            repo,
            f'dep = [{{ path = "{other}", marker = "sys_platform == \'linux\'" }}]\n',
        )

        offenders = find_cross_repo_path_sources(str(repo))
        assert len(offenders) == 1
        assert offenders[0][0] == "dep"


class TestCrossRepoPathSourcesCheck:
    """Functional tests for the registered check."""

    def test_fail_absolute_cross_repo(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, f'dep = {{ path = "{other}", editable = true }}\n')

        result = _run_check(repo)
        assert result.status == "fail"
        assert "dep" in " ".join(result.details or [result.message])

    def test_fail_relative_cross_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "orxtra").mkdir()
        _write_pyproject(repo, 'orxtra = { path = "../orxtra" }\n')

        result = _run_check(repo)
        assert result.status == "fail"

    def test_fail_non_string_path(self, tmp_path):
        """Malformed path values produce a check failure naming the entry."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, 'dep = { path = 123 }\n')

        result = _run_check(repo)
        assert result.status == "fail"
        joined = " ".join(result.details or [result.message])
        assert "invalid path value for source 'dep'" in joined
        assert "pyproject.toml" in joined

    def test_pass_in_repo_path(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "sub").mkdir(parents=True)
        _write_pyproject(repo, 'sub = { path = "sub" }\n')

        result = _run_check(repo)
        assert result.status == "pass"

    def test_pass_workspace_source(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, 'sibling = { workspace = true }\n')

        result = _run_check(repo)
        assert result.status == "pass"

    def test_skip_no_pyproject(self, tmp_path):
        result = _run_check(tmp_path)
        assert result.status == "skip"

    def test_monorepo_member_sibling_passes(self, tmp_path):
        """In a workspace, a path source to another member is in-repo."""
        ws = tmp_path / "mono"
        member = ws / "packages" / "a"
        sibling = ws / "packages" / "b"
        member.mkdir(parents=True)
        sibling.mkdir(parents=True)
        _write_pyproject(member, 'b = { path = "../b" }\n')

        result = _run_check(member, workspace_root=ws)
        assert result.status == "pass"

    def test_check_registered_with_project_tag(self):
        assert "project" in app._check_defs["cross-repo-path-sources"].tags


class TestReleaseGuard:
    """The release-time guard aborts on cross-repo sources (unconditional path)."""

    def test_abort_on_cross_repo_source(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        _write_pyproject(repo, f'dep = {{ path = "{other}" }}\n')

        with pytest.raises(ReleaseValidationError):
            _abort_on_cross_repo_sources(str(repo))
        err = capsys.readouterr().err
        assert "dep" in err
        assert "dev-sources.toml.local-only" in err

    def test_no_abort_when_clean(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_pyproject(repo, None)

        _abort_on_cross_repo_sources(str(repo))  # must not raise

    def test_member_dirs_checked(self, tmp_path, capsys):
        """Releasable mode: member pyprojects are checked too."""
        ws = tmp_path / "mono"
        member = ws / "packages" / "a"
        member.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_pyproject(member, f'dep = {{ path = "{outside}" }}\n')

        with pytest.raises(ReleaseValidationError):
            _abort_on_cross_repo_sources(
                str(ws), boundary_root=str(ws), member_dirs=[str(member)],
            )

    def test_member_sibling_path_allowed(self, tmp_path):
        """Releasable mode: in-workspace sibling paths pass with workspace boundary."""
        ws = tmp_path / "mono"
        member = ws / "packages" / "a"
        sibling = ws / "packages" / "b"
        member.mkdir(parents=True)
        sibling.mkdir(parents=True)
        _write_pyproject(member, 'b = { path = "../b" }\n')

        _abort_on_cross_repo_sources(
            str(ws), boundary_root=str(ws), member_dirs=[str(member)],
        )  # must not raise

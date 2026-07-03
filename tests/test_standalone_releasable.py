"""Tests for standalone (single-project) releasable model.

Covers:
- create_standalone_releasable returns correct Releasable
- Tag format is "v{version}" by default
- Name derived from target read_name or directory basename
- Explicit .rlsbl/releasable.toml overrides defaults
- load_standalone_releasable error handling
- ProjectContext.releasable field
- _check_context_factory wires releasable for standalone projects
"""

import json
import os
import subprocess

import pytest

from rlsbl.context import ProjectContext, create_context
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    Releasable,
    STANDALONE_TAG_FORMAT,
    STANDALONE_RELEASABLE_FILE,
    create_standalone_releasable,
    load_standalone_releasable,
    _derive_standalone_name,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestStandaloneTagFormat:
    """STANDALONE_TAG_FORMAT is v{version}."""

    def test_value(self):
        assert STANDALONE_TAG_FORMAT == "v{version}"


# ---------------------------------------------------------------------------
# create_standalone_releasable: implicit (no releasable.toml)
# ---------------------------------------------------------------------------


class TestCreateStandaloneReleasableImplicit:
    """create_standalone_releasable without .rlsbl/releasable.toml."""

    def test_returns_releasable_instance(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        assert isinstance(rel, Releasable)

    def test_tag_format_is_standalone(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        assert rel.tag_format == "v{version}"

    def test_name_from_pypi_target(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "cool-lib"\nversion = "0.1.0"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        assert rel.name == "cool-lib"

    def test_name_from_npm_target(self, tmp_project):
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "my-npm-pkg", "version": "1.0.0"})
        )
        rel = create_standalone_releasable(tmp_project)
        assert rel.name == "my-npm-pkg"

    def test_name_falls_back_to_dirname(self, tmp_project):
        # No project manifest at all -- falls back to directory name
        rel = create_standalone_releasable(tmp_project)
        assert rel.name == os.path.basename(str(tmp_project))

    def test_name_from_dirname_when_read_name_returns_none(self, tmp_project):
        # pyproject.toml without [project].name
        (tmp_project / "pyproject.toml").write_text("[build-system]\n")
        rel = create_standalone_releasable(tmp_project)
        assert rel.name == os.path.basename(str(tmp_project))


# ---------------------------------------------------------------------------
# create_standalone_releasable: explicit (.rlsbl/releasable.toml)
# ---------------------------------------------------------------------------


class TestCreateStandaloneReleasableExplicit:
    """create_standalone_releasable with .rlsbl/releasable.toml."""

    def _write_releasable_toml(self, root, content):
        rlsbl_dir = root / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / STANDALONE_RELEASABLE_FILE).write_text(content)

    def test_explicit_overrides_name(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "detected-name"\nversion = "1.0.0"\n'
        )
        self._write_releasable_toml(tmp_project, 'name = "explicit-name"\n')
        rel = create_standalone_releasable(tmp_project)
        assert rel.name == "explicit-name"

    def test_explicit_overrides_tag_format(self, tmp_project):
        self._write_releasable_toml(
            tmp_project,
            'name = "myproj"\ntag_format = "release-{version}"\n',
        )
        rel = create_standalone_releasable(tmp_project)
        assert rel.tag_format == "release-{version}"

    def test_explicit_default_tag_format(self, tmp_project):
        self._write_releasable_toml(tmp_project, 'name = "myproj"\n')
        rel = create_standalone_releasable(tmp_project)
        assert rel.tag_format == STANDALONE_TAG_FORMAT

    def test_explicit_returns_releasable_instance(self, tmp_project):
        self._write_releasable_toml(tmp_project, 'name = "myproj"\n')
        rel = create_standalone_releasable(tmp_project)
        assert isinstance(rel, Releasable)


# ---------------------------------------------------------------------------
# load_standalone_releasable: edge cases and errors
# ---------------------------------------------------------------------------


class TestLoadStandaloneReleasable:
    """load_standalone_releasable error handling and edge cases."""

    def _write_releasable_toml(self, root, content):
        rlsbl_dir = root / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / STANDALONE_RELEASABLE_FILE).write_text(content)

    def test_returns_none_when_no_file(self, tmp_project):
        assert load_standalone_releasable(tmp_project) is None

    def test_returns_none_when_no_rlsbl_dir(self, tmp_project):
        assert load_standalone_releasable(tmp_project) is None

    def test_missing_name_raises(self, tmp_project):
        self._write_releasable_toml(tmp_project, 'tag_format = "v{version}"\n')
        with pytest.raises(WorkspaceError, match="missing or invalid 'name'"):
            load_standalone_releasable(tmp_project)

    def test_empty_name_raises(self, tmp_project):
        self._write_releasable_toml(tmp_project, 'name = ""\n')
        # Empty name is caught by Releasable.__post_init__
        with pytest.raises(WorkspaceError):
            load_standalone_releasable(tmp_project)

    def test_non_string_name_raises(self, tmp_project):
        self._write_releasable_toml(tmp_project, "name = 42\n")
        with pytest.raises(WorkspaceError, match="missing or invalid 'name'"):
            load_standalone_releasable(tmp_project)

    def test_non_string_tag_format_raises(self, tmp_project):
        self._write_releasable_toml(
            tmp_project, 'name = "myproj"\ntag_format = 42\n'
        )
        with pytest.raises(WorkspaceError, match="tag_format must be a string"):
            load_standalone_releasable(tmp_project)

    def test_valid_file_returns_releasable(self, tmp_project):
        self._write_releasable_toml(
            tmp_project,
            'name = "myproj"\ntag_format = "v{version}"\n',
        )
        rel = load_standalone_releasable(tmp_project)
        assert rel is not None
        assert rel.name == "myproj"
        assert rel.tag_format == "v{version}"


# ---------------------------------------------------------------------------
# _derive_standalone_name
# ---------------------------------------------------------------------------


class TestDeriveStandaloneName:
    """_derive_standalone_name picks the right name source."""

    def _detect(self, project_root):
        """Helper: detect targets and return (entries, TARGETS) for injection."""
        from rlsbl.targets import detect_targets, TARGETS
        return detect_targets(str(project_root)), TARGETS

    def test_pypi_project_name(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "fromtoml"\nversion = "0.1.0"\n'
        )
        entries, targets_map = self._detect(tmp_project)
        assert _derive_standalone_name(tmp_project, detected_targets=entries, targets_map=targets_map) == "fromtoml"

    def test_npm_package_name(self, tmp_project):
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "fromnpm", "version": "1.0.0"})
        )
        entries, targets_map = self._detect(tmp_project)
        assert _derive_standalone_name(tmp_project, detected_targets=entries, targets_map=targets_map) == "fromnpm"

    def test_no_manifest_returns_dirname(self, tmp_project):
        name = _derive_standalone_name(tmp_project)
        assert name == os.path.basename(str(tmp_project))

    def test_pypi_without_project_name_returns_dirname(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text("[tool.pytest]\n")
        entries, targets_map = self._detect(tmp_project)
        name = _derive_standalone_name(tmp_project, detected_targets=entries, targets_map=targets_map)
        assert name == os.path.basename(str(tmp_project))


# ---------------------------------------------------------------------------
# ProjectContext.releasable field
# ---------------------------------------------------------------------------


class TestProjectContextReleasableField:
    """ProjectContext has an optional releasable field."""

    def test_default_is_none(self):
        from pathlib import Path
        ctx = ProjectContext(
            project_root=Path("/tmp/test"),
            workspace_root=None,
            config={},
        )
        assert ctx.releasable is None

    def test_can_set_releasable(self):
        from pathlib import Path
        rel = Releasable(name="myproj", tag_format="v{version}")
        ctx = ProjectContext(
            project_root=Path("/tmp/test"),
            workspace_root=None,
            config={},
            releasable=rel,
        )
        assert ctx.releasable is rel
        assert ctx.releasable.name == "myproj"

    def test_create_context_has_releasable_none_by_default(self, tmp_project):
        ctx = create_context(tmp_project)
        assert ctx.releasable is None


# ---------------------------------------------------------------------------
# _check_context_factory wiring
# ---------------------------------------------------------------------------


class TestCheckContextFactoryStandalone:
    """_check_context_factory sets releasable on standalone ProjectContext."""

    def test_standalone_context_has_releasable(self, tmp_project):
        # Create a minimal rlsbl project
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"targets": ["pypi"]}')
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )
        # Initialize git repo so find_project_root works
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(tmp_project),
            check=True,
        )

        from rlsbl import _check_context_factory
        ctx = _check_context_factory()

        assert ctx.releasable is not None
        assert isinstance(ctx.releasable, Releasable)
        assert ctx.releasable.name == "testpkg"
        assert ctx.releasable.tag_format == "v{version}"

    def test_standalone_context_with_explicit_toml(self, tmp_project):
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text("{}")
        (rlsbl_dir / STANDALONE_RELEASABLE_FILE).write_text(
            'name = "custom"\ntag_format = "rel-{version}"\n'
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(tmp_project),
            check=True,
        )

        from rlsbl import _check_context_factory
        ctx = _check_context_factory()

        assert ctx.releasable is not None
        assert ctx.releasable.name == "custom"
        assert ctx.releasable.tag_format == "rel-{version}"


# ---------------------------------------------------------------------------
# Existing single-project release flow unchanged
# ---------------------------------------------------------------------------


class TestStandaloneReleasableNoSideEffects:
    """Standalone releasable is purely internal -- no files created."""

    def test_no_files_created(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        before = set(os.listdir(str(tmp_project)))
        create_standalone_releasable(tmp_project)
        after = set(os.listdir(str(tmp_project)))
        assert before == after

    def test_no_rlsbl_dir_created(self, tmp_project):
        create_standalone_releasable(tmp_project)
        assert not (tmp_project / ".rlsbl").exists()

    def test_releasable_is_read_only_abstraction(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        # Releasable is a frozen-like dataclass; verify it has the right fields
        assert hasattr(rel, "name")
        assert hasattr(rel, "tag_format")

    def test_tag_format_produces_correct_tags(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        tag = rel.tag_format.format(version="2.3.4", name=rel.name)
        assert tag == "v2.3.4"

    def test_custom_tag_format_produces_correct_tags(self, tmp_project):
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / STANDALONE_RELEASABLE_FILE).write_text(
            'name = "myproj"\ntag_format = "release-{version}"\n'
        )
        rel = create_standalone_releasable(tmp_project)
        tag = rel.tag_format.format(version="1.0.0", name=rel.name)
        assert tag == "release-1.0.0"

"""Red tests for the releasable-owns-targets bug.

Bug: When per-package config has ``"targets": []`` (explicitly empty) and the
releasable-level config has ``"targets": ["pypi"]``, the per-package empty
list silently replaces the releasable-level targets because ``merge_config``
does shallow-replace for lists.  The releasable's target definition is lost.

Similarly, ``collect_releasable_targets`` unions member-level targets, so
when every member has ``targets: []``, the result is empty even though the
releasable config declares targets.

These tests are expected to FAIL on the current code (red phase).
"""

import json
import os

import pytest

from rlsbl.config import merge_config, read_project_config
from rlsbl.targets import detect_targets


class TestDetectTargetsReadsFromReleasableConfig:
    """detect_targets should inherit targets from releasable config when
    per-package config has an explicitly empty targets list."""

    def test_detect_targets_reads_from_releasable_config(self, tmp_path):
        """Per-package targets: [] should NOT erase releasable-level targets: ["pypi"].

        Current behavior (buggy): merge_config shallow-replaces the list,
        so the merged config has targets=[] and detect_targets returns [].

        Expected behavior: releasable-level targets should be visible when
        per-package explicitly sets targets to empty.
        """
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        # Per-package config.json with explicitly empty targets
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"targets": []})
        )

        # Releasable-level config.json with targets
        rel_dir = tmp_path / "releasable"
        rel_dir.mkdir()
        (rel_dir / "config.json").write_text(
            json.dumps({"targets": ["pypi"]})
        )

        # Create a pyproject.toml so pypi target validation passes
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )

        entries = detect_targets(str(pkg_dir), releasable_config_dir=str(rel_dir))
        # Should find "pypi" from the releasable config
        assert len(entries) == 1, (
            f"Expected 1 target from releasable config, got {len(entries)}: "
            f"{[e.name for e in entries]}"
        )
        assert entries[0].name == "pypi"


class TestCollectReleasableTargetsReadsReleasableConfig:
    """collect_releasable_targets should find targets declared at the
    releasable config level, not just union member-level targets."""

    def test_collect_releasable_targets_reads_releasable_config(self, tmp_path):
        """When releasable config has targets: ["pypi"] but all member packages
        have targets: [], the function should still find "pypi".

        Current behavior (buggy): unions member targets (all empty) -> [].
        Expected behavior: releasable-level targets are discovered.
        """
        from rlsbl.targets import collect_releasable_targets
        from rlsbl.workspace import get_releasable_dir

        workspace_root = str(tmp_path)

        # Set up a releasable config directory with targets
        rel_dir = os.path.join(
            workspace_root, ".rlsbl-monorepo", "releasables", "core"
        )
        os.makedirs(rel_dir, exist_ok=True)
        with open(os.path.join(rel_dir, "config.json"), "w") as f:
            json.dump({"targets": ["pypi"]}, f)

        # Create two member projects, both with targets: []
        for name in ("pkg-a", "pkg-b"):
            proj_dir = tmp_path / name
            proj_dir.mkdir()
            rlsbl_dir = proj_dir / ".rlsbl"
            rlsbl_dir.mkdir()
            (rlsbl_dir / "config.json").write_text(
                json.dumps({"targets": []})
            )
            # Create pyproject.toml so pypi target would pass validation
            (proj_dir / "pyproject.toml").write_text(
                f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
            )

        member_projects = [
            {"path": "pkg-a", "name": "pkg-a", "releasable": "core"},
            {"path": "pkg-b", "name": "pkg-b", "releasable": "core"},
        ]

        target_names = collect_releasable_targets(
            "core", member_projects, workspace_root
        )

        assert "pypi" in target_names, (
            f"Expected 'pypi' from releasable config, got: {target_names}"
        )


class TestReleasableEmptyTargetsIsBanned:
    """An explicitly empty ``targets`` list in a RELEASABLE config is banned and
    surfaces as a hard error in BOTH target-resolution paths
    (collect_releasable_targets used by release init, validate_release_targets
    used by release run), consistent with validate_config_schema. An absent
    ``targets`` key still falls through to member detection -- only the empty
    list is banned."""

    def _make_releasable(self, tmp_path, targets_value):
        rel_dir = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releasables", "core"
        )
        os.makedirs(rel_dir, exist_ok=True)
        with open(os.path.join(rel_dir, "config.json"), "w") as f:
            json.dump({"targets": targets_value}, f)
        return rel_dir

    def test_collect_hard_errors_on_empty(self, tmp_path):
        from rlsbl.errors import ConfigError
        from rlsbl.targets import collect_releasable_targets

        self._make_releasable(tmp_path, [])
        member_projects = [{"path": "pkg-a", "name": "pkg-a", "releasable": "core"}]
        (tmp_path / "pkg-a").mkdir()

        with pytest.raises(ConfigError, match="empty list"):
            collect_releasable_targets("core", member_projects, str(tmp_path))

    def test_validate_hard_errors_on_empty(self, tmp_path):
        from rlsbl.errors import ConfigError
        from rlsbl.commands.release.validate import validate_release_targets
        from rlsbl.release_file import ReleaseConfig

        rel_dir = self._make_releasable(tmp_path, [])
        member_a = tmp_path / "pkg-a"
        member_a.mkdir()
        (member_a / "pyproject.toml").write_text(
            '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
        )

        config = ReleaseConfig(bump="patch", include=["pypi"], exclude=[])
        with pytest.raises(ConfigError, match="empty list"):
            validate_release_targets(
                config, tmp_path,
                member_dirs=[str(member_a)],
                releasable_config_dir=str(rel_dir),
            )

    def test_absent_targets_key_still_falls_through(self, tmp_path):
        """A releasable config WITHOUT a targets key falls through to member
        detection (not banned) in both paths."""
        from rlsbl.targets import collect_releasable_targets

        rel_dir = os.path.join(
            str(tmp_path), ".rlsbl-monorepo", "releasables", "core"
        )
        os.makedirs(rel_dir, exist_ok=True)
        with open(os.path.join(rel_dir, "config.json"), "w") as f:
            json.dump({"publish_mode": "none"}, f)  # no targets key

        member_a = tmp_path / "pkg-a"
        member_a.mkdir()
        (member_a / "pyproject.toml").write_text(
            '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
        )
        member_projects = [{"path": "pkg-a", "name": "pkg-a", "releasable": "core"}]

        names = collect_releasable_targets("core", member_projects, str(tmp_path))
        assert names == ["pypi"], f"expected member fallthrough to ['pypi'], got {names}"

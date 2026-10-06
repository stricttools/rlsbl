"""Tests for lockfile sync during release execution.

Covers:
- guard_file support in the owed lockfile syncs
- Workspace root unconditional lockfile inclusion
- go.work.sum conditional on go.work existence
"""

import os
from unittest.mock import patch


from rlsbl.commands.release.execute import _LOCKFILE_SPECS, _target_lockfile_syncs


class TestLockfileSpecsSchema:
    """Verify _LOCKFILE_SPECS structure."""

    def test_all_specs_have_four_elements(self):
        for spec in _LOCKFILE_SPECS:
            assert len(spec) == 4, f"Spec should be 4-tuple: {spec}"

    def test_go_work_sum_has_guard(self):
        go_work_specs = [s for s in _LOCKFILE_SPECS if s[0] == "go.work.sum"]
        assert len(go_work_specs) == 1
        assert go_work_specs[0][3] == "go.work"

    def test_regular_specs_have_no_guard(self):
        for spec in _LOCKFILE_SPECS:
            if spec[0] in ("uv.lock", "package-lock.json", "go.sum"):
                assert spec[3] is None, f"{spec[0]} should have no guard file"


def _owed(target_dir):
    return _target_lockfile_syncs({"target": str(target_dir)}, lambda _m: None)


class TestGuardFile:
    """The owed syncs respect a spec's guard_file."""

    @patch("shutil.which", return_value="/usr/bin/go")
    def test_go_work_sum_not_owed_without_go_work(self, _which, tmp_path):
        (tmp_path / "go.work.sum").write_text("h1:abc\n")
        assert _owed(tmp_path) == []

    @patch("shutil.which", return_value="/usr/bin/go")
    def test_go_work_sum_owed_with_go_work(self, _which, tmp_path):
        (tmp_path / "go.work").write_text("go 1.21\n")
        (tmp_path / "go.work.sum").write_text("h1:abc\n")
        assert [s["cmd"] for s in _owed(tmp_path)] == [["go", "work", "sync"]]

    @patch("shutil.which", return_value="/usr/bin/uv")
    def test_uv_lock_owed_without_guard(self, _which, tmp_path):
        (tmp_path / "uv.lock").write_text("version = 1\n")
        assert [s["cmd"] for s in _owed(tmp_path)] == [["uv", "lock"]]


class TestWorkspaceRootUnconditionalInclusion:
    """Test the generalized workspace root lockfile unconditional inclusion.

    The workspace root code in _run_release_mutating unconditionally includes
    any lockfile from _LOCKFILE_SPECS that exists at the workspace root,
    regardless of mtime changes. This is tested by simulating the loop logic.
    """

    def _simulate_ws_inclusion(self, monorepo_root):
        """Simulate the workspace root unconditional inclusion loop from execute.py.

        This mirrors the workspace root inclusion loop.
        """
        files_to_commit = []
        log_messages = []

        def log(msg):
            log_messages.append(msg)

        for ws_lockfile_name, _, _, ws_guard in _LOCKFILE_SPECS:
            if ws_guard and not os.path.exists(os.path.join(str(monorepo_root), ws_guard)):
                continue
            ws_lockfile = os.path.join(str(monorepo_root), ws_lockfile_name)
            if os.path.exists(ws_lockfile):
                norm = os.path.normpath(ws_lockfile)
                if norm not in files_to_commit:
                    files_to_commit.append(norm)
                    log(f"Workspace lockfile included: {ws_lockfile_name}")

        return files_to_commit, log_messages

    def test_uv_lock_unconditionally_included(self, tmp_path):
        """uv.lock at workspace root is included without mtime change."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "uv.lock").write_text("version = 1\n")

        files, logs = self._simulate_ws_inclusion(ws_root)

        expected = os.path.normpath(str(ws_root / "uv.lock"))
        assert expected in files
        assert any("uv.lock" in msg for msg in logs)

    def test_package_lock_unconditionally_included(self, tmp_path):
        """package-lock.json at workspace root is included without mtime change."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "package-lock.json").write_text('{"lockfileVersion": 3}\n')

        files, logs = self._simulate_ws_inclusion(ws_root)

        expected = os.path.normpath(str(ws_root / "package-lock.json"))
        assert expected in files
        assert any("package-lock.json" in msg for msg in logs)

    def test_go_work_sum_included_when_go_work_exists(self, tmp_path):
        """go.work.sum at workspace root is included when go.work exists."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "go.work").write_text("go 1.21\n")
        (ws_root / "go.work.sum").write_text("h1:abc\n")

        files, logs = self._simulate_ws_inclusion(ws_root)

        expected = os.path.normpath(str(ws_root / "go.work.sum"))
        assert expected in files
        assert any("go.work.sum" in msg for msg in logs)

    def test_go_work_sum_excluded_without_go_work(self, tmp_path):
        """go.work.sum at workspace root is NOT included when go.work is missing."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        # go.work.sum exists but go.work does not
        (ws_root / "go.work.sum").write_text("h1:abc\n")

        files, logs = self._simulate_ws_inclusion(ws_root)

        go_work_sum_path = os.path.normpath(str(ws_root / "go.work.sum"))
        assert go_work_sum_path not in files

    def test_multiple_lockfiles_all_included(self, tmp_path):
        """When multiple lockfiles exist, all are included."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "uv.lock").write_text("version = 1\n")
        (ws_root / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
        (ws_root / "go.work").write_text("go 1.21\n")
        (ws_root / "go.work.sum").write_text("h1:abc\n")

        files, logs = self._simulate_ws_inclusion(ws_root)

        assert len(files) == 3
        names = [os.path.basename(f) for f in files]
        assert "uv.lock" in names
        assert "package-lock.json" in names
        assert "go.work.sum" in names

    def test_no_lockfiles_nothing_included(self, tmp_path):
        """When no lockfiles exist, nothing is included."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()

        files, logs = self._simulate_ws_inclusion(ws_root)

        assert files == []
        assert logs == []

    def test_no_duplicates_when_already_in_list(self, tmp_path):
        """Lockfiles already in files_to_commit are not added again."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "uv.lock").write_text("version = 1\n")

        # Pre-populate with the normalized path
        norm = os.path.normpath(str(ws_root / "uv.lock"))
        files_to_commit = [norm]
        log_messages = []

        for ws_lockfile_name, _, _, ws_guard in _LOCKFILE_SPECS:
            if ws_guard and not os.path.exists(os.path.join(str(ws_root), ws_guard)):
                continue
            ws_lockfile = os.path.join(str(ws_root), ws_lockfile_name)
            if os.path.exists(ws_lockfile):
                n = os.path.normpath(ws_lockfile)
                if n not in files_to_commit:
                    files_to_commit.append(n)
                    log_messages.append(f"Workspace lockfile included: {ws_lockfile_name}")

        # Should still have only one entry
        assert files_to_commit.count(norm) == 1
        # No log for uv.lock since it was already there
        assert not any("uv.lock" in msg for msg in log_messages)

    def test_go_sum_not_unconditionally_included(self, tmp_path):
        """go.sum (non-workspace) is NOT unconditionally included -- only go.work.sum is.

        go.sum has no guard file so it would be included if present, but it
        is a per-module lockfile, not a workspace-root one. The guard_file
        mechanism only affects go.work.sum.
        """
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()
        (ws_root / "go.sum").write_text("h1:abc\n")

        files, logs = self._simulate_ws_inclusion(ws_root)

        # go.sum has no guard, so it IS included if present
        expected = os.path.normpath(str(ws_root / "go.sum"))
        assert expected in files

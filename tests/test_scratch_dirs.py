"""The scaffolded scratch directories: ``experiments/`` and ``screenshots/``.

Both are created at every project's root by ``rlsbl scaffold``, each carrying a
committed ``.gitignore`` that ignores everything but itself, so the directory
exists in a fresh clone while nothing inside it can be committed by accident.
Repository-walking checks deliberately ignore ``.gitignore``, so they prune both
directories by name at the root of the project being checked.
"""

import json
import os
import subprocess
from pathlib import Path

from rlsbl.context import ProjectContext

from conftest import capture_all_checks, run_git

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The whole content of each scratch directory's committed ``.gitignore``.
SCRATCH_GITIGNORE = "*\n!.gitignore\n"

SCRATCH_TARGETS = ("experiments/.gitignore", "screenshots/.gitignore")

_PYPROJECT = '[project]\nname = "mylib"\nversion = "0.1.0"\n'


def _scratch_mappings():
    """The shared scaffold mappings that write the scratch ``.gitignore`` files."""
    from rlsbl.targets.base import BaseTarget

    base = BaseTarget()
    mappings = [
        m for m in base.shared_template_mappings(None)
        if m["target"] in SCRATCH_TARGETS
    ]
    return base.shared_template_dir(), mappings


class TestScaffoldWritesScratchDirs:
    """Scaffold creates both directories, through the shared template registry."""

    def test_shared_mappings_carry_both_scratch_dirs(self):
        """Both scratch gitignores are target-independent shared mappings."""
        _tpl_dir, mappings = _scratch_mappings()
        assert {m["target"] for m in mappings} == set(SCRATCH_TARGETS)

    def test_fresh_scaffold_creates_both_directories(self, tmp_project):
        """A fresh project gets both directories with the two-line ignore file."""
        from rlsbl.commands.init_cmd import process_mappings

        tpl_dir, mappings = _scratch_mappings()
        created, _skipped, _warnings, _hashes = process_mappings(tpl_dir, mappings, {})

        created_targets = {t for t, _ in created}
        for target in SCRATCH_TARGETS:
            path = tmp_project / target
            assert path.exists(), f"{target} was not created"
            assert path.read_text() == SCRATCH_GITIGNORE
            assert target in created_targets

    def test_managed_files_registry_lists_both(self, tmp_project):
        """Both files enter the managed-files registry scaffold writes."""
        from rlsbl.commands.init_cmd import process_mappings

        tpl_dir, mappings = _scratch_mappings()
        _created, _skipped, _warnings, hashes = process_mappings(tpl_dir, mappings, {})

        for target in SCRATCH_TARGETS:
            assert target in hashes

    def test_rlsbl_own_registry_lists_both(self):
        """rlsbl's own repository carries both files in its managed-files registry."""
        registry = json.loads(
            (REPO_ROOT / ".rlsbl" / "managed-files.json").read_text()
        )
        for target in SCRATCH_TARGETS:
            assert target in registry["files"]

    def test_rlsbl_own_scratch_dirs_on_disk(self):
        """rlsbl's own repository carries both directories with the ignore file."""
        for target in SCRATCH_TARGETS:
            assert (REPO_ROOT / target).read_text() == SCRATCH_GITIGNORE

    def test_rescaffold_over_filled_experiments_changes_nothing(self, tmp_project):
        """Re-scaffolding a project whose experiments/ holds work removes nothing."""
        from rlsbl.commands.init_cmd import process_mappings

        experiments = tmp_project / "experiments"
        experiments.mkdir()
        (experiments / ".gitignore").write_text(SCRATCH_GITIGNORE)
        probe = experiments / "probe-repo" / "note.txt"
        probe.parent.mkdir()
        probe.write_text("a produced repository\n")

        tpl_dir, mappings = _scratch_mappings()
        created, skipped, _warnings, _hashes = process_mappings(tpl_dir, mappings, {})

        assert probe.read_text() == "a produced repository\n"
        assert (experiments / ".gitignore").read_text() == SCRATCH_GITIGNORE
        assert "experiments/.gitignore" not in {t for t, _ in created}
        statuses = dict(skipped)
        assert "experiments/.gitignore" in statuses
        assert statuses["experiments/.gitignore"].startswith("unchanged")

    def test_directories_present_in_a_fresh_clone(self, tmp_path):
        """The committed .gitignore carries the directory into a fresh clone."""
        from rlsbl.commands.init_cmd import process_mappings

        origin = tmp_path / "origin"
        origin.mkdir()
        cwd = os.getcwd()
        os.chdir(origin)
        try:
            tpl_dir, mappings = _scratch_mappings()
            process_mappings(tpl_dir, mappings, {})
        finally:
            os.chdir(cwd)

        run_git(origin, "init", "-q", "-b", "main")
        run_git(origin, "config", "user.email", "test@test.local")
        run_git(origin, "config", "user.name", "Test")
        for target in SCRATCH_TARGETS:
            run_git(origin, "add", target)
        run_git(origin, "commit", "-q", "-m", "scaffold scratch directories")

        # A produced artifact inside the directory is ignored, not committable.
        (origin / "experiments" / "produced.bin").write_text("artifact\n")
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(origin), capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == ""

        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)], check=True,
        )
        for target in SCRATCH_TARGETS:
            assert (clone / target).read_text() == SCRATCH_GITIGNORE


class TestWalksPruneScratchDirs:
    """Every repository walk prunes both directories at the project root."""

    def test_walk_source_files_prunes_scratch_at_the_root(self, tmp_path):
        """A source file planted in either scratch directory is never collected."""
        from rlsbl.lint.utils import walk_source_files

        (tmp_path / "mylib").mkdir()
        (tmp_path / "mylib" / "core.py").write_text("x = 1\n")
        for name in ("experiments", "screenshots"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "probe.py").write_text("y = 2\n")

        found = walk_source_files(str(tmp_path), (".py",), [])
        rel = {os.path.relpath(p, str(tmp_path)) for p in found}
        assert rel == {os.path.join("mylib", "core.py")}

    def test_walk_source_files_keeps_nested_same_named_dirs(self, tmp_path):
        """Only the project root's scratch directories are pruned, not deeper ones."""
        from rlsbl.lint.utils import walk_source_files

        nested = tmp_path / "mylib" / "experiments"
        nested.mkdir(parents=True)
        (nested / "real.py").write_text("x = 1\n")

        found = walk_source_files(str(tmp_path), (".py",), [])
        rel = {os.path.relpath(p, str(tmp_path)) for p in found}
        assert rel == {os.path.join("mylib", "experiments", "real.py")}

    def test_dead_modules_check_ignores_planted_modules(self, tmp_path):
        """dead-modules stays green with an unreferenced module in each scratch dir."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import run\n")
        (pkg / "core.py").write_text("def run(): pass\n")
        for name in ("experiments", "screenshots"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "orphan.py").write_text("z = 3\n")

        captured = capture_all_checks()
        ctx = ProjectContext(
            project_root=Path(tmp_path), workspace_root=None, config={},
        )
        result = captured["dead-modules"](ctx)
        assert result.status == "pass", result.message

    def test_go_source_walk_ignores_planted_go_files(self, tmp_path):
        """The ldflags Go source walk never collects a file from a scratch dir."""
        from rlsbl.ldflags_symbols import _GoSource

        (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
        cmd = tmp_path / "cmd"
        cmd.mkdir()
        (cmd / "main.go").write_text("package main\n\nfunc main() {}\n")
        for name in ("experiments", "screenshots"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "probe.go").write_text("package main\n")

        assert _GoSource(str(tmp_path)).go_files() == ["cmd/main.go"]

    def test_workspace_unregistered_ignores_planted_manifest(
        self, tmp_path, monkeypatch,
    ):
        """workspace-unregistered stays green with a manifest inside a scratch dir."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import WorkspaceProject

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        alpha = repo / "alpha"
        alpha.mkdir()
        (alpha / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )
        # A produced repository inside a scratch directory carries a manifest.
        for name in ("experiments", "screenshots"):
            (repo / name).mkdir()
            (repo / name / "pyproject.toml").write_text(
                '[project]\nname = "probe"\nversion = "0.1.0"\n'
            )
        run_git(repo, "add", "alpha/pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add alpha")

        projects = [WorkspaceProject({"name": "alpha", "path": "alpha"})]
        ctx = WorkspaceCheckContext(
            project_root=repo,
            workspace_root=repo,
            config={},
            projects=projects,
        )
        captured = capture_all_checks()
        result = captured["workspace-unregistered"](ctx)
        assert result.status == "pass", result.message

"""Tests for the scaffold-conflicts check: unresolved git merge conflict
markers in scaffold-managed files must hard-error.

The scaffold three-way merge (git merge-file) intentionally leaves conflict
markers for manual resolution; this check verifies they were actually
resolved before a push or release ships corrupted files.
"""

import json

import pytest

from conftest import make_ctx

from rlsbl import app
from rlsbl.checks.project import find_conflicted_scaffold_files
from rlsbl.commands.release import _abort_on_scaffold_conflicts


CONFLICTED_CONTENT = (
    "name: Publish\n"
    "<<<<<<< HEAD\n"
    "on: push\n"
    "=======\n"
    "on: pull_request\n"
    ">>>>>>> template\n"
)

# A bare '=======' line (e.g. setext heading underline) is NOT a conflict.
SEPARATOR_ONLY_CONTENT = (
    "Heading\n"
    "=======\n"
    "body text\n"
)


def _write_managed_files_json(tmp_project, paths):
    """Write .rlsbl/managed-files.json listing *paths* (relative to root)."""
    rlsbl_dir = tmp_project / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "managed-files.json").write_text(
        json.dumps({"version": 1, "files": {p: "deadbeef" for p in paths}})
        + "\n"
    )


def _run_check(tmp_project):
    ctx = make_ctx(tmp_project)
    return app._check_defs["scaffold-conflicts"].impl(ctx)


class TestScaffoldConflictsCheck:
    """Tests for the scaffold-conflicts check function."""

    def test_managed_file_with_both_markers_fails(self, tmp_project):
        """A managed file containing both conflict markers fails the check,
        and the failing file is named in the details."""
        _write_managed_files_json(tmp_project, [".goreleaser.yml"])
        (tmp_project / ".goreleaser.yml").write_text(CONFLICTED_CONTENT)
        result = _run_check(tmp_project)
        assert result.status == "fail"
        assert any(".goreleaser.yml" in d for d in result.details)

    def test_separator_only_line_passes(self, tmp_project):
        """A bare '=======' line without <<<<<<< / >>>>>>> is not a conflict."""
        _write_managed_files_json(tmp_project, ["README.md"])
        (tmp_project / "README.md").write_text(SEPARATOR_ONLY_CONTENT)
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_start_marker_only_passes(self, tmp_project):
        """A '<<<<<<< ' line without a matching '>>>>>>> ' line passes
        (both markers are required to call a file conflicted)."""
        _write_managed_files_json(tmp_project, ["notes.txt"])
        (tmp_project / "notes.txt").write_text("<<<<<<< HEAD\njust noise\n")
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_clean_files_pass(self, tmp_project):
        """Clean managed files, workflows, and hooks pass."""
        _write_managed_files_json(
            tmp_project, [".github/workflows/publish.yml"]
        )
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\non: push\n")
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-release.sh").write_text("#!/usr/bin/env bash\n")
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_missing_managed_files_json_passes(self, tmp_project):
        """Missing .rlsbl/managed-files.json is skipped gracefully."""
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_malformed_managed_files_json_skipped(self, tmp_project):
        """Malformed managed-files.json does not crash the check."""
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir(parents=True)
        (rlsbl_dir / "managed-files.json").write_text("{not json")
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_missing_listed_file_skipped(self, tmp_project):
        """Files listed in managed-files.json but absent on disk are skipped."""
        _write_managed_files_json(tmp_project, ["gone.yml"])
        result = _run_check(tmp_project)
        assert result.status == "pass"

    def test_conflicted_workflow_caught_without_registry_entry(self, tmp_project):
        """A conflicted .github/workflows file is caught even when it is
        not listed in managed-files.json (and even when the registry file
        does not exist at all)."""
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(CONFLICTED_CONTENT)
        result = _run_check(tmp_project)
        assert result.status == "fail"
        assert any("publish.yml" in d for d in result.details)

    def test_conflicted_hook_caught_without_registry_entry(self, tmp_project):
        """A conflicted .rlsbl/hooks file is caught even when it is not
        listed in managed-files.json."""
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-release.sh").write_text(
            "#!/usr/bin/env bash\n" + CONFLICTED_CONTENT
        )
        result = _run_check(tmp_project)
        assert result.status == "fail"
        assert any("pre-release.sh" in d for d in result.details)


class TestFindConflictedScaffoldFiles:
    """Tests for the shared helper used by both the check and release run."""

    def test_returns_relative_paths_sorted(self, tmp_project):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(CONFLICTED_CONTENT)
        (wf_dir / "ci.yml").write_text(CONFLICTED_CONTENT)
        result = find_conflicted_scaffold_files(tmp_project)
        assert result == [
            ".github/workflows/ci.yml",
            ".github/workflows/publish.yml",
        ]

    def test_file_listed_twice_reported_once(self, tmp_project):
        """A workflow listed in managed-files.json is reported only once."""
        _write_managed_files_json(
            tmp_project, [".github/workflows/publish.yml"]
        )
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(CONFLICTED_CONTENT)
        result = find_conflicted_scaffold_files(tmp_project)
        assert result == [".github/workflows/publish.yml"]

    def test_clean_project_returns_empty(self, tmp_project):
        assert find_conflicted_scaffold_files(tmp_project) == []


class TestReleaseAbortsOnScaffoldConflicts:
    """Release run must abort pre-mutation when scaffold files are conflicted."""

    def test_aborts_and_names_conflicted_files(self, tmp_project, capsys):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text(CONFLICTED_CONTENT)
        with pytest.raises(SystemExit) as excinfo:
            _abort_on_scaffold_conflicts(str(tmp_project))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "conflict" in captured.err
        assert ".github/workflows/publish.yml" in captured.err

    def test_no_op_when_clean(self, tmp_project, capsys):
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "publish.yml").write_text("name: Publish\non: push\n")
        _abort_on_scaffold_conflicts(str(tmp_project))
        captured = capsys.readouterr()
        assert captured.err == ""

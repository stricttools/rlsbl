"""The release passes the about-to-be-released version to selfdoc.

selfdoc resolves version-bearing generated content (the CLI index's Version
line, root-file version directives) from the project's CURRENT version. During
a release, selfdoc runs BEFORE the version bump is written to disk, so every
such line was generated for the previous version -- shipping one release stale
-- and the resulting churn tripped the doc-staleness check on the very next
release, aborting it until the operator committed the release's own
regeneration artifacts and baseline-accepted.

The release now forwards the new version as ``--version-override`` to both
``selfdoc gen`` and ``selfdoc check``.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.commands.release.validate import (
    _run_selfdoc_check,
    _run_selfdoc_gen,
    _selfdoc_version_args,
)

MOD = "rlsbl.commands.release"


def _selfdoc_project(tmp_path):
    (tmp_path / "selfdoc.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0"}) + "\n"
    )
    return tmp_path


def _argv_of(mock_effects):
    assert mock_effects.run.called, "selfdoc must have been invoked"
    return list(mock_effects.run.call_args[0][0])


class TestVersionArgs:

    def test_a_version_produces_the_override_flag(self):
        assert _selfdoc_version_args("1.2.3") == ["--version-override", "1.2.3"]

    def test_no_version_produces_nothing(self):
        assert _selfdoc_version_args(None) == []
        assert _selfdoc_version_args("") == []


class TestOverrideReachesTheSubprocess:
    """Red-green: without the wiring the argv is the bare gen/check command
    and selfdoc resolves the OLD version off disk."""

    def _run(self, tmp_path, fn, **kwargs):
        _selfdoc_project(tmp_path)
        fake_effects = MagicMock()
        with (
            patch(f"{MOD}.require_tool", return_value=True),
            patch(f"{MOD}.effects", fake_effects),
        ):
            fn({}, project_dir=str(tmp_path), **kwargs)
        return _argv_of(fake_effects)

    def test_gen_receives_the_override(self, tmp_path):
        argv = self._run(tmp_path, _run_selfdoc_gen, version="2.0.0")
        assert argv == [
            "selfdoc", "gen", "--no-auto-commit", "--version-override", "2.0.0",
        ]

    def test_check_receives_the_override(self, tmp_path):
        argv = self._run(tmp_path, _run_selfdoc_check, version="2.0.0")
        assert argv == [
            "selfdoc", "check", "--no-auto-commit",
            "--version-override", "2.0.0",
        ]

    def test_without_a_version_the_argv_is_unchanged(self, tmp_path):
        assert self._run(tmp_path, _run_selfdoc_gen) == [
            "selfdoc", "gen", "--no-auto-commit",
        ]

    def test_dry_run_records_the_same_argv(self, tmp_path):
        """A preview records the real argv instead of describing it by hand.

        This used to be a hand-rolled ``Would run: selfdoc gen ...`` print,
        which restated the argv in a second place and could drift from it.
        The call is an ``effects.run``, so a preview records it and the
        framework's would-do log reports exactly what would have run --
        including the version override.
        """
        argv = self._run(tmp_path, _run_selfdoc_gen, version="2.0.0")
        assert argv == [
            "selfdoc", "gen", "--no-auto-commit", "--version-override", "2.0.0",
        ]


class TestReleaseFlowThreadsTheNewVersion:
    """The release flow passes the version it is about to produce, not the
    one currently on disk."""

    def test_release_passes_the_new_version(self, tmp_project):
        from githarness import git
        from rlsbl.commands.release import run_cmd
        from rlsbl.context import create_context

        from test_representative_write_elimination import (
            _rc, _release_patches, _setup_releasable_workspace,
        )

        core = _setup_releasable_workspace(tmp_project)
        # selfdoc.json at the member root makes the release invoke selfdoc.
        (core / "selfdoc.json").write_text(
            json.dumps({"name": "core", "version": "1.0.0"}) + "\n"
        )
        git(tmp_project, "add", "packages/core/selfdoc.json")
        git(tmp_project, "commit", "-q", "-m", "add selfdoc config")
        # Cover the new commit so the changelog preflight passes.
        from rlsbl.workspace import get_releasable_changes_dir

        unreleased = os.path.join(
            get_releasable_changes_dir(str(tmp_project), "alpha"),
            "unreleased.jsonl",
        )
        with open(unreleased, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "commits": [git(tmp_project, "rev-parse", "HEAD")],
                "user_facing": False,
            }) + "\n")
        git(tmp_project, "add", os.path.relpath(unreleased, str(tmp_project)))
        git(tmp_project, "commit", "-q", "-m", "changelog: selfdoc config")

        seen = []

        def record_gen(flags, project_dir=None, version=None):
            seen.append(("gen", version))
            return True

        def record_check(flags, project_dir=None, version=None):
            seen.append(("check", version))
            return True

        ctx = create_context(
            Path(str(core)), workspace_root=Path(str(tmp_project)),
        )
        patches = _release_patches((
            patch("rlsbl.commands.release.wait_for_ci_green",
                  return_value=("green", [])),
            patch("rlsbl.commands.release._run_selfdoc_gen", record_gen),
            patch("rlsbl.commands.release._run_selfdoc_check", record_check),
        ))
        for p in patches:
            p.start()
        try:
            run_cmd(_rc(), {"quiet": True, "skip-lock": True},
                    ctx=ctx)
        finally:
            for p in patches:
                p.stop()

        assert seen == [("gen", "1.0.1"), ("check", "1.0.1")], (
            "both selfdoc steps must receive the version being released "
            "(1.0.1), not the version on disk (1.0.0)"
        )

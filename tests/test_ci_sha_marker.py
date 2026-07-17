"""Test for the CI-SHA release-notes marker (1.3).

At GitHub Release creation, rlsbl appends a machine-parseable marker line to
the release notes: `<!-- rlsbl-ci-sha: <sha> -->`, where <sha> is the pushed
branch tip (the commit CI runs on). The publish gate reads this marker to
learn exactly which commit to gate on.
"""

import subprocess

from unittest.mock import patch

from githarness import git

from test_representative_write_elimination import (  # noqa: E402
    _run_release,
    _setup_releasable_workspace,
)


class TestCiShaMarker:

    def test_release_notes_carry_ci_sha_marker(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)

        captured = {}

        def capturing_gh(args, **kwargs):
            # `release view` must fail so the flow proceeds to create the
            # Release (and write the notes file we want to inspect).
            if args[:2] == ["release", "view"]:
                raise subprocess.CalledProcessError(1, "gh release view")
            if args[:2] == ["release", "create"] and "--notes-file" in args:
                notes_path = args[args.index("--notes-file") + 1]
                with open(notes_path, encoding="utf-8") as f:
                    captured["notes"] = f.read()
            return ""

        # The extra patch on the SAME target wins (started last), so the
        # capturing run_gh replaces the base no-op run_gh.
        _run_release(
            core, tmp_project,
            extra_patches=(
                patch("rlsbl.commands.release.run_gh", side_effect=capturing_gh),
            ),
        )

        assert "notes" in captured, "gh release create must have been invoked"
        tag_sha = git(tmp_project, "rev-list", "-n", "1", "alpha@v1.0.1")
        assert f"<!-- rlsbl-ci-sha: {tag_sha} -->" in captured["notes"], (
            "release notes must carry the CI-SHA marker for the pushed tip"
        )

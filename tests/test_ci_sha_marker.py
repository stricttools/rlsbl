"""Test for the CI-SHA release-notes marker (1.3).

At GitHub Release creation, rlsbl appends a machine-parseable marker line to
the release notes: `<!-- rlsbl-ci-sha: <sha> -->`, where <sha> is the CI-verified
release candidate (the commit the tag points at). The publish gate reads this
marker to learn exactly which commit to gate on.
"""

import os
import subprocess

import pytest
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
            "release notes must carry the CI-SHA marker for the verified candidate"
        )


class TestCiShaMarkerWhenReleaseAlreadyExists:
    """The marker is written unconditionally, not only at creation.

    A GitHub Release that already exists when the notes are written -- a
    resumed release, or one created out of band -- used to skip the marker
    entirely, so the publish gate silently fell back to `$GITHUB_SHA` and
    gated on whatever commit the workflow happened to observe.
    """

    def _run_with_existing_release(self, tmp_project, body):
        core = _setup_releasable_workspace(tmp_project)
        calls = []

        def capturing_gh(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["release", "view"]:
                if "--json" in args:
                    return body
                return "existing release"
            if args[:2] == ["release", "edit"] and "--notes-file" in args:
                path = args[args.index("--notes-file") + 1]
                with open(path, encoding="utf-8") as f:
                    calls.append(("edited-body", f.read()))
            return ""

        _run_release(
            core, tmp_project,
            extra_patches=(
                patch("rlsbl.commands.release.run_gh", side_effect=capturing_gh),
            ),
        )
        return calls

    def _edited_body(self, calls):
        edits = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "edited-body"]
        assert edits, f"expected a `gh release edit`, got {calls}"
        return edits[-1]

    def test_marker_is_edited_into_a_body_that_lacks_it(self, tmp_project):
        calls = self._run_with_existing_release(tmp_project, "Release notes\n")
        tag_sha = git(tmp_project, "rev-list", "-n", "1", "alpha@v1.0.1")
        assert f"<!-- rlsbl-ci-sha: {tag_sha} -->" in self._edited_body(calls)

    def test_a_stale_marker_is_replaced_not_duplicated(self, tmp_project):
        stale = "0" * 40
        calls = self._run_with_existing_release(
            tmp_project, f"Release notes\n\n<!-- rlsbl-ci-sha: {stale} -->\n",
        )
        body = self._edited_body(calls)
        tag_sha = git(tmp_project, "rev-list", "-n", "1", "alpha@v1.0.1")
        assert body.count("rlsbl-ci-sha") == 1
        assert f"<!-- rlsbl-ci-sha: {tag_sha} -->" in body
        assert stale not in body
        assert "Release notes" in body

    def test_a_correct_marker_is_left_alone(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        edits = []

        def capturing_gh(args, **kwargs):
            if args[:2] == ["release", "view"]:
                if "--json" in args:
                    # The marker names the CI-VERIFIED commit, which is the
                    # tag's commit -- an ancestor of HEAD once the finalization
                    # commits land on top of it.
                    sha = git(tmp_project, "rev-list", "-n", "1", "alpha@v1.0.1")
                    return f"Release notes\n\n<!-- rlsbl-ci-sha: {sha} -->\n"
                return "existing release"
            if args[:2] == ["release", "edit"]:
                edits.append(list(args))
            return ""

        _run_release(
            core, tmp_project,
            extra_patches=(
                patch("rlsbl.commands.release.run_gh", side_effect=capturing_gh),
            ),
        )
        assert edits == []


class TestCiShaMarkerReconcileFailureIsFatal:
    """A marker that cannot be read or written is a HARD failure.

    The marker is the publish gate's only precise statement of which commit CI
    proved green. Without it the gate falls back to ``$GITHUB_SHA`` -- whatever
    commit the workflow happens to observe -- so a silent fallback publishes a
    tag gated on a commit nobody verified. This used to be a stderr warning
    whose return value the caller discarded, and the release exited 0.
    """

    def _run(self, tmp_project, gh):
        core = _setup_releasable_workspace(tmp_project)
        with pytest.raises(SystemExit) as exc:
            _run_release(
                core, tmp_project,
                extra_patches=(
                    patch("rlsbl.commands.release.run_gh", side_effect=gh),
                ),
            )
        return exc.value.code

    def test_unreadable_release_body_aborts(self, tmp_project):
        def gh(args, **kwargs):
            if args[:2] == ["release", "view"] and "--json" in args:
                raise subprocess.CalledProcessError(1, "gh release view --json")
            if args[:2] == ["release", "view"]:
                return "existing release"
            return ""

        assert self._run(tmp_project, gh) == 1

    def test_unwritable_marker_aborts(self, tmp_project):
        def gh(args, **kwargs):
            if args[:2] == ["release", "view"]:
                if "--json" in args:
                    return "Release notes\n"
                return "existing release"
            if args[:2] == ["release", "edit"]:
                raise subprocess.CalledProcessError(1, "gh release edit")
            return ""

        assert self._run(tmp_project, gh) == 1

    def test_state_is_preserved_for_resume(self, tmp_project):
        """The release stays resumable: a resume re-attempts the reconcile."""
        from rlsbl.commands.release.release_state import get_state_path
        from rlsbl.workspace import get_releasable_dir

        core = _setup_releasable_workspace(tmp_project)

        def gh(args, **kwargs):
            if args[:2] == ["release", "view"] and "--json" in args:
                raise subprocess.CalledProcessError(1, "gh release view --json")
            if args[:2] == ["release", "view"]:
                return "existing release"
            return ""

        with pytest.raises(SystemExit):
            _run_release(
                core, tmp_project,
                extra_patches=(
                    patch("rlsbl.commands.release.run_gh", side_effect=gh),
                ),
            )

        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        state_path = get_state_path(str(tmp_project), releasable_dir=rel_dir)
        assert os.path.exists(state_path), (
            "the in-progress state must survive so `rlsbl release resume` "
            "can re-attempt the marker reconcile"
        )

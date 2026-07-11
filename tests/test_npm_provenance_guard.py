"""Tests for the release-time npm provenance preflight guard.

npm build-provenance attestations (`npm publish --provenance`) require a
PUBLIC GitHub source repository and GitHub Actions OIDC. When an npm pipeline
declares ``provenance: true``, the release must verify the repository is
public before mutating anything. Private repo => hard error naming the three
ways out. Public repo => pass. ``provenance: false`` => no network at all.
A failed repository resolution (e.g. a non-GitHub remote) is a hard error
telling the user to set ``provenance: false``.
"""

from unittest.mock import patch

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _abort_on_npm_provenance,
)

RUN_GH = "rlsbl.commands.release.validate.run_gh"


def _npm_config(provenance):
    return {"pipelines": {"npm": {"type": "npm", "local": False,
                                  "provenance": provenance}}}


class TestNpmProvenanceGuard:
    def test_private_repo_with_provenance_true_aborts(self):
        with patch(RUN_GH, return_value='{"isPrivate": true}') as gh:
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_npm_provenance([_npm_config(True)], gh_config={})
        gh.assert_called_once()
        msg = str(exc.value)
        # names the three ways out
        assert "public" in msg.lower()
        assert "provenance" in msg.lower() and "false" in msg.lower()
        assert "pipeline" in msg.lower()  # drop the npm pipeline

    def test_public_repo_with_provenance_true_passes(self):
        with patch(RUN_GH, return_value='{"isPrivate": false}') as gh:
            _abort_on_npm_provenance([_npm_config(True)], gh_config={})
        gh.assert_called_once()

    def test_provenance_false_makes_no_network_call(self):
        with patch(RUN_GH) as gh:
            _abort_on_npm_provenance([_npm_config(False)], gh_config={})
        gh.assert_not_called()

    def test_no_npm_pipeline_makes_no_network_call(self):
        with patch(RUN_GH) as gh:
            _abort_on_npm_provenance(
                [{"pipelines": {"p": {"type": "pypi", "local": False}}}],
                gh_config={},
            )
        gh.assert_not_called()

    def test_repo_resolution_failure_is_hard_error(self):
        # Non-GitHub remote: `gh repo view` fails.
        import subprocess
        with patch(RUN_GH, side_effect=subprocess.CalledProcessError(1, "gh")):
            with pytest.raises(ReleaseValidationError) as exc:
                _abort_on_npm_provenance([_npm_config(True)], gh_config={})
        assert "provenance" in str(exc.value).lower()
        assert "false" in str(exc.value).lower()

    def test_unparseable_visibility_is_hard_error(self):
        with patch(RUN_GH, return_value="not json"):
            with pytest.raises(ReleaseValidationError):
                _abort_on_npm_provenance([_npm_config(True)], gh_config={})

    def test_gh_config_forwarded_for_repo_resolution(self):
        sentinel = {"github": {"repo": "owner/name"}}
        with patch(RUN_GH, return_value='{"isPrivate": false}') as gh:
            _abort_on_npm_provenance([_npm_config(True)], gh_config=sentinel)
        # config passed positionally or by keyword
        _, kwargs = gh.call_args
        passed = kwargs.get("config")
        if passed is None and len(gh.call_args[0]) > 1:
            passed = gh.call_args[0][1]
        assert passed is sentinel

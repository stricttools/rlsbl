"""Tests for the ``npm-token-presence`` check.

The failure class: a repository whose CI publishes to npm has no ``NPM_TOKEN``
secret, so the publish job dies with ``ENEEDAUTH`` -- after the release has
already tagged, pushed and created the GitHub Release. The secret's presence is
knowable long before that.

Every probe here is stubbed at the ``effects.gh`` seam: no test contacts
GitHub.
"""

from types import SimpleNamespace

import pytest
import strictcli

from rlsbl import app, effects
from rlsbl.ci_secrets import (
    NPM_TOKEN,
    evaluate_npm_token_presence,
    npm_ci_pipelines,
    npm_token_remedy,
    probe_repo_secret,
)

from conftest import make_ctx


def _gh(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _config(*, pipelines=None, publish_mode="ci", repo="owner/repo"):
    config = {"publish_mode": publish_mode, "github_repo": repo}
    if pipelines is not None:
        config["pipelines"] = pipelines
    return config


NPM_CI = {"npm": {"type": "npm", "local": False, "target": "npm"}}


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


class TestProbe:
    def test_a_zero_exit_is_presence(self, monkeypatch):
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "{}"))
        assert probe_repo_secret("owner/repo", NPM_TOKEN)["status"] == "present"

    def test_a_404_is_absence(self, monkeypatch):
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: _gh(1, "", "gh: Not Found (HTTP 404)"),
        )
        assert probe_repo_secret("owner/repo", NPM_TOKEN)["status"] == "absent"

    def test_a_permission_failure_is_unknown_not_absence(self, monkeypatch):
        """403 must never read as "the secret is not there"."""
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: _gh(1, "", "gh: Forbidden (HTTP 403)"),
        )
        result = probe_repo_secret("owner/repo", NPM_TOKEN)
        assert result["status"] == "unknown"
        assert "403" in result["message"]

    def test_a_failure_to_run_gh_is_unknown(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("gh not found")

        monkeypatch.setattr(effects, "gh", boom)
        assert probe_repo_secret("owner/repo", NPM_TOKEN)["status"] == "unknown"

    def test_a_recorded_call_is_unknown(self, monkeypatch):
        """Under a preview past a recorded mutation, gh answers a carrier."""
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: strictcli.Unsettled("«stale»", None, "probe", True),
        )
        assert probe_repo_secret("owner/repo", NPM_TOKEN)["status"] == "unknown"

    def test_the_argv_is_the_get_pinned_read(self, monkeypatch):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return _gh(0, "{}")

        monkeypatch.setattr(effects, "gh", fake)
        probe_repo_secret("owner/repo", NPM_TOKEN)
        assert seen["args"][:3] == ["api", "--method", "GET"]
        assert seen["args"][3] == f"repos/owner/repo/actions/secrets/{NPM_TOKEN}"

    def test_the_probe_never_asks_for_a_value(self, monkeypatch):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return _gh(0, "{}")

        monkeypatch.setattr(effects, "gh", fake)
        probe_repo_secret("owner/repo", NPM_TOKEN)
        assert not any("token" in a.lower() for a in seen["args"][4:])


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


class TestApplicability:
    def test_a_local_npm_pipeline_needs_no_repository_secret(self):
        pipelines = {"npm": {"type": "npm", "local": True, "target": "npm"}}
        assert npm_ci_pipelines(pipelines and {"pipelines": pipelines}) == []

    def test_a_ci_npm_pipeline_is_in_scope(self):
        assert npm_ci_pipelines({"pipelines": NPM_CI}) == ["npm"]

    def test_publish_mode_none_skips(self):
        verdict = evaluate_npm_token_presence(
            _config(pipelines=NPM_CI, publish_mode="none"), "owner/repo",
        )
        assert verdict.skip_reason is not None

    def test_no_npm_pipeline_skips(self):
        pipelines = {"pypi": {"type": "pypi", "local": False, "target": "pypi"}}
        verdict = evaluate_npm_token_presence(
            _config(pipelines=pipelines), "owner/repo",
        )
        assert verdict.skip_reason is not None

    def test_no_repository_slug_skips(self):
        verdict = evaluate_npm_token_presence(_config(pipelines=NPM_CI), None)
        assert verdict.skip_reason is not None
        assert "GitHub repository" in verdict.skip_reason


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_a_present_secret_passes(self):
        verdict = evaluate_npm_token_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "present"},
        )
        assert verdict.ok

    def test_an_absent_secret_names_the_remedy(self):
        verdict = evaluate_npm_token_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "absent"},
        )
        assert not verdict.ok
        assert npm_token_remedy("owner/repo") in verdict.problems[0]

    def test_an_unanswered_probe_is_an_error_not_a_pass(self):
        """Fail-closed: "we could not ask" is not evidence."""
        verdict = evaluate_npm_token_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "unknown", "message": "no auth"},
        )
        assert not verdict.ok
        assert "no auth" in verdict.problems[0]

    def test_a_missing_publish_mode_is_reported(self):
        verdict = evaluate_npm_token_presence(
            {"pipelines": NPM_CI}, "owner/repo",
            probe=lambda slug, name: {"status": "present"},
        )
        assert not verdict.ok
        assert "publish_mode" in verdict.problems[0]


# ---------------------------------------------------------------------------
# The registered check
# ---------------------------------------------------------------------------


def _run(tmp_path, config):
    return app._check_defs["npm-token-presence"].impl(make_ctx(tmp_path, config))


class TestRegisteredCheck:
    def test_a_present_secret_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(effects, "gh", lambda *a, **k: _gh(0, "{}"))
        result = _run(tmp_path, _config(pipelines=NPM_CI))
        assert result.status == "pass"

    def test_an_absent_secret_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: _gh(1, "", "gh: Not Found (HTTP 404)"),
        )
        result = _run(tmp_path, _config(pipelines=NPM_CI))
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert "gh secret set NPM_TOKEN --repo owner/repo" in text

    def test_an_inconclusive_probe_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            effects, "gh",
            lambda *a, **k: _gh(1, "", "dial tcp: lookup api.github.com"),
        )
        result = _run(tmp_path, _config(pipelines=NPM_CI))
        assert result.status == "fail"

    def test_a_project_without_npm_ci_skips_without_probing(
        self, tmp_path, monkeypatch,
    ):
        def refuse(*a, **k):
            raise AssertionError("the check must not probe when it does not apply")

        monkeypatch.setattr(effects, "gh", refuse)
        result = _run(tmp_path, _config())
        assert result.status == "skip"


@pytest.mark.parametrize("slug", ["owner/repo", "org/other"])
def test_the_remedy_reads_the_local_npmrc(slug):
    remedy = npm_token_remedy(slug)
    assert remedy.startswith(f"gh secret set {NPM_TOKEN} --repo {slug}")
    assert "~/.npmrc" in remedy

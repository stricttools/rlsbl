"""Tests for the ``ci-publish-secrets`` check.

The failure class: a repository whose CI publish job authenticates with a
repository secret does not have it, so the job dies with ``ENEEDAUTH`` -- after
the release has already tagged, pushed and created the GitHub Release. The
secret's presence is knowable long before that.

WHICH secrets are owed is each pipeline's own ``ci_secret_names`` declaration:
npm declares ``NPM_TOKEN``, and pypi declares none because it publishes
through OIDC trusted publishing. The declaration is pinned against the workflow templates that read the secret, so
the two cannot drift apart.

Every probe here is stubbed at the ``effects.gh`` seam: no test contacts
GitHub.
"""

import os
import re
from types import SimpleNamespace

import pytest
import strictcli

from rlsbl import app, effects
from rlsbl.pipelines import PIPELINE_TYPES
from rlsbl.ci_secrets import (
    NPM_TOKEN,
    evaluate_ci_secret_presence,
    probe_repo_secret,
    required_ci_secrets,
    secret_remedy,
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
        """A local publish authenticates from the developer's own ~/.npmrc."""
        pipelines = {"npm": {"type": "npm", "local": True, "target": "npm"}}
        assert required_ci_secrets({"pipelines": pipelines}) == {}

    def test_a_ci_npm_pipeline_declares_its_secret(self):
        assert required_ci_secrets({"pipelines": NPM_CI}) == {
            NPM_TOKEN: ["npm"],
        }

    def test_a_pypi_pipeline_declares_no_secret(self):
        """Trusted publishing (OIDC) needs no repository secret at all."""
        pipelines = {"pypi": {"type": "pypi", "local": False, "target": "pypi"}}
        assert required_ci_secrets({"pipelines": pipelines}) == {}

    def test_publish_mode_none_skips(self):
        verdict = evaluate_ci_secret_presence(
            _config(pipelines=NPM_CI, publish_mode="none"), "owner/repo",
        )
        assert verdict.skip_reason is not None

    def test_a_project_whose_pipelines_need_no_secret_skips(self):
        pipelines = {"pypi": {"type": "pypi", "local": False, "target": "pypi"}}
        verdict = evaluate_ci_secret_presence(
            _config(pipelines=pipelines), "owner/repo",
        )
        assert verdict.skip_reason is not None

    def test_no_repository_slug_skips(self):
        verdict = evaluate_ci_secret_presence(_config(pipelines=NPM_CI), None)
        assert verdict.skip_reason is not None
        assert "GitHub repository" in verdict.skip_reason


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_a_present_secret_passes(self):
        verdict = evaluate_ci_secret_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "present"},
        )
        assert verdict.ok

    def test_an_absent_secret_names_the_remedy(self):
        verdict = evaluate_ci_secret_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "absent"},
        )
        assert not verdict.ok
        assert secret_remedy("owner/repo", NPM_TOKEN) in verdict.problems[0]

    def test_an_unanswered_probe_is_an_error_not_a_pass(self):
        """Fail-closed: "we could not ask" is not evidence."""
        verdict = evaluate_ci_secret_presence(
            _config(pipelines=NPM_CI), "owner/repo",
            probe=lambda slug, name: {"status": "unknown", "message": "no auth"},
        )
        assert not verdict.ok
        assert "no auth" in verdict.problems[0]

    def test_a_missing_publish_mode_is_reported(self):
        verdict = evaluate_ci_secret_presence(
            {"pipelines": NPM_CI}, "owner/repo",
            probe=lambda slug, name: {"status": "present"},
        )
        assert not verdict.ok
        assert "publish_mode" in verdict.problems[0]


# ---------------------------------------------------------------------------
# The registered check
# ---------------------------------------------------------------------------


def _run(tmp_path, config):
    return app._check_defs["ci-publish-secrets"].impl(make_ctx(tmp_path, config))


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


# ---------------------------------------------------------------------------
# The declaration against the workflows that read it
# ---------------------------------------------------------------------------
#
# The declaration is only worth anything if it says what the workflow actually
# reads. These pin the two together in both directions, so a pipeline that
# grows a secret in its template without declaring it -- the failure this check
# was written for -- is a red test rather than a release that tags before dying with an auth error.


#: Actions supplies this to every job; no operator sets it as a repository
#: secret, so no pipeline declares it.
AUTOMATIC_TOKEN = "GITHUB_TOKEN"

_SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")

#: Every config shape that selects a different workflow template, per pipeline
#: type. A new pipeline type has to appear here (see the coverage test below),
#: which is what keeps the scan from silently skipping it.
TEMPLATE_VARIANTS = {
    "npm": [
        {},
        {"artifact": "launcher", "download": "first-run"},
        {"artifact": "launcher", "download": "postinstall"},
    ],
    "pypi": [{}, {"artifact": "launcher"}],
    "go": [{"artifact": "binary"}, {"artifact": "library"}],
    "cloudflare-pages": [{}],
}


def _workflow_secrets(pipeline_type, config):
    """Repository secrets the templates this pipeline emits actually read."""
    cls = PIPELINE_TYPES[pipeline_type]
    pipeline = cls(
        name=pipeline_type, pipeline_type=pipeline_type, local=False,
        config=config,
    )
    directory = pipeline.template_dir()
    if directory is None:
        return pipeline, set()
    found = set()
    for mapping in pipeline.template_mappings(None):
        template = mapping["template"]
        if not template.endswith((".yml.tpl", ".yaml.tpl")):
            continue
        with open(os.path.join(directory, template), encoding="utf-8") as f:
            found |= set(_SECRET_REF.findall(f.read()))
    return pipeline, found - {AUTOMATIC_TOKEN}


class TestDeclarationsMatchTheWorkflows:
    def test_every_pipeline_type_is_covered_by_a_variant(self):
        assert set(TEMPLATE_VARIANTS) == set(PIPELINE_TYPES)

    @pytest.mark.parametrize(
        "pipeline_type,config",
        [
            (name, config)
            for name, configs in sorted(TEMPLATE_VARIANTS.items())
            for config in configs
        ],
    )
    def test_the_declaration_is_exactly_what_the_workflow_reads(
        self, pipeline_type, config,
    ):
        pipeline, in_templates = _workflow_secrets(pipeline_type, config)
        assert set(pipeline.ci_secret_names()) == in_templates

    def test_every_npm_publish_workflow_reads_only_the_npm_token(self):
        """Covers the pnpm and yarn templates, which lockfile detection picks."""
        directory = PIPELINE_TYPES["npm"](
            name="npm", pipeline_type="npm", local=False, config={},
        ).template_dir()
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".yml.tpl"):
                continue
            with open(os.path.join(directory, entry), encoding="utf-8") as f:
                secrets = set(_SECRET_REF.findall(f.read())) - {AUTOMATIC_TOKEN}
            assert secrets <= {NPM_TOKEN}, entry

    def test_a_local_pipeline_declares_nothing_even_with_a_secret_workflow(self):
        """The credential is then the developer's own environment."""
        local = PIPELINE_TYPES["npm"](
            name="npm", pipeline_type="npm", local=True, config={},
        )
        assert local.ci_secret_names() == []


@pytest.mark.parametrize("slug", ["owner/repo", "org/other"])
def test_the_remedy_reads_the_local_npmrc(slug):
    remedy = secret_remedy(slug, NPM_TOKEN)
    assert remedy.startswith(f"gh secret set {NPM_TOKEN} --repo {slug}")
    assert "~/.npmrc" in remedy

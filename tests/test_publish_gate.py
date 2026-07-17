"""Tests for the publish CI gate.

Publish workflows trigger on ``release: published`` and ``workflow_dispatch``
and used to RACE CI on the same commit -- a broken artifact could publish
before CI reported. Every publish workflow now starts with a ``gate`` job
that polls the GitHub checks API for the release commit's CI check runs and
only lets the publish jobs run once every matching check concluded
``success``. All publish jobs depend on the gate (``needs: gate``).

Key properties verified here:

- The gate resolves the commit REF-BASED (``$GITHUB_SHA`` for the ref the
  run executes on) and never reads the release event payload, so a
  ``workflow_dispatch`` retry at the tag ref behaves identically to the
  original release-triggered run.
- Conclusion semantics: success proceeds; failure/timed_out/cancelled/
  skipped are hard errors with explicit explanations (never a silent
  wait-forever); no matching checks after a grace window is a hard error.
- Every publish template carries a per-ref concurrency group with
  ``cancel-in-progress: false`` (a retry queues behind an in-flight run
  for the same tag instead of racing it; a publish is never cancelled).
- The gate is defined ONCE in Python (rlsbl/publish_gate.py) and injected
  into templates via the ``{{publishGate}}`` variable -- no hand-copies.
- The merged multi-target publish workflow dedupes to exactly one gate.
- npm wrapper jobs (``{{npmPublishJobs}}``) depend on the gate.
- The monorepo publish router emits one shared gate that resolves the
  releasing project's prefixed CI check names from the tag ref, and all
  inlined member jobs are rewired to it (member gates are stripped).
"""

import glob
import os
import re

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import _generate_merged_publish, process_template
from rlsbl.commands.monorepo.publish_inline import (
    generate_inline_publish_router,
    prefix_jobs,
    transform_project_jobs,
)
from rlsbl.npm_wrapper import (
    PlatformArtifact,
    build_npm_publish_jobs,
)
from rlsbl.publish_gate import (
    GATE_JOB_KEY,
    GATE_POLL_SCRIPT,
    PUBLISH_CONCURRENCY_GROUP,
    build_gate_job,
    build_router_gate_job,
    ci_check_regex_for_targets,
    gate_job_template_snippet,
)

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)


def _publish_templates():
    """Return sorted paths of all shipped publish templates."""
    return sorted(glob.glob(os.path.join(TEMPLATES_ROOT, "*", "publish*.yml.tpl")))


# Vars sufficient to render any publish template to valid YAML.
RENDER_VARS = {
    "publishGate": gate_job_template_snippet("^(test)( \\(.*\\))?$"),
    "registryUrl": "https://registry.npmjs.org",
    "modulePath": "github.com/owner/repo",
    "zig.projectName": "myproj",
    "zig.minRequiredZig": "0.13.0",
    # Block-insertion vars: empty so the base template stands alone.
    "npmPublishJobs": "",
    "cratesPublishJobs": "",
    "homebrewEnv": "",
}


def _yaml_load(text):
    return YAML(typ="safe").load(text)


def _render(tpl_path, extra_vars=None):
    """Render a publish template to a parsed YAML dict."""
    with open(tpl_path, encoding="utf-8") as f:
        raw = f.read()
    vars_dict = dict(RENDER_VARS)
    if extra_vars:
        vars_dict.update(extra_vars)
    content, unreplaced = process_template(raw, vars_dict, template_path=tpl_path)
    assert not unreplaced, f"{tpl_path}: unresolved vars {unreplaced}"
    return content, _yaml_load(content)


class TestPublishTemplatesEnumerated:
    def test_all_publish_templates_enumerated(self):
        """Sanity guard: the glob finds the known publish templates."""
        rels = {
            os.path.relpath(p, TEMPLATES_ROOT).replace(os.sep, "/")
            for p in _publish_templates()
        }
        expected = {
            "cargo/publish.yml.tpl",
            "deno/publish.yml.tpl",
            "docker/publish.yml.tpl",
            "go/publish.yml.tpl",
            "hex/publish.yml.tpl",
            "maven/publish.yml.tpl",
            "maven/publish-central.yml.tpl",
            "npm/publish.yml.tpl",
            "npm/publish-pnpm.yml.tpl",
            "npm/publish-yarn.yml.tpl",
            "pypi/publish.yml.tpl",
            "zig/publish.yml.tpl",
        }
        assert expected <= rels


@pytest.mark.parametrize(
    "tpl_path",
    _publish_templates(),
    ids=lambda p: os.path.relpath(p, TEMPLATES_ROOT).replace(os.sep, "/"),
)
class TestPublishTemplateGating:
    def test_has_publish_gate_placeholder_first_under_jobs(self, tpl_path):
        """{{publishGate}} is the first entry under jobs:."""
        with open(tpl_path, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"^jobs:\n(\{\{publishGate\}\})$", content, re.MULTILINE)
        assert m, "jobs: must be immediately followed by {{publishGate}}"

    def test_has_per_ref_concurrency(self, tpl_path):
        """Per-ref concurrency group with cancel-in-progress: false."""
        with open(tpl_path, encoding="utf-8") as f:
            content = f.read()
        assert "\nconcurrency:\n" in content
        assert f"group: {PUBLISH_CONCURRENCY_GROUP}" in content
        assert "cancel-in-progress: false" in content
        on_idx = content.index("\non:")
        conc_idx = content.index("\nconcurrency:")
        jobs_idx = content.index("\njobs:")
        assert on_idx < conc_idx < jobs_idx

    def test_release_payload_only_in_tag_fallback(self, tpl_path):
        """Templates only read github.event.release.tag_name as a fallback
        for the inputs.tag workflow_dispatch input -- never for other data."""
        with open(tpl_path, encoding="utf-8") as f:
            content = f.read()
        # The only allowed usage is: inputs.tag || github.event.release.tag_name
        # Strip that pattern, then verify no other release payload reads remain
        stripped = content.replace(
            "${{ inputs.tag || github.event.release.tag_name }}", ""
        )
        assert "github.event.release" not in stripped, (
            "templates must not read github.event.release except as "
            "fallback in: inputs.tag || github.event.release.tag_name"
        )

    def test_rendered_all_jobs_need_gate(self, tpl_path):
        """Rendered template: gate exists, every other job needs it."""
        content, doc = _render(tpl_path)
        jobs = doc["jobs"]
        assert GATE_JOB_KEY in jobs
        for key, job in jobs.items():
            if key == GATE_JOB_KEY:
                continue
            needs = job.get("needs")
            needs_list = [needs] if isinstance(needs, str) else (needs or [])
            assert GATE_JOB_KEY in needs_list, f"job {key} must need gate"

    def test_rendered_gate_has_checks_read_permission(self, tpl_path):
        """The gate carries job-level checks: read (workflow permission
        blocks are restrictive; job-level grants exactly what gate needs)."""
        _, doc = _render(tpl_path)
        gate = doc["jobs"][GATE_JOB_KEY]
        assert gate["permissions"]["checks"] == "read"

    def test_rendered_gate_is_payload_free(self, tpl_path):
        """The gate never touches github.event -- ref-based only."""
        _, doc = _render(tpl_path)
        gate = doc["jobs"][GATE_JOB_KEY]
        assert "github.event" not in repr(gate)


class TestGateJob:
    def test_gate_polls_check_runs_ref_based(self):
        assert "check-runs" in GATE_POLL_SCRIPT
        assert "GITHUB_SHA" in GATE_POLL_SCRIPT
        assert "github.event" not in GATE_POLL_SCRIPT

    def test_gate_script_excludes_own_workflow_run(self):
        """The publish run's own check runs (gate, publish jobs) must not
        deadlock the poll loop."""
        assert "GITHUB_RUN_ID" in GATE_POLL_SCRIPT

    def test_gate_script_explains_all_blocking_conclusions(self):
        for word in ("cancelled", "skipped", "failure", "timed_out"):
            assert word in GATE_POLL_SCRIPT, f"missing {word} handling"
        # Cancelled/skipped are hard errors with explanations, not waits.
        assert "CANCELLED" in GATE_POLL_SCRIPT
        assert "SKIPPED" in GATE_POLL_SCRIPT

    def test_gate_script_has_grace_window_hard_error(self):
        assert "GATE_GRACE_MINUTES" in GATE_POLL_SCRIPT
        assert "no CI check runs" in GATE_POLL_SCRIPT

    def test_build_gate_job_env_and_permissions(self):
        job = build_gate_job(check_regex="^(test)$")
        assert job["permissions"] == {"checks": "read"}
        assert job["env"]["CI_CHECK_REGEX"] == "^(test)$"
        assert job["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        assert job["env"]["GATE_TIMEOUT_MINUTES"] == "20"

    def test_ci_check_regex_single_target(self):
        assert ci_check_regex_for_targets(["pypi"]) == r"^(test)( \(.*\))?$"

    def test_ci_check_regex_union(self):
        assert (
            ci_check_regex_for_targets(["docker", "pypi"])
            == r"^(build|test)( \(.*\))?$"
        )

    def test_ci_check_regex_unknown_target_defaults_to_test(self):
        assert ci_check_regex_for_targets(["nosuch"]) == r"^(test)( \(.*\))?$"

    def test_snippet_is_indented_job_block(self):
        snippet = gate_job_template_snippet("^(test)$")
        assert snippet.startswith("  gate:")
        # Parses when placed under a jobs: key.
        doc = _yaml_load("jobs:\n" + snippet + "\n")
        assert GATE_JOB_KEY in doc["jobs"]


class TestNpmWrapperJobsGated:
    def test_npm_publish_jobs_need_gate(self):
        artifacts = [
            PlatformArtifact(
                npm_platform="linux-x64",
                os_constraint="linux",
                cpu_constraint="x64",
                asset_pattern="{name}_{version}_linux_amd64.tar.gz",
                extract_cmd="tar -xzf",
                binary_name="mytool",
            )
        ]
        out = build_npm_publish_jobs("mytool", artifacts)
        assert "needs: [gate, goreleaser]" in out

    def test_npm_publish_jobs_custom_depends_on_still_gated(self):
        artifacts = []
        out = build_npm_publish_jobs(
            "mytool", artifacts, depends_on="build-and-upload"
        )
        assert "needs: [gate, build-and-upload]" in out


class TestMergedPublishGate:
    """The merged multi-target publish workflow has exactly one gate."""

    def test_exactly_one_gate_all_jobs_behind_it(self):
        result = _generate_merged_publish(
            ["npm", "pypi"],
            template_vars={"registryUrl": "https://registry.npmjs.org"},
        )
        doc = _yaml_load(result)
        jobs = doc["jobs"]
        gate_keys = [k for k in jobs if k == GATE_JOB_KEY or k.endswith("-gate")]
        assert gate_keys == [GATE_JOB_KEY]
        for key, job in jobs.items():
            if key == GATE_JOB_KEY:
                continue
            needs = job.get("needs")
            needs_list = [needs] if isinstance(needs, str) else (needs or [])
            assert GATE_JOB_KEY in needs_list, f"job {key} must need gate"

    def test_gate_regex_is_union_of_target_ci_jobs(self):
        result = _generate_merged_publish(
            ["docker", "pypi"],
            template_vars={},
        )
        doc = _yaml_load(result)
        gate = doc["jobs"][GATE_JOB_KEY]
        assert gate["env"]["CI_CHECK_REGEX"] == r"^(build|test)( \(.*\))?$"

    def test_merged_has_per_ref_concurrency(self):
        result = _generate_merged_publish(
            ["npm", "pypi"],
            template_vars={"registryUrl": "https://registry.npmjs.org"},
        )
        doc = _yaml_load(result)
        assert doc["concurrency"]["group"] == PUBLISH_CONCURRENCY_GROUP
        assert doc["concurrency"]["cancel-in-progress"] is False

    def test_multi_job_target_needs_rewired_through_rename(self):
        """go's npm-publish job (needs: [gate, goreleaser]) is renamed to
        go-npm-publish and its needs follow the goreleaser->go rename."""
        artifacts = [
            PlatformArtifact(
                npm_platform="linux-x64",
                os_constraint="linux",
                cpu_constraint="x64",
                asset_pattern="{name}_{version}_linux_amd64.tar.gz",
                extract_cmd="tar -xzf",
                binary_name="mytool",
            )
        ]
        npm_jobs = build_npm_publish_jobs("mytool", artifacts)
        result = _generate_merged_publish(
            ["go", "pypi"],
            template_vars={"npmPublishJobs": npm_jobs, "homebrewEnv": ""},
        )
        doc = _yaml_load(result)
        jobs = doc["jobs"]
        assert "go" in jobs  # goreleaser renamed to go
        assert "go-npm-publish" in jobs
        needs = jobs["go-npm-publish"]["needs"]
        assert GATE_JOB_KEY in needs
        assert "go" in needs
        assert "goreleaser" not in needs


MEMBER_PUBLISH_WITH_GATE = """\
name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      checks: read
    steps:
      - run: echo poll
  publish:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
"""

MEMBER_PUBLISH_WITHOUT_GATE = """\
name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
"""


def _write_member_publish(root, path, content):
    wf_dir = os.path.join(root, path, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    wf_path = os.path.join(wf_dir, "publish.yml")
    with open(wf_path, "w", encoding="utf-8") as f:
        f.write(content)
    return wf_path


class TestPrefixJobsGatePreserved:
    def test_gate_needs_not_prefixed(self):
        jobs = {
            "publish": {"needs": ["gate", "build"], "steps": []},
            "build": {"steps": []},
        }
        out = prefix_jobs("proj", jobs)
        assert out["proj-publish"]["needs"] == ["gate", "proj-build"]

    def test_gate_needs_string_not_prefixed(self):
        jobs = {"publish": {"needs": "gate", "steps": []}}
        out = prefix_jobs("proj", jobs)
        assert out["proj-publish"]["needs"] == "gate"


class TestTransformProjectJobs:
    def test_member_gate_stripped_and_needs_rewired(self, tmp_path):
        wf = _write_member_publish(str(tmp_path), "pkga", MEMBER_PUBLISH_WITH_GATE)
        jobs = transform_project_jobs("pkga", "pkga", "pkga-v", wf)
        assert "pkga-gate" not in jobs
        assert GATE_JOB_KEY not in jobs
        needs = jobs["pkga-publish"]["needs"]
        needs_list = [needs] if isinstance(needs, str) else needs
        assert GATE_JOB_KEY in needs_list

    def test_member_without_gate_still_wired_to_shared_gate(self, tmp_path):
        """Older member workflows without a gate job get needs: gate added."""
        wf = _write_member_publish(str(tmp_path), "pkgb", MEMBER_PUBLISH_WITHOUT_GATE)
        jobs = transform_project_jobs("pkgb", "pkgb", "pkgb-v", wf)
        needs = jobs["pkgb-publish"]["needs"]
        needs_list = [needs] if isinstance(needs, str) else needs
        assert GATE_JOB_KEY in needs_list

    def test_condition_is_ref_based(self, tmp_path):
        """Member job conditions match github.ref_name, not the release
        payload -- so a dispatch retry at the tag ref hits the same jobs."""
        wf = _write_member_publish(str(tmp_path), "pkga", MEMBER_PUBLISH_WITH_GATE)
        jobs = transform_project_jobs("pkga", "pkga", "pkga-v", wf)
        cond = jobs["pkga-publish"]["if"]
        assert cond == "startsWith(inputs.tag || github.ref_name, 'pkga-v')"


class TestRouterGate:
    def _projects(self, tmp_path):
        _write_member_publish(str(tmp_path), "pkga", MEMBER_PUBLISH_WITH_GATE)
        _write_member_publish(str(tmp_path), "pkgb", MEMBER_PUBLISH_WITHOUT_GATE)
        return [
            {"name": "pkga", "path": "pkga", "_ci_files": ["pkga-ci.yml"]},
            {
                "name": "pkgb",
                "path": "pkgb",
                "_ci_files": ["pkgb-ci-pypi.yml", "pkgb-ci-go.yml"],
            },
        ]

    def test_router_has_one_shared_gate(self, tmp_path):
        projects = self._projects(tmp_path)
        result = generate_inline_publish_router(projects, str(tmp_path))
        doc = _yaml_load(result)
        jobs = doc["jobs"]
        gate_keys = [k for k in jobs if k.endswith("gate")]
        assert gate_keys == [GATE_JOB_KEY]
        for key, job in jobs.items():
            if key in (GATE_JOB_KEY, "no-op"):
                continue
            needs = job.get("needs")
            needs_list = [needs] if isinstance(needs, str) else (needs or [])
            assert GATE_JOB_KEY in needs_list, f"job {key} must need gate"

    def test_router_gate_resolves_prefix_to_ci_checks(self, tmp_path):
        """The shared gate maps the tag ref to the releasing project's
        prefixed CI check names (router job keys, ' / ' separator)."""
        projects = self._projects(tmp_path)
        result = generate_inline_publish_router(projects, str(tmp_path))
        doc = _yaml_load(result)
        gate = doc["jobs"][GATE_JOB_KEY]
        resolver = gate["steps"][0]["run"]
        assert '"pkga@v"*)' in resolver
        assert '"pkgb@v"*)' in resolver
        # CI check-run names match the CI workflow filenames (minus .yml),
        # not the project name. re.escape escapes hyphens; the jq regex
        # engine treats \- as -.
        assert r"^(pkga\-ci) / " in resolver
        assert r"^(pkgb\-ci\-pypi|pkgb\-ci\-go) / " in resolver
        assert "GITHUB_ENV" in resolver
        # Unmatched refs (e.g. a bare dispatch from main) are a hard error.
        assert "exit 1" in resolver

    def test_router_conditions_ref_based_no_payload(self, tmp_path):
        projects = self._projects(tmp_path)
        result = generate_inline_publish_router(projects, str(tmp_path))
        assert "github.event.release.tag_name" not in result
        assert "startsWith(inputs.tag || github.ref_name, 'pkga@v')" in result

    def test_router_has_per_ref_concurrency(self, tmp_path):
        projects = self._projects(tmp_path)
        result = generate_inline_publish_router(projects, str(tmp_path))
        doc = _yaml_load(result)
        assert doc["concurrency"]["group"] == PUBLISH_CONCURRENCY_GROUP
        assert doc["concurrency"]["cancel-in-progress"] is False

    def test_router_documents_retry_contract(self, tmp_path):
        projects = self._projects(tmp_path)
        result = generate_inline_publish_router(projects, str(tmp_path))
        assert "--ref" in result  # retry-dispatch-at-tag-ref contract comment

    def test_longer_prefixes_matched_first(self, tmp_path):
        """Overlapping prefixes (pkga vs pkga-extra) must match longest-first
        in the resolver case statement."""
        _write_member_publish(str(tmp_path), "pkga", MEMBER_PUBLISH_WITH_GATE)
        _write_member_publish(
            str(tmp_path), "pkga-extra", MEMBER_PUBLISH_WITH_GATE
        )
        projects = [
            {"name": "pkga", "path": "pkga", "_ci_files": ["pkga-ci.yml"]},
            {
                "name": "pkga-extra",
                "path": "pkga-extra",
                "_ci_files": ["pkga-extra-ci.yml"],
            },
        ]
        result = generate_inline_publish_router(projects, str(tmp_path))
        doc = _yaml_load(result)
        resolver = doc["jobs"][GATE_JOB_KEY]["steps"][0]["run"]
        assert resolver.index('"pkga-extra@v"*)') < resolver.index('"pkga@v"*)')


class TestRouterGateRegexFromBuilder:
    def test_build_router_gate_job_case_arms(self):
        job = build_router_gate_job(
            [("pkga-v", "^(pkga) / "), ("pkgb-v", "^(pkgb) / ")]
        )
        assert job["permissions"] == {"checks": "read"}
        resolver = job["steps"][0]["run"]
        assert '"pkga-v"*)' in resolver
        poll = job["steps"][1]["run"]
        assert poll == GATE_POLL_SCRIPT

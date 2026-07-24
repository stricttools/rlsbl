"""Tests for the ``requires-services`` check (Phase 7.3).

The check hard-errors when config declares CI services / test_env that the
rendered workflow files on disk do not provision, and skips visibly when
nothing is declared.
"""


from rlsbl import app

from conftest import make_ctx


PROVISIONED_CI_GO = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
    env:
      PGDESIGN_DB: postgres://test@localhost/postgres
      PGDESIGN_REQUIRE_DB: "1"
    steps:
      - uses: actions/checkout@v6
      - run: go test ./...
"""

PLAIN_CI_GO = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: go test ./...
"""


def _config():
    return {
        "publish_mode": "ci",
        "targets": ["go"],
        "services": {
            "postgres": {"targets": ["go"], "image": "postgres:17"},
        },
        "test_env": {
            "PGDESIGN_DB": "postgres://test@localhost/postgres",
            "PGDESIGN_REQUIRE_DB": "1",
        },
    }


def _write_ci(project_root, name, content):
    wf = project_root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(content)


def _run(project_root, config):
    ctx = make_ctx(project_root, config=config)
    return app._check_defs["requires-services"].impl(ctx)


class TestRequiresServices:
    def test_skips_when_nothing_declared(self, tmp_path):
        result = _run(tmp_path, {"publish_mode": "ci", "targets": ["go"]})
        assert result.status == "skip"

    def test_pass_when_provisioned(self, tmp_path):
        _write_ci(tmp_path, "ci-go.yml", PROVISIONED_CI_GO)
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_fail_when_service_missing(self, tmp_path):
        _write_ci(tmp_path, "ci-go.yml", PLAIN_CI_GO)
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert "postgres" in text
        assert "scaffold" in text

    def test_fail_when_no_workflows_dir(self, tmp_path):
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert "workflows" in text

    def test_fail_when_test_env_key_missing(self, tmp_path):
        # Service provisioned but a test_env key is absent from the workflow.
        cfg = _config()
        cfg["test_env"]["PGDESIGN_EXTRA"] = "x"
        _write_ci(tmp_path, "ci-go.yml", PROVISIONED_CI_GO)
        result = _run(tmp_path, cfg)
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert "PGDESIGN_EXTRA" in text

    def test_fail_when_workflow_for_target_absent(self, tmp_path):
        # Config declares service for target go, but only a pypi CI exists.
        _write_ci(tmp_path, "ci-pypi.yml", PLAIN_CI_GO)
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert "no CI workflow" in text or "provisions no such service" in text

    def test_pass_single_target_ci_yml(self, tmp_path):
        # Single-target projects render ci.yml (no target suffix).
        _write_ci(tmp_path, "ci.yml", PROVISIONED_CI_GO)
        result = _run(tmp_path, _config())
        assert result.status == "pass"

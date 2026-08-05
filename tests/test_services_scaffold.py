"""Tests for CI service-container scaffold rendering (Phase 7.2).

Covers :func:`rlsbl.ci_yaml.make_ci_workflow_transform`:
- golden render: a postgres service + test_env expressed as config renders a
  workflow functionally equivalent to a hand-maintained services block;
- idempotent re-scaffold;
- three-way merge cleanliness (unrelated hand edits survive, no conflict
  markers) via ``git merge-file`` against the pre-feature base;
- per-target scoping (a service scoped to ``go`` never lands in ``ci-pypi.yml``).
"""

import subprocess


from rlsbl.ci_yaml import make_ci_workflow_transform


# Plain Go CI workflow as rendered by the go template (pre-services).
BASE_GO_CI = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow_ref }}-${{ github.sha }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-go@v6
        with:
          go-version-file: go.mod
      - run: go vet ./...
      - run: go test ./... -race -short -timeout=10m
"""


def _pgdesign_config():
    return {
        "targets": ["go", {"name": "pypi", "path": "pypi/"}],
        "services": {
            "postgres": {
                "targets": ["go"],
                "image": "postgres:17",
                "env": {
                    "POSTGRES_USER": "test",
                    "POSTGRES_PASSWORD": "test",
                    "POSTGRES_DB": "postgres",
                },
                "ports": ["5432:5432"],
                "health": {
                    "cmd": "pg_isready -U test",
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                },
                "setup": {
                    "commands": [
                        "apt-get update && apt-get install -y --no-install-recommends "
                        "postgresql-17-partman && " + "r" + "m -rf /var/lib/apt/lists/*"
                    ],
                    "verify_sql": (
                        "SELECT name, default_version FROM pg_available_extensions "
                        "WHERE name = 'pg_partman';"
                    ),
                },
            }
        },
        "test_env": {
            "PGDESIGN_DB": "postgres://test:test@localhost:5432/postgres?sslmode=disable",
            "PGDESIGN_REQUIRE_DB": "1",
        },
    }


def _inject(content, target="ci-go.yml", config=None, single_target=None):
    """Render *content* through the scaffold's CI-workflow transform."""
    transform = make_ci_workflow_transform(
        config or _pgdesign_config(), single_target=single_target
    )
    if transform is None:
        return content
    return transform(f".github/workflows/{target}", content)


class TestGoldenRender:
    def test_functionally_equivalent_block(self):
        out = _inject(BASE_GO_CI)
        # service definition
        assert "services:" in out
        assert "postgres:" in out
        assert "image: postgres:17" in out
        assert "POSTGRES_USER: test" in out
        assert "- 5432:5432" in out
        # health options must be ONE space-joined line (docker create flags),
        # never newline-separated -- that is the folded-block bug guard.
        opts_line = next(
            l for l in out.splitlines() if l.strip().startswith("options:")
        )
        assert '--health-cmd "pg_isready -U test"' in opts_line
        assert "--health-interval 10s" in opts_line
        assert "--health-timeout 5s" in opts_line
        assert "--health-retries 5" in opts_line
        # workflow-level test env
        assert (
            "PGDESIGN_DB: postgres://test:test@localhost:5432/postgres?sslmode=disable"
            in out
        )
        assert "PGDESIGN_REQUIRE_DB:" in out
        # setup step: docker exec install + psql verify
        assert "docker exec ${{ job.services.postgres.id }} bash -c" in out
        assert "postgresql-17-partman" in out
        assert (
            'PGPASSWORD=test psql -h localhost -U test -d postgres -c '
            '"SELECT name, default_version FROM pg_available_extensions '
            "WHERE name = 'pg_partman';\"" in out
        )
        # original steps preserved
        assert "go test ./... -race -short -timeout=10m" in out
        # services/env appear before steps in the test job
        assert out.index("services:") < out.index("steps:")
        assert out.index("PGDESIGN_DB") < out.index("steps:")

    def test_idempotent_rescaffold(self):
        once = _inject(BASE_GO_CI)
        twice = _inject(once)
        assert once == twice


class TestScoping:
    def test_service_not_injected_into_non_target_workflow(self):
        # postgres is scoped to target "go"; a pypi CI workflow must stay clean.
        out = _inject(BASE_GO_CI, target="ci-pypi.yml")
        assert out == BASE_GO_CI
        assert "postgres" not in out
        assert "PGDESIGN_DB" not in out

    def test_custom_workflow_never_touched(self):
        out = _inject(BASE_GO_CI, target="ci-custom.yml")
        assert out == BASE_GO_CI

    def test_single_target_ci_yml_injected(self):
        out = _inject(BASE_GO_CI, target="ci.yml", single_target="go")
        assert "services:" in out
        assert "postgres:" in out


class TestPlanIntegration:
    """The transform runs inside ``plan_mappings``, so the plan's status,
    content and stored merge base all come out of one pipeline."""

    @staticmethod
    def _plan(tmp_path, on_disk=None, base=None, config=None):
        """Plan a ci-go.yml mapping from a synthetic template dir."""
        from rlsbl.commands.init_cmd import plan_mappings, _save_base

        tpl_dir = tmp_path / "tpl"
        tpl_dir.mkdir(exist_ok=True)
        (tpl_dir / "ci.yml.tpl").write_text(BASE_GO_CI)
        target = ".github/workflows/ci-go.yml"
        if on_disk is not None:
            wf = tmp_path / ".github" / "workflows"
            wf.mkdir(parents=True, exist_ok=True)
            (wf / "ci-go.yml").write_text(on_disk)
        if base is not None:
            _save_base(target, base)
        return plan_mappings(
            str(tpl_dir), [{"template": "ci.yml.tpl", "target": target}], {},
            transform=make_ci_workflow_transform(
                _pgdesign_config() if config is None else config
            ),
        )[0]

    def test_no_services_is_a_strict_no_op(self, tmp_path, monkeypatch):
        """No services/test_env and no subdirectory: there is no transform."""
        assert make_ci_workflow_transform({}) is None

    def test_first_scaffold_writes_the_services(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = self._plan(tmp_path, on_disk=BASE_GO_CI, base=BASE_GO_CI)
        assert plan["action"] == "write"
        assert "services:" in plan["content"]
        # The stored base carries the services too, so the NEXT run compares
        # like against like.
        assert "services:" in plan["base_content"]

    def test_rescaffold_identical_reports_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        injected = _inject(BASE_GO_CI)
        plan = self._plan(tmp_path, on_disk=injected, base=injected)
        assert plan["action"] == "none"
        assert plan["status"] == "unchanged"

    def test_scoped_out_workflow_untouched(self, tmp_path, monkeypatch):
        """A workflow the services are not scoped to renders as the plain template."""
        monkeypatch.chdir(tmp_path)
        transform = make_ci_workflow_transform(_pgdesign_config())
        out = transform(".github/workflows/ci-pypi.yml", BASE_GO_CI)
        assert out == BASE_GO_CI


class TestMergeCleanliness:
    """A re-scaffold three-way merge preserves hand edits without conflicts."""

    def _git_merge_file(self, tmp_path, base, ours, theirs):
        (tmp_path / "base").write_text(base)
        (tmp_path / "ours").write_text(ours)
        (tmp_path / "theirs").write_text(theirs)
        # git merge-file <current> <base> <other>: merges base->other into
        # current (in place). Exit 0 = clean, >0 = conflict count.
        proc = subprocess.run(
            ["git", "merge-file", "-p",
             str(tmp_path / "ours"), str(tmp_path / "base"), str(tmp_path / "theirs")],
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout

    def test_unrelated_hand_edit_survives_clean(self, tmp_path):
        # base: pre-feature plain template.
        base = BASE_GO_CI
        # ours: same file with an unrelated hand edit on the go-test line.
        ours = base.replace(
            "go test ./... -race -short -timeout=10m",
            "go test ./... -race -short -timeout=10m -count=1",
        )
        # theirs: re-scaffold output (services injected into the plain base).
        theirs = _inject(base)
        rc, merged = self._git_merge_file(tmp_path, base, ours, theirs)
        assert rc == 0, f"expected clean merge, got conflicts:\n{merged}"
        assert "<<<<<<<" not in merged
        # both changes present: hand edit + injected services.
        assert "-count=1" in merged
        assert "services:" in merged
        assert "postgres:" in merged

    def test_rescaffold_over_generated_is_noop_merge(self, tmp_path):
        # base: pre-feature plain template; ours: already-scaffolded (services);
        # theirs: re-scaffold (services again). No conflict, services intact.
        base = BASE_GO_CI
        ours = _inject(base)
        theirs = _inject(base)
        rc, merged = self._git_merge_file(tmp_path, base, ours, theirs)
        assert rc == 0
        assert "<<<<<<<" not in merged
        assert "postgres:" in merged

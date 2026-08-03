"""Tests for the ``test_sandbox`` config family: validation, scaffold emission,
and the ``stricttest-floor`` adoption check.

rlsbl distributes the outer layer of the stricttest floor -- the bubblewrap
runner script -- and enforces that an adopted repo actually has a working one.
"""

import json
import stat
from pathlib import Path

import pytest

from conftest import capture_all_checks, make_ctx

from rlsbl.commands.init_cmd import apply_plans, plan_mappings, process_template
from rlsbl.context import ProjectContext
from rlsbl.errors import ConfigError
from rlsbl.targets.base import BaseTarget
from rlsbl.test_sandbox import (
    CONFIG_KEY,
    SANDBOX_ENV_VAR,
    TEMPLATE_NAME,
    evaluate_floor,
    runner_mapping,
    template_vars,
    validate_test_sandbox_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "rlsbl" / "templates" / "shared" / TEMPLATE_NAME


def _section(**overrides):
    section = {
        "runner_path": "scripts/test.sh",
        "command": "uv sync --offline && uv run --offline pytest",
        "default_args": "-q -n auto",
        "caches": ["uv", "go"],
        "prewarm": ["scripts/test-prewarm.sh"],
        "extra_env": {"LEGACY_SANDBOX": "1"},
        "ci_workflows": [".github/workflows/ci.yml"],
    }
    section.update(overrides)
    for key, value in list(section.items()):
        if value is None:
            del section[key]
    return {"publish_mode": "ci", CONFIG_KEY: section}


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_absent_section_is_valid(self):
        validate_test_sandbox_config({"publish_mode": "ci"})
        validate_test_sandbox_config({})
        validate_test_sandbox_config(None)

    def test_full_section_is_valid(self):
        validate_test_sandbox_config(_section())

    def test_minimal_section_is_valid(self):
        validate_test_sandbox_config(
            {CONFIG_KEY: {"runner_path": "test.sh", "command": "go test ./..."}}
        )

    def test_non_dict_section(self):
        with pytest.raises(ConfigError, match="must be a map"):
            validate_test_sandbox_config({CONFIG_KEY: ["scripts/test.sh"]})

    def test_unknown_key(self):
        with pytest.raises(ConfigError, match="unknown key"):
            validate_test_sandbox_config(_section(sandbox_env="X"))

    @pytest.mark.parametrize("missing", ["runner_path", "command"])
    def test_missing_required_key(self, missing):
        config = _section()
        del config[CONFIG_KEY][missing]
        with pytest.raises(ConfigError, match=f"missing required key.*{missing}"):
            validate_test_sandbox_config(config)

    @pytest.mark.parametrize(
        "bad_path", ["/abs/test.sh", "~/test.sh", "../outside/test.sh", ""]
    )
    def test_bad_runner_path(self, bad_path):
        with pytest.raises(ConfigError, match="runner_path"):
            validate_test_sandbox_config(_section(runner_path=bad_path))

    def test_empty_command(self):
        with pytest.raises(ConfigError, match="command"):
            validate_test_sandbox_config(_section(command="   "))

    def test_single_quote_in_command_rejected(self):
        with pytest.raises(ConfigError, match="single quote"):
            validate_test_sandbox_config(_section(command="pytest -k 'foo'"))

    def test_single_quote_in_default_args_rejected(self):
        with pytest.raises(ConfigError, match="single quote"):
            validate_test_sandbox_config(_section(default_args="-k 'x'"))

    def test_unknown_cache_rejected(self):
        with pytest.raises(ConfigError, match="caches accepts only"):
            validate_test_sandbox_config(_section(caches=["uv", "cargo"]))

    def test_caches_must_be_list(self):
        with pytest.raises(ConfigError, match="caches must be a list"):
            validate_test_sandbox_config(_section(caches="uv"))

    def test_prewarm_entries_must_be_strings(self):
        with pytest.raises(ConfigError, match="prewarm"):
            validate_test_sandbox_config(_section(prewarm=[""]))

    def test_ci_workflow_paths_must_be_relative(self):
        with pytest.raises(ConfigError, match="ci_workflows"):
            validate_test_sandbox_config(
                _section(ci_workflows=["/etc/ci.yml"])
            )

    def test_extra_env_must_be_map(self):
        with pytest.raises(ConfigError, match="extra_env must be a map"):
            validate_test_sandbox_config(_section(extra_env=["A=1"]))

    def test_extra_env_bad_name(self):
        with pytest.raises(ConfigError, match="not a valid"):
            validate_test_sandbox_config(_section(extra_env={"9BAD": "1"}))

    def test_extra_env_unsafe_value(self):
        with pytest.raises(ConfigError, match="cannot pass through"):
            validate_test_sandbox_config(_section(extra_env={"X": "a b"}))

    def test_extra_env_cannot_redeclare_sandbox_var(self):
        with pytest.raises(ConfigError, match=SANDBOX_ENV_VAR):
            validate_test_sandbox_config(_section(extra_env={SANDBOX_ENV_VAR: "1"}))

    def test_config_schema_check_surfaces_the_error(self, tmp_path):
        """A malformed family is reported by the config-schema check."""
        (tmp_path / ".rlsbl").mkdir()
        config = _section(caches=["nope"])
        (tmp_path / ".rlsbl" / "config.json").write_text(json.dumps(config))
        checks = capture_all_checks()
        result = checks["config-schema"](make_ctx(tmp_path, config))
        assert result.status == "fail"
        assert any("caches accepts only" in m for m in _texts(result))


# ---------------------------------------------------------------------------
# Template rendering + scaffold emission
# ---------------------------------------------------------------------------


class TestTemplateVars:
    def test_absent_family_yields_no_vars(self):
        assert template_vars({"publish_mode": "ci"}) == {}

    def test_vars_derived_from_config(self):
        v = template_vars(_section())
        assert v["sandboxRunnerPath"] == "scripts/test.sh"
        assert v["sandboxRootRelative"] == ".."
        assert v["sandboxCommand"] == "uv sync --offline && uv run --offline pytest"
        assert v["sandboxDefaultArgs"] == "-q -n auto"
        assert v["sandboxCaches"] == "uv go"
        assert v["sandboxPrewarm"] == "scripts/test-prewarm.sh"
        assert v["sandboxExtraEnv"] == "  --setenv LEGACY_SANDBOX 1"

    def test_root_relative_depth(self):
        assert template_vars(_section(runner_path="test.sh"))[
            "sandboxRootRelative"
        ] == "."
        assert template_vars(_section(runner_path="a/b/test.sh"))[
            "sandboxRootRelative"
        ] == "../.."

    def test_optional_keys_render_empty(self):
        v = template_vars(
            {CONFIG_KEY: {"runner_path": "test.sh", "command": "go test ./..."}}
        )
        assert v["sandboxCaches"] == ""
        assert v["sandboxPrewarm"] == ""
        assert v["sandboxExtraEnv"] == ""
        assert v["sandboxDefaultArgs"] == ""


class TestRunnerMapping:
    def test_absent_family_emits_nothing(self):
        ctx = ProjectContext(
            project_root=Path("."), workspace_root=None, config={"publish_mode": "ci"}
        )
        targets = {m["target"] for m in BaseTarget().shared_template_mappings(ctx)}
        assert "scripts/test.sh" not in targets

    def test_declared_family_emits_executable_runner(self):
        ctx = ProjectContext(
            project_root=Path("."), workspace_root=None, config=_section()
        )
        mappings = BaseTarget().shared_template_mappings(ctx)
        runner = [m for m in mappings if m["target"] == "scripts/test.sh"]
        assert runner == [
            {
                "template": TEMPLATE_NAME,
                "target": "scripts/test.sh",
                "executable": True,
            }
        ]

    def test_mapping_is_none_without_family(self):
        assert runner_mapping({"publish_mode": "ci"}) is None


class TestScaffoldEmission:
    """Scaffolding a repo whose config declares the family emits the runner."""

    def _scaffold(self, tmp_path, config, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mappings = [m for m in BaseTarget().shared_template_mappings(
            ProjectContext(project_root=tmp_path, workspace_root=None, config=config)
        ) if m["template"] == TEMPLATE_NAME]
        plans = plan_mappings(
            str(TEMPLATE_PATH.parent), mappings, template_vars(config)
        )
        return apply_plans(plans)

    def test_runner_is_emitted_executable_and_substituted(self, tmp_path, monkeypatch):
        config = _section()
        created, _skipped, warnings, _hashes = self._scaffold(
            tmp_path, config, monkeypatch
        )
        assert warnings == []
        assert [t for t, _ in created] == ["scripts/test.sh"]

        runner = tmp_path / "scripts" / "test.sh"
        assert runner.is_file()
        mode = runner.stat().st_mode
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP and mode & stat.S_IXOTH

        content = runner.read_text()
        assert "{{" not in content
        # The sandbox env var the stricttest floor reads.
        assert f"--setenv {SANDBOX_ENV_VAR} 1" in content
        # Config-driven pieces.
        assert 'SANDBOX_CACHES="uv go"' in content
        assert "uv sync --offline && uv run --offline pytest" in content
        assert "-q -n auto" in content
        assert "scripts/test-prewarm.sh" in content
        assert "--setenv LEGACY_SANDBOX 1" in content
        # The bwrap preflight probe rides along verbatim.
        assert "bwrap could not create the --unshare-net sandbox" in content
        assert "kernel.apparmor_restrict_unprivileged_userns=0" in content

    def test_emitted_runner_is_valid_bash(self, tmp_path, monkeypatch):
        import subprocess

        self._scaffold(tmp_path, _section(), monkeypatch)
        result = subprocess.run(
            ["bash", "-n", str(tmp_path / "scripts" / "test.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_minimal_config_renders_valid_bash(self, tmp_path, monkeypatch):
        import subprocess

        config = {
            CONFIG_KEY: {"runner_path": "test.sh", "command": "go test ./..."}
        }
        self._scaffold(tmp_path, config, monkeypatch)
        content = (tmp_path / "test.sh").read_text()
        assert 'SANDBOX_CACHES=""' in content
        assert f"--setenv {SANDBOX_ENV_VAR} 1" in content
        result = subprocess.run(
            ["bash", "-n", str(tmp_path / "test.sh")], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_stripped_executable_bit_is_healed(self, tmp_path, monkeypatch):
        config = _section()
        self._scaffold(tmp_path, config, monkeypatch)
        runner = tmp_path / "scripts" / "test.sh"
        runner.chmod(0o644)
        self._scaffold(tmp_path, config, monkeypatch)
        assert runner.stat().st_mode & stat.S_IXUSR


def test_rlsbl_own_runner_is_a_template_instance():
    """rlsbl dogfoods the shared template: its runner IS the rendered template.

    A hand edit to scripts/test.sh (rather than to the template plus a
    re-scaffold) makes this fail on purpose -- the distributor must run the
    same runner it ships.
    """
    config = json.loads((REPO_ROOT / ".rlsbl" / "config.json").read_text())
    rendered, unreplaced = process_template(
        TEMPLATE_PATH.read_text(), template_vars(config)
    )
    assert unreplaced == []
    assert (REPO_ROOT / "scripts" / "test.sh").read_text() == rendered


# ---------------------------------------------------------------------------
# The stricttest-floor check
# ---------------------------------------------------------------------------


def _repo(tmp_path, *, config=None, runner=True, executable=True,
          workflow=None, pyproject=None):
    """Build a fixture repo directory for the floor check."""
    (tmp_path / ".rlsbl").mkdir(exist_ok=True)
    config = config if config is not None else {"publish_mode": "ci"}
    (tmp_path / ".rlsbl" / "config.json").write_text(json.dumps(config))
    section = config.get(CONFIG_KEY)
    if section and runner:
        runner_file = tmp_path / section["runner_path"]
        runner_file.parent.mkdir(parents=True, exist_ok=True)
        runner_file.write_text("#!/usr/bin/env bash\n")
        runner_file.chmod(0o755 if executable else 0o644)
    if workflow is not None:
        for path, content in workflow.items():
            wf = tmp_path / path
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(content)
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    return config


def _texts(result):
    """Problem texts from a check outcome."""
    return [p.text for p in result.problems]


def _run_floor_check(tmp_path, config):
    checks = capture_all_checks()
    return checks["stricttest-floor"](make_ctx(tmp_path, config))


class TestFloorCheck:
    def test_unadopted_repo_skips_visibly(self, tmp_path):
        config = _repo(tmp_path)
        result = _run_floor_check(tmp_path, config)
        assert result.status == "skip"
        assert "not adopted" in result.message

    def test_adopted_green(self, tmp_path):
        config = _repo(
            tmp_path,
            config=_section(prewarm=None, extra_env=None),
            workflow={".github/workflows/ci.yml": "  - run: scripts/test.sh\n"},
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "pass", _texts(result)

    def test_missing_runner_fails(self, tmp_path):
        config = _repo(
            tmp_path,
            config=_section(ci_workflows=None),
            runner=False,
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("does not exist" in m for m in _texts(result))

    def test_non_executable_runner_fails(self, tmp_path):
        config = _repo(
            tmp_path,
            config=_section(ci_workflows=None),
            executable=False,
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("not executable" in m for m in _texts(result))

    def test_incomplete_family_fails(self, tmp_path):
        config = {"publish_mode": "ci", CONFIG_KEY: {"command": "pytest"}}
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / ".rlsbl" / "config.json").write_text(json.dumps(config))
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("missing required key" in m for m in _texts(result))

    def test_ci_workflow_not_using_runner_fails(self, tmp_path):
        config = _repo(
            tmp_path,
            config=_section(),
            workflow={".github/workflows/ci.yml": "  - run: uv run pytest\n"},
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("does not invoke the sandbox runner" in m for m in _texts(result))

    def test_missing_ci_workflow_fails(self, tmp_path):
        config = _repo(tmp_path, config=_section())
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("which does not exist" in m for m in _texts(result))

    def test_plugin_adopted_without_runner_but_sandbox_required(self, tmp_path):
        config = _repo(
            tmp_path,
            pyproject=(
                "[project]\nname = 'x'\nversion = '0'\n"
                "[dependency-groups]\ndev = ['stricttest>=0.1']\n"
                "[tool.pytest.ini_options]\n"
                'stricttest_sandbox_required = "true"\n'
            ),
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "fail"
        assert any("no sandbox runner is distributed" in m for m in _texts(result))

    def test_plugin_adopted_without_runner_and_not_required(self, tmp_path):
        config = _repo(
            tmp_path,
            pyproject=(
                "[project]\nname = 'x'\nversion = '0'\n"
                "dependencies = ['stricttest']\n"
                "[tool.pytest.ini_options]\n"
                'stricttest_sandbox_required = "false"\n'
            ),
        )
        result = _run_floor_check(tmp_path, config)
        assert result.status == "pass"

    def test_verdict_notes_name_the_runner(self, tmp_path):
        config = _repo(
            tmp_path,
            config=_section(ci_workflows=None),
        )
        verdict = evaluate_floor(config, str(tmp_path))
        assert verdict.adopted and verdict.ok
        assert any("scripts/test.sh" in n for n in verdict.notes)

    def test_rlsbl_itself_passes_the_floor_check(self):
        config = json.loads((REPO_ROOT / ".rlsbl" / "config.json").read_text())
        verdict = evaluate_floor(config, str(REPO_ROOT))
        assert verdict.adopted
        assert verdict.ok, verdict.problems

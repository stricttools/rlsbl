"""Tests for the ``test_sandbox`` config family: validation, scaffold emission,
and the ``stricttest-floor`` adoption check.

rlsbl distributes the outer layer of the stricttest floor -- the bubblewrap
runner script -- and enforces that an adopted repo actually has a working one.
"""

import json
import os
import stat
import subprocess
import time
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


class TestDevOverlayBlock:
    """The runner honours `rlsbl dev sync` overlays, for uv repos only.

    Without this, a repo whose environment carries an editable sibling checkout
    runs its bare suite against that checkout and its sandboxed suite against
    the locked registry wheel -- two runs verifying different code.
    """

    def _render(self, config):
        rendered, unreplaced = process_template(
            TEMPLATE_PATH.read_text(), template_vars(config)
        )
        assert unreplaced == []
        return rendered

    def test_uv_repo_carries_the_block(self):
        content = self._render(_section(caches=["uv", "go"]))
        assert "dev-sources.toml.local-only" in content
        assert "dev-overlays-state.toml.local-only" in content
        assert "--setenv SANDBOX_UV_SYNC_ARGS" in content
        assert "--setenv SANDBOX_OVERLAY_INSTALL" in content
        assert "--setenv SANDBOX_UV_RUN_ARGS" in content
        assert "uv pip install --offline -e" in content
        assert 'echo "[sandbox] dependency source:' in content

    def test_non_uv_repo_has_no_block(self):
        content = self._render(_section(caches=["go"]))
        assert "dev-sources.toml.local-only" not in content
        assert "SANDBOX_UV_SYNC_ARGS" not in content
        assert "dependency source:" not in content

    def test_block_refuses_a_command_that_ignores_the_variables(self):
        """An overlay bound but not installed would be a preview that lies."""
        content = self._render(_section(caches=["uv"]))
        assert "does not reference" in content
        assert (
            "for ov_var in SANDBOX_UV_SYNC_ARGS SANDBOX_OVERLAY_INSTALL "
            "SANDBOX_UV_RUN_ARGS" in content
        )

    # -- the embedded resolver, run as the runner runs it --------------------

    def _resolver(self):
        """The python program the runner embeds, lifted from the template."""
        content = TEMPLATE_PATH.read_text()
        start = content.index("<<'PYOVERLAY'\n") + len("<<'PYOVERLAY'\n")
        end = content.index("\nPYOVERLAY", start)
        return content[start:end]

    def _run_resolver(self, root):
        return subprocess.run(
            ["python3", "-c", self._resolver(), str(root)],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _write(path, package, checkout):
        path.write_text(
            f'[[overlay]]\npackage = "{package}"\npath = "{checkout}"\n'
        )

    def test_no_files_is_registry_mode(self, tmp_path):
        result = self._run_resolver(tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_agreeing_files_emit_the_overlay(self, tmp_path):
        checkout = tmp_path / "sibling"
        checkout.mkdir()
        self._write(tmp_path / "dev-sources.toml.local-only", "sib", checkout)
        self._write(
            tmp_path / "dev-overlays-state.toml.local-only", "sib", checkout
        )
        result = self._run_resolver(tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == f"sib\t{os.path.realpath(checkout)}"

    def test_declared_but_never_synced_is_a_hard_error(self, tmp_path):
        checkout = tmp_path / "sibling"
        checkout.mkdir()
        self._write(tmp_path / "dev-sources.toml.local-only", "sib", checkout)
        result = self._run_resolver(tmp_path)
        assert result.returncode == 1
        assert "never installed" in result.stderr
        assert "rlsbl dev sync" in result.stderr

    def test_disagreeing_paths_are_a_hard_error(self, tmp_path):
        first = tmp_path / "sibling"
        second = tmp_path / "elsewhere"
        first.mkdir()
        second.mkdir()
        self._write(tmp_path / "dev-sources.toml.local-only", "sib", first)
        self._write(
            tmp_path / "dev-overlays-state.toml.local-only", "sib", second
        )
        result = self._run_resolver(tmp_path)
        assert result.returncode == 1
        assert "but synced from" in result.stderr

    def test_missing_checkout_is_a_hard_error(self, tmp_path):
        gone = tmp_path / "gone"
        self._write(tmp_path / "dev-sources.toml.local-only", "sib", gone)
        self._write(tmp_path / "dev-overlays-state.toml.local-only", "sib", gone)
        result = self._run_resolver(tmp_path)
        assert result.returncode == 1
        assert "does not exist" in result.stderr


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
# Orphaned scratch sweep
# ---------------------------------------------------------------------------


RUNNER = REPO_ROOT / "scripts" / "test.sh"

# Comfortably past the runner's 5-minute grace window.
_AGED_SECONDS = 3600


def _age(path):
    """Backdate a path past the sweep's grace window."""
    stale = time.time() - _AGED_SECONDS
    os.utime(path, (stale, stale))


def _scratch(parent, name, *, pid=None, aged=True):
    """Create a scratch dir with an optional owner-PID sidecar."""
    directory = parent / name
    directory.mkdir(parents=True)
    (directory / "payload.txt").write_text("throwaway\n")
    if pid is not None:
        (parent / f"{name}.pid").write_text(f"{pid}\n")
    if aged:
        _age(directory)
    return directory


def _reaped_pid():
    """A PID that is guaranteed to have exited and been reaped."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _sweep(tmp_path):
    """Run the real runner's ``--sweep-only`` mode over a private TMPDIR."""
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    uv_cache = tmp_path / "uvcache"
    env = {
        **os.environ,
        "TMPDIR": str(tmpdir),
        "UV_CACHE_DIR": str(uv_cache),
    }
    result = subprocess.run(
        ["bash", str(RUNNER), "--sweep-only"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result


class TestOrphanedScratchSweep:
    """A killed run never fires the EXIT trap, so it leaks a full copy of the
    repo under TMPDIR. The next run sweeps its own leftovers -- and only those
    whose owner process is provably gone."""

    def test_sweeps_dead_and_unowned_leaves_live_alone(self, tmp_path):
        tmpdir = tmp_path / "tmpdir"
        tmpdir.mkdir()

        live = _scratch(tmpdir, "test-sandbox-work.LIVEAA", pid=os.getpid())
        dead = _scratch(tmpdir, "test-sandbox-work.DEADAA", pid=_reaped_pid())
        nopid = _scratch(tmpdir, "test-sandbox-work.NOPIDA")
        # A concurrent run that has just mktemp'd but not yet written its
        # sidecar: no PID file, but inside the grace window.
        fresh = _scratch(tmpdir, "test-sandbox-work.FRESHA", aged=False)
        unrelated = tmpdir / "flutter_tools.KEEPME"
        unrelated.mkdir()
        _age(unrelated)

        result = _sweep(tmp_path)

        assert live.exists(), "a live owner's scratch must never be swept"
        assert fresh.exists(), "a dir inside the grace window must survive"
        assert unrelated.exists(), "only the runner's own prefixes are swept"
        assert not dead.exists(), "a dead owner's scratch must be reclaimed"
        assert not nopid.exists(), "an unowned stale scratch must be reclaimed"
        assert not (tmpdir / "test-sandbox-work.DEADAA.pid").exists()
        assert "swept 2 orphaned scratch dir(s)" in result.stderr

    def test_sweeps_orphaned_uv_cache_clones(self, tmp_path):
        """The uv cache clone leaks the same way, next to the real cache."""
        uv_cache = tmp_path / "uvcache"
        uv_cache.mkdir()
        clone = _scratch(tmp_path, "uvcache.sandbox.DEADAA", pid=_reaped_pid())

        _sweep(tmp_path)

        assert not clone.exists()
        assert uv_cache.exists(), "the real cache must never be swept"

    def test_empty_tmpdir_is_a_no_op(self, tmp_path):
        result = _sweep(tmp_path)
        assert "orphaned scratch" not in result.stderr

    def test_runner_does_not_exec_the_sandbox(self):
        """``exec bwrap`` replaces the shell, so the EXIT trap that removes
        the scratch dirs never runs -- which leaked a full repo copy on EVERY
        run, successful ones included, not just killed ones. bwrap must be a
        child whose status the runner forwards after cleaning up.
        """
        for text in (RUNNER.read_text(), TEMPLATE_PATH.read_text()):
            assert "exec bwrap" not in text
            assert "trap cleanup EXIT" in text


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

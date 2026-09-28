"""rlsbl's options: every check is an option, rlsbl:<check name>.

The registry rlsbl ships is generated from its check registry; a repository's
entries in .strictmetadata/options/ give each check its value (strictcli's
check value resolver), decide which adoption settings its config may carry,
and are written by `rlsbl options set`.
"""

import json
import tomllib
from pathlib import Path

import pytest

import rlsbl
from conftest import (
    capture_all_checks,
    make_ctx,
    make_workspace,
    run_git,
    run_named_options_command,
    set_option,
)
from rlsbl.config import validate_config_schema
from rlsbl.errors import ConfigError
from rlsbl.options import (
    ADOPTION_SETTINGS,
    OptionsError,
    checked_registry,
    load,
    option_value,
    set_entry,
)
from rlsbl.options_registry import (
    ADOPTION_OPTIONS,
    CHECKS_TOML,
    FRAMEWORK_CHECKS,
    NON_CHECK_OPTIONS,
    PATH_SCOPE,
    REGISTRY_TOML,
    declarations,
    render,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The areas the options' subject files are named for.
SUBJECTS = {
    "changelog", "release", "workspace", "dependencies", "tests", "code",
    "project", "scaffold", "push",
}


def _checks_toml():
    with open(CHECKS_TOML, "rb") as f:
        return tomllib.load(f)["checks"]


def _project(tmp_path, config=None, name="proj"):
    """A git repository holding one rlsbl project."""
    root = tmp_path / name
    root.mkdir()
    run_git(root, "init", "-q")
    (root / ".rlsbl").mkdir()
    config = {"publish_mode": "none"} if config is None else config
    (root / ".rlsbl" / "config.json").write_text(json.dumps(config))
    return root


def _entries(root, subject, body):
    """Hand-write one subject document."""
    directory = root / ".strictmetadata" / "options"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.toml").write_text('owner = "strictspec"\n')
    (directory / f"{subject}.toml").write_text("format_version = 1\n" + body)


def _check(root, monkeypatch, *argv):
    monkeypatch.chdir(root)
    return rlsbl.app.test(["check", *argv])


def _row(output, name):
    """The result row of check *name* in a check run's output."""
    for line in output.splitlines():
        parts = line.split()
        # A run's row leads with its status; a listing's with the name.
        if parts[:1] == [name] or (len(parts) >= 2 and parts[1] == name):
            return line
    raise AssertionError(f"no row for {name} in:\n{output}")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_the_shipped_registry_is_a_fresh_rendering(self):
        """scripts/gen_options_registry.py regenerates it; never hand-edited."""
        assert REGISTRY_TOML.read_text(encoding="utf-8") == render()

    def test_strictspec_accepts_it(self):
        registry = checked_registry()
        expected = len(_checks_toml()) + len(FRAMEWORK_CHECKS) + len(NON_CHECK_OPTIONS)
        assert len(registry.names()) == expected

    def test_every_check_is_an_option_ranked_by_its_severity(self):
        registry = checked_registry()
        for name, check in _checks_toml().items():
            decl = registry.option(name).declaration
            if check["severity"] == "error":
                assert decl.values == "error > warn > off", name
            else:
                assert decl.values == "warn > off", name
            if name in ADOPTION_OPTIONS:
                assert decl.default == "off", name
            else:
                assert decl.default == check["severity"], name
            assert decl.subject == check["subject"], name
            assert decl.description == check["description"], name
            assert list(decl.requires) == sorted(check["depends_on"]), name

    def test_framework_checks_are_the_checks_strictcli_registers(self, monkeypatch):
        """The table the generator carries for strictcli's own checks names
        what the framework registers into rlsbl's app, at their severities."""
        monkeypatch.chdir(REPO_ROOT)
        rlsbl.app.reset_check_provider_cache()
        rlsbl.app._materialize_check_providers()
        defs = rlsbl.app._check_defs
        from rlsbl.external_checks import validate_external_checks

        external = {
            e["name"] for e in validate_external_checks(
                json.loads((REPO_ROOT / ".rlsbl" / "config.json").read_text())
            )
        }
        framework = set(defs) - set(_checks_toml()) - external
        assert framework == set(FRAMEWORK_CHECKS)
        for name in framework:
            assert defs[name].severity == FRAMEWORK_CHECKS[name][0], name

    def test_adoption_options_default_off_and_take_a_path_scope(self):
        registry = checked_registry()
        assert set(ADOPTION_OPTIONS) == set(ADOPTION_SETTINGS)
        for name in ADOPTION_OPTIONS:
            decl = registry.option(name).declaration
            assert decl.default == "off", name
            assert decl.scope == PATH_SCOPE, name
        others = [d for d in declarations() if d["name"] not in ADOPTION_OPTIONS]
        assert all(d["scope"] == "none" for d in others)

    def test_test_sandbox_is_on_or_off(self):
        decl = checked_registry().option("test-sandbox").declaration
        assert (decl.values, decl.default, decl.subject) == ("on > off", "off", "tests")

    def test_every_check_has_a_one_line_description_and_a_ruled_subject(self):
        for name, check in _checks_toml().items():
            assert check["subject"] in SUBJECTS, name
            assert check["description"].strip() and "\n" not in check["description"], name
        for name, (_sev, subject, description) in FRAMEWORK_CHECKS.items():
            assert subject in SUBJECTS and description.strip(), name

    def test_the_retired_reminder_check_is_gone(self):
        assert "changelog-format-version" not in _checks_toml()
        assert "changelog-format-version-gate" in _checks_toml()


# ---------------------------------------------------------------------------
# Check values
# ---------------------------------------------------------------------------


class TestCheckValues:
    def test_without_entries_every_check_runs_at_its_default(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        result = _check(root, monkeypatch, "--list")
        assert result.exit_code == 0, result.stderr
        assert "rlsbl:dep-floors default" in _row(result.stdout, "dep-floors")
        assert _row(result.stdout, "dep-floors").split()[-3] == "off"
        assert _row(result.stdout, "license-file").split()[-2:] == ["error", "default"]

    def test_a_warn_entry_reports_without_failing(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        set_option(root, "license-file", "warn", ideal="error")
        result = _check(root, monkeypatch, "--name", "license-file")
        row = _row(result.stdout, "license-file")
        assert row.startswith("WARN"), result.stdout
        listing = _check(root, monkeypatch, "--list").stdout
        assert "rlsbl:license-file in .strictmetadata/options/project.toml" in _row(
            listing, "license-file"
        )

    def test_an_off_entry_does_not_run_the_check(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        set_option(root, "license-file", "off", ideal="error")
        result = _check(root, monkeypatch, "--name", "license-file")
        assert _row(result.stdout, "license-file").startswith("OFF"), result.stdout
        assert result.exit_code == 0

    def test_invalid_entries_stop_the_run_and_the_fix_clears_them(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        _entries(root, "project", (
            '\n[[entry]]\nid = "rlsbl:license-file"\ncurrent = "loud"\n'
            'ideal = "error"\nreason = "typo"\n'
        ))
        result = _check(root, monkeypatch, "--name", "license-file")
        assert result.exit_code == 1
        output = result.stdout + result.stderr
        assert "STRICTSPEC_OPTIONS_UNDECLARED_CURRENT" in output
        assert "runs nothing with them" in output
        assert "license-file" not in [ln.split()[1] for ln in result.stdout.splitlines() if len(ln.split()) > 1]
        # The refusal names `rlsbl options set` as the way to rewrite it.
        fixed = rlsbl.app.test([
            "options", "set", "rlsbl:license-file", "--current", "warn",
            "--ideal", "error", "--reason", "no license yet", "--no-auto-commit",
        ])
        assert fixed.exit_code == 0, fixed.stderr
        assert _check(root, monkeypatch, "--name", "license-file").stdout.startswith("WARN")

    def test_switching_off_an_option_others_require_is_refused_until_they_are_off(
        self, tmp_path, monkeypatch,
    ):
        root = _project(tmp_path)
        _entries(root, "project", (
            '\n[[entry]]\nid = "rlsbl:version-consistency"\ncurrent = "off"\n'
            'ideal = "error"\nreason = "mid-migration"\n'
        ))
        result = _check(root, monkeypatch, "--name", "version-consistency")
        output = result.stdout + result.stderr
        assert result.exit_code == 1
        assert "STRICTSPEC_OPTIONS_DEPENDENTS_NOT_OFF" in output
        assert '"rlsbl:changelog-entry"' in output and '"rlsbl:unpublished-refs"' in output
        # The fix the diagnostic names: switch each of them off in its own
        # entry with its own reason.
        with open(root / ".strictmetadata" / "options" / "changelog.toml", "w") as f:
            f.write(
                'format_version = 1\n\n[[entry]]\nid = "rlsbl:changelog-entry"\n'
                'current = "off"\nideal = "error"\nreason = "follows version-consistency"\n'
            )
        with open(root / ".strictmetadata" / "options" / "release.toml", "w") as f:
            f.write(
                'format_version = 1\n\n[[entry]]\nid = "rlsbl:unpublished-refs"\n'
                'current = "off"\nideal = "error"\nreason = "follows version-consistency"\n'
            )
        result = _check(root, monkeypatch, "--name", "version-consistency")
        assert _row(result.stdout, "version-consistency").startswith("OFF"), result.stdout + result.stderr

    def test_outside_a_git_repository_every_option_is_at_its_default(self, tmp_path):
        assert load(tmp_path).root is None
        assert option_value("dep-floors", tmp_path).value == "off"


class TestPathScopes:
    def _workspace(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        run_git(root, "init", "-q")
        make_workspace(root, [
            {"path": "cli", "name": "cli"},
            {"path": "lib", "name": "lib"},
        ])
        return root

    def test_a_scoped_entry_covers_its_member_only(self, tmp_path):
        root = self._workspace(tmp_path)
        set_option(root / "cli", "dep-floors", "error", scope="cli")
        assert option_value("dep-floors", root / "cli").value == "error"
        assert option_value("dep-floors", root / "lib").value == "off"
        assert option_value("dep-floors", root).value == "off"

    def test_a_scoped_entry_wins_over_an_unscoped_one(self, tmp_path):
        root = self._workspace(tmp_path)
        set_option(root, "lint", "error")
        set_option(root / "cli", "lint", "warn", ideal="error", scope="cli")
        assert option_value("lint", root / "cli").value == "warn"
        assert option_value("lint", root / "lib").value == "error"

    def test_a_scope_naming_no_member_is_refused_naming_the_members(self, tmp_path):
        root = self._workspace(tmp_path)
        with pytest.raises(OptionsError) as exc:
            set_option(root, "dep-floors", "error", scope="cl")
        assert "names no member of this workspace" in str(exc.value)
        assert "cli, lib" in str(exc.value)
        # Using a scope the refusal lists is accepted.
        set_option(root, "dep-floors", "error", scope="cli")

    def test_a_scope_outside_a_workspace_is_refused(self, tmp_path):
        root = _project(tmp_path)
        with pytest.raises(OptionsError, match="belongs to no rlsbl workspace"):
            set_option(root, "dep-floors", "error", scope="cli")

    def test_an_option_without_a_scope_kind_takes_no_scope(self, tmp_path):
        root = self._workspace(tmp_path)
        with pytest.raises(OptionsError, match="STRICTSPEC_OPTIONS_SCOPE_NOT_ACCEPTED"):
            set_option(root, "license-file", "warn", ideal="error", scope="cli")

    def test_a_programmatic_run_resolves_for_its_context(self, tmp_path, monkeypatch):
        """run_checks_for resolves each value for the project its context was
        built for, not for the directory the process stands in."""
        root = self._workspace(tmp_path)
        (root / "cli" / ".rlsbl").mkdir(parents=True)
        (root / "cli" / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "none", "internal_dep_floors": []})
        )
        set_option(root / "cli", "dep-floors", "error", scope="cli")
        monkeypatch.chdir(root)
        ctx = make_ctx(root / "cli")
        def status(results):
            return [r.outcome.status for r in results if r.name == "dep-floors"]

        results, _listed, _code = rlsbl.run_checks_for(ctx, name_glob="dep-floors")
        assert status(results) == ["pass"]
        results, _listed, _code = rlsbl.app.run_checks(ctx, name_glob="dep-floors")
        assert status(results) == ["off"]

    def test_a_programmatic_run_reads_external_checks_for_its_context(
        self, tmp_path, monkeypatch,
    ):
        """run_checks_for runs the external checks the context's project
        declares, not those of the directory the process stands in: a release
        run from a workspace root for one member runs that member's checks."""
        root = self._workspace(tmp_path)
        (root / "cli" / ".rlsbl").mkdir(parents=True)
        (root / "cli" / ".rlsbl" / "config.json").write_text(json.dumps({
            "publish_mode": "none",
            "external_checks": [{
                "name": "member-own-check", "kind": "freeform",
                "command": "true", "tag": "preflight",
            }],
        }))
        monkeypatch.chdir(root)
        ctx = make_ctx(root / "cli")

        results, _listed, _code = rlsbl.run_checks_for(
            ctx, name_glob="member-own-check",
        )
        assert [(r.name, r.outcome.status) for r in results] == [
            ("member-own-check", "pass"),
        ]
        # The process's own directory declares no such check, so a plain run
        # from here afterwards does not see the member's definition.
        results, _listed, _code = rlsbl.app.run_checks(
            ctx, name_glob="member-own-check",
        )
        assert results == []


# ---------------------------------------------------------------------------
# The options commands
# ---------------------------------------------------------------------------


class TestOptionsRegistryCommand:
    def test_prints_the_shipped_document(self):
        result = rlsbl.app.test(["options", "registry"])
        assert result.exit_code == 0
        assert result.stdout == REGISTRY_TOML.read_text(encoding="utf-8")

    def test_json_carries_the_declarations(self):
        result = rlsbl.app.test(["options", "registry", "--json"])
        assert result.exit_code == 0
        assert result.data["format_version"] == 2
        assert result.data["option"] == declarations()


class TestOptionsSetCommand:
    def _set(self, root, monkeypatch, *argv):
        monkeypatch.chdir(root)
        return rlsbl.app.test(["options", "set", *argv])

    def test_creates_the_directory_manifest_and_entry_and_commits(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        run_git(root, "commit", "-q", "--allow-empty", "-m", "init")
        result = self._set(
            root, monkeypatch, "rlsbl:dep-floors", "--current", "error",
            "--ideal", "error", "--reason", "floors policed",
        )
        assert result.exit_code == 0, result.stderr
        assert "Created rlsbl:dep-floors in .strictmetadata/options/dependencies.toml" in result.stdout
        assert "Committed." in result.stdout
        options = root / ".strictmetadata" / "options"
        assert (options / "manifest.toml").read_text() == 'owner = "strictspec"\n'
        doc = tomllib.loads((options / "dependencies.toml").read_text())
        assert doc == {"format_version": 1, "entry": [{
            "id": "rlsbl:dep-floors", "current": "error", "ideal": "error",
            "reason": "floors policed",
        }]}
        from githarness import git

        assert "Autogenerated: true" in git(root, "log", "-1", "--format=%B")
        assert ".strictmetadata" not in git(root, "status", "--porcelain")

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        monkeypatch.chdir(root)
        result = rlsbl.app.test([
            "--dry-run", "options", "set", "rlsbl:dep-floors", "--current",
            "error", "--ideal", "error", "--reason", "floors policed",
        ])
        assert result.exit_code == 0, result.stderr
        assert "Dry run: would write rlsbl:dep-floors" in result.stdout
        assert not (root / ".strictmetadata").exists()

    def test_updates_an_entry_keeping_every_other_line(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        _entries(root, "code", (
            "# kept\n\n[[entry]]\nid = \"rlsbl:lint\"\ncurrent = \"warn\"\n"
            "ideal = \"error\"\nreason = \"noisy\"\n"
        ))
        result = self._set(
            root, monkeypatch, "rlsbl:lint", "--current", "error", "--ideal",
            "error", "--reason", "clean now", "--no-auto-commit",
        )
        assert result.exit_code == 0, result.stderr
        text = (root / ".strictmetadata" / "options" / "code.toml").read_text()
        assert "# kept" in text
        assert tomllib.loads(text)["entry"] == [{
            "id": "rlsbl:lint", "current": "error", "ideal": "error", "reason": "clean now",
        }]
        again = self._set(
            root, monkeypatch, "rlsbl:lint", "--current", "error", "--ideal",
            "error", "--reason", "clean now", "--no-auto-commit",
        )
        assert "already holds rlsbl:lint" in again.stdout

    def test_refuses_another_tools_id(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        result = self._set(
            root, monkeypatch, "selfdoc:low-numeric-data-density", "--current",
            "off", "--ideal", "off", "--reason", "x", "--no-auto-commit",
        )
        assert result.exit_code == 1
        assert "is an option of selfdoc, not of rlsbl" in result.stderr
        assert not (root / ".strictmetadata").exists()

    def test_refuses_a_value_the_option_does_not_declare(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        result = self._set(
            root, monkeypatch, "rlsbl:lock", "--current", "error", "--ideal",
            "error", "--reason", "x", "--no-auto-commit",
        )
        assert result.exit_code == 1
        assert "STRICTSPEC_OPTIONS_UNDECLARED_CURRENT" in result.stderr
        assert "nothing was written" in result.stderr

    def test_refuses_a_manifest_naming_another_owner_and_the_named_line_fixes_it(
        self, tmp_path, monkeypatch,
    ):
        root = _project(tmp_path)
        options = root / ".strictmetadata" / "options"
        options.mkdir(parents=True)
        (options / "manifest.toml").write_text('owner = "selfdoc"\n')
        argv = ("rlsbl:dep-floors", "--current", "error", "--ideal", "error",
                "--reason", "x", "--no-auto-commit")
        result = self._set(root, monkeypatch, *argv)
        assert result.exit_code == 1
        assert "write this line instead" in result.stderr
        line = result.stderr.rstrip().splitlines()[-1]
        (options / "manifest.toml").write_text(line + "\n")
        assert self._set(root, monkeypatch, *argv).exit_code == 0


# ---------------------------------------------------------------------------
# Config settings the options govern
# ---------------------------------------------------------------------------


#: One valid declaration of each adoption setting.
SETTINGS = {
    "dep-floors": {"internal_dep_floors": ["strictcli"]},
    "format": {"checks": {"format": {"paths": ["pkg"]}}},
    "lint": {"checks": {"lint": {"paths": ["pkg"]}}},
    "strictspec-certificate-gate": {"strictspec_gate": {"certificate": "cert.json"}},
    "test-sandbox": {"test_sandbox": {"runner_path": "scripts/test.sh", "command": "pytest"}},
    "type-check": {"checks": {"type-check": {"paths": ["pkg"]}}},
}


def _schema_errors(root, config):
    checks = capture_all_checks()
    result = checks["config-schema"](make_ctx(root, config))
    return result.status, " ".join(p.text for p in result.problems)


class TestRetiredFormatVersionKey:
    @pytest.mark.parametrize("value", [True, False])
    def test_config_schema_refuses_it_naming_the_fix(self, tmp_path, value):
        root = _project(tmp_path)
        status, text = _schema_errors(
            root, {"publish_mode": "none", "changelog_format_version_enforced": value},
        )
        assert status == "fail"
        assert "changelog_format_version_enforced is retired" in text
        assert "rlsbl options set rlsbl:changelog-format-version-gate --current off" in text

    def test_deleting_the_key_clears_it(self, tmp_path):
        root = _project(tmp_path)
        config = {"publish_mode": "none", "changelog_format_version_enforced": True}
        assert _schema_errors(root, config)[0] == "fail"
        del config["changelog_format_version_enforced"]
        assert _schema_errors(root, config)[0] == "pass"

    def test_the_named_entry_keeps_enforcement_off(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        config = {"publish_mode": "none", "changelog_format_version_enforced": False}
        status, text = _schema_errors(root, config)
        assert status == "fail"
        result = run_named_options_command(text, root, monkeypatch)
        assert result.exit_code == 0, result.stderr
        del config["changelog_format_version_enforced"]
        assert _schema_errors(root, config)[0] == "pass"
        assert option_value("changelog-format-version-gate", root).value == "off"

    def test_the_release_refuses_it_too(self, tmp_path):
        root = _project(tmp_path)
        with pytest.raises(ConfigError, match="changelog_format_version_enforced is retired"):
            validate_config_schema(
                {"publish_mode": "none", "changelog_format_version_enforced": True},
                project_dir=str(root),
            )


class TestAdoptionSettings:
    @pytest.mark.parametrize("name", sorted(SETTINGS))
    def test_a_setting_while_its_option_is_off_is_dead_config(self, tmp_path, name):
        root = _project(tmp_path)
        status, text = _schema_errors(root, {"publish_mode": "none", **SETTINGS[name]})
        assert status == "fail"
        assert f"rlsbl:{name} is off (rlsbl:{name} default), so nothing reads it" in text
        assert f"rlsbl options set rlsbl:{name}" in text

    @pytest.mark.parametrize("name", sorted(SETTINGS))
    def test_deleting_the_dead_setting_clears_it(self, tmp_path, name):
        root = _project(tmp_path)
        assert _schema_errors(root, {"publish_mode": "none", **SETTINGS[name]})[0] == "fail"
        assert _schema_errors(root, {"publish_mode": "none"})[0] == "pass"

    @pytest.mark.parametrize("name", sorted(SETTINGS))
    def test_the_named_command_switches_the_option_on(self, tmp_path, monkeypatch, name):
        root = _project(tmp_path)
        config = {"publish_mode": "none", **SETTINGS[name]}
        _status, text = _schema_errors(root, config)
        result = run_named_options_command(text, root, monkeypatch)
        assert result.exit_code == 0, result.stderr
        assert _schema_errors(root, config) == ("pass", "")

    @pytest.mark.parametrize("name", sorted(SETTINGS))
    def test_an_option_on_without_its_setting_is_refused(self, tmp_path, name):
        root = _project(tmp_path)
        set_option(root, name, checked_registry().option(name).ranking.values[0])
        status, text = _schema_errors(root, {"publish_mode": "none"})
        assert status == "fail"
        assert f"rlsbl:{name} is" in text and "declares no" in text
        # Either fix the refusal names clears it: declaring the setting ...
        assert _schema_errors(root, {"publish_mode": "none", **SETTINGS[name]})[0] == "pass"
        # ... or deleting the entry from the file it names.
        subject = checked_registry().option(name).declaration.subject
        assert f".strictmetadata/options/{subject}.toml" in text
        (root / ".strictmetadata" / "options" / f"{subject}.toml").unlink()
        assert _schema_errors(root, {"publish_mode": "none"})[0] == "pass"

    def test_a_workspace_member_is_told_to_scope_the_entry(self, tmp_path, monkeypatch):
        root = tmp_path / "ws"
        root.mkdir()
        run_git(root, "init", "-q")
        make_workspace(root, [{"path": "cli", "name": "cli"}, {"path": "lib", "name": "lib"}])
        member = root / "cli"
        (member / ".rlsbl").mkdir(parents=True, exist_ok=True)
        config = {"publish_mode": "none", "internal_dep_floors": ["strictcli"]}
        _status, text = _schema_errors(member, config)
        assert "--scope cli" in text
        assert run_named_options_command(text, member, monkeypatch).exit_code == 0
        assert _schema_errors(member, config)[0] == "pass"
        assert option_value("dep-floors", root / "lib").value == "off"


# ---------------------------------------------------------------------------
# The changelog format-version gate
# ---------------------------------------------------------------------------


class TestFormatVersionGate:
    UNSTAMPED = '{"commits":[],"user_facing":false}\n'

    def _changelog_project(self, tmp_path):
        root = _project(tmp_path)
        (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n')
        (root / ".rlsbl" / "changes").mkdir()
        (root / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(self.UNSTAMPED)
        return root

    def test_an_unstamped_line_fails_by_default(self, tmp_path, monkeypatch):
        root = self._changelog_project(tmp_path)
        result = _check(root, monkeypatch, "--name", "changelog-format-version-gate")
        assert _row(result.stdout, "changelog-format-version-gate").startswith("FAIL"), result.stdout

    def test_stamping_the_line_clears_it(self, tmp_path, monkeypatch):
        root = self._changelog_project(tmp_path)
        (root / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(
            '{"format_version":1,"commits":[],"user_facing":false}\n'
        )
        result = _check(root, monkeypatch, "--name", "changelog-format-version-gate")
        assert _row(result.stdout, "changelog-format-version-gate").startswith("PASS"), result.stdout

    def test_at_warn_it_still_runs_and_reports(self, tmp_path, monkeypatch):
        """The finding's own fix: the named entry makes it report without
        blocking. The check is not skipped."""
        root = self._changelog_project(tmp_path)
        result = _check(root, monkeypatch, "--name", "changelog-format-version-gate", "--verbose")
        assert "rlsbl options set rlsbl:changelog-format-version-gate --current warn" in result.stdout
        assert run_named_options_command(result.stdout, root, monkeypatch).exit_code == 0
        result = _check(root, monkeypatch, "--name", "changelog-format-version-gate")
        assert _row(result.stdout, "changelog-format-version-gate").startswith("WARN"), result.stdout

    def test_at_off_it_does_not_run(self, tmp_path, monkeypatch):
        root = self._changelog_project(tmp_path)
        set_option(root, "changelog-format-version-gate", "off", ideal="error")
        result = _check(root, monkeypatch, "--name", "changelog-format-version-gate")
        assert _row(result.stdout, "changelog-format-version-gate").startswith("OFF")


def test_set_entry_refuses_outside_a_repository(tmp_path):
    with pytest.raises(OptionsError, match="not inside a git repository"):
        set_entry(tmp_path, "rlsbl:dep-floors", "error", "error", "x")

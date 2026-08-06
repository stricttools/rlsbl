"""Tests for PgdesignTarget: pgdesign.toml handling, detection, version read/write."""

import os
import subprocess
import tempfile

import pytest
import tomlkit

from conftest import make_ctx
from rlsbl.errors import VersionError
from rlsbl.targets.pgdesign import PgdesignTarget
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets import TARGETS


MINIMAL_TOML = """\
[project]
version = "1.2.3"
schemas = ["auth.toml"]
migrations_dir = "migrations"
"""


class TestPgdesignTargetProtocol:
    """Verify PgdesignTarget satisfies the ReleaseTarget protocol."""

    def test_is_release_target(self):
        target = PgdesignTarget()
        assert isinstance(target, ReleaseTarget)

    def test_name(self):
        target = PgdesignTarget()
        assert target.name == "pgdesign"

    def test_version_file(self):
        target = PgdesignTarget()
        assert target.version_file() == "pgdesign.toml"

    def test_registered_in_targets(self):
        assert "pgdesign" in TARGETS
        assert isinstance(TARGETS["pgdesign"], PgdesignTarget)


class TestPgdesignTargetDetect:
    """Detection: pgdesign.toml in root or schema/ subdir."""

    def test_detect_true_root(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target.detect(d) is True

    def test_detect_true_schema_subdir(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            with open(os.path.join(schema_dir, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target.detect(d) is True

    def test_detect_false_empty_dir(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_false_wrong_file(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write("[project]\n")
            assert target.detect(d) is False


class TestPgdesignTargetReadVersion:
    """Reading version from pgdesign.toml [project].version."""

    def test_read_version_root(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target.read_version(d) == "1.2.3"

    def test_read_version_schema_subdir(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            with open(os.path.join(schema_dir, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target.read_version(d) == "1.2.3"

    def test_read_version_raises_no_file(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            try:
                target.read_version(d)
                assert False, "Expected FileNotFoundError"
            except FileNotFoundError:
                pass

    def test_read_version_raises_no_version_field(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write('[project]\nschemas = ["auth.toml"]\n')
            try:
                target.read_version(d)
                assert False, "Expected VersionError"
            except VersionError:
                pass

    def test_read_version_prefers_root(self):
        """When pgdesign.toml exists in both root and schema/, root wins."""
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write('[project]\nversion = "2.0.0"\nschemas = []\n')
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            with open(os.path.join(schema_dir, "pgdesign.toml"), "w") as f:
                f.write('[project]\nversion = "1.0.0"\nschemas = []\n')
            assert target.read_version(d) == "2.0.0"


class TestPgdesignTargetWriteVersion:
    """Writing version to pgdesign.toml atomically."""

    def test_write_version_updates(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pgdesign.toml")
            with open(path, "w") as f:
                f.write(MINIMAL_TOML)
            result = target.write_version(d, "2.0.0", ctx=make_ctx(d))
            assert result == ["pgdesign.toml"]
            with open(path, "r") as f:
                doc = tomlkit.parse(f.read())
            assert str(doc["project"]["version"]) == "2.0.0"

    def test_write_version_preserves_other_fields(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pgdesign.toml")
            with open(path, "w") as f:
                f.write(MINIMAL_TOML)
            target.write_version(d, "3.0.0", ctx=make_ctx(d))
            with open(path, "r") as f:
                doc = tomlkit.parse(f.read())
            assert doc["project"]["schemas"] == ["auth.toml"]
            assert doc["project"]["migrations_dir"] == "migrations"

    def test_write_version_schema_subdir(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            path = os.path.join(schema_dir, "pgdesign.toml")
            with open(path, "w") as f:
                f.write(MINIMAL_TOML)
            result = target.write_version(d, "4.0.0", ctx=make_ctx(d))
            assert result == [os.path.join("schema", "pgdesign.toml")]
            with open(path, "r") as f:
                doc = tomlkit.parse(f.read())
            assert str(doc["project"]["version"]) == "4.0.0"

    def test_write_version_no_tmp_left(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pgdesign.toml")
            with open(path, "w") as f:
                f.write(MINIMAL_TOML)
            target.write_version(d, "1.0.0", ctx=make_ctx(d))
            files = os.listdir(d)
            assert "pgdesign.toml.tmp" not in files

    def test_write_version_creates_project_section(self):
        """If [project] section is missing, it gets created."""
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pgdesign.toml")
            with open(path, "w") as f:
                f.write("[database]\nurl = \"postgres://localhost/test\"\n")
            target.write_version(d, "0.1.0", ctx=make_ctx(d))
            with open(path, "r") as f:
                doc = tomlkit.parse(f.read())
            assert str(doc["project"]["version"]) == "0.1.0"
            assert str(doc["database"]["url"]) == "postgres://localhost/test"


class TestPgdesignTargetTemplateVars:
    """Template variable extraction."""

    def test_template_vars_with_version(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            vars = target.template_vars(d, d)
            assert vars["name"] == os.path.basename(d)
            assert vars["version"] == "1.2.3"

    def test_template_vars_fallback(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            vars = target.template_vars(d, d)
            assert vars["name"] == os.path.basename(d)
            assert vars["version"] == "0.0.0"


class TestPgdesignTargetSchemaDir:
    """Internal _schema_dir resolution."""

    def test_schema_dir_root(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target._schema_dir(d) == d

    def test_schema_dir_subdir(self):
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            with open(os.path.join(schema_dir, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            assert target._schema_dir(d) == schema_dir


class TestPgdesignTargetBuild:
    """build() shells out to pgdesign to validate the schema.

    pgdesign 0.12.0 removed the `validate` command in favour of the check
    framework (`pgdesign check --tag validation`). The check command takes no
    positional path -- it resolves the project from the process working
    directory (its CheckContext root is os.Getwd(), and config discovery only
    walks UP from there) -- so the schema directory the old positional argument
    carried has to be expressed as cwd instead.
    """

    def _capture(self, monkeypatch, returncode=0, stderr=""):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(
                argv, returncode, stdout="", stderr=stderr
            )

        monkeypatch.setattr("rlsbl.effects.run", fake_run)
        return calls

    def test_build_invokes_check_tag_validation(self, monkeypatch):
        """The shipped argv must be the check-framework form, not `validate`."""
        calls = self._capture(monkeypatch)
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            target.build(d, "1.2.3")
        assert len(calls) == 1
        argv, _ = calls[0]
        assert argv == ["pgdesign", "check", "--tag", "validation"]

    def test_build_passes_schema_dir_as_cwd_root(self, monkeypatch):
        """A root-level pgdesign.toml means cwd is the project directory."""
        calls = self._capture(monkeypatch)
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            target.build(d, "1.2.3")
            assert calls[0][1]["cwd"] == d

    def test_build_passes_schema_subdir_as_cwd(self, monkeypatch):
        """A schema/ subdir must become cwd -- config discovery never walks down."""
        calls = self._capture(monkeypatch)
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            schema_dir = os.path.join(d, "schema")
            os.makedirs(schema_dir)
            with open(os.path.join(schema_dir, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            target.build(d, "1.2.3")
            assert calls[0][1]["cwd"] == schema_dir

    def test_build_no_positional_path_argument(self, monkeypatch):
        """`pgdesign check` accepts no positional path; passing one would abort."""
        calls = self._capture(monkeypatch)
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            target.build(d, "1.2.3")
            argv, _ = calls[0]
            assert d not in argv
            assert not any(a.startswith("/") for a in argv[1:])

    def test_build_applies_configured_timeout(self, monkeypatch):
        calls = self._capture(monkeypatch)
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            target.build(d, "1.2.3", config={"build_timeout": 17})
            assert calls[0][1]["timeout"] == 17

    def test_build_raises_on_nonzero(self, monkeypatch):
        self._capture(monkeypatch, returncode=1, stderr="E101: bad column")
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            with pytest.raises(RuntimeError):
                target.build(d, "1.2.3")

    def test_build_failure_message_names_the_real_command(
        self, monkeypatch, capsys
    ):
        """The diagnostic must name the command that actually ran."""
        self._capture(monkeypatch, returncode=1, stderr="E101: bad column")
        target = PgdesignTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pgdesign.toml"), "w") as f:
                f.write(MINIMAL_TOML)
            with pytest.raises(RuntimeError):
                target.build(d, "1.2.3")
        err = capsys.readouterr().err
        assert "pgdesign check --tag validation" in err
        assert "pgdesign validate" not in err
        assert "E101: bad column" in err

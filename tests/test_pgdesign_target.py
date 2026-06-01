"""Tests for PgdesignTarget: pgdesign.toml handling, detection, version read/write."""

import os
import tempfile

import tomlkit

from conftest import make_ctx
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
                assert False, "Expected ValueError"
            except ValueError:
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

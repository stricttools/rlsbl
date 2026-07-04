"""Pgdesign release target for projects using pgdesign to manage database schemas, with version tracking in pgdesign.toml."""

import os
import subprocess
import sys

import tomlkit

from .base import BaseTarget, TemplateVars
from ..errors import VersionError


class PgdesignTarget(BaseTarget):
    """Release target for pgdesign database schema projects.

    Detects pgdesign.toml in the project root or schema/ subdirectory.
    Version is stored in [project] version within pgdesign.toml.
    On release, validates the schema and generates migrations if configured.
    """

    detection_files = ("pgdesign.toml",)
    BUILD_TIMEOUT_DEFAULT = 60
    capabilities = frozenset({"read_name"})
    ecosystem = "PostgreSQL"

    @property
    def name(self):
        return "pgdesign"

    def detect(self, dir_path):
        """True if pgdesign.toml exists in root or schema/ subdir."""
        return (
            os.path.exists(os.path.join(dir_path, "pgdesign.toml"))
            or os.path.exists(os.path.join(dir_path, "schema", "pgdesign.toml"))
        )

    def _toml_path(self, dir_path):
        """Resolve the actual path to pgdesign.toml."""
        root_path = os.path.join(dir_path, "pgdesign.toml")
        if os.path.exists(root_path):
            return root_path
        schema_path = os.path.join(dir_path, "schema", "pgdesign.toml")
        if os.path.exists(schema_path):
            return schema_path
        return root_path  # default to root for creation

    def _schema_dir(self, dir_path):
        """Resolve the directory containing pgdesign.toml."""
        root_path = os.path.join(dir_path, "pgdesign.toml")
        if os.path.exists(root_path):
            return dir_path
        schema_path = os.path.join(dir_path, "schema", "pgdesign.toml")
        if os.path.exists(schema_path):
            return os.path.join(dir_path, "schema")
        return dir_path

    def read_name(self, dir_path, ctx):
        """Return the directory name as the project name."""
        return os.path.basename(os.path.abspath(dir_path))

    def read_metadata(self, dir_path):
        """Pgdesign projects have no standard metadata beyond version."""
        return {}

    def read_version(self, dir_path):
        """Read version from pgdesign.toml [project].version."""
        path = self._toml_path(dir_path)
        if not os.path.exists(path):
            raise FileNotFoundError(
                "No pgdesign.toml found. Create one with a [project] section."
            )
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        project = doc.get("project")
        if project is None or "version" not in project:
            raise VersionError(
                f"No [project].version in {path}"
            )
        return str(project["version"])

    def write_version(self, dir_path, version, ctx):
        """Update [project].version in pgdesign.toml using tomlkit round-trip.

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        path = self._toml_path(dir_path)
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        if "project" not in doc:
            doc["project"] = tomlkit.table()
        doc["project"]["version"] = version
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp_path, path)
        return [os.path.relpath(path, dir_path)]

    def version_file(self, dir_path=None):
        return "pgdesign.toml"

    def build(self, dir_path, version, *, config=None):
        """Validate the pgdesign schema. Fails the release if errors exist."""
        timeout = self._resolve_build_timeout(config)
        schema_dir = self._schema_dir(dir_path)
        result = subprocess.run(
            ["pgdesign", "validate", schema_dir],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            print(
                f"pgdesign validate failed:\n{result.stderr or result.stdout}",
                file=sys.stderr,
            )
            raise RuntimeError("pgdesign schema validation failed")

    def template_vars(self, dir_path, ctx):
        """Extract template variables."""
        dir_name = os.path.basename(os.path.abspath(dir_path))
        try:
            version = self.read_version(dir_path)
        except (FileNotFoundError, VersionError):
            version = "0.0.0"
        return TemplateVars(self.name, {
            "name": dir_name,
            "version": version,
        })

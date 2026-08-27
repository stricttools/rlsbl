"""Pgdesign release target for projects using pgdesign to manage database schemas, with version tracking in pgdesign.toml."""

import os
import sys

import tomlkit

from .base import BaseTarget, TemplateVars
from ..errors import VersionError
from .. import effects


class PgdesignTarget(BaseTarget):
    """Release target for pgdesign database schema projects.

    Detects pgdesign.toml in the directory being scanned. A project whose
    schema lives in a subdirectory declares that subdirectory as the target
    path in .rlsbl/config.json -- detection never walks down looking for it.
    Version is stored in [project] version within pgdesign.toml.
    On release, validates the schema and generates migrations if configured.
    """

    detection_files = ("pgdesign.toml",)
    BUILD_TIMEOUT_DEFAULT = 60
    capabilities = frozenset({"read_name", "ci_templates"})
    ecosystem = "PostgreSQL"

    @property
    def name(self):
        return "pgdesign"

    def detect(self, dir_path):
        """True if pgdesign.toml exists in *dir_path* itself."""
        return os.path.exists(os.path.join(dir_path, "pgdesign.toml"))

    def _toml_path(self, dir_path):
        """The pgdesign.toml path for *dir_path*; existence is not checked."""
        return os.path.join(dir_path, "pgdesign.toml")

    def _require_toml_path(self, dir_path):
        """Return the pgdesign.toml path, or raise naming the remedy.

        The error fires here, in resolution, rather than in detection:
        detection runs over every directory in a repository, so a schema
        subdirectory nobody declared must be invisible to it. Once a pgdesign
        target IS declared, the file it names has to exist, and the remedy
        for a schema in a subdirectory is the explicit target path.
        """
        path = self._toml_path(dir_path)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No pgdesign.toml in {dir_path}. Create one with a [project] "
                f"section, or -- if the schema lives in a subdirectory -- "
                f"declare that subdirectory as the target path in "
                f'.rlsbl/config.json: "targets": '
                f'[{{"name": "pgdesign", "path": "schema"}}]'
            )
        return path

    def read_name(self, dir_path, ctx):
        """Return the directory name as the project name."""
        return os.path.basename(os.path.abspath(dir_path))

    def read_metadata(self, dir_path):
        """Pgdesign projects have no standard metadata beyond version."""
        return {}

    def read_version(self, dir_path):
        """Read version from pgdesign.toml [project].version."""
        path = self._require_toml_path(dir_path)
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
        path = self._require_toml_path(dir_path)
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        if "project" not in doc:
            doc["project"] = tomlkit.table()
        doc["project"]["version"] = version
        effects.atomic_write_text(path, tomlkit.dumps(doc))
        return [os.path.relpath(path, dir_path)]

    def version_file(self, dir_path=None):
        return "pgdesign.toml"

    def build(self, dir_path, version, *, config=None):
        """Validate the pgdesign schema. Fails the release if errors exist.

        pgdesign 0.12.0 removed the `validate` command in favour of the check
        framework. `pgdesign check --tag validation` takes no positional path:
        it resolves the project from the process working directory (its check
        context root is the cwd, and config discovery only walks UP from
        there). The schema directory the old positional argument carried is
        therefore expressed as cwd -- and that directory is *dir_path*, which
        for a schema in a subdirectory is the declared target path.

        `--ignore-warnings` is always passed: pgdesign's check framework exits
        nonzero on warn-severity results, and warnings are advisory under its
        own severity model. Only errors are release-blocking, so warnings must
        never abort a release.
        """
        timeout = self._resolve_build_timeout(config)
        self._require_toml_path(dir_path)
        result = effects.run(
            ["pgdesign", "check", "--tag", "validation", "--ignore-warnings"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            print(
                "pgdesign check --tag validation failed:\n"
                f"{result.stderr or result.stdout}",
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

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "pgdesign"
        )

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

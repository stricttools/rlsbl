"""Pgdesign release target for projects using pgdesign to manage database schemas, with version tracking in pgdesign.toml."""

import json
import os
import sys

import tomlkit

from .base import PACKAGE_RENAME_UNSUPPORTED
from .base import BaseTarget, TemplateVars
from ..errors import ConfigError, VersionError
from .. import effects

#: The file the scaffolded CI installs Go from, next to pgdesign.toml.
GO_VERSION_FILE = ".go-version"


_VALIDATION_ARGV = ["pgdesign", "check", "--tag", "validation", "--json"]
_VALIDATION_COMMAND = " ".join(_VALIDATION_ARGV)


def _validation_verdict(result):
    """Read `pgdesign check --tag validation --json`: None when it passed,
    else the text naming why it did not.

    The machine output is strictcli's envelope (interface_version 2, as
    pgdesign 0.27.1 prints it): the check results are its ``payload``, one
    object per check with ``name``, ``status``, ``message``, and
    ``problems``. Only a ``fail`` status fails; each ``warn`` result's
    problems are printed as they are reported. Output that is not that
    envelope, a selection that matched no check, and a nonzero exit with
    neither a failure nor a warning to show for it are refused, never read as
    a pass.
    """
    raw = (result.stdout or "").strip()
    try:
        doc = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        doc = None
    if not isinstance(doc, dict) or doc.get("interface_version") != 2:
        return (
            f"its output is not strictcli's machine envelope "
            f"(interface_version 2, which pgdesign 0.27.1 prints); it exited "
            f"{result.returncode} with:\n{result.stderr or result.stdout}"
        )
    diagnostics = "\n".join(
        str(d.get("message", "")) for d in doc.get("diagnostics") or []
        if isinstance(d, dict)
    )
    results = doc.get("payload")
    if not results:
        return (
            f"the validation tag selected no checks, so nothing was "
            f"validated; pgdesign said:\n{diagnostics}"
        )
    if not isinstance(results, list) or not all(
        isinstance(r, dict) and "status" in r for r in results
    ):
        return f"its payload is not a list of check results:\n{raw}"

    failed = []
    for r in results:
        problems = [
            p for p in r.get("problems") or [] if isinstance(p, dict)
        ]
        if r["status"] == "warn":
            for p in problems:
                print(f"  WARN  {r.get('name')}: {p.get('text')}", file=sys.stderr)
        elif r["status"] == "fail":
            lines = [f"{r.get('name')}: {r.get('message')}"]
            lines += [
                f"  [{p.get('severity')}] {p.get('text')}" for p in problems
            ]
            failed.append("\n".join(lines))
    if failed:
        return "\n".join(failed)
    if result.returncode != 0 and not any(r["status"] == "warn" for r in results):
        return (
            f"it exited {result.returncode} without reporting a failure or a "
            f"warning:\n{result.stderr or diagnostics}"
        )
    return None


def machine_go_version():
    """The version of the Go installed on this machine, without the ``go``
    prefix (``1.26.6``).

    Raises:
        ConfigError: no usable go command, naming the file to write by hand.
    """
    remedy = (
        f"Create {GO_VERSION_FILE} next to pgdesign.toml yourself, holding one "
        f"line with the Go version CI should install (e.g. 1.26.6), and re-run "
        f"rlsbl scaffold."
    )
    try:
        result = effects.run(
            ["go", "env", "GOVERSION"], capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ConfigError(
            f"the pgdesign CI installs the Go named in {GO_VERSION_FILE}, which "
            f"does not exist, and there is no go command here to read a version "
            f"from ({exc}). {remedy}"
        ) from exc
    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw.startswith("go") or not raw[2:3].isdigit():
        raise ConfigError(
            f"the pgdesign CI installs the Go named in {GO_VERSION_FILE}, which "
            f"does not exist, and `go env GOVERSION` gave no release version "
            f"({raw or 'no output'!r}). {remedy}"
        )
    return raw[2:]


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
    ecosystem = "PostgreSQL"

    # rlsbl does not rename this target's package name; the operator edits it by hand.
    package_rename = PACKAGE_RENAME_UNSUPPORTED
    package_name_field = 'the project directory name'

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

        The verdict is read from the machine output, not the exit code:
        pgdesign's check exits nonzero on a warn-severity result too, and its
        warnings are advisory under its own severity model. A result whose
        status is `fail` fails the release and is named; warnings are printed
        and never block. See :func:`_validation_verdict`.
        """
        timeout = self._resolve_build_timeout(config)
        self._require_toml_path(dir_path)
        result = effects.run(
            _VALIDATION_ARGV,
            cwd=dir_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        problem = _validation_verdict(result)
        if problem is not None:
            print(f"{_VALIDATION_COMMAND} failed:\n{problem}", file=sys.stderr)
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

    def ensure_ci_inputs(self, dir_path, *, dry_run=False):
        """Create ``.go-version`` beside pgdesign.toml when it is missing.

        The scaffolded CI installs the Go it names, which is meant to be the
        Go the project is developed with, so it is seeded from the Go on this
        machine. An existing file is the project's and is never rewritten;
        changing the version is an edit to that file.
        """
        path = os.path.normpath(os.path.join(dir_path, GO_VERSION_FILE))
        if os.path.exists(path):
            return []
        version = machine_go_version()
        if not dry_run:
            effects.atomic_write_text(path, version + "\n")
        return [(path, f"created (Go {version}, the go on this machine)")]

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

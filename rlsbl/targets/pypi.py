"""PyPI release target that manages version tracking in pyproject.toml and scaffolds CI workflows for OIDC-based publishing to the PyPI index."""

import ast
import os
import re
import shutil
import subprocess
import tempfile
import tomllib

import tomlkit

from .base import BaseTarget, TemplateVars
from ..errors import VersionError
from ..utils import run

_MIN_VERSION_RE = re.compile(r">=\s*(\d+\.\d+(?:\.\d+)?)")

# Semver pre-release preid -> PEP 440 pre-release tag
_SEMVER_TO_PEP440 = {"alpha": "a", "beta": "b", "rc": "rc"}
# PEP 440 pre-release tag -> semver preid
_PEP440_TO_SEMVER = {v: k for k, v in _SEMVER_TO_PEP440.items()}

# Matches a PEP 440 pre-release suffix at the end of a version string:
# e.g. "1.2.3a0", "1.2.3b1", "1.2.3rc2"
_PEP440_PRE_RE = re.compile(r"^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$")


def find_dunder_version_node(content: str) -> "ast.Constant | None":
    """Find an __version__ assignment with a static string literal via AST.

    Handles both plain assignments (``__version__ = "1.0.0"``) and typed
    annotations (``__version__: str = "1.0.0"``).  Returns the
    ``ast.Constant`` node for the string value, or None if not found or
    the file has a syntax error.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__version__"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "__version__"
                and node.value is not None
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value

    return None


def has_any_dunder_version(content: str) -> bool:
    """Check whether the source contains any form of __version__ definition.

    Returns True if the content contains any assignment, annotated
    assignment, or import of ``__version__`` -- regardless of whether the
    value is a static literal.  Returns False on SyntaxError or if no
    ``__version__`` definition is found.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__version__":
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.names and any(
                alias.name == "__version__" for alias in node.names
            ):
                return True

    return False


class PypiTarget(BaseTarget):
    """Release target for Python projects (pyproject.toml)."""

    detection_files = ("pyproject.toml",)
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates", "dev_install", "publication_probe"})
    ecosystem = "Python / PyPI"

    @property
    def name(self):
        return "pypi"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "pyproject.toml"))

    def read_name(self, dir_path, ctx):
        """Read the project name from pyproject.toml."""
        toml_path = os.path.join(dir_path, "pyproject.toml")
        if not os.path.exists(toml_path):
            return None
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name")

    def read_metadata(self, dir_path):
        """Read license and description from pyproject.toml."""
        toml_path = os.path.join(dir_path, "pyproject.toml")
        if not os.path.exists(toml_path):
            return {}
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        project = data.get("project", {})
        result = {}
        license_val = project.get("license")
        if isinstance(license_val, str):
            result["license"] = license_val
        elif isinstance(license_val, dict) and license_val.get("text"):
            result["license"] = license_val["text"]
        description = project.get("description")
        if description:
            result["description"] = description
        return result

    def publication_probe(self, dir_path, version, ctx=None):
        """Probe PyPI for a specific version of this package."""
        from ..publication_probe import PublicationProbeResult, PublicationStatus

        pkg_name = self.read_name(dir_path, ctx)
        if not pkg_name:
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="pypi",
                version=version,
                message="no project name in pyproject.toml",
            )

        # PyPI uses PEP 440 versions, so translate if needed
        pep440_version = self.format_version(version)

        import json as _json
        import urllib.error
        from ..commands.check import _request_with_backoff

        url = f"https://pypi.org/pypi/{pkg_name}/{pep440_version}/json"
        try:
            with _request_with_backoff(url) as resp:
                _json.loads(resp.read())
            return PublicationProbeResult(
                status=PublicationStatus.PUBLISHED,
                registry="pypi",
                version=version,
                message=f"{pkg_name}=={pep440_version} found on PyPI",
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return PublicationProbeResult(
                    status=PublicationStatus.UNPUBLISHED,
                    registry="pypi",
                    version=version,
                    message=f"{pkg_name}=={pep440_version} not found on PyPI",
                )
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="pypi",
                version=version,
                message=f"PyPI API error: HTTP {e.code}",
            )
        except Exception as e:
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="pypi",
                version=version,
                message=f"PyPI API error: {e}",
            )

    def format_version(self, version):
        """Translate semver pre-release to PEP 440 format.

        - ``1.2.3-alpha.0`` -> ``1.2.3a0``
        - ``1.2.3-beta.1``  -> ``1.2.3b1``
        - ``1.2.3-rc.2``    -> ``1.2.3rc2``
        - Stable versions pass through unchanged.
        """
        if "-" not in version:
            return version
        base, suffix = version.split("-", 1)
        parts = suffix.rsplit(".", 1)
        if len(parts) != 2:
            return version
        preid, counter = parts[0], parts[1]
        pep440_tag = _SEMVER_TO_PEP440.get(preid)
        if pep440_tag is None:
            return version
        return f"{base}{pep440_tag}{counter}"

    @staticmethod
    def _pep440_to_semver(version):
        """Translate PEP 440 pre-release back to semver format.

        - ``1.2.3a0``  -> ``1.2.3-alpha.0``
        - ``1.2.3b1``  -> ``1.2.3-beta.1``
        - ``1.2.3rc2`` -> ``1.2.3-rc.2``
        - Stable versions pass through unchanged.
        """
        m = _PEP440_PRE_RE.match(version)
        if not m:
            return version
        base, tag, counter = m.group(1), m.group(2), m.group(3)
        preid = _PEP440_TO_SEMVER.get(tag)
        if preid is None:
            return version
        return f"{base}-{preid}.{counter}"

    def read_version(self, dir_path):
        """Read the version from pyproject.toml in the given directory.

        If the version is in PEP 440 pre-release format (e.g. ``1.2.3a0``),
        it is converted back to semver format (``1.2.3-alpha.0``) so that
        all internal version handling uses a consistent semver representation.
        """
        toml_path = os.path.join(dir_path, "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        try:
            raw = data["project"]["version"]
        except KeyError:
            raise VersionError(f"No [project].version in {toml_path}")
        return self._pep440_to_semver(raw)

    def write_version(self, dir_path, version, ctx):
        """Write a new version to pyproject.toml and __version__ in package source.

        Translates semver pre-release versions to PEP 440 format before
        writing (e.g. ``1.2.3-alpha.0`` -> ``1.2.3a0``).

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        pep440_version = self.format_version(version)
        path = os.path.join(dir_path, "pyproject.toml")
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        doc["project"]["version"] = pep440_version
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp_path, path)

        modified = ["pyproject.toml"]
        dunder_path = self._update_dunder_version(dir_path, doc, pep440_version)
        if dunder_path:
            modified.append(dunder_path)
        return modified

    def _update_dunder_version(self, dir_path, doc, version):
        """Update __version__ in the package's __init__.py if present.

        Uses AST parsing to find __version__ assignments with static string
        literals, handling plain assignments and typed annotations.

        Returns the relative path (relative to dir_path) of the file that
        was modified, or None if no file was updated.
        """
        from .utils import detect_python_package_root

        name = doc.get("project", {}).get("name")
        if not name:
            return None

        pkg_root = detect_python_package_root(dir_path)
        if pkg_root is None:
            return None

        init_rel = os.path.join(pkg_root, "__init__.py")
        init_path = os.path.join(dir_path, init_rel)
        if not os.path.isfile(init_path):
            return None

        with open(init_path, "r", encoding="utf-8") as f:
            content = f.read()

        node = find_dunder_version_node(content)
        if node is None:
            return None

        # Read the original quote character from source to preserve style.
        line = content.splitlines()[node.lineno - 1]
        quote_char = line[node.col_offset]

        # Build the replacement literal and splice it into the source.
        new_literal = f"{quote_char}{version}{quote_char}"
        lines = content.splitlines(keepends=True)
        target_line = lines[node.lineno - 1]
        lines[node.lineno - 1] = (
            target_line[:node.col_offset]
            + new_literal
            + target_line[node.end_col_offset:]
        )
        new_content = "".join(lines)

        if new_content != content:
            tmp = init_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp, init_path)
            return init_rel

        return None

    def version_file(self, dir_path=None):
        return "pyproject.toml"

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "pypi"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from the target project's pyproject.toml."""
        toml_path = os.path.join(dir_path, "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        project = data.get("project", {})
        name = project.get("name", "")
        version = project.get("version", "0.1.0")

        # Extract author -- fall back to git config
        from .utils import _get_git_author
        author = _get_git_author()

        # Extract repo name from project.urls
        repo_name = ""
        urls = project.get("urls", {})
        for url in urls.values():
            match = re.search(r"github\.com/([^/\s\"]+/[^/\s\"]+)", url)
            if match:
                repo_name = match.group(1).removesuffix(".git")
                break

        # Derive binCommand from project.scripts (CLI entry points)
        bin_command = ""
        scripts = project.get("scripts", {})
        if scripts:
            bin_command = next(iter(scripts))  # first script entry

        # Derive the actual Python import name using shared detection.
        from .utils import detect_python_package_root
        pkg_root = detect_python_package_root(dir_path)
        if pkg_root:
            # Strip src/ prefix (common hatch src layout) and convert path to module name
            import_name = pkg_root.removeprefix("src/").replace("/", ".")
        else:
            import_name = name.replace("-", "_")

        result = {
            "name": name,
            "version": version,
            "binCommand": bin_command,
            "author": author,
            "repoName": repo_name,
            "importName": import_name,
            "publishSetup": "Configure Trusted Publishing on pypi.org for automated PyPI releases",
        }

        requires_python = project.get("requires-python")
        if requires_python:
            m = _MIN_VERSION_RE.search(requires_python)
            if m:
                result["minRequiredPython"] = m.group(1)

        # Detect a pytest declaration (same probe the release test runner
        # uses) so scaffolded CI runs the suite after the import smoke test.
        from ..testing import _probe_pytest_location
        if _probe_pytest_location(dir_path) is not None:
            result["hasPytest"] = "true"

        return TemplateVars(self.name, result)

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def build(self, dir_path, version, *, config=None):
        """Build the package, rewriting path deps if in a monorepo context.

        When the project has path dependencies (e.g., sibling packages in a
        monorepo), copies the project to a temp directory with rewritten
        pyproject.toml so the working tree is never modified.  Otherwise runs
        ``uv build`` in place.
        """
        from ..dep_rewrite import build_rewrite_map, detect_path_deps
        from ..workspace import find_workspace_root, load_workspace
        from ..workspace_graph import WorkspaceGraph

        timeout = self._resolve_build_timeout(config)
        pyproject_path = os.path.join(dir_path, "pyproject.toml")
        workspace_root = find_workspace_root(dir_path)

        # Only attempt rewriting when inside a monorepo with path deps
        if workspace_root and detect_path_deps(pyproject_path):
            projects = load_workspace(workspace_root)
            graph = WorkspaceGraph(workspace_root, projects)
            rewrite_map = build_rewrite_map(workspace_root, projects, graph)

            if rewrite_map:
                self._build_with_rewrite(dir_path, pyproject_path, rewrite_map, timeout=timeout)
                return

        # No monorepo or no path deps -- build in place
        run("uv", ["build", "--out-dir", "dist"], env=os.environ, cwd=dir_path, timeout=timeout)

    # Directories excluded when copying the project to a temp build dir
    _COPY_EXCLUDE = {".git", "__pycache__", ".rlsbl", ".rlsbl-monorepo", "dist"}

    def _build_with_rewrite(self, dir_path, pyproject_path, rewrite_map, *, timeout=120):
        """Copy project to a temp dir with rewritten deps, then build."""
        from ..dep_rewrite import rewrite_pyproject_deps

        with open(pyproject_path, "r", encoding="utf-8") as f:
            original_content = f.read()

        rewritten_content = rewrite_pyproject_deps(original_content, rewrite_map)

        tmp_dir = tempfile.mkdtemp(prefix="rlsbl-build-")
        try:
            def _ignore(directory, contents):
                # Only filter at the top level of the project
                if os.path.realpath(directory) == os.path.realpath(dir_path):
                    return [c for c in contents if c in self._COPY_EXCLUDE]
                # Skip __pycache__ and .pyc everywhere
                return [c for c in contents if c == "__pycache__" or c.endswith(".pyc")]

            tmp_project = os.path.join(tmp_dir, "project")
            shutil.copytree(dir_path, tmp_project, ignore=_ignore)

            # Overwrite pyproject.toml with rewritten version
            tmp_pyproject = os.path.join(tmp_project, "pyproject.toml")
            with open(tmp_pyproject, "w", encoding="utf-8") as f:
                f.write(rewritten_content)

            # Build in the temp dir, output to the real project's dist/
            dist_dir = os.path.abspath(os.path.join(dir_path, "dist"))
            os.makedirs(dist_dir, exist_ok=True)
            subprocess.run(
                ["uv", "build", "--out-dir", dist_dir],
                cwd=tmp_project,
                check=True,
                capture_output=True,
                text=True,
                env=os.environ,
                timeout=timeout,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "pyproject.toml"))

    def get_project_init_hint(self):
        return 'Run "uv init" first'

    def dev_install_command(self, project_dir):
        return {
            "global": {
                "tool": "uv",
                "purpose": "for editable Python install",
                "args": ["tool", "install", "-e", "."],
                "uninstall_args_template": ["tool", "uninstall", "{name}"],
            },
            # Local mode: sync the project's .venv with declared dependencies.
            # `uv sync` is idempotent; there is no symmetric uninstall.
            "venv": {
                "tool": "uv",
                "purpose": "for syncing the project venv",
                "args": ["sync", "--all-packages"],
                "uninstall_args_template": None,
            },
        }

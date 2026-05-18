"""PyPI release target that manages version tracking in pyproject.toml and scaffolds CI workflows for OIDC-based publishing to the PyPI index."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import tomlkit

from .base import BaseTarget
from ..config import get_publish_config
from ..utils import run

_MIN_VERSION_RE = re.compile(r">=\s*(\d+\.\d+(?:\.\d+)?)")


class PypiTarget(BaseTarget):
    """Release target for Python projects (pyproject.toml)."""

    @property
    def name(self):
        return "pypi"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "pyproject.toml"))

    def read_name(self, dir_path):
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

    def read_version(self, dir_path):
        """Read the version from pyproject.toml in the given directory."""
        toml_path = os.path.join(dir_path, "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        try:
            return data["project"]["version"]
        except KeyError:
            raise ValueError(f"No [project].version in {toml_path}")

    def write_version(self, dir_path, version):
        """Write a new version to pyproject.toml and __version__ in package source.

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        path = os.path.join(dir_path, "pyproject.toml")
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
        doc["project"]["version"] = version
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp_path, path)

        modified = ["pyproject.toml"]
        dunder_path = self._update_dunder_version(dir_path, doc, version)
        if dunder_path:
            modified.append(dunder_path)
        return modified

    def _update_dunder_version(self, dir_path, doc, version):
        """Update __version__ in the package's __init__.py if present.

        Returns the relative path (relative to dir_path) of the file that
        was modified, or None if no file was updated.
        """
        _VERSION_RE = re.compile(r'(__version__\s*=\s*["\'])[\d.]+(["\'])')

        name = doc.get("project", {}).get("name")
        if not name:
            return None

        pkg_name = name.replace("-", "_")
        candidates = [
            (os.path.join(dir_path, pkg_name, "__init__.py"),
             os.path.join(pkg_name, "__init__.py")),
            (os.path.join(dir_path, "src", pkg_name, "__init__.py"),
             os.path.join("src", pkg_name, "__init__.py")),
        ]

        for init_path, rel_path in candidates:
            if not os.path.isfile(init_path):
                continue
            with open(init_path, "r", encoding="utf-8") as f:
                content = f.read()
            new_content = _VERSION_RE.sub(rf'\g<1>{version}\g<2>', content)
            if new_content != content:
                tmp = init_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(new_content)
                os.replace(tmp, init_path)
                return rel_path
            break
        return None

    def version_file(self):
        return "pyproject.toml"

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "pypi"
        )

    def template_vars(self, dir_path):
        """Extract template variables from the target project's pyproject.toml."""
        toml_path = os.path.join(dir_path, "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        project = data.get("project", {})
        name = project.get("name", "")
        version = project.get("version", "0.1.0")

        # Extract author -- fall back to git config
        author = ""
        try:
            author = run("git", ["config", "user.name"])
        except Exception:
            pass

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

        # Derive the actual Python import name.
        # 1) Check hatch build config for an explicit packages list.
        import_name = None
        hatch = data.get("tool", {}).get("hatch", {})
        packages = (
            hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages")
        )
        if packages and isinstance(packages, list) and len(packages) > 0:
            # Strip src/ prefix (common hatch src layout) and convert path to module name
            first_pkg = packages[0]
            import_name = first_pkg.removeprefix("src/").replace("/", ".")

        # 2) Fall back to filesystem detection, then underscore convention.
        if not import_name:
            underscored = name.replace("-", "_")
            if os.path.isdir(os.path.join(dir_path, underscored)):
                import_name = underscored
            elif os.path.isdir(os.path.join(dir_path, name)):
                import_name = name
            else:
                import_name = underscored  # fallback to convention

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

        return result

    def template_mappings(self):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def build(self, dir_path, version):
        """Build the package, rewriting path deps if in a monorepo context.

        When the project has path dependencies (e.g., sibling packages in a
        monorepo), copies the project to a temp directory with rewritten
        pyproject.toml so the working tree is never modified.  Otherwise runs
        ``uv build`` in place.
        """
        from ..dep_rewrite import build_rewrite_map, detect_path_deps, rewrite_pyproject_deps
        from ..workspace import find_workspace_root, load_workspace
        from ..workspace_graph import WorkspaceGraph

        pyproject_path = os.path.join(dir_path, "pyproject.toml")
        workspace_root = find_workspace_root(dir_path)

        # Only attempt rewriting when inside a monorepo with path deps
        if workspace_root and detect_path_deps(pyproject_path):
            projects = load_workspace(workspace_root)
            graph = WorkspaceGraph(workspace_root, projects)
            rewrite_map = build_rewrite_map(workspace_root, projects, graph)

            if rewrite_map:
                self._build_with_rewrite(dir_path, pyproject_path, rewrite_map)
                return

        # No monorepo or no path deps -- build in place
        dist_dir = os.path.join(dir_path, "dist")
        run("uv", ["build", "--out-dir", dist_dir], env=os.environ)

    # Directories excluded when copying the project to a temp build dir
    _COPY_EXCLUDE = {".git", "__pycache__", ".rlsbl", ".rlsbl-monorepo", "dist"}

    def _build_with_rewrite(self, dir_path, pyproject_path, rewrite_map):
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
            dist_dir = os.path.join(dir_path, "dist")
            os.makedirs(dist_dir, exist_ok=True)
            subprocess.run(
                ["uv", "build", "--out-dir", dist_dir],
                cwd=tmp_project,
                check=True,
                capture_output=True,
                text=True,
                env=os.environ,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def publish(self, dir_path, version):
        """Publish to PyPI based on per-target config and token availability.

        Without config, accepts either PYPI_TOKEN or TWINE_PASSWORD. With
        config that sets token_var, only the named variable is consulted.
        """
        pub_config = get_publish_config(self.name)

        if pub_config.get("local") is False:
            print(f"Skipping local {self.name} publish (config: local=false). CI will handle it.")
            return

        token_var = pub_config.get("token_var")
        if token_var:
            token = os.environ.get(token_var)
            missing_msg = f"no {token_var}"
        else:
            token = os.environ.get("PYPI_TOKEN") or os.environ.get("TWINE_PASSWORD")
            missing_msg = "no PYPI_TOKEN"

        if not token:
            if pub_config.get("local") is True:
                effective_var = token_var or "PYPI_TOKEN"
                print(
                    f"ERROR: {self.name} publish requested (local=true) but {effective_var} is not set.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Skipping local PyPI publish ({missing_msg}). CI will handle it.")
            return

        try:
            run("uv", ["build"], env=os.environ)
            run("uv", ["publish"], env={
                **os.environ,
                "UV_PUBLISH_TOKEN": token,
            })
            print(f"Published to PyPI: {version}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"PyPI publish failed: {exc}") from exc

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "pyproject.toml"))

    def get_project_init_hint(self):
        return 'Run "uv init" first'

    def dev_install_command(self, project_dir):
        return {
            "tool": "uv",
            "purpose": "for editable Python install",
            "args": ["tool", "install", "-e", "."],
            "uninstall_args_template": ["tool", "uninstall", "{name}"],
        }

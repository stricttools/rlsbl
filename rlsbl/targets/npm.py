"""npm release target that manages version tracking in package.json and scaffolds CI workflows for automated publishing to the npm registry."""

import json
import os
import re

from .base import BaseTarget, TemplateVars
from ..errors import VersionError
from .. import effects

_MIN_VERSION_RE = re.compile(r">=\s*(\d+(?:\.\d+)*)")


class NpmTarget(BaseTarget):
    """Release target for npm/Node.js projects (package.json)."""

    detection_files = ("package.json",)
    lint_language = "npm"
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates", "dev_install", "publication_probe"})
    ecosystem = "Node.js / npm"

    @property
    def name(self):
        return "npm"

    def read_name(self, dir_path, ctx):
        """Read the package name from package.json."""
        pkg_path = os.path.join(dir_path, "package.json")
        if not os.path.exists(pkg_path):
            return None
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        return pkg.get("name")

    def read_metadata(self, dir_path):
        """Read license and description from package.json."""
        pkg_path = os.path.join(dir_path, "package.json")
        if not os.path.exists(pkg_path):
            return {}
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        result = {}
        license_val = pkg.get("license")
        if license_val:
            result["license"] = license_val
        description = pkg.get("description")
        if description:
            result["description"] = description
        return result

    def publication_probe(self, dir_path, version, ctx=None):
        """Probe npm registry for a specific version of this package."""
        from ..publication_probe import PublicationProbeResult, PublicationStatus

        pkg_name = self.read_name(dir_path, ctx)
        if not pkg_name:
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="npm",
                version=version,
                message="no package name in package.json",
            )

        # Query the registry for this specific version
        import json as _json
        import urllib.error
        from ..commands.check import _request_with_backoff

        url = f"https://registry.npmjs.org/{pkg_name}/{version}"
        try:
            with _request_with_backoff(url) as resp:
                _json.loads(resp.read())
            return PublicationProbeResult(
                status=PublicationStatus.PUBLISHED,
                registry="npm",
                version=version,
                message=f"{pkg_name}@{version} found on npm",
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return PublicationProbeResult(
                    status=PublicationStatus.UNPUBLISHED,
                    registry="npm",
                    version=version,
                    message=f"{pkg_name}@{version} not found on npm",
                )
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="npm",
                version=version,
                message=f"npm API error: HTTP {e.code}",
            )
        except Exception as e:
            return PublicationProbeResult(
                status=PublicationStatus.UNPROBEABLE,
                registry="npm",
                version=version,
                message=f"npm API error: {e}",
            )

    def _detect_package_manager(self, dir_path):
        """Detect the package manager by walking up from dir_path to the git root.

        Checks each directory for lock files in priority order:
        - pnpm-lock.yaml -> "pnpm"
        - yarn.lock -> "yarn"
        - package-lock.json -> "npm"

        Stops at the git root (.git directory) or filesystem root.
        Returns "npm" as fallback if no lock file is found.
        """
        current = os.path.abspath(dir_path)
        while True:
            for lockfile, pm in [
                ("pnpm-lock.yaml", "pnpm"),
                ("yarn.lock", "yarn"),
                ("package-lock.json", "npm"),
            ]:
                if os.path.exists(os.path.join(current, lockfile)):
                    return pm
            # Stop if we reached the git root
            if os.path.isdir(os.path.join(current, ".git")):
                break
            parent = os.path.dirname(current)
            if parent == current:
                # Filesystem root reached
                break
            current = parent
        return "npm"

    def read_version(self, dir_path):
        """Read the version from package.json in the given directory."""
        pkg_path = os.path.join(dir_path, "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        if "version" not in pkg:
            raise VersionError(f"No 'version' field in {pkg_path}")
        return pkg["version"]

    def write_version(self, dir_path, version, ctx):
        """Write a new version to package.json, preserving formatting.

        Returns a list of relative file paths that were modified.
        """
        pkg_path = os.path.join(dir_path, "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Detect indent: look for the first indented line
        indent_match = re.search(r'^( +|\t+)"', raw, re.MULTILINE)
        indent = indent_match.group(1) if indent_match else "  "

        pkg = json.loads(raw)
        pkg["version"] = version

        # Preserve trailing newline if present
        trailing_newline = "\n" if raw.endswith("\n") else ""
        output = json.dumps(pkg, indent=indent, ensure_ascii=False) + trailing_newline
        effects.atomic_write_text(pkg_path, output)
        return [self.version_file()]

    def version_file(self, dir_path=None):
        """Return the version file name for npm projects."""
        return "package.json"

    def tag_format(self, version):
        """Return the git tag string for an npm release version."""
        return f"v{version}"

    def template_dir(self):
        """Return the path to the npm-specific template directory."""
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "npm"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables from the target project's package.json."""
        pkg_path = os.path.join(dir_path, "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)

        # Derive binCommand from the bin field (first key if object, or package name)
        bin_command = pkg.get("name", "")
        bin_field = pkg.get("bin")
        if isinstance(bin_field, dict) and bin_field:
            bin_command = next(iter(bin_field))
        elif isinstance(bin_field, str):
            bin_command = pkg.get("name", "")

        # Derive repoName from repository field
        repo_name = ""
        repository = pkg.get("repository")
        if repository:
            url = repository if isinstance(repository, str) else (repository.get("url") or "")
            match = re.search(r"github\.com[/:]([^/]+/[^/.]+)", url)
            if match:
                repo_name = match.group(1)

        # Use publishConfig.registry from package.json if set, otherwise default
        publish_config = pkg.get("publishConfig", {})
        registry_url = publish_config.get("registry", "https://registry.npmjs.org")

        result = {
            "name": pkg.get("name", ""),
            "version": pkg.get("version", "0.1.0"),
            "binCommand": bin_command,
            "author": pkg.get("author", ""),
            "repoName": repo_name,
            "registryUrl": registry_url,
            "publishSetup": "Requires NPM_TOKEN secret on GitHub (Settings > Secrets > Actions)",
            "packageManager": self._detect_package_manager(dir_path),
        }

        engines = pkg.get("engines", {})
        node_engine = engines.get("node")
        if node_engine:
            m = _MIN_VERSION_RE.search(node_engine)
            if m:
                result["minRequiredNode"] = m.group(1)

        return TemplateVars(self.name, result)

    def template_mappings(self, ctx):
        """Return CI and npmignore template mappings, selecting the CI template by package manager."""
        pm = self._detect_package_manager(".")
        if pm == "pnpm":
            ci_template = "ci-pnpm.yml.tpl"
        elif pm == "yarn":
            ci_template = "ci-yarn.yml.tpl"
        else:
            ci_template = "ci.yml.tpl"
        return [
            {"template": ci_template, "target": ".github/workflows/ci.yml"},
            {"template": "npmignore.tpl", "target": ".npmignore"},
        ]

    def check_project_exists(self, dir_path):
        """Return True if package.json exists in dir_path."""
        return os.path.exists(os.path.join(dir_path, "package.json"))

    def get_project_init_hint(self):
        """Return a hint for initializing an npm project."""
        return 'Run "npm init" first'

    def dev_install_command(self, project_dir):
        """Return install specs for npm link (global) and npm install (local)."""
        return {
            "global": {
                "tool": "npm",
                "purpose": "for npm link",
                "args": ["link"],
                # `npm unlink` inside the package directory removes the global symlink.
                "uninstall_args_template": ["unlink"],
            },
            # Local mode: install dependencies into node_modules without creating
            # a global symlink. There is no clean automated uninstall.
            "venv": {
                "tool": "npm",
                "purpose": "for npm install",
                "args": ["install"],
                "uninstall_args_template": None,
            },
        }

    def run_tests(self, *, project_dir=None, workspace_root=None,
                  skip_sync=False, config=None, check_timeout=None):
        """Run the package's `npm test` script."""
        from ..testing import _run_npm_tests, resolve_test_timeout
        from .outcomes import SuiteRunOutcome, SuiteRunStatus

        timeout = resolve_test_timeout(config, check_timeout)
        passed = _run_npm_tests(project_dir=project_dir, check_timeout=timeout)
        return SuiteRunOutcome(
            status=SuiteRunStatus.PASSED if passed else SuiteRunStatus.FAILED,
            message=f"{self.name} tests {'passed' if passed else 'failed'}",
        )

    supports_dep_floors = True

    def find_dead_modules(self, root, *, exclude_dirs=None, suppress=frozenset()):
        """Breadth-first reachability from the package's entry points."""
        from ..dep_validation import find_dead_npm_modules

        return [
            (path, "not reachable from any entry point")
            for path in find_dead_npm_modules(root, exclude_dirs=exclude_dirs)
            if path not in suppress
        ]

    def find_circular_dependencies(self, root, *, exclude_dirs=None):
        """Detect circular imports between the package's source files."""
        from ..dep_validation import find_circular_npm_deps

        return find_circular_npm_deps(root, exclude_dirs=exclude_dirs)

    def normalize_package_name(self, raw_name):
        """npm folds a name by removing hyphens, underscores and dots."""
        from .utils import normalize_npm

        return normalize_npm(raw_name)

    def query_latest_version(self, name):
        """Ask the npm registry for the latest published version."""
        from ..registry import query_npm_version

        return query_npm_version(name)

    claim_token_env_vars = ("NPM_TOKEN",)

    def claim_placeholder(self, name, tmpdir):
        """Publish a version 0.0.0 package.json to reserve *name* on npm."""
        package_json = {
            "name": name,
            "version": "0.0.0",
            "description": "Name reservation",
        }
        with effects.open_write(os.path.join(tmpdir, "package.json"), "w") as f:
            json.dump(package_json, f, indent=2)
            f.write("\n")

        effects.run(
            ["npm", "publish", "--access", "public"],
            grant="publish",
            resource=f"npm:{name}",
            cwd=tmpdir,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return f"https://www.npmjs.com/package/{name}"

    def yank(self, project_dir, version, tag, *, reason=None, dry_run=False):
        """Deprecate a published version on the npm registry.

        npm has no delete; ``npm deprecate`` attaches a warning that every
        install of that exact version prints.
        """
        import subprocess

        from .outcomes import YankOutcome, YankStatus

        pkg_name = self.read_name(project_dir, None)
        if not pkg_name:
            return YankOutcome(
                status=YankStatus.INCOMPLETE,
                message="npm: cannot determine package name, skipping",
            )

        deprecation_msg = reason or "This version has been yanked."
        spec = f"{pkg_name}@{version}"

        if dry_run:
            return YankOutcome(
                status=YankStatus.DONE,
                message=f'npm: would run: npm deprecate {spec} "{deprecation_msg}"',
            )

        try:
            effects.run(
                ["npm", "deprecate", spec, deprecation_msg],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            return YankOutcome(
                status=YankStatus.INCOMPLETE,
                message=f"npm: deprecation failed: {(e.stderr or '').strip()}",
            )
        except FileNotFoundError:
            return YankOutcome(
                status=YankStatus.INCOMPLETE,
                message="npm: npm CLI not found",
            )
        return YankOutcome(status=YankStatus.DONE, message=f"npm: deprecated {spec}")

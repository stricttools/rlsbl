"""npm release target that manages version tracking in package.json and scaffolds CI workflows for automated publishing to the npm registry."""

import json
import os
import re

from .base import (
    PACKAGE_RENAME_MANIFEST_FIELD,
    BaseTarget,
    ManifestRenamePlan,
    TemplateVars,
)
from ..errors import ConfigError, VersionError
from ..scratch_dirs import RUNNER_CHOSEN_BY_PROJECT
from .. import effects

_MIN_VERSION_RE = re.compile(r">=\s*(\d+(?:\.\d+)*)")


def _npmrc_has_login(path):
    """Does the npm config file at *path* carry a registry login?"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if sep and value.strip() and key.strip().endswith(("_authToken", "_auth")):
            return True
    return False


def _missing_package_json_message(pkg_path, ctx):
    """The refusal for an npm target directory holding no ``package.json``."""
    project_root = getattr(ctx, "project_root", None)
    workspace_root = getattr(ctx, "workspace_root", None)
    shown = pkg_path
    owner = "this project"
    if workspace_root is not None and project_root is not None:
        from ..workspace import resolve_project

        member = resolve_project(str(workspace_root), str(project_root))
        shown = os.path.relpath(os.path.abspath(pkg_path), os.path.abspath(str(workspace_root)))
        if member is not None:
            owner = f"workspace member '{member.name}' ({member.path}/)"
    return (
        f"{owner} has an npm target, but {shown} does not exist, so there is "
        f"no package to scaffold or publish. Either create the package there "
        f"(npm init), or remove the npm entry from the targets list in the "
        f".rlsbl/config.json that declares it (a releasable member inherits its "
        f"releasable's targets from .rlsbl-monorepo/releasables/<name>/config.json)."
    )


class NpmTarget(BaseTarget):
    """Release target for npm/Node.js projects (package.json)."""

    detection_files = ("package.json",)
    lint_language = "npm"
    ecosystem = "Node.js / npm"

    # `npm test` runs the project's own test script, which names whichever
    # runner the project chose (jest, vitest, mocha, `node --test`, ...). Each
    # has its own configuration file, several of them executable JavaScript,
    # and rlsbl writes none of them -- so telling the runner to skip the
    # scratch directories would mean taking ownership of a file rlsbl does not
    # own and cannot merge into safely. It writes nothing instead.
    scratch_test_exclusion = RUNNER_CHOSEN_BY_PROJECT

    package_rename = PACKAGE_RENAME_MANIFEST_FIELD
    package_name_field = 'package.json "name"'

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

    def package_rename_plan(self, dir_path, old, new):
        """Rename package.json's ``name``, formatted as ``write_version`` writes it."""
        pkg_path = os.path.join(dir_path, "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        pkg = json.loads(raw)
        current = pkg.get("name")
        if current != old:
            return ManifestRenamePlan(pkg_path, current, 0, raw)
        indent_match = re.search(r'^( +|\t+)"', raw, re.MULTILINE)
        indent = indent_match.group(1) if indent_match else "  "
        pkg["name"] = new
        trailing_newline = "\n" if raw.endswith("\n") else ""
        new_text = json.dumps(pkg, indent=indent, ensure_ascii=False) + trailing_newline
        return ManifestRenamePlan(pkg_path, current, 1, new_text)

    def package_command_names(self, dir_path, name):
        """``bin`` entries named *name* (an object ``bin``; a string one follows the package name)."""
        pkg_path = os.path.join(dir_path, "package.json")
        with open(pkg_path, "r", encoding="utf-8") as f:
            bins = json.load(f).get("bin")
        if isinstance(bins, dict) and name in bins:
            return [(pkg_path, f'bin "{name}"')]
        return []

    def package_name_problems(self, name):
        """npm's rules for a new package's name."""
        from ..package_names import npm_name_problems

        return npm_name_problems(name)

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
        """Extract template variables from the target project's package.json.

        A target directory with no ``package.json`` is refused, naming the
        workspace member (when there is one) and the missing file.
        """
        pkg_path = os.path.join(dir_path, "package.json")
        if not os.path.isfile(pkg_path):
            raise ConfigError(_missing_package_json_message(pkg_path, ctx))
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

    def ci_template_vars(self, dir_path):
        """``npm.nodeMatrix``: every supported Node line ``engines.node`` admits.

        A package with no ``engines.node`` is refused: it states no Node
        support, and a matrix rlsbl picked would test versions nobody claimed.
        """
        from ..node_matrix import NodeMatrixError, project_node_matrix, render_matrix

        try:
            lines = project_node_matrix(dir_path)
        except NodeMatrixError as exc:
            raise ConfigError(str(exc)) from exc
        return {"npm.nodeMatrix": render_matrix(lines)}

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

    def claim_credentials(self):
        """``NPM_TOKEN`` when set, otherwise npm's own ``~/.npmrc`` login."""
        if os.environ.get("NPM_TOKEN"):
            return "NPM_TOKEN"
        if _npmrc_has_login(os.path.join(os.path.expanduser("~"), ".npmrc")):
            return "~/.npmrc"
        raise ConfigError(
            "no npm credentials to claim a name with: NPM_TOKEN is not set, and "
            "~/.npmrc holds no registry login (the _authToken line `npm login` "
            "writes). Log in with `npm login`, or set NPM_TOKEN."
        )

    def claim_placeholder(self, name, tmpdir):
        """Publish a version 0.0.0 package.json to reserve *name* on npm."""
        if self.claim_credentials() == "NPM_TOKEN":
            # npm reads a token only from an .npmrc. This one names the
            # variable, which npm expands at run time, so the secret is never
            # written to disk; with no token set, npm's own ~/.npmrc is used.
            with effects.open_write(os.path.join(tmpdir, ".npmrc"), "w") as f:
                f.write("//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n")
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

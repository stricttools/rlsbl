"""npm release target that manages version tracking in package.json and scaffolds CI workflows for automated publishing to the npm registry."""

import json
import os
import re

from .base import BaseTarget

_MIN_VERSION_RE = re.compile(r">=\s*(\d+(?:\.\d+)*)")


class NpmTarget(BaseTarget):
    """Release target for npm/Node.js projects (package.json)."""

    detection_files = ("package.json",)
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates", "dev_install"})
    ecosystem = "Node.js / npm"

    @property
    def name(self):
        return "npm"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "package.json"))

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
            raise ValueError(f"No 'version' field in {pkg_path}")
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
        # Atomic write: write to temp file, then rename
        tmp_path = pkg_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(output)
        os.replace(tmp_path, pkg_path)
        return [self.version_file()]

    def version_file(self, dir_path=None):
        return "package.json"

    def tag_format(self, version):
        return f"v{version}"

    def template_dir(self):
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

        return result

    def template_mappings(self, ctx):
        pm = self._detect_package_manager(".")
        if pm == "pnpm":
            ci_template = "ci-pnpm.yml.tpl"
            publish_template = "publish-pnpm.yml.tpl"
        elif pm == "yarn":
            ci_template = "ci-yarn.yml.tpl"
            publish_template = "publish-yarn.yml.tpl"
        else:
            ci_template = "ci.yml.tpl"
            publish_template = "publish.yml.tpl"
        return [
            {"template": ci_template, "target": ".github/workflows/ci.yml"},
            {"template": publish_template, "target": ".github/workflows/publish.yml"},
            {"template": "npmignore.tpl", "target": ".npmignore"},
        ]

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "package.json"))

    def get_project_init_hint(self):
        return 'Run "npm init" first'

    def dev_install_command(self, project_dir):
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

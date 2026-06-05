"""Plain release target for projects with no build system, using a VERSION file for version tracking with tagging and GitHub Releases only."""

import os

import tomlkit

from .base import BaseTarget

VERSION_FILE = "VERSION"

# Primary manifest files for all other targets. If any of these exist,
# the directory belongs to a more specific target and plain should not
# auto-detect.
_OTHER_TARGET_MANIFESTS = (
    "package.json",        # npm
    "pyproject.toml",      # pypi
    "go.mod",              # go
    "Cargo.toml",          # cargo
    "pubspec.yaml",        # dart, flutter
    "Package.swift",       # swift, swift-apple
    "mix.exs",             # hex
    "deno.json",           # deno
    "deno.jsonc",          # deno
    "Dockerfile",          # docker
    "build.gradle.kts",    # maven
    "build.gradle",        # maven
    "pom.xml",             # maven
    "build.zig.zon",       # zig
    "build.zig",           # zig
    "pgdesign.toml",       # pgdesign
    "selfdoc.json",        # docs
    "version.json",        # spec
)


class PlainTarget(BaseTarget):
    """Release target for projects that have no build system or package registry."""

    capabilities = frozenset()
    ecosystem = "Plain"
    auto_detectable = "conditional"

    # Plain is opt-in only; no detection files to avoid false positives
    # (VERSION is too generic -- every Go/Swift/Docker project has one).

    @property
    def name(self):
        return "plain"

    def detect(self, dir_path):
        # Auto-detect when a VERSION file exists and no other target's
        # primary manifest is present.
        if not os.path.exists(os.path.join(dir_path, VERSION_FILE)):
            return False
        for manifest in _OTHER_TARGET_MANIFESTS:
            if os.path.exists(os.path.join(dir_path, manifest)):
                return False
        return True

    def read_version(self, dir_path):
        """Read version from the VERSION file."""
        version_path = os.path.join(dir_path, VERSION_FILE)
        if not os.path.exists(version_path):
            raise FileNotFoundError(
                f"No {VERSION_FILE} file found. Run 'rlsbl scaffold' first."
            )
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def write_version(self, dir_path, version, ctx):
        """Write the new version to the VERSION file and pyproject.toml atomically.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        tmp_path = version_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.replace(tmp_path, version_path)

        modified = [self.version_file()]

        # Also bump pyproject.toml if it exists and has [project].version
        pyproject_path = os.path.join(dir_path, "pyproject.toml")
        if os.path.exists(pyproject_path):
            with open(pyproject_path, "r", encoding="utf-8") as f:
                doc = tomlkit.parse(f.read())
            project = doc.get("project")
            if project is not None and "version" in project:
                doc["project"]["version"] = version
                tmp_pyproject = pyproject_path + ".tmp"
                with open(tmp_pyproject, "w", encoding="utf-8") as f:
                    f.write(tomlkit.dumps(doc))
                os.replace(tmp_pyproject, pyproject_path)
                modified.append("pyproject.toml")

        return modified

    def version_file(self, dir_path=None):
        return VERSION_FILE

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "plain"
        )

    def template_mappings(self, ctx):
        return [{"template": "VERSION.tpl", "target": "VERSION"}]

    def check_project_exists(self, dir_path):
        # Plain targets are always valid -- scaffold creates the VERSION file.
        return True

    def template_vars(self, dir_path, ctx):
        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"
        return {
            "name": os.path.basename(os.path.abspath(dir_path)),
            "version": version,
        }

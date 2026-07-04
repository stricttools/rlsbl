"""Dart release target that manages version tracking in pubspec.yaml and scaffolds CI workflows for publishing to pub.dev."""

import os

from ruamel.yaml import YAML

from .base import BaseTarget
from ..errors import VersionError


class DartTarget(BaseTarget):
    """Release target for Dart packages (pubspec.yaml)."""

    detection_files = ("pubspec.yaml",)
    capabilities = frozenset({"read_name", "read_metadata", "ci_templates"})
    ecosystem = "Dart / pub.dev"

    @property
    def name(self):
        return "dart"

    def detect(self, dir_path):
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return False
        # A pubspec.yaml with a flutter: section is a Flutter project,
        # not a plain Dart project. Leave those for a future Flutter target.
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if not isinstance(data, dict):
            return False
        return "flutter" not in data

    def read_version(self, dir_path):
        """Read the version from pubspec.yaml, stripping any +N build number."""
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        version = data.get("version")
        if not version:
            raise VersionError(f"No 'version' field in {pubspec}")
        version = str(version)
        # Strip build number: "1.2.3+4" -> "1.2.3"
        return version.split("+")[0]

    def write_version(self, dir_path, version, ctx):
        """Write a new version to pubspec.yaml, preserving comments and formatting.

        Handles build number (+N) based on .rlsbl/config.json:
        - build_number.enabled=true, strategy="increment": increment +N
        - Otherwise: preserve existing +N if present

        Returns a list of relative file paths that were modified.
        """
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        yaml = YAML()
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)

        old_version = str(data.get("version", ""))
        new_version = self._compute_version_with_build_number(old_version, version, dir_path, ctx=ctx)

        data["version"] = new_version

        tmp_path = pubspec + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        os.replace(tmp_path, pubspec)
        return [self.version_file()]

    def _compute_version_with_build_number(self, old_version, new_semver, dir_path, ctx):
        """Determine the full version string including build number handling."""
        config = ctx.config
        build_config = config.get("build_number", {})
        enabled = build_config.get("enabled", False)
        strategy = build_config.get("strategy", "increment")

        # Extract existing build number from old version
        old_build = None
        if "+" in old_version:
            old_build = old_version.split("+", 1)[1]

        if enabled and strategy == "increment":
            current_n = int(old_build) if old_build and old_build.isdigit() else 0
            return f"{new_semver}+{current_n + 1}"

        # Not enabled: preserve existing build number if present
        if old_build is not None:
            return f"{new_semver}+{old_build}"
        return new_semver

    def version_file(self, dir_path=None):
        return "pubspec.yaml"

    def read_name(self, dir_path, ctx):
        """Read the package name from pubspec.yaml."""
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return None
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if not isinstance(data, dict):
            return None
        return data.get("name")

    def read_metadata(self, dir_path):
        """Read description and license from pubspec.yaml."""
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return {}
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if not isinstance(data, dict):
            return {}
        result = {}
        description = data.get("description")
        if description:
            result["description"] = description
        # Dart uses a top-level "license" field (SPDX identifier string
        # in pubspec.yaml for Dart 3+, or inferred from LICENSE file).
        license_val = data.get("license")
        if license_val:
            result["license"] = license_val
        return result

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "dart"
        )

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

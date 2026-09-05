"""Dart release target that manages version tracking in pubspec.yaml and scaffolds CI workflows for publishing to pub.dev."""

import io
import os

from ruamel.yaml import YAML

from .base import BaseTarget
from ..errors import VersionError
from .. import effects


class DartTarget(BaseTarget):
    """Release target for Dart packages (pubspec.yaml)."""

    detection_files = ("pubspec.yaml",)
    ecosystem = "Dart / pub.dev"

    # Repo-relative files this ecosystem treats as an entry point by naming
    # convention alone, on top of the two the manifest yields (the
    # ``lib/<package>.dart`` barrel and every ``bin/*.dart`` script). Empty for
    # plain Dart, where the manifest is the only authority; Flutter overrides it.
    _convention_entry_points: tuple[str, ...] = ()

    @property
    def name(self):
        return "dart"

    def find_dead_modules(self, root, *, exclude_dirs=None, suppress=frozenset()):
        """Breadth-first reachability from the package's entry points."""
        from ..dep_validation import find_dead_dart_modules

        return [
            (path, "not reachable from any entry point")
            for path in find_dead_dart_modules(
                root,
                exclude_dirs=exclude_dirs,
                extra_entry_points=self._convention_entry_points,
            )
            if path not in suppress
        ]

    def find_circular_dependencies(self, root, *, exclude_dirs=None):
        """Detect circular imports between the package's Dart sources."""
        from ..dep_validation import find_circular_dart_deps

        return find_circular_dart_deps(root, exclude_dirs=exclude_dirs)

    def detect(self, dir_path):
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return False
        # A pubspec.yaml with a flutter: section is a Flutter project, not a
        # plain Dart project: FlutterTarget claims it, with the complementary
        # detection (``"flutter" in data``), so exactly one of the two answers.
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

        buffer = io.StringIO()
        yaml.dump(data, buffer)
        effects.atomic_write_text(pubspec, buffer.getvalue())
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

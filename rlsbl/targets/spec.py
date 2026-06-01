"""Spec release target using version.json as the source of truth for specification projects where the tagged GitHub Release is the publication."""

import json
import os

from .base import BaseTarget


class SpecTarget(BaseTarget):
    """Release target for specification projects (version.json).

    Expects version.json with at least {"version": "X.Y.Z"}.
    Extra fields are preserved on version bumps.
    """

    @property
    def name(self):
        return "spec"

    def read_name(self, dir_path, ctx):
        """Return the directory name as the project name."""
        return os.path.basename(os.path.abspath(dir_path))

    def read_metadata(self, dir_path):
        """Spec projects have no standard metadata in version.json."""
        return {}

    def detect(self, dir_path):
        """True if version.json exists in root or spec/ subdir."""
        return (
            os.path.exists(os.path.join(dir_path, "version.json"))
            or os.path.exists(os.path.join(dir_path, "spec", "version.json"))
        )

    def _version_json_path(self, dir_path):
        """Resolve the actual path to version.json."""
        root_path = os.path.join(dir_path, "version.json")
        if os.path.exists(root_path):
            return root_path
        spec_path = os.path.join(dir_path, "spec", "version.json")
        if os.path.exists(spec_path):
            return spec_path
        return root_path  # default to root for creation

    def read_version(self, dir_path):
        """Read version from version.json."""
        path = self._version_json_path(dir_path)
        if not os.path.exists(path):
            raise FileNotFoundError(
                "No version.json file found. Run 'rlsbl scaffold' first."
            )
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["version"]

    def write_version(self, dir_path, version, ctx):
        """Write the new version to version.json atomically.

        Returns a list of relative file paths (relative to dir_path) that
        were modified.
        """
        path = self._version_json_path(dir_path)
        # Read existing data to preserve other fields
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        data["version"] = version
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        return [os.path.relpath(path, dir_path)]

    def version_file(self):
        return "version.json"

    def tag_format(self, version):
        return f"spec-v{version}"

    def publish(self, dir_path, version, ctx):
        """No-op: the git tag IS the publication."""
        pass

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "spec"
        )

    def template_vars(self, dir_path, ctx):
        """Extract template variables."""
        # Name from directory
        dir_name = os.path.basename(os.path.abspath(dir_path))

        try:
            version = self.read_version(dir_path)
        except (FileNotFoundError, KeyError):
            version = "0.0.0"

        return {
            "name": dir_name,
            "version": version,
        }

    def template_mappings(self):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

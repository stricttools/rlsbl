"""Swift Apple-platform release target requiring explicit config declaration, using a VERSION file for versioning and macOS-only CI runners."""

import os
import re

from .base import BaseTarget
from ..utils import run

VERSION_FILE = "VERSION"


class SwiftAppleTarget(BaseTarget):
    """Release target for Apple-platform Swift projects (macOS-only CI)."""

    @property
    def name(self):
        return "swift-apple"

    def detect(self, dir_path):
        """Never auto-detect; must be declared in .rlsbl/config.json."""
        return False

    def read_name(self, dir_path):
        """Extract name from Package.swift."""
        package_path = os.path.join(dir_path, "Package.swift")
        if not os.path.exists(package_path):
            return None
        with open(package_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'name:\s*"([^"]+)"', content)
        return match.group(1) if match else None

    def read_metadata(self, dir_path):
        """Swift packages have no standard license/description in Package.swift."""
        return {}

    def read_version(self, dir_path):
        """Read version from the VERSION file."""
        version_path = os.path.join(dir_path, VERSION_FILE)
        if not os.path.exists(version_path):
            raise FileNotFoundError(
                f"No {VERSION_FILE} file found. Run 'rlsbl scaffold' first."
            )
        with open(version_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def write_version(self, dir_path, version):
        """Write the new version to the VERSION file atomically.

        Returns a list of relative file paths that were modified.
        """
        version_path = os.path.join(dir_path, VERSION_FILE)
        tmp_path = version_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.replace(tmp_path, version_path)
        return [self.version_file()]

    def version_file(self):
        return VERSION_FILE

    def publish(self, dir_path, version, project_root):
        """No-op: the git tag IS the publication for SPM."""
        pass

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "swift-apple"
        )

    def template_vars(self, dir_path, project_root):
        """Extract template variables from Package.swift."""
        package_name = ""
        package_path = os.path.join(dir_path, "Package.swift")
        if os.path.exists(package_path):
            with open(package_path, "r", encoding="utf-8") as f:
                content = f.read()
            match = re.search(r'name:\s*"([^"]+)"', content)
            if match:
                package_name = match.group(1)

        author = ""
        try:
            author = run("git", ["config", "user.name"])
        except Exception:
            pass

        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"

        return {
            "name": package_name,
            "version": version,
            "author": author,
        }

    def template_mappings(self):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Package.swift"))

    def get_project_init_hint(self):
        return "Add 'swift-apple' to the 'targets' array in .rlsbl/config.json"

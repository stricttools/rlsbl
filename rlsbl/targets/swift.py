"""Swift release target using a VERSION file as the source of truth, with tag-based publishing since SPM resolves packages by git tag directly."""

import os
import re

from .base import BaseTarget
from ..utils import run

VERSION_FILE = "VERSION"


class SwiftTarget(BaseTarget):
    """Release target for Swift packages (Package.swift + VERSION file)."""

    detection_files = ("Package.swift",)

    @property
    def name(self):
        return "swift"

    def detect(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Package.swift"))

    def read_name(self, dir_path, ctx):
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

    def write_version(self, dir_path, version, ctx):
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

    def tag_format(self, version):
        return f"v{version}"

    def publish(self, dir_path, version, ctx):
        """No-op: the git tag IS the publication for SPM."""
        pass

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "swift"
        )

    def template_vars(self, dir_path, ctx):
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

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def check_project_exists(self, dir_path):
        return os.path.exists(os.path.join(dir_path, "Package.swift"))

    def get_project_init_hint(self):
        return 'Run "swift package init" first'

    def dev_install_command(self, project_dir):
        return {
            "global": {
                "tool": "swift",
                "purpose": "for swift build",
                "args": ["build"],
                "uninstall_args_template": None,
            },
            # Swift Package Manager has no per-project venv concept; deps live
            # in .build/ which is populated as a side effect of `swift build`.
            "venv": None,
        }

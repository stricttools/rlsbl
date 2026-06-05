"""Swift Apple-platform release target requiring explicit config declaration, using a VERSION file for versioning and macOS-only CI runners."""

import os

from .swift import SwiftTarget


class SwiftAppleTarget(SwiftTarget):
    """Release target for Apple-platform Swift projects (macOS-only CI).

    Inherits versioning, publishing, and Package.swift parsing from SwiftTarget.
    Overrides detection (opt-in only), template directory (macOS CI), and
    dev install (not supported).
    """

    # Opt-in only; shares Package.swift with swift target.
    detection_files = ()
    capabilities = frozenset({"read_name", "ci_templates"})
    ecosystem = "Swift (Apple)"
    auto_detectable = "no"

    @property
    def name(self):
        return "swift-apple"

    def detect(self, dir_path):
        """Never auto-detect; must be declared in .rlsbl/config.json."""
        return False

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "swift-apple"
        )

    def get_project_init_hint(self):
        return "Add 'swift-apple' to the 'targets' array in .rlsbl/config.json"

    def dev_install_command(self, project_dir):
        return {"global": None, "venv": None}

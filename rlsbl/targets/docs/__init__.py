"""Docs target -- thin wrapper delegating to the selfdoc CLI so rlsbl can detect, build, and deploy selfdoc documentation sites without duplicating any of selfdoc's logic.

Detection is based on the presence of selfdoc.json in the project root.
Build and deploy are delegated entirely to the `selfdoc` CLI tool.
"""

import os
import subprocess
import sys

from ..base import BaseTarget
from ...config import get_publish_config
from ...utils import require_tool


class DocsTarget(BaseTarget):
    """Release target that delegates documentation to selfdoc."""

    @property
    def name(self):
        return "docs"

    def _selfdoc_available(self):
        """Return True if the selfdoc CLI is installed and runnable."""
        return require_tool("selfdoc", fatal=False) is not None

    def detect(self, dir_path):
        """True if selfdoc.json exists in the given directory."""
        return os.path.exists(os.path.join(dir_path, "selfdoc.json"))

    def read_version(self, dir_path):
        """Docs don't have their own version -- return fallback."""
        return "0.0.0"

    def write_version(self, dir_path, version):
        """No-op: docs inherit version from primary target."""
        return []

    def version_file(self):
        """No version file -- docs inherit from primary target."""
        return None

    def tag_format(self, version):
        """No separate tag -- uses primary target's tag."""
        return None

    def build(self, dir_path, version):
        """Delegate to selfdoc build."""
        if not self._selfdoc_available():
            return
        subprocess.run(["selfdoc", "build"], cwd=dir_path, check=True, timeout=300)

    def publish(self, dir_path, version):
        """Delegate to selfdoc deploy, gated by per-target config and credentials.

        Behaviour:
        - config local=false: skip with message
        - config local=true and selfdoc missing: error
        - config local=true and selfdoc present: run `selfdoc deploy`
        - no config: only attempt if both CF_ACCOUNT_ID and CF_PAGES_API_TOKEN
          are set (and selfdoc is available); otherwise skip silently
        """
        pub_config = get_publish_config(self.name)

        if pub_config.get("local") is False:
            print(f"Skipping local {self.name} publish (config: local=false). CI will handle it.")
            return

        if pub_config.get("local") is True:
            if not self._selfdoc_available():
                print(
                    f"ERROR: {self.name} publish requested (local=true) but 'selfdoc' is not on PATH.",
                    file=sys.stderr,
                )
                sys.exit(1)
            subprocess.run(["selfdoc", "deploy"], cwd=dir_path, check=True, timeout=300)
            return

        # No explicit config: only attempt when credentials are present.
        cf_account = os.environ.get("CF_ACCOUNT_ID")
        cf_token = os.environ.get("CF_PAGES_API_TOKEN")
        if not cf_account or not cf_token:
            print(
                "Skipping local docs publish (CF_ACCOUNT_ID/CF_PAGES_API_TOKEN not set). "
                "Post-release hook or CI will handle it."
            )
            return

        if not self._selfdoc_available():
            print("Skipping local docs publish ('selfdoc' not on PATH).")
            return

        subprocess.run(["selfdoc", "deploy"], cwd=dir_path, check=True, timeout=300)

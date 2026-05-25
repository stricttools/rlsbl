"""Docs target -- thin wrapper delegating to the selfdoc CLI so rlsbl can detect, build, and deploy selfdoc documentation sites without duplicating any of selfdoc's logic.

Detection is based on the presence of selfdoc.json in the project root.
Build and deploy are delegated entirely to the `selfdoc` CLI tool.
"""

import json
import os
import subprocess
import sys
import tempfile

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
        """Read version from selfdoc.json; fall back to '0.0.0' when absent."""
        config_path = os.path.join(dir_path, "selfdoc.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("version", "0.0.0")
        except (OSError, json.JSONDecodeError):
            return "0.0.0"

    def write_version(self, dir_path, version):
        """Write version to selfdoc.json atomically, preserving formatting."""
        config_path = os.path.join(dir_path, "selfdoc.json")
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        data["version"] = version
        versions = data.get("versions")
        if versions and isinstance(versions, list):
            versions[-1]["version"] = version
        # Detect indent from existing file
        indent = 2
        for line in raw.splitlines()[1:]:
            stripped = line.lstrip()
            if stripped:
                indent = len(line) - len(stripped)
                break
        new_content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=dir_path, prefix=".selfdoc.json.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, config_path)
        except BaseException:
            os.unlink(tmp_path)
            raise
        return ["selfdoc.json"]

    def version_file(self):
        """Version is stored in selfdoc.json."""
        return "selfdoc.json"

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

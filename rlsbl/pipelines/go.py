"""Go pipeline that notifies the Go module proxy of new versions, verifies module availability, and optionally installs the binary locally."""

import glob
import os
import re
import subprocess

from .base import BasePipeline
from ..utils import require_tool, run


class GoPipeline(BasePipeline):
    """Pipeline that notifies the Go module proxy for new versions.

    Go modules don't use token-based publishing. The proxy is notified
    via ``go list -m``, and CLI binaries are installed locally via
    ``go install``.
    """

    def template_dir(self) -> str | None:
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "go"
        )

    def template_mappings(self, ctx) -> list[dict[str, str]]:
        return [
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
        ]

    def build_assets(self, dir_path: str, version: str, dist_dir: str, ctx) -> list[str]:
        from .build import build_go_assets
        return build_go_assets(dir_path, version, dist_dir)

    def publish(self, dir_path: str, version: str, ctx) -> None:
        if not self.local:
            print(f"  Skipping pipeline '{self.name}' local publish (config: local=false)")
            return

        module_path = self._read_module_path(dir_path)
        if not module_path:
            print("Warning: could not read module path from go.mod, skipping proxy notification")
            return

        if not require_tool("go", fatal=False):
            print("Warning: 'go' not found on PATH, skipping proxy notification")
            return

        ref = f"{module_path}@v{version}"
        env = {**os.environ, "GOPROXY": "proxy.golang.org"}
        try:
            run("go", ["list", "-m", ref], env=env)
            print(f"Notified Go module proxy: {ref}")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"Warning: proxy notification failed for {ref}: {exc}")

        # Install the binary locally for CLI projects
        install_path = self._detect_install_path(dir_path)
        if install_path:
            try:
                subprocess.run(
                    ["go", "install", install_path],
                    cwd=dir_path,
                    check=True,
                )
                print(f"Installed: go install {install_path}")
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                print(f"Warning: go install failed: {exc}")

    def required_env_vars(self) -> list[str]:
        # Go uses GITHUB_TOKEN which is always available in CI
        return []

    def _read_module_path(self, dir_path: str) -> str:
        """Extract the module path from go.mod."""
        mod_path = os.path.join(dir_path, "go.mod")
        if not os.path.exists(mod_path):
            return ""
        with open(mod_path, encoding="utf-8") as f:
            content = f.read()
        match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
        return match.group(1) if match else ""

    def _detect_install_path(self, dir_path: str) -> str | None:
        """Determine the go install path for CLI projects."""
        # Check cmd/ layout first
        matches = glob.glob(os.path.join(dir_path, "cmd", "*", "main.go"))
        if matches:
            cmd_dirs = set(os.path.dirname(m) for m in matches)
            if len(cmd_dirs) == 1:
                with open(matches[0], encoding="utf-8") as f:
                    first_line = f.readline()
                if re.match(r"^package\s+main\b", first_line):
                    cmd_name = os.path.basename(os.path.dirname(matches[0]))
                    return f"./cmd/{cmd_name}"

        # Check root main.go
        main_go = os.path.join(dir_path, "main.go")
        if os.path.exists(main_go):
            with open(main_go, encoding="utf-8") as f:
                first_line = f.readline()
            if re.match(r"^package\s+main\b", first_line):
                return "."

        return None

"""Go pipeline that notifies the Go module proxy of new versions, verifies module availability, and installs declared binaries locally."""

import os
import subprocess

from .base import BasePipeline
from ..errors import ConfigError
from ..go_introspect import (
    describe_main_packages,
    list_main_packages,
    validate_install_paths,
)
from ..utils import read_go_module_path, require_tool, run


class GoPipeline(BasePipeline):
    """Pipeline that notifies the Go module proxy for new versions.

    Go modules don't use token-based publishing. The proxy is notified
    via ``go list -m``, and CLI binaries are installed locally via
    ``go install`` for every path declared in the pipeline's
    ``install_paths`` config key.

    All failures raise -- the outer release flow decides fatality.
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

        module_path = read_go_module_path(dir_path)
        if module_path is None:
            raise ConfigError(
                f"could not read module path from go.mod in {dir_path}"
            )

        require_tool("go", purpose="for Go module proxy notification and go install")

        ref = f"{module_path}@v{version}"
        env = {**os.environ, "GOPROXY": "proxy.golang.org"}
        try:
            run("go", ["list", "-m", ref], env=env)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Go module proxy notification failed for {ref}: {exc}") from exc
        print(f"Notified Go module proxy: {ref}")

        # Install the declared binaries locally. install_paths is mandatory
        # for local go pipelines -- detection only validates declarations.
        install_paths = self.config.get("install_paths")
        if install_paths is None:
            mains = list_main_packages(dir_path)
            raise ConfigError(
                f"go pipeline '{self.name}' has local=true but does not declare "
                "'install_paths' in .rlsbl/config.json. "
                f"{describe_main_packages(mains)} "
                'Add e.g. "install_paths": '
                f"{[p.rel_dir for p in mains] or ['./cmd/<name>']!r} "
                "to the pipeline entry."
            )
        paths = validate_install_paths(dir_path, install_paths)
        for path in paths:
            try:
                subprocess.run(
                    ["go", "install", path],
                    cwd=dir_path,
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                raise RuntimeError(f"go install failed for {path}: {exc}") from exc
            print(f"Installed: go install {path}")

    def required_env_vars(self) -> list[str]:
        # Go uses GITHUB_TOKEN which is always available in CI
        return []

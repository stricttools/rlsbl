"""Zig release target using a VERSION file as the source of truth with automatic build.zig.zon synchronization and cross-compilation CI setup."""

import os
import re

from .base import BaseTarget, TemplateVars
from .zig_version import read_zig_version, write_zig_version
from ..npm_wrapper import (
    build_artifacts,
    build_npm_publish_jobs,
    load_platform_config,
    npm_wrapper_enabled,
    npm_wrapper_template_mappings,
)

VERSION_FILE = "VERSION"
ZON_FILE = "build.zig.zon"
BUILD_ZIG_FILE = "build.zig"

_ZON_NAME_RE = re.compile(r'\.name\s*=\s*"([^"]+)"')
_ZON_MIN_ZIG_RE = re.compile(r'\.minimum_zig_version\s*=\s*"([^"]+)"')
_BUILD_EXE_RE = re.compile(r'(?:exe\(|addExecutable\()')

# Zig cross-compilation target triples for each npm platform.
ZIG_TARGET_MAP: dict[str, str] = {
    "linux-x64": "x86_64-linux",
    "linux-arm64": "aarch64-linux",
    "darwin-x64": "x86_64-macos",
    "darwin-arm64": "aarch64-macos",
    "win32-x64": "x86_64-windows",
    "win32-arm64": "aarch64-windows",
}


def _zig_archive_fn(spec, name):
    """Return (asset_pattern, extract_cmd, binary_name) for a Zig build.

    Zig produces raw binaries (not archives), so extract_cmd is always None.
    """
    triple = ZIG_TARGET_MAP[spec.npm_platform]
    is_windows = "win32" in spec.npm_platform
    exe_suffix = ".exe" if is_windows else ""
    asset = f"{name}-{triple}{exe_suffix}"
    binary = f"{name}{exe_suffix}"
    return (asset, None, binary)


class ZigTarget(BaseTarget):
    """Release target for Zig projects (build.zig.zon + VERSION file)."""

    detection_files = ("build.zig.zon", "build.zig")
    capabilities = frozenset({"read_name", "ci_templates", "dev_install"})
    ecosystem = "Zig"

    @property
    def name(self):
        return "zig"

    def detect(self, dir_path):
        """Return True if build.zig.zon exists, or build.zig as secondary."""
        if os.path.exists(os.path.join(dir_path, ZON_FILE)):
            return True
        return os.path.exists(os.path.join(dir_path, BUILD_ZIG_FILE))

    def read_version(self, dir_path):
        """Read version, delegating to zig_version helpers."""
        return read_zig_version(dir_path)

    def write_version(self, dir_path, version, ctx):
        """Write version, delegating to zig_version helpers.

        Returns a list of relative file paths that were modified.
        """
        return write_zig_version(dir_path, version)

    def version_file(self, dir_path=None):
        return VERSION_FILE

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "zig"
        )

    def read_name(self, dir_path, ctx):
        """Extract .name from build.zig.zon, or None."""
        return self._read_zon_field(dir_path, _ZON_NAME_RE)

    def _read_zon_field(self, dir_path, pattern):
        """Read a single regex-captured field from build.zig.zon."""
        zon_path = os.path.join(dir_path, ZON_FILE)
        if not os.path.exists(zon_path):
            return None
        with open(zon_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = pattern.search(content)
        return match.group(1) if match else None

    def _is_library(self, dir_path):
        """Heuristic: library if build.zig doesn't contain exe( or addExecutable(."""
        build_path = os.path.join(dir_path, BUILD_ZIG_FILE)
        if not os.path.exists(build_path):
            return True
        with open(build_path, "r", encoding="utf-8") as f:
            content = f.read()
        return not bool(_BUILD_EXE_RE.search(content))

    def template_vars(self, dir_path, ctx):
        """Extract template variables from build.zig.zon and build.zig."""
        config = ctx.config if ctx else {}
        name = self._read_zon_field(dir_path, _ZON_NAME_RE)
        if not name:
            name = os.path.basename(os.path.abspath(dir_path))

        try:
            version = self.read_version(dir_path)
        except FileNotFoundError:
            version = "0.0.0"

        min_zig = self._read_zon_field(dir_path, _ZON_MIN_ZIG_RE)
        if not min_zig:
            min_zig = "0.14.0"

        is_library = self._is_library(dir_path)

        # npm binary wrapper support -- explicit enabled gate; hard-errors
        # if the removed scope/npm_scope key is present.
        npm_wrapper_on = npm_wrapper_enabled(config or {})

        npm_publish_jobs = ""
        if npm_wrapper_on and not is_library:
            specs = load_platform_config(config or {})
            artifacts = build_artifacts(specs, name, _zig_archive_fn)
            npm_publish_jobs = build_npm_publish_jobs(
                name, artifacts, depends_on="build-and-upload"
            )

        return TemplateVars(self.name, {
            "name": name,
            "version": version,
            "minRequiredZig": min_zig,
            "projectName": name,
            "isLibrary": is_library,
            "npmPublishJobs": npm_publish_jobs,
        })

    def template_mappings(self, ctx):
        return [
            {"template": "VERSION.tpl", "target": "VERSION"},
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]

    def shared_template_mappings(self, ctx):
        mappings = super().shared_template_mappings(ctx)
        if not self._is_library(str(ctx.project_root)):
            config = ctx.config if ctx else {}
            if npm_wrapper_enabled(config):
                mappings.extend(npm_wrapper_template_mappings())
        return mappings

    def check_project_exists(self, dir_path):
        return self.detect(dir_path)

    def get_project_init_hint(self):
        return 'Run "zig init" first, or create a build.zig.zon manually'

    def dev_install_command(self, project_dir):
        return {
            "global": {
                "tool": "zig",
                "purpose": "for zig build install",
                "args": ["build", "install"],
                "uninstall_args_template": None,
            },
            # Zig has no per-project venv concept; deps come via build.zig.zon.
            "venv": None,
        }

"""Release target discovery and registry that maps ecosystem identifiers (npm, pypi, go, cargo, deno, zig, swift, hex, docker, maven, plain) to their corresponding target classes for version bumps and scaffolding."""

import os
import sys
from typing import NamedTuple

from .npm import NpmTarget
from .pypi import PypiTarget
from .go import GoTarget
from .swift import SwiftTarget
from .swift_apple import SwiftAppleTarget
from .spec import SpecTarget
from .hex import HexTarget
from .deno import DenoTarget
from .cargo import CargoTarget
from .dart import DartTarget
from .docker import DockerTarget
from .flutter import FlutterTarget
from .maven import MavenTarget
from .native_android import NativeAndroidTarget
from .native_ios import NativeIosTarget
from .zig import ZigTarget
from .pgdesign import PgdesignTarget
from .plain import PlainTarget
from .protocol import ReleaseTarget
from .base import BaseTarget
from ..config import read_project_config
from ..errors import ConfigError


class TargetEntry(NamedTuple):
    """A detected target with its directory path."""
    name: str
    path: str

# Instantiated targets dict (replaces the old module-based REGISTRIES)
TARGETS = {
    "npm": NpmTarget(),
    "pypi": PypiTarget(),
    "go": GoTarget(),
    "swift": SwiftTarget(),
    "swift-apple": SwiftAppleTarget(),
    "spec": SpecTarget(),
    "hex": HexTarget(),
    "deno": DenoTarget(),
    "cargo": CargoTarget(),
    "dart": DartTarget(),
    "docker": DockerTarget(),
    "flutter": FlutterTarget(),
    "maven": MavenTarget(),
    "native-android": NativeAndroidTarget(),
    "native-ios": NativeIosTarget(),
    "zig": ZigTarget(),
    "pgdesign": PgdesignTarget(),
    "plain": PlainTarget(),
}


def resolve_releasable_config_dir_for_ctx(ctx):
    """Resolve the releasable config directory from a check context.

    When ``ctx`` is a WorkspaceCheckContext and the current project
    belongs to a releasable, returns the releasable config dir path.
    Otherwise returns None.

    Uses the project attribute on the context to determine releasable
    membership.
    """
    from ..check_context import WorkspaceCheckContext

    if not isinstance(ctx, WorkspaceCheckContext):
        return None
    if ctx.project is None:
        return None
    return resolve_releasable_config_dir(ctx.project, ctx.workspace_root)


def resolve_releasable_config_dir(proj, workspace_root):
    """Resolve the releasable config directory for a workspace project.

    Checks the project's ``releasable`` field (works with both
    WorkspaceProject objects and plain dicts). Returns the path to
    the releasable's state directory if the project belongs to a named
    releasable, or None otherwise.

    Args:
        proj: a WorkspaceProject or dict with optional ``releasable`` key.
        workspace_root: path to the monorepo root (str or Path).

    Returns:
        Path string to the releasable config directory, or None.
    """
    from ..workspace import WorkspaceProject, get_releasable_dir

    if isinstance(proj, WorkspaceProject):
        rel_name = proj.releasable if isinstance(proj.releasable, str) else None
    elif isinstance(proj, dict):
        val = proj.get("releasable")
        rel_name = val if isinstance(val, str) else None
    else:
        return None

    if rel_name is not None:
        return get_releasable_dir(str(workspace_root), rel_name)
    return None


def _parse_target_entry(entry, base_dir):
    """Parse a target config entry (string or dict) into a TargetEntry."""
    if isinstance(entry, str):
        return TargetEntry(name=entry, path=base_dir)
    if isinstance(entry, dict):
        name = entry.get("name")
        if not name:
            raise ConfigError(f"target entry missing 'name': {entry}")
        path = entry.get("path", base_dir)
        resolved = os.path.join(base_dir, path) if not os.path.isabs(path) else path
        return TargetEntry(name=name, path=resolved)
    raise TypeError(f"invalid target entry type: {type(entry)}")


def _auto_detect(dir_path):
    """Auto-detect targets from project file presence.

    Returns list of TargetEntry(name, path) tuples for targets whose
    manifest files exist in dir_path.
    """
    found = []
    for name, target in TARGETS.items():
        if target.detect(dir_path):
            found.append(TargetEntry(name=name, path=dir_path))
    return found


def detect_targets(dir_path=".", releasable_config_dir=None):
    """Detect which targets are applicable in the given directory.

    Uses ``read_project_config()`` with optional releasable-level
    inheritance to get the merged config view.  If the merged config
    has a ``"targets"`` list, uses that (opt-in config).  Each entry
    can be a plain string (defaults to dir_path) or a dict with
    ``"name"`` and optional ``"path"`` (subdirectory relative to dir_path).

    Two-tier rule when ``targets`` key is absent from the merged config:

    - **No ``.rlsbl/config.json`` exists** (discovery case): fall through
      to auto-detection from project file manifests.
    - **``.rlsbl/config.json`` exists but merged config has no ``targets``
      key**: auto-detect to populate hints, then raise ``ConfigError``
      listing detected targets as suggestions.

    Args:
        dir_path: project directory to scan.
        releasable_config_dir: optional path to a releasable's state
            directory for config inheritance (4-level precedence).

    Returns list of TargetEntry(name, path) tuples.
    """
    config = read_project_config(dir_path, releasable_config_dir=releasable_config_dir)
    configured = config.get("targets")

    if configured is not None and isinstance(configured, list):
        # Config-declared targets take precedence (including empty list)
        result = []
        for entry in configured:
            try:
                te = _parse_target_entry(entry, dir_path)
            except (ConfigError, TypeError) as e:
                print(f"Warning: {e}, skipping", file=sys.stderr)
                continue
            if te.name in TARGETS:
                if te.path != dir_path and not os.path.isdir(te.path):
                    print(f"Warning: target '{te.name}' path '{te.path}' does not exist",
                          file=sys.stderr)
                result.append(te)
            else:
                print(f"Warning: unknown target '{te.name}' in config, skipping",
                      file=sys.stderr)
        return result

    # No targets key in the merged config -- apply two-tier rule
    has_config_file = os.path.exists(os.path.join(dir_path, ".rlsbl", "config.json"))

    if not has_config_file:
        # Discovery case: no config.json at all, auto-detect
        return _auto_detect(dir_path)

    # Config file exists but no targets key: hard error with hints
    hints = _auto_detect(dir_path)
    hint_names = [e.name for e in hints]
    if hint_names:
        suggestion = (
            f"Add a \"targets\" key to .rlsbl/config.json. "
            f"Auto-detected targets: {', '.join(hint_names)}"
        )
    else:
        suggestion = (
            "Add a \"targets\" key to .rlsbl/config.json. "
            "No targets could be auto-detected from project manifests."
        )
    raise ConfigError(
        f"Config exists but no \"targets\" key in merged config for {dir_path}. {suggestion}"
    )

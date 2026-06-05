"""Release target discovery and registry that maps ecosystem identifiers (npm, pypi, go, cargo, deno, zig, swift, hex, docker, maven, plain) to their corresponding target classes for version bumps and scaffolding."""

import os
import sys
from typing import NamedTuple

from .npm import NpmTarget
from .pypi import PypiTarget
from .go import GoTarget
from .docs import DocsTarget
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
from .zig import ZigTarget
from .pgdesign import PgdesignTarget
from .plain import PlainTarget
from .protocol import ReleaseTarget
from .base import BaseTarget
from ..config import read_json_config


class TargetEntry(NamedTuple):
    """A detected target with its directory path."""
    name: str
    path: str

# Instantiated targets dict (replaces the old module-based REGISTRIES)
TARGETS = {
    "npm": NpmTarget(),
    "pypi": PypiTarget(),
    "go": GoTarget(),
    "docs": DocsTarget(),
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
    "zig": ZigTarget(),
    "pgdesign": PgdesignTarget(),
    "plain": PlainTarget(),
}


def _parse_target_entry(entry, base_dir):
    """Parse a target config entry (string or dict) into a TargetEntry."""
    if isinstance(entry, str):
        return TargetEntry(name=entry, path=base_dir)
    if isinstance(entry, dict):
        name = entry.get("name")
        if not name:
            raise ValueError(f"target entry missing 'name': {entry}")
        path = entry.get("path", base_dir)
        resolved = os.path.join(base_dir, path) if not os.path.isabs(path) else path
        return TargetEntry(name=name, path=resolved)
    raise TypeError(f"invalid target entry type: {type(entry)}")


def detect_targets(dir_path="."):
    """Detect which targets are applicable in the given directory.

    If .rlsbl/config.json has a "targets" array, use that (opt-in config).
    Each entry can be a plain string (defaults to dir_path) or a dict with
    "name" and optional "path" (subdirectory relative to dir_path).

    Otherwise, fall back to auto-detection based on project file presence.

    Returns list of TargetEntry(name, path) tuples.
    """
    config_path = os.path.join(dir_path, ".rlsbl", "config.json")
    config = read_json_config(config_path)
    configured = config.get("targets")

    if configured is not None and isinstance(configured, list):
        # Config-declared targets take precedence
        result = []
        for entry in configured:
            try:
                te = _parse_target_entry(entry, dir_path)
            except (ValueError, TypeError) as e:
                print(f"Warning: {e}, skipping", file=sys.stderr)
                continue
            if te.name in TARGETS:
                if te.path != dir_path and not os.path.isdir(te.path):
                    print(f"Warning: target '{te.name}' path '{te.path}' does not exist",
                          file=sys.stderr)
                result.append(te)
            else:
                print(f"Warning: unknown target '{te.name}' in .rlsbl/config.json, skipping",
                      file=sys.stderr)
        return result

    # Fallback: auto-detect from project files
    found = []
    for name, target in TARGETS.items():
        if target.detect(dir_path):
            found.append(TargetEntry(name=name, path=dir_path))
    return found

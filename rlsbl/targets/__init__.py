"""Release target discovery and registry that maps ecosystem identifiers (npm, pypi, go, deno, zig, swift, hex, docker, maven, plain) to their corresponding target classes for version bumps and scaffolding."""

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
from ..config import read_json_config, read_project_config
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


def targets_with_builtin_tests():
    """Targets that ship a built-in test runner.

    Derived from the targets that override ``run_tests``. The ``test-suite``
    and ``test-suite-workspace`` checks each used to carry their own copy of
    this set, which could disagree with the dispatch and with each other.
    """
    return frozenset(
        name for name, target in TARGETS.items() if target.has_builtin_test_runner
    )


def targets_with_library_lint():
    """Targets that participate in library boundary lint.

    Derived by asking every registered target which lint language its sources
    are written in. Adding a target with a ``lint_language`` puts it in scope
    automatically; there is no set to remember to edit.
    """
    return frozenset(
        name for name, target in TARGETS.items() if target.lint_language is not None
    )


def targets_with_version_queries():
    """Targets whose registry can be asked for a package's latest version.

    Derived from the targets that override ``query_latest_version``. Replaces
    the hand-mapped dispatch table ``rlsbl.registry`` used to carry.
    """
    return frozenset(
        name
        for name, target in TARGETS.items()
        if type(target).query_latest_version is not BaseTarget.query_latest_version
    )


def claimable_targets():
    """Targets on which ``rlsbl claim-name`` can reserve a name.

    Derived from the targets that override ``claim_placeholder``. The command
    used to hard-code the pair and then branch on the name twice more.
    """
    return frozenset(
        name
        for name, target in TARGETS.items()
        if type(target).claim_placeholder is not BaseTarget.claim_placeholder
    )


def resolve_releasable_config_dir_for_ctx(ctx):
    """Resolve the releasable config directory from a check context.

    When ``ctx`` has workspace_root and project attributes (i.e. is a
    WorkspaceCheckContext), returns the releasable config dir path.
    Otherwise returns None.

    Uses duck typing to avoid importing check_context (which would
    create a circular dependency).
    """
    workspace_root = getattr(ctx, "workspace_root", None)
    if workspace_root is None:
        return None
    project = getattr(ctx, "project", None)
    if project is None:
        return None
    return resolve_releasable_config_dir(project, workspace_root)


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
    from ..workspace_types import WorkspaceProject, get_releasable_dir

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
    # When the project is a releasable member, the releasable config's
    # targets are authoritative -- per-package config cannot override them.
    # This prevents per-package targets: [] from erasing releasable-level
    # targets: ["pypi"] via merge_config's shallow-replace semantics.
    if releasable_config_dir is not None:
        rel_config_path = os.path.join(str(releasable_config_dir), "config.json")
        rel_config = read_json_config(rel_config_path)
        rel_targets = rel_config.get("targets")
        if rel_targets is not None and isinstance(rel_targets, list):
            # Releasable defines targets -- use them directly, skip merge
            result = []
            for entry in rel_targets:
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


def read_releasable_targets(rel_config_path):
    """Read the ``targets`` list from a releasable config, enforcing the ban.

    Returns:
    - the declared targets as a list of **names**, when the config has a
      non-empty ``targets`` list. An entry may be a bare name or the record
      form a target in a subdirectory needs (``{"name": "npm", "path":
      "npm"}``); both reduce to the name here, because both callers want
      names -- ``validate_release_targets`` puts them in a set and
      ``collect_releasable_targets`` puts them in the release file's include
      list, and a record reaching either is a crash rather than a diagnosis;
    - ``None``, when the ``targets`` key is absent (callers fall through to
      member-level detection -- backward compat for releasables that haven't
      declared targets yet).

    Raises ``ConfigError`` when ``targets`` is an explicitly empty list, or a
    present-but-non-list value (string, dict, ...) -- the same loud story as
    ``validate_config_schema``.  A non-list value is never silently treated as
    absent.  This keeps the two releasable target-resolution paths
    (``collect_releasable_targets`` used by release init, and
    ``validate_release_targets`` used by release run) behaving identically on
    the banned config, instead of one short-circuiting while the other
    silently falls through.
    """
    from ..config import empty_targets_ban_message, non_list_targets_ban_message

    rel_config = read_json_config(rel_config_path)
    rel_targets = rel_config.get("targets")
    if rel_targets is None:
        return None
    if not isinstance(rel_targets, list):
        raise ConfigError(non_list_targets_ban_message(rel_config_path, rel_targets))
    if not rel_targets:
        raise ConfigError(empty_targets_ban_message(rel_config_path))
    names = []
    for entry in rel_targets:
        if isinstance(entry, str):
            names.append(entry)
            continue
        if isinstance(entry, dict):
            name = entry.get("name")
            if not name:
                raise ConfigError(
                    f"target entry missing 'name' in {rel_config_path}: {entry}"
                )
            names.append(name)
            continue
        raise ConfigError(
            f"invalid target entry in {rel_config_path}: {entry!r} "
            f"({type(entry).__name__}); declare a name or a "
            '{"name": ..., "path": ...} record'
        )
    return names


def collect_releasable_targets(releasable_name, member_projects, workspace_root):
    """Collect targets for a releasable, preferring the releasable config.

    If the releasable's config.json has a non-empty ``targets`` key, returns
    those directly (the releasable is the source of truth for targets in
    explicit mode).  An explicitly empty ``targets`` list is banned and raises
    ``ConfigError`` (see ``read_releasable_targets``).  When the key is absent,
    falls back to unioning member-level detected targets for backward
    compatibility with releasables that haven't set targets yet.

    Returns a deduplicated list of target names.
    """
    # Try the releasable config first
    rel_config_path = os.path.join(
        workspace_root, ".rlsbl-monorepo", "releasables",
        releasable_name, "config.json",
    )
    rel_targets = read_releasable_targets(rel_config_path)
    if rel_targets is not None:
        return list(rel_targets)

    # Fallback: union member-level targets (backward compat)
    seen = set()
    result = []
    for proj in member_projects:
        project_dir = os.path.join(workspace_root, proj["path"])
        rel_dir = resolve_releasable_config_dir(proj, workspace_root)
        entries = detect_targets(project_dir, releasable_config_dir=rel_dir)
        for e in entries:
            if e.name not in seen:
                seen.add(e.name)
                result.append(e.name)
    return result

"""Developer utilities for locally installing rlsbl projects for editable development."""

import os
import subprocess
import sys

from ..targets import TARGETS, detect_targets
from ..utils import require_tool
from ..workspace import find_workspace_root, load_workspace


def run_install(flags):
    """Entry point for `rlsbl dev install`.

    Detects whether we're in a monorepo (via workspace.toml) and dispatches
    to either the single-project or multi-project installer.
    """
    workspace_root = find_workspace_root(".")
    if workspace_root is not None:
        return _install_monorepo(workspace_root, flags)
    return _install_single(".", flags)


def _install_single(project_dir, flags):
    """Install (or uninstall) all detected targets in a single project directory."""
    targets = detect_targets(project_dir)
    if not targets:
        print(f"Error: no targets detected in {project_dir}", file=sys.stderr)
        return 1

    uninstall = bool(flags.get("uninstall"))
    success = True
    any_handled = False

    for entry in targets:
        name = entry.name
        target = TARGETS.get(name)
        spec = target.dev_install_command(project_dir) if target is not None else None
        if spec is None:
            print(f"Skipping {name}: install not yet supported for this target")
            continue

        tool_path = require_tool(spec["tool"], purpose=spec["purpose"], fatal=False)
        if tool_path is None:
            print(f"Skipping {name}: {spec['tool']} not on PATH")
            continue

        if uninstall:
            template = spec.get("uninstall_args_template")
            if template is None:
                print(f"Skipping {name} uninstall: not supported for this target")
                continue
            pkg_name = _resolve_project_name(project_dir, name)
            dir_name = os.path.basename(os.path.abspath(project_dir))
            args = [a.format(name=pkg_name, dir=dir_name) for a in template]
        else:
            args = list(spec["args"])

        action = "Uninstalling" if uninstall else "Installing"
        print(f"{action} {name} from {project_dir}...")
        any_handled = True
        result = subprocess.run([spec["tool"]] + args, cwd=project_dir)
        if result.returncode != 0:
            print(
                f"Error: {action.lower()} failed for {name} (exit {result.returncode})",
                file=sys.stderr,
            )
            success = False

    if not any_handled:
        # All targets were skipped (unsupported or missing tools). Not a hard
        # failure -- we already printed reasons -- but signal "no work done".
        return 0

    return 0 if success else 1


def _install_monorepo(workspace_root, flags):
    """Iterate workspace projects, applying include/exclude filters."""
    if not flags.get("all") and not flags.get("include") and not flags.get("exclude"):
        print(
            "Error: in monorepo mode, you must specify --all, --include, or --exclude.\n"
            "Use --all to install every project, or --include/--exclude to filter.",
            file=sys.stderr,
        )
        return 1

    projects = load_workspace(workspace_root)
    include = _split_csv(flags.get("include"))
    exclude = _split_csv(flags.get("exclude"))

    selected = []
    for proj in projects:
        name = proj["name"]
        if include and name not in include:
            continue
        if exclude and name in exclude:
            continue
        selected.append(proj)

    if not selected:
        print("No projects matched the filter.", file=sys.stderr)
        return 1

    overall = True
    for proj in selected:
        path = os.path.join(workspace_root, proj["path"])
        print(f"\n=== {proj['name']} ===")
        rc = _install_single(path, flags)
        if rc != 0:
            overall = False
    return 0 if overall else 1


def _split_csv(value):
    """Split a comma-separated string into a list, ignoring empty entries."""
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _resolve_project_name(project_dir, target_name):
    """Read the package name from the target's manifest, falling back to dir basename."""
    target = TARGETS.get(target_name)
    if target is not None:
        try:
            name = target.read_name(project_dir)
            if name:
                return name
        except Exception:
            pass
    return os.path.basename(os.path.abspath(project_dir))

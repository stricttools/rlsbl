#!/usr/bin/env python3
"""Fleet sweep for banned config keys across all rlsbl-managed projects.

Scans all repos under ~/Projects for .rlsbl/config.json and
.rlsbl-monorepo/ configs, reporting any that contain:
  - targets: []  (empty targets list)
  - release.mode  (any value -- PR mode removed)

Dry-run mode (default): reports violations.
--fix mode: sets private: true and removes the banned keys.
"""

import json
import os
import sys


def find_config_files():
    """Find all .rlsbl/config.json files under ~/Projects.

    Also finds per-project configs inside monorepo workspaces and
    releasable-level configs under .rlsbl-monorepo/releasables/.

    Returns a sorted list of absolute paths to config.json files.
    """
    configs = []
    base = os.path.expanduser("~/Projects")
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        # Standalone / per-project config
        config = os.path.join(root, ".rlsbl", "config.json")
        if os.path.isfile(config):
            configs.append(config)

        # Releasable-level configs in monorepo
        releasables_dir = os.path.join(root, ".rlsbl-monorepo", "releasables")
        if os.path.isdir(releasables_dir):
            for rel_name in os.listdir(releasables_dir):
                rel_config = os.path.join(
                    releasables_dir, rel_name, "config.json",
                )
                if os.path.isfile(rel_config):
                    configs.append(rel_config)

    return sorted(set(configs))


def check_config(config_path):
    """Check a single config file for banned keys.

    Returns a list of (violation_type, detail) tuples.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return [("read_error", str(e))]

    if not isinstance(config, dict):
        return [("not_dict", f"config is {type(config).__name__}, not dict")]

    violations = []

    # Check targets: []
    targets = config.get("targets")
    if isinstance(targets, list) and len(targets) == 0:
        violations.append(("empty_targets", "targets is an empty list"))

    # Check release.mode
    release = config.get("release")
    if isinstance(release, dict) and "mode" in release:
        violations.append(("release_mode", f"release.mode = {release['mode']!r}"))

    return violations


def fix_config(config_path, violations):
    """Fix banned keys in a config file.

    - empty_targets: removes "targets" key, sets "private": true
    - release_mode: removes "mode" from "release" section
      (removes entire "release" section if empty afterward)
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    for vtype, _ in violations:
        if vtype == "empty_targets":
            del config["targets"]
            config["private"] = True
        elif vtype == "release_mode":
            del config["release"]["mode"]
            if not config["release"]:
                del config["release"]

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def main():
    fix_mode = "--fix" in sys.argv

    configs = find_config_files()
    print(f"Scanned: {len(configs)} config file(s) under ~/Projects\n")

    total_violations = 0
    files_with_violations = 0

    for config_path in configs:
        violations = check_config(config_path)
        if not violations:
            continue

        files_with_violations += 1
        # Show path relative to ~/Projects for readability
        base = os.path.expanduser("~/Projects")
        rel_path = os.path.relpath(config_path, base)

        for vtype, detail in violations:
            total_violations += 1
            if fix_mode and vtype not in ("read_error", "not_dict"):
                print(f"  FIXED  {rel_path}: {detail}")
            else:
                print(f"  FOUND  {rel_path}: {detail}")

        if fix_mode:
            fixable = [
                (v, d) for v, d in violations
                if v not in ("read_error", "not_dict")
            ]
            if fixable:
                fix_config(config_path, fixable)

    print()
    if total_violations == 0:
        print("No banned config keys found.")
    else:
        action = "fixed" if fix_mode else "found"
        print(
            f"{total_violations} violation(s) in {files_with_violations} file(s) "
            f"{action}."
        )
        if not fix_mode:
            print("Run with --fix to apply corrections.")

    return 0 if total_violations == 0 or fix_mode else 1


if __name__ == "__main__":
    sys.exit(main())

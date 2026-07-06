#!/usr/bin/env python3
"""Fleet sweep: add coverage_unit=commit to all configs missing the key.

Scans all repos under ~/Projects for .rlsbl/config.json and
.rlsbl-monorepo/ releasable configs, reporting any that are missing
the coverage_unit key.

Dry-run mode (default): reports configs missing the key.
--fix mode: adds "coverage_unit": "commit" to each one.
"""

import json
import os
import sys


def find_config_files():
    """Find all .rlsbl/config.json files under ~/Projects.

    Also finds releasable-level configs under .rlsbl-monorepo/releasables/.
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
    """Check a single config file for missing coverage_unit.

    Returns True if coverage_unit is missing, False otherwise.
    Returns None on read error.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(config, dict):
        return None

    return "coverage_unit" not in config


def fix_config(config_path):
    """Add coverage_unit=commit to a config file."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["coverage_unit"] = "commit"

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def main():
    fix_mode = "--fix" in sys.argv

    configs = find_config_files()
    print(f"Scanned: {len(configs)} config file(s) under ~/Projects\n")

    missing_count = 0

    for config_path in configs:
        is_missing = check_config(config_path)
        if is_missing is None:
            continue  # read error, skip
        if not is_missing:
            continue  # already has coverage_unit

        missing_count += 1
        base = os.path.expanduser("~/Projects")
        rel_path = os.path.relpath(config_path, base)

        if fix_mode:
            fix_config(config_path)
            print(f"  FIXED  {rel_path}: added coverage_unit=commit")
        else:
            print(f"  FOUND  {rel_path}: missing coverage_unit")

    print()
    if missing_count == 0:
        print("All configs have coverage_unit set.")
    else:
        action = "fixed" if fix_mode else "found"
        print(f"{missing_count} config(s) missing coverage_unit {action}.")
        if not fix_mode:
            print("Run with --fix to add coverage_unit=commit to each.")

    return 0 if missing_count == 0 or fix_mode else 1


if __name__ == "__main__":
    sys.exit(main())

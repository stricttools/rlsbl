#!/usr/bin/env python3
"""Fleet sweep migrating the deprecated ``private`` config key to ``publish_mode``.

Scans all repos under ~/Projects for .rlsbl/config.json and releasable-level
configs under .rlsbl-monorepo/releasables/, reporting (and, with --fix,
rewriting) any that still carry the old boolean ``private`` key:

  - ``"private": false`` -> ``"publish_mode": "ci"``   (publish via CI)
  - ``"private": true``  -> ``"publish_mode": "none"``  (suppress publishing)

The replacement happens in place at the same key position, so the surrounding
config layout is preserved. A config that already has ``publish_mode`` and no
``private`` key is left untouched.

Dry-run mode (default): reports files that need migration.
--fix mode: rewrites them.

Discovery mirrors scripts/sweep_banned_configs.py.
"""

import json
import os
import sys


def find_config_files():
    """Find all .rlsbl/config.json and releasable-level config.json files.

    Returns a sorted list of absolute paths.
    """
    configs = []
    base = os.path.expanduser("~/Projects")
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        config = os.path.join(root, ".rlsbl", "config.json")
        if os.path.isfile(config):
            configs.append(config)

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
    """Return a (violation_type, detail) tuple describing needed migration.

    Returns None when the file needs no migration.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return ("read_error", str(e))

    if not isinstance(config, dict):
        return ("not_dict", f"config is {type(config).__name__}, not dict")

    if "private" not in config:
        return None

    private = config["private"]
    if not isinstance(private, bool):
        return ("non_bool_private", f'"private" is {private!r}, not a bool')

    mode = "none" if private else "ci"
    return ("private_key", f'"private": {json.dumps(private)} -> "publish_mode": "{mode}"')


def migrate_config_dict(config):
    """Return a new config dict with ``private`` replaced by ``publish_mode``.

    Preserves key order: the new ``publish_mode`` key sits exactly where
    ``private`` was. Raises ValueError if ``private`` is not a bool.
    """
    private = config["private"]
    if not isinstance(private, bool):
        raise ValueError(f'"private" is {private!r}, not a bool')
    mode = "none" if private else "ci"

    new = {}
    for key, value in config.items():
        if key == "private":
            new["publish_mode"] = mode
        else:
            new[key] = value
    return new


def fix_config(config_path):
    """Rewrite a config file in place, migrating ``private`` to ``publish_mode``."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config = migrate_config_dict(config)

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def main():
    fix_mode = "--fix" in sys.argv

    configs = find_config_files()
    print(f"Scanned: {len(configs)} config file(s) under ~/Projects\n")

    total = 0
    files_migrated = 0
    base = os.path.expanduser("~/Projects")

    for config_path in configs:
        result = check_config(config_path)
        if result is None:
            continue

        vtype, detail = result
        rel_path = os.path.relpath(config_path, base)

        if vtype in ("read_error", "not_dict", "non_bool_private"):
            total += 1
            print(f"  SKIP   {rel_path}: {detail}")
            continue

        total += 1
        files_migrated += 1
        if fix_mode:
            fix_config(config_path)
            print(f"  FIXED  {rel_path}: {detail}")
        else:
            print(f"  FOUND  {rel_path}: {detail}")

    print()
    if files_migrated == 0:
        print("No configs with the deprecated \"private\" key found.")
    else:
        action = "migrated" if fix_mode else "found"
        print(f"{files_migrated} file(s) {action}.")
        if not fix_mode:
            print("Run with --fix to apply the migration.")

    return 0 if files_migrated == 0 or fix_mode else 1


if __name__ == "__main__":
    sys.exit(main())

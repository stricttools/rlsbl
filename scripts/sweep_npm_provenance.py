#!/usr/bin/env python3
"""Fleet sweep: set the mandatory npm-pipeline ``provenance`` key from repo visibility.

Scans all repos under ~/Projects for .rlsbl/config.json and .rlsbl-monorepo/
releasable configs that declare an npm-type pipeline. The ``provenance`` key
is now mandatory on npm pipelines and its value depends on repository
visibility:

- public GitHub repo  -> provenance = true  (OIDC build-provenance works)
- private GitHub repo -> provenance = false (provenance impossible)
- non-GitHub host     -> provenance = false (`gh repo view` fails; no OIDC)

Visibility is probed per repo via ``gh repo view --json isPrivate`` run from
the repo's git root. Results are cached per git root.

Dry-run mode (default): reports npm configs whose provenance key is missing or
disagrees with actual visibility.
--fix mode: writes the correct provenance value into each such config.
"""

import json
import os
import subprocess
import sys


def find_config_files():
    """Find all .rlsbl/config.json and releasable config.json files under ~/Projects."""
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


def _load(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(config, dict):
        return None
    return config


def npm_pipeline_names(config):
    """Return the names of npm-type pipelines in *config* (possibly empty)."""
    pipelines = config.get("pipelines") or {}
    if not isinstance(pipelines, dict):
        return []
    return [
        name for name, e in pipelines.items()
        if isinstance(e, dict) and e.get("type") == "npm"
    ]


def find_git_root(start_path):
    """Return the nearest ancestor directory containing .git, or None."""
    current = os.path.abspath(start_path)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def desired_provenance(git_root, cache):
    """Return the desired provenance bool for a repo at *git_root*.

    Probes ``gh repo view --json isPrivate`` from *git_root*. public -> True,
    private -> False, any failure (non-GitHub host, gh missing) -> False.
    Cached per git_root.
    """
    if git_root in cache:
        return cache[git_root]

    result = False
    if git_root is not None:
        try:
            out = subprocess.run(
                ["gh", "repo", "view", "--json", "isPrivate"],
                cwd=git_root, capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout)
                is_private = data.get("isPrivate")
                if isinstance(is_private, bool):
                    result = not is_private
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            result = False

    cache[git_root] = result
    return result


def fix_config(config_path, config, value):
    """Set provenance=*value* on every npm pipeline in the config and write it."""
    for name in npm_pipeline_names(config):
        config["pipelines"][name]["provenance"] = value

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def main():
    fix_mode = "--fix" in sys.argv

    configs = find_config_files()
    base = os.path.expanduser("~/Projects")
    vis_cache = {}

    changed = 0
    scanned_npm = 0

    for config_path in configs:
        config = _load(config_path)
        if config is None:
            continue
        npm_names = npm_pipeline_names(config)
        if not npm_names:
            continue
        scanned_npm += 1

        git_root = find_git_root(config_path)
        want = desired_provenance(git_root, vis_cache)

        # Does any npm pipeline disagree with the desired value?
        needs_fix = any(
            config["pipelines"][n].get("provenance") != want
            for n in npm_names
        )
        if not needs_fix:
            continue

        changed += 1
        rel_path = os.path.relpath(config_path, base)
        vis = "public" if want else "private/non-GitHub"
        if fix_mode:
            fix_config(config_path, config, want)
            print(f"  FIXED  {rel_path}: provenance={str(want).lower()} ({vis})")
        else:
            current = {n: config["pipelines"][n].get("provenance") for n in npm_names}
            print(
                f"  FOUND  {rel_path}: want provenance={str(want).lower()} "
                f"({vis}); current={current}"
            )

    print()
    print(f"Scanned {scanned_npm} npm-pipeline config(s) under ~/Projects.")
    if changed == 0:
        print("All npm-pipeline configs have the correct provenance value.")
    else:
        action = "fixed" if fix_mode else "found"
        print(f"{changed} config(s) needing a provenance change {action}.")
        if not fix_mode:
            print("Run with --fix to set provenance from repo visibility.")

    return 0 if changed == 0 or fix_mode else 1


if __name__ == "__main__":
    sys.exit(main())

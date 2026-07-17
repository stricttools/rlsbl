#!/usr/bin/env python3
"""Fleet sweep: backfill the mandatory pipeline ``target`` link and go ``artifact`` key.

The v0.105.0+ config-schema validator makes two pipeline keys mandatory:

1. ``target`` on every pipeline entry -- an explicit link declaring what the
   pipeline publishes for. There is no name-based inference in the validator;
   the link must be present. This sweep derives it from declared targets:

   - if the pipeline KEY names a declared target -> ``target`` = key
   - else if the pipeline ``type`` names a declared target -> ``target`` = type
   - else if the pipeline ``type`` is a deploy-only type (cloudflare-pages,
     which publishes no release artifact) -> ``target`` = null
   - else the entry is ambiguous: it is reported and SKIPPED, never guessed.

2. ``artifact`` on every ``type: "go"`` pipeline -- ``"binary"`` or
   ``"library"``. Derived from the project layout: a project with any
   ``package main`` entry point is a ``binary``, otherwise a ``library``.

Scans all repos under ~/Projects for .rlsbl/config.json and
.rlsbl-monorepo/releasables/*/config.json.

Dry-run mode (default): prints a table of every pending change and every
ambiguous (skipped) entry, then exits 1 if any change is pending.
--fix mode: writes the derived values atomically (tempfile + os.replace,
indent=2 + trailing newline). No auto-commit.
"""

import json
import os
import sys

# Pipeline types that deploy a site/docs and publish no release artifact for
# any versioned target. Their target link is null (a targetless publisher).
DEPLOY_ONLY_TYPES = {"cloudflare-pages"}

# Directories never containing the project's own main package.
SKIP_GO_DIRS = {".git", ".rlsbl", "vendor", "node_modules", "__pycache__"}


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


def valid_target_names(config):
    """Return the set of declared target names (string and dict forms)."""
    names = set()
    raw = config.get("targets")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                names.add(entry)
            elif isinstance(entry, dict):
                tname = entry.get("name")
                if isinstance(tname, str) and tname:
                    names.add(tname)
    return names


def project_root_for(config_path):
    """Return the project root directory for a config file.

    ``<root>/.rlsbl/config.json``                            -> ``<root>``
    ``<root>/.rlsbl-monorepo/releasables/<n>/config.json``   -> ``<root>``
    """
    parts = os.path.abspath(config_path).split(os.sep)
    if ".rlsbl-monorepo" in parts:
        idx = parts.index(".rlsbl-monorepo")
        return os.sep.join(parts[:idx]) or os.sep
    # <root>/.rlsbl/config.json -> two levels up
    return os.path.dirname(os.path.dirname(os.path.abspath(config_path)))


def detect_go_artifact(project_root):
    """Return ``"binary"`` if the project has any ``package main``, else ``"library"``."""
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in SKIP_GO_DIRS]
        for fname in files:
            if not fname.endswith(".go") or fname.endswith("_test.go"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("//"):
                            continue
                        if s.startswith("package "):
                            if s == "package main" or s.startswith("package main"):
                                return "binary"
                            break  # first package clause decides this file
            except OSError:
                continue
    return "library"


def derive_target(pipeline_name, entry, targets):
    """Return ``(status, value)`` for the derived target link.

    status is one of: ``"name"`` (value is a target name), ``"null"``
    (value is None -- a targetless publisher), or ``"ambiguous"``
    (value is None -- cannot be derived; caller must skip).
    """
    if pipeline_name in targets:
        return ("name", pipeline_name)
    ptype = entry.get("type")
    if isinstance(ptype, str) and ptype in targets:
        return ("name", ptype)
    if isinstance(ptype, str) and ptype in DEPLOY_ONLY_TYPES:
        return ("null", None)
    return ("ambiguous", None)


def plan_config(config_path, config):
    """Return ``(changes, ambiguities)`` for one config without mutating it.

    ``changes`` is a list of ``(pipeline_name, field, value, note)``.
    ``ambiguities`` is a list of ``(pipeline_name, reason)``.
    """
    changes = []
    ambiguities = []
    pipelines = config.get("pipelines")
    if not isinstance(pipelines, dict):
        return changes, ambiguities

    targets = valid_target_names(config)
    root = project_root_for(config_path)

    for pname, entry in pipelines.items():
        if not isinstance(entry, dict):
            continue

        if "target" not in entry:
            status, value = derive_target(pname, entry, targets)
            if status == "ambiguous":
                ambiguities.append((
                    pname,
                    f"key '{pname}' and type '{entry.get('type')}' name no "
                    f"declared target {sorted(targets)}",
                ))
            else:
                shown = "null" if value is None else value
                changes.append((pname, "target", value, f"target={shown}"))

        if entry.get("type") == "go" and "artifact" not in entry:
            artifact = detect_go_artifact(root)
            changes.append((pname, "artifact", artifact, f"artifact={artifact}"))

    return changes, ambiguities


def apply_changes(config_path, config, changes):
    """Apply *changes* to *config* and write it atomically."""
    for pname, field, value, _note in changes:
        config["pipelines"][pname][field] = value

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def main():
    fix_mode = "--fix" in sys.argv

    configs = find_config_files()
    base = os.path.expanduser("~/Projects")

    scanned = 0
    changed_configs = 0
    total_changes = 0
    total_ambiguous = 0

    for config_path in configs:
        config = _load(config_path)
        if config is None:
            continue
        pipelines = config.get("pipelines")
        if not isinstance(pipelines, dict) or not pipelines:
            continue
        scanned += 1

        changes, ambiguities = plan_config(config_path, config)
        rel_path = os.path.relpath(config_path, base)

        for pname, reason in ambiguities:
            total_ambiguous += 1
            print(f"  SKIP   {rel_path}: pipeline '{pname}' AMBIGUOUS -- {reason}")

        if not changes:
            continue

        changed_configs += 1
        total_changes += len(changes)
        summary = ", ".join(f"{p}.{note}" for p, _f, _v, note in changes)
        if fix_mode:
            apply_changes(config_path, config, changes)
            print(f"  FIXED  {rel_path}: {summary}")
        else:
            print(f"  FOUND  {rel_path}: {summary}")

    print()
    print(f"Scanned {scanned} config(s) with pipelines under ~/Projects.")
    if total_ambiguous:
        print(f"{total_ambiguous} ambiguous pipeline(s) skipped (need manual review).")
    if total_changes == 0:
        print("All pipelines have a target link and go artifact key.")
    else:
        action = "fixed" if fix_mode else "found"
        print(
            f"{total_changes} change(s) across {changed_configs} config(s) {action}."
        )
        if not fix_mode:
            print("Run with --fix to backfill target links and go artifact keys.")

    return 0 if total_changes == 0 or fix_mode else 1


if __name__ == "__main__":
    sys.exit(main())

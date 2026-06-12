#!/usr/bin/env python3
"""Informative-only script: detect orphaned scaffold files across all local rlsbl projects.

Does NOT modify any files. Reports what WOULD be cleaned.
"""
import json
import os
import subprocess
import sys

def find_rlsbl_projects():
    """Find all directories with .rlsbl/config.json under ~/Projects."""
    projects = []
    base = os.path.expanduser("~/Projects")
    for root, dirs, files in os.walk(base):
        # Don't descend into .git, node_modules, .venv, etc.
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__", "dist", "build")]
        config = os.path.join(root, ".rlsbl", "config.json")
        if os.path.isfile(config):
            projects.append(root)
            dirs.clear()  # Don't descend further
    return sorted(projects)

def get_declared_targets(project_dir):
    """Read targets from .rlsbl/config.json."""
    config_path = os.path.join(project_dir, ".rlsbl", "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        targets = config.get("targets", [])
        names = set()
        for t in targets:
            if isinstance(t, str):
                names.add(t)
            elif isinstance(t, dict):
                names.add(t.get("name", ""))
        return names
    except (json.JSONDecodeError, OSError):
        return set()

def detect_orphaned_lint_configs(project_dir, targets):
    """Check for lint configs that don't match declared targets."""
    lint_dir = os.path.join(project_dir, ".rlsbl", "lint")
    if not os.path.isdir(lint_dir):
        return []
    
    target_to_lint = {"pypi": "python.toml", "npm": "npm.toml", "go": "go.toml"}
    needed = {target_to_lint[t] for t in targets if t in target_to_lint}
    
    orphans = []
    for f in os.listdir(lint_dir):
        if f.endswith(".toml") and f not in needed:
            orphans.append(os.path.join(".rlsbl", "lint", f))
    return orphans

def detect_orphaned_ci(project_dir, targets):
    """Check for CI workflow files that don't match declared targets."""
    wf_dir = os.path.join(project_dir, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return []
    
    orphans = []
    has_per_target_ci = any(f.startswith("ci-") and f.endswith(".yml") for f in os.listdir(wf_dir))
    
    if has_per_target_ci:
        # Multi-target mode: old ci.yml is orphaned
        if os.path.isfile(os.path.join(wf_dir, "ci.yml")):
            orphans.append(os.path.join(".github", "workflows", "ci.yml"))
    
    return orphans

def main():
    projects = find_rlsbl_projects()
    print(f"Found {len(projects)} rlsbl projects\n")
    
    total_orphans = 0
    for proj in projects:
        name = os.path.basename(proj)
        targets = get_declared_targets(proj)
        
        orphans = []
        orphans.extend(detect_orphaned_lint_configs(proj, targets))
        orphans.extend(detect_orphaned_ci(proj, targets))
        
        if orphans:
            total_orphans += len(orphans)
            print(f"{name} ({len(orphans)} orphans):")
            for o in orphans:
                print(f"  {o}")
        
    print(f"\nTotal: {total_orphans} orphaned files across {len(projects)} projects")

if __name__ == "__main__":
    main()

"""Monorepo mirror command that performs the initial git subtree split, pushes to a standalone mirror repository, and scaffolds CI workflows."""

import json
import os
import subprocess
import sys
import tempfile

from ...git_util import validate_subtree_remote_ssh_host
from ...utils import run
from ...workspace import find_workspace_root, load_workspace


def _cmd_mirror(flags, project_root):
    """Split a monorepo project into its subtree mirror and scaffold CI."""
    project_name = flags["project"]

    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    project = None
    for p in projects:
        if p.name == project_name:
            project = p
            break

    if project is None:
        available = ", ".join(p.name for p in projects)
        print(f"Error: project '{project_name}' not found in workspace. Available: {available}", file=sys.stderr)
        sys.exit(1)

    subtree_remote = project.get("subtree_remote", "")
    if not subtree_remote:
        print(f"Error: project '{project_name}' has no subtree_remote configured.", file=sys.stderr)
        print("Set it with: rlsbl monorepo add --subtree-remote <url> <path>", file=sys.stderr)
        sys.exit(1)

    # Validate SSH host consistency between subtree_remote and origin
    validate_subtree_remote_ssh_host(subtree_remote, root)

    # Validate remote is reachable
    try:
        run("git", ["ls-remote", subtree_remote], cwd=root)
    except subprocess.CalledProcessError:
        print(f"Error: subtree remote is not reachable: {subtree_remote}", file=sys.stderr)
        sys.exit(1)

    project_path = project.path
    tmp_branch = "_rlsbl-mirror-tmp"

    # Subtree split
    print(f"Splitting subtree for {project_path}...")
    try:
        run("git", ["subtree", "split", f"--prefix={project_path}", "-b", tmp_branch], cwd=root)
    except subprocess.CalledProcessError as e:
        print(f"Error: subtree split failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        sys.exit(1)

    # Push to mirror
    print(f"Pushing to {subtree_remote}...")
    try:
        run("git", ["push", subtree_remote, f"{tmp_branch}:refs/heads/main"], cwd=root)
    except subprocess.CalledProcessError as e:
        # Clean up branch before exiting
        try:
            run("git", ["branch", "-D", tmp_branch], cwd=root)
        except subprocess.CalledProcessError:
            pass
        print(f"Error: push to mirror failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        sys.exit(1)

    # Clean up temp branch
    try:
        run("git", ["branch", "-D", tmp_branch], cwd=root)
    except subprocess.CalledProcessError:
        pass

    # Clone mirror to temp dir, scaffold CI, commit, push
    tmpdir = tempfile.mkdtemp(prefix="rlsbl-mirror-")
    try:
        print(f"Cloning mirror to scaffold CI...")
        run("git", ["clone", subtree_remote, tmpdir])

        # Read the sub-project's .rlsbl/config.json to get the target type
        sub_config_path = os.path.join(root, project_path, ".rlsbl", "config.json")
        config = {}
        if os.path.isfile(sub_config_path):
            with open(sub_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

        # Write .rlsbl/config.json in the clone
        clone_rlsbl_dir = os.path.join(tmpdir, ".rlsbl")
        os.makedirs(clone_rlsbl_dir, exist_ok=True)
        clone_config_path = os.path.join(clone_rlsbl_dir, "config.json")
        with open(clone_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

        # Run scaffold
        print("Scaffolding CI in mirror...")
        result = subprocess.run(
            ["rlsbl", "scaffold"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Warning: scaffold returned {result.returncode}", file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)

        # Commit and push
        subprocess.run(
            ["git", "add", "-A"],
            cwd=tmpdir,
            check=True,
            capture_output=True,
            text=True,
        )

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            check=True,
        )
        if status.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", "chore: scaffold rlsbl CI"],
                cwd=tmpdir,
                check=True,
                capture_output=True,
                text=True,
            )
            run("git", ["push", "origin", "main"], cwd=tmpdir)
            print(f"Mirror initialized and CI scaffolded: {subtree_remote}")
        else:
            print(f"Mirror initialized (no scaffold changes to commit): {subtree_remote}")

    finally:
        # Clean up temp dir
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

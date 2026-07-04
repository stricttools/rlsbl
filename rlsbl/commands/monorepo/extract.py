"""Extract and absorb operations for moving packages in and out of monorepos, including history migration, changelog transfer, and workspace.toml updates.

Provides:
- require_filter_repo(): dependency check for git-filter-repo
- cmd_extract(): extract a package from a monorepo into a new repository
- cmd_absorb(): absorb an external repository as a package in the monorepo
- cmd_extract_releasable(): extract all member packages of a releasable
"""

import json
import os
import shutil
import subprocess

from ...changelog.files import get_changes_dir, read_unreleased, list_versioned_files
from ...changelog.schema import parse_jsonl, serialize_entry
from ...errors import RlsblError
from ...workspace import (
    get_releasable_changes_dir,
    load_workspace,
    load_releasables,
    members_of,
    save_workspace,
)


class ExtractError(RlsblError):
    """Error during extract or absorb operations."""


def require_filter_repo():
    """Raise if git-filter-repo is not installed.

    Checks that the ``git-filter-repo`` command is available on PATH.
    Raises ExtractError with install instructions if missing.
    """
    path = shutil.which("git-filter-repo")
    if path is None:
        raise ExtractError(
            "git-filter-repo is not installed. "
            "Install it with: pip install git-filter-repo\n"
            "Or see: https://github.com/newren/git-filter-repo#how-do-i-install-it"
        )
    return path


def _run_git(cwd, *args):
    """Run a git command and return stdout. Raises subprocess.CalledProcessError on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _find_project(projects, package_name):
    """Find a project by name in the workspace project list.

    Returns the WorkspaceProject or raises ExtractError.
    """
    for proj in projects:
        if proj.name == package_name:
            return proj
    available = ", ".join(p.name for p in projects)
    raise ExtractError(
        f"package '{package_name}' not found in workspace. Available: {available}"
    )


def _get_default_branch(cwd):
    """Detect the default branch name (main or master) of a repo."""
    try:
        branch = _run_git(cwd, "symbolic-ref", "--short", "HEAD")
        return branch
    except subprocess.CalledProcessError:
        return "main"


def _filter_changelog_entries(entries, package_path, repo_root):
    """Filter changelog entries to those touching a specific package path.

    An entry matches if:
    - It has a ``packages`` field containing the package name, OR
    - Any of its commits touch files under the package path (checked via
      git diff-tree if repo_root is provided and the commit exists).

    When we cannot determine relevance (e.g. no packages field and commits
    are not resolvable), the entry is included (conservative approach).
    """
    pkg_dir = package_path.rstrip("/") + "/"
    filtered = []

    for entry in entries:
        # If the entry has an explicit packages field, use it
        if entry.packages is not None:
            pkg_base = os.path.basename(package_path.rstrip("/"))
            if pkg_base in entry.packages or package_path in entry.packages:
                filtered.append(entry)
            continue

        # Try to determine from commit paths
        if repo_root:
            touches_package = False
            for commit_hash in entry.commits:
                try:
                    files_output = _run_git(
                        repo_root, "diff-tree", "--no-commit-id", "-r",
                        "--name-only", commit_hash
                    )
                    for fpath in files_output.splitlines():
                        if fpath.startswith(pkg_dir) or fpath == package_path:
                            touches_package = True
                            break
                except subprocess.CalledProcessError:
                    # Commit not resolvable -- include conservatively
                    touches_package = True
                    break
                if touches_package:
                    break
            if touches_package:
                filtered.append(entry)
        else:
            # No repo root -- include conservatively
            filtered.append(entry)

    return filtered


def _migrate_changelog_to_new_repo(
    source_changes_dir, target_changes_dir, package_path, repo_root
):
    """Migrate changelog entries relevant to a package into a new repo's changes dir.

    Reads unreleased.jsonl and all versioned JSONL files from source_changes_dir,
    filters entries to those relevant to the package, and writes them into
    target_changes_dir.

    Returns (files_written, entries_migrated) tuple.
    """
    os.makedirs(target_changes_dir, exist_ok=True)
    files_written = 0
    entries_migrated = 0

    # Migrate unreleased entries
    unreleased_entries = read_unreleased(os.path.dirname(source_changes_dir))
    if unreleased_entries:
        # read_unreleased expects the project path, not changes_dir directly
        # Re-read from the actual changes dir
        unreleased_path = os.path.join(source_changes_dir, "unreleased.jsonl")
        if os.path.isfile(unreleased_path):
            unreleased_entries = parse_jsonl(unreleased_path)
    else:
        unreleased_path = os.path.join(source_changes_dir, "unreleased.jsonl")
        if os.path.isfile(unreleased_path):
            unreleased_entries = parse_jsonl(unreleased_path)

    if unreleased_entries:
        filtered = _filter_changelog_entries(unreleased_entries, package_path, repo_root)
        if filtered:
            target_unreleased = os.path.join(target_changes_dir, "unreleased.jsonl")
            with open(target_unreleased, "w", encoding="utf-8") as f:
                for entry in filtered:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(filtered)

    # Migrate versioned entries
    versioned = list_versioned_files(source_changes_dir)
    for version_str, version_path in versioned:
        version_entries = parse_jsonl(version_path)
        filtered = _filter_changelog_entries(version_entries, package_path, repo_root)
        if filtered:
            target_version = os.path.join(target_changes_dir, f"{version_str}.jsonl")
            with open(target_version, "w", encoding="utf-8") as f:
                for entry in filtered:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(filtered)

    # Ensure unreleased.jsonl exists even if empty
    target_unreleased = os.path.join(target_changes_dir, "unreleased.jsonl")
    if not os.path.isfile(target_unreleased):
        with open(target_unreleased, "w", encoding="utf-8") as f:
            pass
        files_written += 1

    return files_written, entries_migrated


def _create_rlsbl_config(target_path, source_config_path=None):
    """Create a .rlsbl/ config in the target repo.

    If source_config_path is provided and exists, copies relevant config.
    Otherwise creates a minimal config.
    """
    rlsbl_dir = os.path.join(target_path, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)

    config = {}
    if source_config_path and os.path.isfile(source_config_path):
        with open(source_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Ensure private field exists
    if "private" not in config:
        config["private"] = False

    config_path = os.path.join(rlsbl_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    return config_path


def _remove_project_from_workspace(workspace_root, package_name, projects):
    """Remove a project from workspace.toml by name.

    Returns the updated project list.
    """
    updated = [p for p in projects if p.name != package_name]
    if len(updated) == len(projects):
        raise ExtractError(
            f"package '{package_name}' not found in workspace projects"
        )
    save_workspace(workspace_root, updated)
    return updated


def validate_extract_preconditions(workspace_root, package_name, target_repo_path):
    """Validate that extraction can proceed.

    Checks:
    - Package exists in workspace.toml
    - Target path does not already exist
    - git-filter-repo is installed

    Returns (projects, project) tuple.
    """
    require_filter_repo()

    projects = load_workspace(workspace_root)
    project = _find_project(projects, package_name)

    if os.path.exists(target_repo_path):
        raise ExtractError(
            f"target path already exists: {target_repo_path}"
        )

    return projects, project


def cmd_extract(workspace_root, package_name, target_repo_path, *, dry_run=False):
    """Extract a package from the monorepo into a new repository.

    Steps:
    1. Validate package exists in workspace.toml
    2. Clone the monorepo to target_repo_path
    3. Run ``git filter-repo --path <pkg-dir>`` on the clone to keep only
       that package's history
    4. Migrate changelog: filter JSONL entries to those touching the
       extracted package
    5. Create ``.rlsbl/`` config in the new repo
    6. Update source monorepo: remove project from workspace.toml

    Args:
        workspace_root: path to the monorepo root.
        package_name: name of the package to extract.
        target_repo_path: path where the new repo will be created.
        dry_run: if True, validate but do not perform the extraction.

    Returns:
        A dict with extraction details: package_name, target_path,
        entries_migrated, files_written.
    """
    projects, project = validate_extract_preconditions(
        workspace_root, package_name, target_repo_path
    )
    package_path = project.path

    if dry_run:
        return {
            "package_name": package_name,
            "target_path": target_repo_path,
            "package_path": package_path,
            "dry_run": True,
        }

    # Clone the monorepo to target path
    _run_git(workspace_root, "clone", "--no-local", ".", target_repo_path)

    # Run git filter-repo to keep only the package's directory
    subprocess.run(
        ["git-filter-repo", "--path", package_path, "--force"],
        cwd=target_repo_path,
        check=True,
        capture_output=True,
        text=True,
    )

    # After filter-repo, the package dir is at root level with its subpath.
    # Move files from the subdirectory to root if the path has depth.
    if "/" in package_path or os.sep in package_path:
        subprocess.run(
            [
                "git-filter-repo",
                "--path-rename", f"{package_path}/:",
                "--force",
            ],
            cwd=target_repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # Single-level path: rename pkg/ to root
        subprocess.run(
            [
                "git-filter-repo",
                "--path-rename", f"{package_path}/:",
                "--force",
            ],
            cwd=target_repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    # Migrate changelog
    source_changes_dir = get_changes_dir(
        os.path.join(workspace_root, package_path)
    )
    target_changes_dir = os.path.join(target_repo_path, ".rlsbl", "changes")

    files_written = 0
    entries_migrated = 0
    if os.path.isdir(source_changes_dir):
        files_written, entries_migrated = _migrate_changelog_to_new_repo(
            source_changes_dir, target_changes_dir, package_path, workspace_root
        )

    # Create .rlsbl/ config
    source_config = os.path.join(
        workspace_root, package_path, ".rlsbl", "config.json"
    )
    _create_rlsbl_config(target_repo_path, source_config)

    # Update source monorepo: remove project from workspace.toml
    _remove_project_from_workspace(workspace_root, package_name, projects)

    return {
        "package_name": package_name,
        "target_path": target_repo_path,
        "package_path": package_path,
        "entries_migrated": entries_migrated,
        "files_written": files_written,
    }


def validate_absorb_preconditions(workspace_root, source_repo_path, package_name):
    """Validate that absorption can proceed.

    Checks:
    - Source repo exists and is a git repo
    - Package name is not already in workspace.toml

    Returns projects list.
    """
    if not os.path.isdir(source_repo_path):
        raise ExtractError(
            f"source repo path does not exist: {source_repo_path}"
        )

    if not os.path.isdir(os.path.join(source_repo_path, ".git")):
        raise ExtractError(
            f"source path is not a git repository: {source_repo_path}"
        )

    projects = load_workspace(workspace_root)
    for proj in projects:
        if proj.name == package_name:
            raise ExtractError(
                f"package '{package_name}' already exists in workspace"
            )

    return projects


def _migrate_changelog_from_source(source_repo_path, target_changes_dir):
    """Migrate changelog entries from a source repo into the monorepo's changes dir.

    Reads .rlsbl/changes/ from the source repo and writes entries into
    target_changes_dir, appending to existing files.

    Returns (files_written, entries_migrated) tuple.
    """
    source_changes_dir = get_changes_dir(source_repo_path)
    if not os.path.isdir(source_changes_dir):
        return 0, 0

    os.makedirs(target_changes_dir, exist_ok=True)
    files_written = 0
    entries_migrated = 0

    # Migrate unreleased entries (append to existing)
    source_unreleased = os.path.join(source_changes_dir, "unreleased.jsonl")
    if os.path.isfile(source_unreleased):
        source_entries = parse_jsonl(source_unreleased)
        if source_entries:
            target_unreleased = os.path.join(target_changes_dir, "unreleased.jsonl")
            with open(target_unreleased, "a", encoding="utf-8") as f:
                for entry in source_entries:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(source_entries)

    # Migrate versioned entries
    versioned = list_versioned_files(source_changes_dir)
    for version_str, version_path in versioned:
        version_entries = parse_jsonl(version_path)
        if version_entries:
            target_version = os.path.join(target_changes_dir, f"{version_str}.jsonl")
            with open(target_version, "a", encoding="utf-8") as f:
                for entry in version_entries:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(version_entries)

    return files_written, entries_migrated


def cmd_absorb(
    workspace_root, source_repo_path, package_name, releasable_name=None,
    *, dry_run=False
):
    """Absorb an external repository as a package in the monorepo.

    Steps:
    1. Run ``git subtree add --prefix=<package_name> <source_repo_path> <branch>``
    2. Add project to workspace.toml with the specified releasable
    3. Migrate changelog: read source repo's ``.rlsbl/changes/``, write to
       the package's changelog in the monorepo
    4. Return details for the caller to regenerate CI

    Args:
        workspace_root: path to the monorepo root.
        source_repo_path: path to the external repository.
        package_name: name for the package in the monorepo.
        releasable_name: optional releasable to assign the package to.
        dry_run: if True, validate but do not perform the absorption.

    Returns:
        A dict with absorption details: package_name, source_path,
        entries_migrated, files_written.
    """
    projects = validate_absorb_preconditions(
        workspace_root, source_repo_path, package_name
    )

    source_branch = _get_default_branch(source_repo_path)

    if dry_run:
        return {
            "package_name": package_name,
            "source_path": source_repo_path,
            "source_branch": source_branch,
            "dry_run": True,
        }

    # Run git subtree add
    _run_git(
        workspace_root, "subtree", "add",
        f"--prefix={package_name}", source_repo_path, source_branch
    )

    # Build project entry
    new_project = {"path": package_name, "name": package_name}
    if releasable_name is not None:
        new_project["releasable"] = releasable_name

    # Add project to workspace.toml
    from ...workspace import WorkspaceProject
    projects.append(WorkspaceProject(new_project))
    save_workspace(workspace_root, projects)

    # Migrate changelog from source repo.
    # When absorbing into a releasable, route entries to the releasable's
    # changes dir instead of the per-package dir.
    if releasable_name is not None:
        target_changes_dir = get_releasable_changes_dir(
            workspace_root, releasable_name
        )
    else:
        target_changes_dir = get_changes_dir(
            os.path.join(workspace_root, package_name)
        )
    files_written, entries_migrated = _migrate_changelog_from_source(
        source_repo_path, target_changes_dir
    )

    return {
        "package_name": package_name,
        "source_path": source_repo_path,
        "source_branch": source_branch,
        "entries_migrated": entries_migrated,
        "files_written": files_written,
    }


def cmd_extract_releasable(
    workspace_root, releasable_name, target_repo_path, *, dry_run=False
):
    """Extract all member packages of a releasable into a new repository.

    If the releasable has one member, the result is a single-project repo.
    If it has multiple members, the result is a monorepo with workspace.toml.

    Steps:
    1. Resolve releasable members from workspace.toml
    2. Clone the monorepo
    3. Run ``git filter-repo --path <dir1> --path <dir2> ...`` for all
       member paths
    4. Migrate changelog for each member
    5. Create appropriate config (single-project or monorepo)
    6. Update source monorepo: remove all member projects from workspace.toml

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable to extract.
        target_repo_path: path where the new repo will be created.
        dry_run: if True, validate but do not perform the extraction.

    Returns:
        A dict with extraction details.
    """
    require_filter_repo()

    projects = load_workspace(workspace_root)
    releasables = load_releasables(workspace_root, projects)

    # Find the releasable
    target_releasable = None
    for rel in releasables:
        if rel.name == releasable_name:
            target_releasable = rel
            break
    if target_releasable is None:
        available = ", ".join(r.name for r in releasables)
        raise ExtractError(
            f"releasable '{releasable_name}' not found. Available: {available}"
        )

    member_projects = members_of(releasable_name, projects)
    if not member_projects:
        raise ExtractError(
            f"releasable '{releasable_name}' has no member packages"
        )

    if os.path.exists(target_repo_path):
        raise ExtractError(
            f"target path already exists: {target_repo_path}"
        )

    member_paths = [p.path for p in member_projects]
    member_names = [p.name for p in member_projects]
    is_multi = len(member_projects) > 1

    if dry_run:
        return {
            "releasable_name": releasable_name,
            "target_path": target_repo_path,
            "member_packages": member_names,
            "is_monorepo": is_multi,
            "dry_run": True,
        }

    # Clone the monorepo to target path
    _run_git(workspace_root, "clone", "--no-local", ".", target_repo_path)

    # Run git filter-repo to keep only the member paths
    filter_args = []
    for path in member_paths:
        filter_args.extend(["--path", path])
    filter_args.append("--force")

    subprocess.run(
        ["git-filter-repo"] + filter_args,
        cwd=target_repo_path,
        check=True,
        capture_output=True,
        text=True,
    )

    # For single-member releasable, move files to root
    if not is_multi:
        pkg_path = member_paths[0]
        subprocess.run(
            [
                "git-filter-repo",
                "--path-rename", f"{pkg_path}/:",
                "--force",
            ],
            cwd=target_repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    # Migrate changelogs
    total_entries = 0
    total_files = 0
    for proj in member_projects:
        source_changes_dir = get_changes_dir(
            os.path.join(workspace_root, proj.path)
        )
        if is_multi:
            target_changes_dir = os.path.join(
                target_repo_path, proj.path, ".rlsbl", "changes"
            )
        else:
            target_changes_dir = os.path.join(
                target_repo_path, ".rlsbl", "changes"
            )

        if os.path.isdir(source_changes_dir):
            fw, em = _migrate_changelog_to_new_repo(
                source_changes_dir, target_changes_dir, proj.path, workspace_root
            )
            total_files += fw
            total_entries += em

    # Create config
    if is_multi:
        # Create a monorepo workspace.toml in the new repo
        new_projects = []
        from ...workspace import WorkspaceProject as WP
        for proj in member_projects:
            new_proj = {"path": proj.path, "name": proj.name}
            new_projects.append(WP(new_proj))
        save_workspace(target_repo_path, new_projects)
    else:
        # Single-project repo: create .rlsbl/ config
        source_config = os.path.join(
            workspace_root, member_paths[0], ".rlsbl", "config.json"
        )
        _create_rlsbl_config(target_repo_path, source_config)

    # Update source monorepo: remove all member projects
    remaining = [p for p in projects if p.name not in member_names]
    # Also remove the releasable definition if in explicit mode
    remaining_releasables = [r for r in releasables if r.name != releasable_name]
    save_workspace(workspace_root, remaining, releasables=remaining_releasables)

    return {
        "releasable_name": releasable_name,
        "target_path": target_repo_path,
        "member_packages": member_names,
        "is_monorepo": is_multi,
        "entries_migrated": total_entries,
        "files_written": total_files,
    }

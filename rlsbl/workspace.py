"""Workspace data layer for monorepo support handling discovery, loading, saving, and resolution of workspaces from workspace.toml config."""

import os
import tomllib

import tomlkit


WORKSPACE_DIR = ".rlsbl-monorepo"
WORKSPACE_FILE = "workspace.toml"


def find_workspace_root(start_path="."):
    """Walk up from start_path looking for a .rlsbl-monorepo/workspace.toml.

    Returns the directory containing .rlsbl-monorepo/, or None if not found.
    """
    current = os.path.realpath(start_path)
    while True:
        candidate = os.path.join(current, WORKSPACE_DIR, WORKSPACE_FILE)
        if os.path.isfile(candidate):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_workspace(root):
    """Read and validate workspace.toml, returning the list of project dicts.

    Each project dict has at least 'path' (str) and 'name' (str, defaults to
    basename of path).

    Raises FileNotFoundError if workspace.toml doesn't exist.
    Raises ValueError on invalid structure.
    """
    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    if "projects" not in data:
        raise ValueError("workspace.toml missing required 'projects' key")

    projects = data["projects"]
    if not isinstance(projects, list):
        raise ValueError("'projects' must be a list of tables")

    result = []
    for i, proj in enumerate(projects):
        if not isinstance(proj, dict):
            raise ValueError(f"projects[{i}] must be a table, got {type(proj).__name__}")
        if "path" not in proj or not isinstance(proj["path"], str):
            raise ValueError(f"projects[{i}] missing required 'path' string")
        entry = dict(proj)
        # Normalize: strip trailing slashes so stored paths are consistent.
        # Belt-and-suspenders with target-level tag format defenses.
        entry["path"] = entry["path"].rstrip("/")
        if "name" not in entry or not entry["name"]:
            entry["name"] = os.path.basename(entry["path"])
        result.append(entry)

    return result


def save_workspace(root, projects):
    """Write workspace.toml atomically using tomlkit for clean TOML output.

    Preserves top-level sections, comments, and formatting from the existing
    file by reading it with tomlkit first and modifying the ``[[projects]]``
    array in-place.  Falls back to creating a new document when the file does
    not yet exist.

    Creates .rlsbl-monorepo/ directory if it doesn't exist.
    """
    ws_dir = os.path.join(root, WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)

    target = os.path.join(ws_dir, WORKSPACE_FILE)

    # Read existing document to preserve non-project sections/comments.
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            doc = tomlkit.loads(f.read())
        # Remove old projects key so we can replace it.
        if "projects" in doc:
            del doc["projects"]
    else:
        doc = tomlkit.document()

    if not projects:
        # Empty AoT produces no output in tomlkit; use inline array instead
        doc.add("projects", tomlkit.array())
    else:
        aot = tomlkit.aot()
        for proj in projects:
            table = tomlkit.table()
            table.add("path", proj["path"])
            table.add("name", proj["name"])
            for key in sorted(proj.keys()):
                if key not in ("path", "name"):
                    table.add(key, proj[key])
            aot.append(table)
        doc.add("projects", aot)

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp, target)


def resolve_project(root, cwd="."):
    """Determine which project cwd is inside, returning its dict or None.

    If multiple projects match (nested paths), returns the most specific one.
    """
    abs_root = os.path.realpath(root)
    abs_cwd = os.path.realpath(cwd)

    projects = load_workspace(root)

    best_match = None
    best_len = -1
    for proj in projects:
        proj_abs = os.path.realpath(os.path.join(abs_root, proj["path"]))
        # cwd must be the project dir or a subdirectory of it
        if abs_cwd == proj_abs or abs_cwd.startswith(proj_abs + os.sep):
            if len(proj_abs) > best_len:
                best_match = proj
                best_len = len(proj_abs)

    return best_match

"""Ecosystem keyword injection that adds the rlsbl tag to package manifests (package.json, pyproject.toml, etc.) and GitHub repository topics."""

import json
import os
import re
import subprocess

import tomlkit

from .utils import extract_github_repo_from_remote, run, run_gh, run_gh_unscoped
from . import effects


def ensure_npm_keyword(dir_path=".", quiet=False, *, project_root):
    """Add "rlsbl" to the keywords array in package.json if not already present."""
    if project_root is not None and dir_path == ".":
        dir_path = str(project_root)
    pkg_path = os.path.join(dir_path, "package.json")
    with open(pkg_path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Detect indent: look for the first indented line
    indent_match = re.search(r'^( +|\t+)"', raw, re.MULTILINE)
    indent = indent_match.group(1) if indent_match else "  "

    pkg = json.loads(raw)
    keywords = pkg.get("keywords", [])

    if "rlsbl" in keywords:
        return False

    keywords.append("rlsbl")
    pkg["keywords"] = keywords

    # Preserve trailing newline if present
    trailing_newline = "\n" if raw.endswith("\n") else ""
    output = json.dumps(pkg, indent=indent, ensure_ascii=False) + trailing_newline

    effects.atomic_write_text(pkg_path, output)

    if not quiet:
        print('Tagged package.json with "rlsbl" keyword')
    return True


def ensure_pypi_keyword(dir_path=".", quiet=False, *, project_root):
    """Add "rlsbl" to the keywords array in pyproject.toml if not already present."""
    if project_root is not None and dir_path == ".":
        dir_path = str(project_root)
    pyproject_path = os.path.join(dir_path, "pyproject.toml")
    with open(pyproject_path, "r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())

    project = doc.get("project")
    if project is None:
        return False

    keywords = project.get("keywords")
    if keywords is None:
        project["keywords"] = ["rlsbl"]
    elif "rlsbl" in keywords:
        return False
    else:
        keywords.append("rlsbl")

    effects.atomic_write_text(pyproject_path, tomlkit.dumps(doc))

    if not quiet:
        print('Tagged pyproject.toml with "rlsbl" keyword')
    return True


def ensure_github_topic(quiet=False):
    """Add "rlsbl" topic to the GitHub repository if not already present.

    Both API calls go through ``gh api``, which resolves the credential inside
    its own process (``GH_TOKEN`` / ``GITHUB_TOKEN`` / the stored login, in
    gh's own precedence).  rlsbl never asks for the raw token: putting a live
    credential on a captured stdout pipe is what the observe standard forbids
    (see :mod:`rlsbl.observe_allowlist`).
    """
    # Detect repo name
    repo_name = None
    try:
        repo_name = run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not repo_name:
        # Fallback: parse from git remote
        try:
            remote_url = run("git", ["remote", "get-url", "origin"])
            repo_name = extract_github_repo_from_remote(remote_url)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if not repo_name:
        if not quiet:
            print("Warning: could not detect GitHub repository name.")
        return False

    owner, repo = repo_name.split("/", 1)
    api_path = f"repos/{owner}/{repo}/topics"

    # GET existing topics.  ``--method GET`` is what makes this match the
    # GET-pinned observe prefix, so a preview really reads the current topics.
    try:
        raw = run_gh_unscoped(
            ["api", "--method", "GET", api_path], timeout=15,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        if not quiet:
            print(f"Warning: failed to fetch GitHub topics: {e}")
        return False
    if effects.unsettled(raw):
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if not quiet:
            print(f"Warning: failed to fetch GitHub topics: {e}")
        return False

    topics = data.get("names", [])
    if "rlsbl" in topics:
        return False

    # PUT with the merged topics list.  Not an observe: a preview records it.
    topics.append("rlsbl")
    argv = ["api", "--method", "PUT", api_path]
    for name in topics:
        argv += ["-f", f"names[]={name}"]
    try:
        run_gh_unscoped(argv, timeout=15)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        if not quiet:
            print(f"Warning: failed to set GitHub topics: {e}")
        return False

    if not quiet:
        print('Added "rlsbl" topic to GitHub repository')
    return True


def ensure_tags(registries, target_paths=None, quiet=False, *, project_root):
    """Tag manifests and GitHub repo based on detected registries."""
    if target_paths is None:
        target_paths = {}
    if "npm" in registries:
        ensure_npm_keyword(target_paths.get("npm", "."), quiet=quiet, project_root=project_root)
    if "pypi" in registries:
        ensure_pypi_keyword(target_paths.get("pypi", "."), quiet=quiet, project_root=project_root)
    ensure_github_topic(quiet=quiet)

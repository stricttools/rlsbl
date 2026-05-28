"""Ecosystem keyword injection that adds the rlsbl tag to package manifests (package.json, pyproject.toml, etc.) and GitHub repository topics."""

import json
import os
import re
import subprocess
import urllib.request
import urllib.error

import tomlkit

from .utils import run


def ensure_npm_keyword(dir_path=".", quiet=False, project_root=None):
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

    # Atomic write: write to temp file, then rename
    tmp_path = pkg_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(output)
    os.replace(tmp_path, pkg_path)

    if not quiet:
        print('Tagged package.json with "rlsbl" keyword')
    return True


def ensure_pypi_keyword(dir_path=".", quiet=False, project_root=None):
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

    tmp_path = pyproject_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
    os.replace(tmp_path, pyproject_path)

    if not quiet:
        print('Tagged pyproject.toml with "rlsbl" keyword')
    return True


def ensure_github_topic(quiet=False):
    """Add "rlsbl" topic to the GitHub repository if not already present."""
    # Try to get a GitHub token (env var first, then gh CLI)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            token = run("gh", ["auth", "token"])
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if not token:
        if not quiet:
            print("No GitHub token available. Run 'gh auth login' or set GITHUB_TOKEN.")
        return False

    # Detect repo name
    repo_name = None
    try:
        repo_name = run("gh", ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not repo_name:
        # Fallback: parse from git remote
        try:
            remote_url = run("git", ["remote", "get-url", "origin"])
            match = re.search(r"github\.com[/:]([^/]+/[^/.]+)", remote_url)
            if match:
                repo_name = match.group(1).removesuffix(".git")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if not repo_name:
        if not quiet:
            print("Warning: could not detect GitHub repository name.")
        return False

    owner, repo = repo_name.split("/", 1)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/topics"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "rlsbl-cli",
    }

    # GET existing topics
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        if not quiet:
            print(f"Warning: failed to fetch GitHub topics: {e}")
        return False

    topics = data.get("names", [])
    if "rlsbl" in topics:
        return False

    # PUT with merged topics list
    topics.append("rlsbl")
    payload = json.dumps({"names": topics}).encode("utf-8")
    try:
        req = urllib.request.Request(api_url, data=payload, headers=headers, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()  # consume response
    except (urllib.error.URLError, OSError) as e:
        if not quiet:
            print(f"Warning: failed to set GitHub topics: {e}")
        return False

    if not quiet:
        print('Added "rlsbl" topic to GitHub repository')
    return True


def ensure_tags(registries, target_paths=None, quiet=False, project_root=None):
    """Tag manifests and GitHub repo based on detected registries."""
    if target_paths is None:
        target_paths = {}
    if "npm" in registries:
        ensure_npm_keyword(target_paths.get("npm", "."), quiet=quiet, project_root=project_root)
    if "pypi" in registries:
        ensure_pypi_keyword(target_paths.get("pypi", "."), quiet=quiet, project_root=project_root)
    ensure_github_topic(quiet=quiet)

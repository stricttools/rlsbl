"""Registry version query functions for npm, PyPI, and the Go proxy, returning the latest published version for name availability and dependency checks."""

import json
import urllib.error

from rlsbl.commands.check import _request_with_backoff


def query_npm_version(name):
    """Query the npm registry for the latest version of a package.

    Returns {"status": "found", "version": "X.Y.Z"} on success,
    {"status": "not_found"} if the package does not exist,
    or {"status": "error", "message": "..."} on failure.
    """
    url = f"https://registry.npmjs.org/{name}"
    try:
        with _request_with_backoff(url) as resp:
            data = json.loads(resp.read())
        version = data.get("dist-tags", {}).get("latest")
        if version is None:
            return {"status": "error", "message": "No dist-tags.latest in response"}
        return {"status": "found", "version": version}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found"}
        return {"status": "error", "message": f"HTTP {e.code}"}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def query_pypi_version(name):
    """Query PyPI for the latest version of a package.

    Returns {"status": "found", "version": "X.Y.Z"} on success,
    {"status": "not_found"} if the package does not exist,
    or {"status": "error", "message": "..."} on failure.
    """
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with _request_with_backoff(url) as resp:
            data = json.loads(resp.read())
        version = data.get("info", {}).get("version")
        if version is None:
            return {"status": "error", "message": "No info.version in response"}
        return {"status": "found", "version": version}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found"}
        return {"status": "error", "message": f"HTTP {e.code}"}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def query_pypi_release(name, version):
    """Ask PyPI whether ONE specific version of a package is published.

    ``query_pypi_version`` answers "what is the latest?", which cannot decide
    whether an older or a just-locked version exists on the index. This one
    reads the per-release endpoint instead.

    Returns {"status": "found"} when the release exists,
    {"status": "not_found"} when the package or that version does not,
    or {"status": "error", "message": "..."} on failure. Callers that must be
    certain treat "error" as a refusal, never as absence.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with _request_with_backoff(url) as resp:
            resp.read()
        return {"status": "found"}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found"}
        return {"status": "error", "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def query_go_version(module_path):
    """Query the Go module proxy for the latest version of a module.

    Returns {"status": "found", "version": "X.Y.Z"} on success (v prefix stripped),
    {"status": "not_found"} if the module does not exist,
    or {"status": "error", "message": "..."} on failure.
    """
    url = f"https://proxy.golang.org/{module_path}/@latest"
    try:
        with _request_with_backoff(url) as resp:
            data = json.loads(resp.read())
        version = data.get("Version")
        if version is None:
            return {"status": "error", "message": "No Version in response"}
        if version.startswith("v"):
            version = version[1:]
        return {"status": "found", "version": version}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found"}
        return {"status": "error", "message": f"HTTP {e.code}"}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


def query_registry_version(name, registry):
    """Query a registry for the latest version of a package.

    The registry argument is a TARGET NAME, and the target answers: each one
    that has a version API overrides ``query_latest_version``, and the base
    implementation answers ``Unknown registry`` for the rest. This replaced a
    hand-maintained dispatch dict that had to be edited alongside the target
    registry and could silently disagree with it.

    Returns {"status": "found", "version": "X.Y.Z"} on success,
    {"status": "not_found"} if the package does not exist,
    {"status": "error", "message": "..."} on failure,
    or {"status": "error", "message": "Unknown registry: ..."} for
    unrecognized registry names.
    """
    from .targets import TARGETS

    target = TARGETS.get(registry)
    if target is None:
        return {"status": "error", "message": f"Unknown registry: {registry}"}
    return target.query_latest_version(name)

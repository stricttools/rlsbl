"""Registry version query functions for npm, PyPI, Go proxy, and crates.io, returning the latest published version for name availability and dependency checks."""

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


def query_crates_version(name):
    """Query crates.io for the latest version of a crate.

    Returns {"status": "found", "version": "X.Y.Z"} on success,
    {"status": "not_found"} if the crate does not exist,
    or {"status": "error", "message": "..."} on failure.
    """
    url = f"https://crates.io/api/v1/crates/{name}"
    headers = {"User-Agent": "rlsbl-cli"}
    try:
        with _request_with_backoff(url, headers=headers) as resp:
            data = json.loads(resp.read())
        version = data.get("crate", {}).get("max_version")
        if version is None:
            return {"status": "error", "message": "No crate.max_version in response"}
        return {"status": "found", "version": version}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found"}
        return {"status": "error", "message": f"HTTP {e.code}"}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e) or "Network error"}


_REGISTRY_DISPATCH = {
    "npm": query_npm_version,
    "pypi": query_pypi_version,
    "go": query_go_version,
    "cargo": query_crates_version,
}


def query_registry_version(name, registry):
    """Query a registry for the latest version of a package.

    Dispatches to the appropriate registry-specific function based on
    the registry parameter ("npm", "pypi", "go", or "cargo").

    Returns {"status": "found", "version": "X.Y.Z"} on success,
    {"status": "not_found"} if the package does not exist,
    {"status": "error", "message": "..."} on failure,
    or {"status": "error", "message": "Unknown registry: ..."} for
    unrecognized registry names.
    """
    fn = _REGISTRY_DISPATCH.get(registry)
    if fn is None:
        return {"status": "error", "message": f"Unknown registry: {registry}"}
    return fn(name)

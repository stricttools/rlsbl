"""Dev-overlay sentinel loading and venv inspection, shared between the
``rlsbl dev sync``/``dev status`` commands and the ``dev-overlay-drift`` check.

Extracted from commands/dev_sync.py to break the checks/ -> commands/ layering
violation: checks/project.py's ``dev-overlay-drift`` check needs to read the
overlay sentinel and inspect the venv, but the checks layer must not import the
commands layer. This module hosts the read-side logic (sentinel loading, venv
dist-info inspection, overlay classification) so both layers can consume it
without a cross-layer import. The write-side (``_write_sentinel``) and the sync
orchestration stay in commands/dev_sync.py, which is the only writer.

Pure relocation -- no behavior change from the original dev_sync definitions.
"""

import glob
import json
import os
import re
import tomllib
from importlib.metadata import Distribution, DistributionFinder, MetadataPathFinder
from urllib.parse import unquote, urlparse

from .utils import detect_uv_workspace_root


class MalformedSentinelError(Exception):
    """Raised when the overlay sentinel exists but cannot be parsed or read.

    A present-but-corrupt sentinel must never read as "no overlays declared"
    (which would silently SKIP the drift check and exit `dev status` 0). The
    sentinel is regenerable local state, so the remedy is to delete it and
    re-run `rlsbl dev sync`. Callers surface this loudly rather than degrading.
    """


# Written by _write_sentinel after successful overlays; read by the
# `dev-overlay-drift` check and `rlsbl dev status` to detect when a bare
# `uv sync`/`uv run` silently reinstalled the registry wheel over an overlay.
# Gitignored fleet-wide via the same *.local-only pattern as the overrides
# file (verified against rlsbl's .gitignore and the shared scaffold template).
SENTINEL_FILENAME = "dev-overlays-state.toml.local-only"

# Overlay health states shared by the drift check and `dev status`.
OVERLAY_HEALTHY = "healthy"
OVERLAY_WIPED = "wiped"
OVERLAY_MISSING = "missing"


def _normalize(name):
    """PEP 503 distribution-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_sentinel(project_root):
    """Read SENTINEL_FILENAME. Returns a list of
    {"package", "path", "version"} dicts, or None when the sentinel does not
    exist.

    A missing sentinel means no overlays were ever declared -- e.g. a fresh CI
    checkout, where the gitignored sentinel never existed. That is the honest
    not-applicable state (skip), never a failure.

    A present-but-unparseable sentinel (invalid TOML) or a present-but-
    unreadable one (OSError) is a hard error (:class:`MalformedSentinelError`),
    NEVER a silent empty list: reading corruption as "no overlays" would make
    the drift check SKIP and `dev status` exit 0 while overlays may in fact be
    wiped. Mirrors ``_load_overlays``, which also hard-errors on invalid TOML.
    """
    file_path = os.path.join(str(project_root), SENTINEL_FILENAME)
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise MalformedSentinelError(
            f"Error: the dev-overlay sentinel {file_path} exists but is not "
            f"valid TOML: {e}. The sentinel is regenerable local state -- "
            f"delete it and re-run `rlsbl dev sync` to rewrite it."
        )
    except OSError as e:
        raise MalformedSentinelError(
            f"Error: the dev-overlay sentinel {file_path} exists but could not "
            f"be read: {e}. The sentinel is regenerable local state -- delete "
            f"it and re-run `rlsbl dev sync` to rewrite it."
        )
    entries = data.get("overlay") or []
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "package": entry.get("package"),
                "path": entry.get("path"),
                "version": entry.get("version") or None,
            }
        )
    return result


def project_environment(project_root):
    """Return the directory of the environment uv manages for *project_root*.

    uv gives a project exactly ONE environment, and it is not always a ``.venv``
    beside the project's own pyproject.toml:

    - A **uv workspace member** shares the environment at the WORKSPACE ROOT
      (``uv sync`` and ``uv pip install`` from inside the member both target
      it), and has no ``.venv`` of its own. Looking only under the member
      directory reported a perfectly healthy overlay as missing.
    - ``UV_PROJECT_ENVIRONMENT`` relocates it entirely; a relative value is
      resolved against the workspace root. Read here for the same reason: it
      is uv's own selection of the directory this function has to describe,
      not rlsbl configuration.
    """
    root = os.path.abspath(str(project_root))
    base = detect_uv_workspace_root(root) or root
    override = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if override:
        return override if os.path.isabs(override) else os.path.join(base, override)
    return os.path.join(base, ".venv")


def _site_packages_dirs(env_dir):
    """Return the existing ``site-packages`` directories inside an environment
    (one per Python minor version present)."""
    patterns = (
        os.path.join(env_dir, "lib", "python*", "site-packages"),
        os.path.join(env_dir, "Lib", "site-packages"),  # Windows layout
    )
    found = []
    for pattern in patterns:
        found.extend(d for d in glob.glob(pattern) if os.path.isdir(d))
    return found


def _read_direct_url(dist):
    """Return ``(editable, path)`` from an installed distribution's
    ``direct_url.json`` (PEP 610).

    A registry wheel has no ``direct_url.json`` -> ``(False, None)``. An
    editable install writes ``dir_info.editable = true`` and a ``file://`` url
    pointing at the checkout -> ``(True, "/abs/checkout")``. A non-editable
    local install -> ``(False, "/abs/path")``.

    This record is the only honest evidence of editability. An editable install
    does NOT put a package directory in site-packages -- it writes a ``.pth``
    import hook whose name and content vary by build backend -- so nothing in
    the file layout may be used to decide this.
    """
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, None
    editable = bool((data.get("dir_info") or {}).get("editable"))
    url = data.get("url")
    path = None
    if isinstance(url, str) and url.startswith("file:"):
        path = unquote(urlparse(url).path)
    return editable, path


def inspect_installed(project_root, package):
    """Inspect the project's environment for how *package* is installed.

    Returns ``{"found", "editable", "path", "version", "environment"}``:
    - ``found=False``: the environment holds no distribution named *package*.
    - ``editable=True`` with ``path``: an editable install; ``path`` is the
      ``file://`` checkout its ``direct_url.json`` points at.
    - ``editable=False``: a registry wheel or non-editable install -- i.e. the
      overlay was wiped.
    - ``environment``: the environment directory that was inspected, so a
      not-found answer can say where it looked.

    Distributions are read through ``importlib.metadata`` pointed at the
    environment's ``site-packages``, so dist-info naming, metadata parsing and
    the PEP 610 record all follow the packaging standard rather than a
    hand-rolled guess at the on-disk layout.
    """
    target = _normalize(package)
    env_dir = project_environment(project_root)
    sites = _site_packages_dirs(env_dir)
    # The environment is foreign, mutable state (a `uv sync` between two calls
    # in one process changes it), so never answer from importlib's path cache.
    MetadataPathFinder.invalidate_caches()
    context = DistributionFinder.Context(path=sites)
    for dist in Distribution.discover(context=context):
        try:
            name = dist.metadata["Name"]
        except Exception:
            name = None
        if not name or _normalize(name) != target:
            continue
        editable, path = _read_direct_url(dist)
        try:
            version = dist.version
        except Exception:
            version = None
        return {
            "found": True,
            "editable": editable,
            "path": path,
            "version": version,
            "environment": env_dir,
        }
    return {
        "found": False,
        "editable": False,
        "path": None,
        "version": None,
        "environment": env_dir,
    }


def classify_overlay(entry, installed):
    """Compare a sentinel *entry* against the *installed* venv state.

    Returns ``(state, detail)`` where *state* is OVERLAY_HEALTHY /
    OVERLAY_WIPED / OVERLAY_MISSING and *detail* is a human-readable line
    naming the package and the exact ``rlsbl dev sync`` remediation.
    """
    package = entry["package"]
    declared_path = entry["path"]

    if not installed["found"]:
        env = installed.get("environment")
        where = f" ({env})" if env else ""
        return (
            OVERLAY_MISSING,
            f"{package}: declared as an editable overlay of {declared_path} "
            f"but not installed in the project environment{where} at all "
            "-- run `rlsbl dev sync`",
        )
    if not installed["editable"]:
        actual_version = installed["version"] or "unknown version"
        return (
            OVERLAY_WIPED,
            f"{package}: overlay wiped -- now a registry install "
            f"({actual_version}), no longer editable at {declared_path}. A "
            "bare `uv sync`/`uv run` reinstalled the locked wheel; run "
            "`rlsbl dev sync` to restore the overlay",
        )

    inst_path = installed["path"]
    if inst_path is None or (
        os.path.realpath(inst_path) != os.path.realpath(declared_path)
    ):
        return (
            OVERLAY_WIPED,
            f"{package}: editable install points at {inst_path}, not the "
            f"declared overlay path {declared_path} -- run `rlsbl dev sync`",
        )

    return (
        OVERLAY_HEALTHY,
        f"{package}: editable at {declared_path} "
        f"(version {installed['version'] or 'dynamic'})",
    )

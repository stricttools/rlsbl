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
from urllib.parse import unquote, urlparse


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


def _venv_site_packages(project_root):
    """Return existing ``site-packages`` directories under the project's
    ``.venv`` (one per Python minor version present)."""
    pattern = os.path.join(
        str(project_root), ".venv", "lib", "python*", "site-packages"
    )
    return [d for d in glob.glob(pattern) if os.path.isdir(d)]


def _read_dist_info_metadata(dist_info):
    """Return ``(name, version)`` from a ``*.dist-info`` directory's METADATA
    file, falling back to the directory-name split when METADATA is absent.
    Either element may be None if unreadable."""
    meta_path = os.path.join(dist_info, "METADATA")
    if os.path.isfile(meta_path):
        name = version = None
        try:
            with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        break  # blank line ends the RFC822 header block
                    if name is None and line.startswith("Name:"):
                        name = line[len("Name:"):].strip()
                    elif version is None and line.startswith("Version:"):
                        version = line[len("Version:"):].strip()
                    if name and version:
                        break
        except OSError:
            return None, None
        return name, version

    base = os.path.basename(dist_info)
    if base.endswith(".dist-info"):
        stem = base[: -len(".dist-info")]
        name, _, version = stem.rpartition("-")
        if name:
            return name, version
    return None, None


def _read_direct_url(dist_info):
    """Return ``(editable, path)`` from a dist-info's ``direct_url.json``.

    A registry wheel has no ``direct_url.json`` -> ``(False, None)``. A uv
    editable install writes ``dir_info.editable = true`` and a ``file://`` url
    pointing at the checkout -> ``(True, "/abs/checkout")``. A non-editable
    local install -> ``(False, "/abs/path")``.
    """
    du_path = os.path.join(dist_info, "direct_url.json")
    if not os.path.isfile(du_path):
        return False, None
    try:
        with open(du_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, None
    editable = bool((data.get("dir_info") or {}).get("editable"))
    url = data.get("url")
    path = None
    if isinstance(url, str) and url.startswith("file:"):
        path = unquote(urlparse(url).path)
    return editable, path


def inspect_installed(project_root, package):
    """Inspect the project's ``.venv`` for how *package* is installed.

    Returns ``{"found", "editable", "path", "version"}``:
    - ``found=False``: no dist-info for *package* in the venv (missing).
    - ``editable=True`` with ``path``: uv editable install; ``path`` is the
      ``file://`` checkout it points at.
    - ``editable=False``: a registry wheel or non-editable install -- i.e. the
      overlay was wiped.
    """
    target = _normalize(package)
    for site in _venv_site_packages(project_root):
        for dist_info in glob.glob(os.path.join(site, "*.dist-info")):
            meta_name, meta_version = _read_dist_info_metadata(dist_info)
            if meta_name is None or _normalize(meta_name) != target:
                continue
            editable, path = _read_direct_url(dist_info)
            return {
                "found": True,
                "editable": editable,
                "path": path,
                "version": meta_version,
            }
    return {"found": False, "editable": False, "path": None, "version": None}


def classify_overlay(entry, installed):
    """Compare a sentinel *entry* against the *installed* venv state.

    Returns ``(state, detail)`` where *state* is OVERLAY_HEALTHY /
    OVERLAY_WIPED / OVERLAY_MISSING and *detail* is a human-readable line
    naming the package and the exact ``rlsbl dev sync`` remediation.
    """
    package = entry["package"]
    declared_path = entry["path"]

    if not installed["found"]:
        return (
            OVERLAY_MISSING,
            f"{package}: declared as an editable overlay of {declared_path} "
            "but not installed in the venv at all -- run `rlsbl dev sync`",
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

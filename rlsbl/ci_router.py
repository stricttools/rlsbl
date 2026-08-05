"""Shared CI-router job-key helpers for monorepo workflows, providing the single source of truth for project-prefixed job keys in ci-router.yml.

The monorepo CI router (``ci-router.yml``) inlines every project's CI jobs,
keyed ``<prefix>-<job>`` where the prefix is the CI file name minus ``.yml``.
These helpers are the single source of truth for those prefixes, consumed by
the inline publish gate (``commands.monorepo.publish_inline``) and the
``workspace-ci-synced`` check. They live in this low-level module so the check
layer can use them without importing from the command layer.
"""

import glob
import os
import re

# The generated router itself, which must never be read back as a project CI
# source (the transform pipeline is not idempotent on its own output).
CI_ROUTER_FILE = "ci-router.yml"


def discover_project_ci_sources(project_dir) -> list[str]:
    """Absolute paths of *project_dir*'s own CI workflow source files.

    ``ci.yml`` plus every ``ci-*.yml`` except the generated router. This is
    the ONE discovery shared by ``monorepo sync`` (which inlines these files
    into the router, minting the job keys) and the release CI gate (which
    must know the check-run names those keys produce), so the router and the
    gate can never be derived from different file sets.
    """
    wf_dir = os.path.join(str(project_dir), ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return []
    sources = []
    single = os.path.join(wf_dir, "ci.yml")
    if os.path.isfile(single):
        sources.append(single)
    for path in sorted(glob.glob(os.path.join(wf_dir, "ci-*.yml"))):
        if os.path.basename(path) != CI_ROUTER_FILE:
            sources.append(path)
    return sources


def router_ci_dest_name(project_name: str, ci_basename: str) -> str:
    """Router-side file name for one of *project_name*'s CI files.

    ``ci.yml`` -> ``{name}-ci.yml``; ``ci-{target}.yml`` ->
    ``{name}-ci-{target}.yml``. The name minus ``.yml`` is the job-key prefix
    the router stamps into every inlined job's display name.
    """
    if ci_basename == "ci.yml":
        return f"{project_name}-ci.yml"
    return f"{project_name}-{ci_basename}"


def router_ci_files(project_name: str, project_dir) -> list[str]:
    """The router file names *project_name*'s own CI sources map to."""
    return [
        router_ci_dest_name(project_name, os.path.basename(src))
        for src in discover_project_ci_sources(project_dir)
    ]


def _router_ci_job_keys(project, project_dir=None) -> list[str]:
    """Return the CI router's job-key prefixes for *project*.

    Mirrors ``sync._generate_router``: every CI file uses the file name minus
    ``.yml`` as its job-key prefix. Reusable-workflow check runs are named
    ``<prefix> / <ci job name>``, which is what the shared gate matches.

    ``_ci_files`` is populated by ``monorepo sync``. Callers outside sync
    (the release CI gate) pass *project_dir* instead, and the same discovery
    that fed sync recomputes the list from disk.
    """
    name = project["name"]
    # ``project`` is a Mapping-like object -- a plain dict or a
    # ``WorkspaceProject`` (returned by ``load_workspace``), both of which
    # support ``.get``. Guarding on ``isinstance(project, dict)`` silently
    # dropped ``_ci_files`` for ``WorkspaceProject`` and fell back to
    # ``<name>-ci``, producing a gate regex that never matched the real
    # per-target check-run name ``<name>-ci-<target> / test``.
    ci_files = project.get("_ci_files")
    if not ci_files and project_dir is not None:
        ci_files = router_ci_files(name, project_dir)
    if not ci_files:
        ci_files = [f"{name}-ci.yml"]
    return [ci_file.removesuffix(".yml") for ci_file in ci_files]


def _router_ci_check_regex(project, project_dir=None) -> str:
    """Regex matching *project*'s prefixed CI check-run names."""
    alternation = "|".join(
        re.escape(k) for k in _router_ci_job_keys(project, project_dir)
    )
    return f"^({alternation}) / "

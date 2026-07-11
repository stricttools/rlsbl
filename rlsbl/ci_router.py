"""Shared CI-router job-key helpers.

The monorepo CI router (``ci-router.yml``) inlines every project's CI jobs,
keyed ``<prefix>-<job>`` where the prefix is the CI file name minus ``.yml``.
These helpers are the single source of truth for those prefixes, consumed by
the inline publish gate (``commands.monorepo.publish_inline``) and the
``workspace-ci-synced`` check. They live in this low-level module so the check
layer can use them without importing from the command layer.
"""

import re


def _router_ci_job_keys(project) -> list[str]:
    """Return the CI router's job-key prefixes for *project*.

    Mirrors ``sync._generate_router``: every CI file uses the file name minus
    ``.yml`` as its job-key prefix. Reusable-workflow check runs are named
    ``<prefix> / <ci job name>``, which is what the shared gate matches.
    """
    name = project["name"]
    ci_files = project.get("_ci_files") if isinstance(project, dict) else None
    if not ci_files:
        ci_files = [f"{name}-ci.yml"]
    return [ci_file.removesuffix(".yml") for ci_file in ci_files]


def _router_ci_check_regex(project) -> str:
    """Regex matching *project*'s prefixed CI check-run names."""
    alternation = "|".join(re.escape(k) for k in _router_ci_job_keys(project))
    return f"^({alternation}) / "

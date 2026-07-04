"""Inline publish logic for monorepo projects: workflow parsing and YAML emission."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from io import StringIO

from ruamel.yaml import YAML

from ...errors import ConfigError
from ...publish_gate import (
    GATE_JOB_KEY,
    build_router_gate_job,
    publish_concurrency_block,
)
from .sync import _get_monorepo_tag_prefix


def parse_publish_workflow(path: str) -> dict:
    """Parse a GitHub Actions publish workflow file.

    Reads the YAML file at *path*, validates it has a ``jobs:`` key, and
    returns a dict with the top-level keys that matter for inline publish
    generation.

    Returns a dict with keys:
        jobs       -- the ``jobs`` mapping from the workflow
        permissions -- workflow-level ``permissions`` mapping, or None
        env        -- workflow-level ``env`` mapping, or None
        name       -- workflow ``name`` string, or None
    """
    with open(path) as f:
        data = YAML(typ='safe').load(f)

    if not isinstance(data, dict) or "jobs" not in data:
        raise ConfigError(f"Workflow file {path} is missing a 'jobs' key")

    return {
        "jobs": data["jobs"],
        "permissions": data.get("permissions"),
        "env": data.get("env"),
        "name": data.get("name"),
    }


def _literal_str_representer(representer, data):
    """Represent multi-line strings with ``|`` literal block style."""
    if "\n" in data:
        return representer.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return representer.represent_scalar("tag:yaml.org,2002:str", data)


def emit_workflow(workflow_dict: dict) -> str:
    """Emit a workflow dict as a YAML string.

    Uses literal block style (``|``) for multi-line strings and preserves
    key order.  The custom representer is registered on a private Dumper
    subclass so the global ``yaml`` state is never modified.
    """
    yml = YAML()
    yml.default_flow_style = False
    yml.representer.add_representer(str, _literal_str_representer)
    stream = StringIO()
    yml.dump(workflow_dict, stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Job transformation pipeline
# ---------------------------------------------------------------------------


def prefix_jobs(project_name: str, jobs: dict) -> dict:
    """Prefix every job key with ``{project_name}-`` and rewrite ``needs:`` references.

    ``needs: gate`` references are preserved as-is: the router emits ONE
    shared gate job, so member references to it must never be force-prefixed
    (a ``{project}-gate`` need would dangle).

    Returns a new dict; the original *jobs* is not mutated.
    """
    jobs = copy.deepcopy(jobs)
    # Build old→new key mapping; the shared gate maps to itself.
    key_map = {key: f"{project_name}-{key}" for key in jobs}
    key_map[GATE_JOB_KEY] = GATE_JOB_KEY

    result: dict = {}
    for old_key, job in jobs.items():
        needs = job.get("needs")
        if needs is not None:
            if isinstance(needs, str):
                job["needs"] = key_map.get(needs, f"{project_name}-{needs}")
            elif isinstance(needs, list):
                job["needs"] = [
                    key_map.get(n, f"{project_name}-{n}") for n in needs
                ]
        result[key_map[old_key]] = job

    return result


def inject_job_metadata(jobs: dict, tag_prefix: str, working_dir: str) -> dict:
    """Add ``if:`` condition and ``defaults.run.working-directory`` to every job.

    Returns a new dict; the original *jobs* is not mutated.
    """
    jobs = copy.deepcopy(jobs)
    result: dict = {}
    for key, job in jobs.items():
        # Ref-based condition: true for release events AND workflow_dispatch
        # retries at the tag ref. The release event payload is empty on
        # dispatches, so matching github.event.release.tag_name would skip
        # every job on a retry.
        job["if"] = f"startsWith(github.ref_name, '{tag_prefix}')"

        defaults = job.get("defaults", {})
        run_block = defaults.get("run", {})
        run_block["working-directory"] = working_dir
        defaults["run"] = run_block
        job["defaults"] = defaults

        result[key] = job
    return result


_SETUP_VERSION_FILE_KEYS = {
    "actions/setup-go": "go-version-file",
    "actions/setup-python": "python-version-file",
    "actions/setup-node": "node-version-file",
}


def rewrite_action_paths(jobs: dict, project_path: str) -> dict:
    """Rewrite action inputs that contain file paths so they are relative to *project_path*.

    Handles:
    - ``pypa/gh-action-pypi-publish``: sets ``with.packages-dir`` to ``{project_path}/dist/``
    - ``actions/setup-{go,python,node}``: prefixes version-file paths with *project_path*

    Returns a new dict; the original *jobs* is not mutated.
    """
    jobs = copy.deepcopy(jobs)

    for job in jobs.values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")

            # PyPI publish action
            if "pypa/gh-action-pypi-publish" in uses:
                with_block = step.setdefault("with", {})
                with_block["packages-dir"] = f"{project_path}/dist/"

            # Setup actions with version-file inputs
            for action_substring, version_key in _SETUP_VERSION_FILE_KEYS.items():
                if action_substring in uses:
                    with_block = step.get("with", {})
                    if version_key in with_block:
                        val = with_block[version_key]
                        if isinstance(val, str) and not val.startswith(f"{project_path}/"):
                            with_block[version_key] = f"{project_path}/{val}"

    return jobs


def resolve_permissions(jobs: dict, workflow_permissions: dict | None) -> dict:
    """Push workflow-level permissions down to jobs that lack their own.

    - Jobs with an explicit ``permissions:`` key keep it unchanged.
    - Jobs without ``permissions:`` inherit *workflow_permissions* (if non-None).
    - If *workflow_permissions* is None and the job has no permissions, nothing is added.

    Returns a new dict; the original *jobs* is not mutated.
    """
    jobs = copy.deepcopy(jobs)

    for job in jobs.values():
        if "permissions" not in job and workflow_permissions is not None:
            job["permissions"] = copy.deepcopy(workflow_permissions)

    return jobs


def transform_project_jobs(
    project_name: str,
    project_path: str,
    tag_prefix: str,
    workflow_path: str,
) -> dict:
    """Parse a sub-project's publish workflow and transform its jobs for the monorepo router.

    Applies all transforms in the correct order:
    1. strip the member's own gate job (the router emits ONE shared gate)
    2. resolve_permissions (before prefixing — references original job structure)
    3. rewrite_action_paths
    4. inject_job_metadata
    5. prefix_jobs (last — changes keys; ``needs: gate`` is preserved)
    6. ensure every inlined job depends on the shared gate

    Returns a dict of transformed jobs ready for merging into the root workflow.
    """
    workflow = parse_publish_workflow(workflow_path)
    jobs = dict(workflow["jobs"])

    # The member workflow's gate polls the member's standalone CI check
    # names, which do not exist in a monorepo (CI runs via the router as
    # reusable workflows). The router's shared gate replaces it.
    jobs.pop(GATE_JOB_KEY, None)

    jobs = resolve_permissions(jobs, workflow.get("permissions"))
    jobs = rewrite_action_paths(jobs, project_path)
    jobs = inject_job_metadata(jobs, tag_prefix, project_path)
    jobs = prefix_jobs(project_name, jobs)

    # Wire every inlined job to the shared gate (covers member workflows
    # scaffolded before the gate existed).
    for job in jobs.values():
        needs = job.get("needs")
        if needs is None:
            job["needs"] = GATE_JOB_KEY
        elif isinstance(needs, str):
            if needs != GATE_JOB_KEY:
                job["needs"] = [GATE_JOB_KEY, needs]
        elif GATE_JOB_KEY not in needs:
            job["needs"] = [GATE_JOB_KEY, *needs]

    return jobs


# ---------------------------------------------------------------------------
# Inline publish router generator
# ---------------------------------------------------------------------------


def _router_ci_job_keys(project) -> list[str]:
    """Return the CI router's job keys for *project*.

    Mirrors ``sync._generate_router``: every CI file uses the file name
    minus ``.yml`` as its job key. Reusable-workflow check runs are named
    ``<job key> / <ci job name>``, which is what the shared gate matches.
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


def generate_inline_publish_router(projects_with_publish: list, root: str, releasables=None) -> str:
    """Generate a monorepo publish router with all sub-project jobs inlined.

    Instead of calling per-project reusable workflows via ``workflow_call``,
    this inlines every sub-project's publish jobs directly into a single
    ``publish.yml``.  Each job gets an ``if: startsWith(github.ref_name,
    ...)`` condition so only the relevant project's jobs run on a given
    release -- ref-based so a ``workflow_dispatch`` retry at the tag ref
    hits the same jobs as the original release event.

    A single shared ``gate`` job blocks all inlined publish jobs until the
    releasing project's CI check runs (resolved from the tag ref) conclude
    successfully. Member gate jobs are stripped during inlining.

    When *releasables* are provided, tag prefixes are derived from the
    releasable's ``tag_format`` instead of the target's ``monorepo_tag_glob``.

    Returns the complete YAML string, ready to write to disk.
    """
    all_jobs: dict = {
        "no-op": {
            "runs-on": "ubuntu-latest",
            "if": "true",
            "permissions": {},
            "steps": [{"run": "echo 'No publish target for this release tag'"}],
        },
    }

    prefix_regex_pairs: list = []
    for project in projects_with_publish:
        tag_prefix = _get_monorepo_tag_prefix(project, root, releasables=releasables)
        prefix_regex_pairs.append((tag_prefix, _router_ci_check_regex(project)))
        workflow_path = os.path.join(
            root, project["path"], ".github", "workflows", "publish.yml"
        )
        jobs = transform_project_jobs(
            project["name"],
            project["path"].rstrip("/"),
            tag_prefix,
            workflow_path,
        )
        all_jobs.update(jobs)

    all_jobs = {
        GATE_JOB_KEY: build_router_gate_job(prefix_regex_pairs),
        **all_jobs,
    }

    workflow_dict = {
        "name": "Publish Router",
        "on": {"release": {"types": ["published"]}, "workflow_dispatch": None},
        # Per-ref publish concurrency: a dispatch retry at the same tag
        # queues behind the in-flight run; never cancel a publish.
        "concurrency": publish_concurrency_block(),
        "jobs": all_jobs,
    }

    yaml_str = emit_workflow(workflow_dict)
    header = (
        "# DO NOT EDIT -- generated by rlsbl monorepo sync\n"
        "# Retry contract: to re-run a release publish, dispatch this workflow\n"
        "# AT THE TAG REF: gh workflow run publish.yml --ref <tag>. Job\n"
        "# conditions and the gate resolve the releasing project from the ref,\n"
        "# never from the release event payload (dispatches have none).\n"
    )
    return header + yaml_str


# ---------------------------------------------------------------------------
# Publish hash cache (skip unnecessary regeneration)
# ---------------------------------------------------------------------------

PUBLISH_CACHE_FILENAME = "publish-cache.json"


def compute_publish_hashes(projects: list, root: str) -> dict:
    """Compute SHA256 hashes of each project's publish workflow.

    Returns a dict mapping project name to the hex digest of its
    ``publish.yml`` content, or ``None`` if the project has no publish
    workflow.
    """
    result: dict = {}
    for project in projects:
        wf_path = os.path.join(
            root, project["path"], ".github", "workflows", "publish.yml"
        )
        if os.path.isfile(wf_path):
            with open(wf_path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            result[project["name"]] = digest
        else:
            result[project["name"]] = None
    return result


def load_publish_cache(monorepo_dir: str) -> dict | None:
    """Load the publish hash cache from *monorepo_dir*.

    Returns the parsed dict, or ``None`` if the cache file does not
    exist or contains invalid JSON.
    """
    cache_path = os.path.join(monorepo_dir, PUBLISH_CACHE_FILENAME)
    if not os.path.isfile(cache_path):
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_publish_cache(monorepo_dir: str, hashes: dict) -> str:
    """Write the publish hash cache to *monorepo_dir*.

    Returns the absolute path to the written cache file.
    """
    cache_path = os.path.join(monorepo_dir, PUBLISH_CACHE_FILENAME)
    with open(cache_path, "w") as f:
        json.dump(hashes, f, indent=2)
        f.write("\n")
    return cache_path


def should_regenerate_router(
    cached: dict | None, current: dict, router_path: str
) -> bool:
    """Decide whether the publish router needs regeneration.

    Returns ``False`` (skip) only when *cached* matches *current* exactly
    AND *router_path* exists on disk.  Any mismatch -- missing cache,
    changed hash, added/removed project, missing router file -- returns
    ``True``.
    """
    if cached is None:
        return True
    if cached != current:
        return True
    if not os.path.isfile(router_path):
        return True
    return False

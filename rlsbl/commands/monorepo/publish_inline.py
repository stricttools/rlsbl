"""Inline publish logic for monorepo projects: workflow parsing and YAML emission."""

from __future__ import annotations

import copy

import yaml


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
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "jobs" not in data:
        raise ValueError(f"Workflow file {path} is missing a 'jobs' key")

    return {
        "jobs": data["jobs"],
        "permissions": data.get("permissions"),
        "env": data.get("env"),
        "name": data.get("name"),
    }


class _LiteralBlockDumper(yaml.SafeDumper):
    """SafeDumper subclass that emits multi-line strings as YAML literal blocks."""


def _literal_str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Represent multi-line strings with ``|`` literal block style."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralBlockDumper.add_representer(str, _literal_str_representer)


def emit_workflow(workflow_dict: dict) -> str:
    """Emit a workflow dict as a YAML string.

    Uses literal block style (``|``) for multi-line strings and preserves
    key order.  The custom representer is registered on a private Dumper
    subclass so the global ``yaml`` state is never modified.
    """
    return yaml.dump(
        workflow_dict,
        Dumper=_LiteralBlockDumper,
        default_flow_style=False,
        sort_keys=False,
    )


# ---------------------------------------------------------------------------
# Job transformation pipeline
# ---------------------------------------------------------------------------


def prefix_jobs(project_name: str, jobs: dict) -> dict:
    """Prefix every job key with ``{project_name}-`` and rewrite ``needs:`` references.

    Returns a new dict; the original *jobs* is not mutated.
    """
    jobs = copy.deepcopy(jobs)
    # Build old→new key mapping
    key_map = {key: f"{project_name}-{key}" for key in jobs}

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
        job["if"] = f"startsWith(github.event.release.tag_name, '{tag_prefix}')"

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
    1. resolve_permissions (before prefixing — references original job structure)
    2. rewrite_action_paths
    3. inject_job_metadata
    4. prefix_jobs (last — changes keys)

    Returns a dict of transformed jobs ready for merging into the root workflow.
    """
    workflow = parse_publish_workflow(workflow_path)
    jobs = workflow["jobs"]

    jobs = resolve_permissions(jobs, workflow.get("permissions"))
    jobs = rewrite_action_paths(jobs, project_path)
    jobs = inject_job_metadata(jobs, tag_prefix, project_path)
    jobs = prefix_jobs(project_name, jobs)

    return jobs

"""Inline publish logic for monorepo projects: workflow parsing and YAML emission."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from io import StringIO

from ruamel.yaml import YAML

from ...errors import ConfigError
from ...publish_gate import (
    GATE_JOB_KEY,
    build_router_gate_job,
    publish_concurrency_block,
)
from .sync import _get_monorepo_tag_prefix, validate_router_reusable_calls


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
        content = f.read()
    return parse_publish_workflow_content(content, source=path)


def parse_publish_workflow_content(content: str, source: str = "<string>") -> dict:
    """Parse a publish workflow from a YAML string.

    Same contract as :func:`parse_publish_workflow` but reads from an
    in-memory string. Used when the workflow is rendered from templates
    (root publisher) rather than read from disk.
    """
    data = YAML(typ='safe').load(content)

    if not isinstance(data, dict) or "jobs" not in data:
        raise ConfigError(f"Workflow {source} is missing a 'jobs' key")

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
        job["if"] = f"startsWith(inputs.tag || github.ref_name, '{tag_prefix}')"

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
    return transform_parsed_jobs(
        project_name,
        project_path,
        tag_prefix,
        workflow["jobs"],
        workflow.get("permissions"),
    )


def transform_parsed_jobs(
    project_name: str,
    project_path: str,
    tag_prefix: str,
    jobs: dict,
    permissions: dict | None,
) -> dict:
    """Transform already-parsed publish jobs for the monorepo router.

    Shared by :func:`transform_project_jobs` (member workflows read from
    disk) and the root-publisher path (jobs rendered from templates). See
    :func:`transform_project_jobs` for the full ordered transform contract.
    """
    jobs = dict(jobs)

    # The source workflow's gate polls its standalone CI check names, which
    # do not exist in the monorepo (CI runs via the router as reusable
    # workflows). The router's shared gate replaces it.
    jobs.pop(GATE_JOB_KEY, None)

    jobs = resolve_permissions(jobs, permissions)
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


# CI-router job-key helpers live in the low-level rlsbl.ci_router module so the
# check layer can share them without importing from the command layer.
from ...ci_router import _router_ci_check_regex  # noqa: E402


def _require_root_publish_gate_regex(project: dict, root: str) -> str:
    """Return the mandatory ``publish_gate_check_regex`` for a root publisher.

    Unlike sub-project members -- whose CI runs through the generated
    ``ci-router.yml`` with known ``<prefix> / <job>`` check-run names -- the
    root package's CI lives in a hand-authored workflow whose check-run names
    rlsbl cannot infer. The gate must match check runs that actually execute
    at the release SHA, so the regex is a REQUIRED config key with no default
    (this is a security gate; a wrong or absent regex would let publishes run
    ungated).

    Raises ConfigError (hard error) when the key is missing.
    """
    from ...config import read_project_config
    from ...targets import resolve_releasable_config_dir

    rel_dir = resolve_releasable_config_dir(project, root)
    project_dir = os.path.join(root, project["path"])
    config = read_project_config(project_dir, releasable_config_dir=rel_dir)

    regex = config.get("publish_gate_check_regex")
    if not isinstance(regex, str) or not regex:
        # Name the config surface the value belongs in.
        if rel_dir is not None:
            surface = os.path.join(rel_dir, "config.json")
        else:
            surface = os.path.join(project_dir, ".rlsbl", "config.json")
        raise ConfigError(
            "Root publisher (path='.') requires the config key "
            "'publish_gate_check_regex'.\n"
            "The monorepo publish gate must wait for the root package's CI "
            "check runs to succeed on the release commit before publishing. "
            "The root package's CI lives in a hand-authored workflow whose "
            "check-run names rlsbl cannot infer (unlike sub-project members "
            "routed through ci-router.yml), so you must declare the regex "
            "that matches those check-run names -- there is no default.\n"
            f"Add it to {surface}, e.g.:\n"
            '  "publish_gate_check_regex": "^(test|lint)( \\\\(.*\\\\))?$"\n'
            "The regex must match the check-run names that actually execute "
            "at the release SHA."
        )
    return regex


def _render_root_publisher_jobs(
    project: dict, root: str, tag_prefix: str
) -> dict:
    """Render the root publisher's publish jobs from config/templates.

    The root's publish.yml IS the router output, so it must never be read as
    a source (source==destination, and the transform pipeline is not
    idempotent on its own output). Instead the jobs are rendered from the
    project's pipeline config using the SAME standalone publish-template
    rendering the scaffold uses (``_generate_merged_publish``), then fed
    through the shared transform pipeline like every other member. This makes
    subsequent syncs regenerate from config, never from the router output --
    idempotent by construction.
    """
    from datetime import datetime

    from ...targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from ...commands.init_cmd import _generate_merged_publish
    from .sync import _build_project_template_vars

    project_path = project["path"].rstrip("/")
    project_dir = os.path.join(root, project["path"])
    rel_dir = resolve_releasable_config_dir(project, root)

    entries = detect_targets(project_dir, releasable_config_dir=rel_dir)
    targets = [e.name for e in entries if e.name in TARGETS]
    if not targets:
        raise ConfigError(
            f"Root publisher '{project['name']}' (path='.') has no publish "
            "targets; cannot generate publish jobs."
        )

    tvars = _build_project_template_vars(project_dir, root)
    tvars["year"] = str(datetime.now().year)

    # target_paths={} -> every target's path defaults to "." (repo root),
    # so no working-directory/packages-dir subdir rewriting occurs here.
    merged_yaml = _generate_merged_publish(targets, tvars, target_paths={})
    workflow = parse_publish_workflow_content(
        merged_yaml, source=f"<rendered root publisher {project['name']}>"
    )
    return transform_parsed_jobs(
        _root_job_prefix(project, root),
        project_path,
        tag_prefix,
        workflow["jobs"],
        workflow.get("permissions"),
    )


def _root_job_prefix(project: dict, root: str) -> str:
    """Return a valid GitHub job-key prefix for the root publisher.

    A root project's derived name is the basename of its path (".") -- not a
    valid job ID (GitHub job IDs must match ``[A-Za-z_][A-Za-z0-9_-]*``). Use
    the releasable name when the root belongs to one (explicit mode), else the
    repository directory name.
    """
    name = project.get("name")
    if isinstance(name, str) and name not in (".", "", "/", "./"):
        return name
    rel = project.get("releasable")
    if isinstance(rel, str) and rel:
        return rel
    return os.path.basename(os.path.realpath(root)) or "root"


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
        if project.get("_root_publisher"):
            # Root publisher: source==destination, so generate jobs from config
            # templates and take the gate check-regex from a mandatory config
            # key (the root's real CI check names are unknowable to rlsbl).
            prefix_regex_pairs.append(
                (tag_prefix, _require_root_publish_gate_regex(project, root))
            )
            jobs = _render_root_publisher_jobs(project, root, tag_prefix)
        else:
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

    # Generated routers must never route via reusable-workflow calls --
    # GitHub rejects workflows referencing 20+ of them.
    validate_router_reusable_calls(all_jobs, "publish.yml (publish router)")

    workflow_dict = {
        "name": "Publish Router",
        "on": {
            "release": {"types": ["published"]},
            "workflow_dispatch": {
                "inputs": {
                    "tag": {
                        "description": "Release tag to publish (e.g. pkga@v1.2.3). Overrides the ref for retry dispatch.",
                        "required": False,
                        "type": "string",
                    },
                },
            },
        },
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

    A reserved ``__rlsbl_version__`` key carries the current rlsbl version.
    Member publish.yml hashes don't change when only the router *generator*
    changes across an rlsbl upgrade, so without this key a version bump that
    alters router output would silently skip regeneration. Seeding the version
    into the cache structure invalidates the cache on any rlsbl version change.
    """
    from ... import __version__

    result: dict = {"__rlsbl_version__": __version__}
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

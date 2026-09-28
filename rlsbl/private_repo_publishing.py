"""What a release from a PRIVATE repository must not publish.

Some publishing features only make sense for a public source repository, and on
a private one they either fail after the tag is already public or quietly
reveal the private repository:

* **npm build provenance** (``"provenance": true`` on an npm pipeline) needs a
  public source repository; npm refuses it for a private one.
* **PyPI attestations**, which ``pypa/gh-action-pypi-publish`` attaches by
  default, record the repository's name, workflow file and commit in a public
  transparency log. rlsbl scaffolds ``attestations: false`` into the publish
  workflow of a private repository.
* **The Go module proxy** cannot fetch a private module, and caches every
  version it is asked about for good. A Go pipeline reaches it when it
  publishes a library from CI (the publish job asks the proxy for the new
  version) or runs locally (the local pipeline notifies the proxy).

:func:`public_only_uses` finds these in a project's configs and its generated
workflows without touching the network; the repository's visibility is asked
only when there is something to ask about. The release refuses them before
anything is pushed, and the ``private-repo-publishing`` check reports them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ruamel.yaml import YAML

PYPI_PUBLISH_ACTION = "pypa/gh-action-pypi-publish"

#: The regeneration command for the workflows rlsbl scaffolds.
REGENERATE_WORKFLOWS = (
    "Run `rlsbl scaffold` (`rlsbl monorepo sync` in a monorepo) to regenerate "
    "it with `attestations: false`, and commit the result."
)


@dataclass(frozen=True)
class PublicOnlyUse:
    """One publishing feature that needs a public repository, and its fix."""

    what: str
    fix: str

    def sentence(self) -> str:
        return f"{self.what} {self.fix}"


def _publishes(config) -> bool:
    """Does *config* publish at all? ``publish_mode: "none"`` publishes nothing."""
    return (config or {}).get("publish_mode") != "none"


def _pipelines(config):
    pipelines = (config or {}).get("pipelines") or {}
    return pipelines.items() if isinstance(pipelines, dict) else ()


def config_uses(configs) -> list[PublicOnlyUse]:
    """The public-only features the pipeline configs in *configs* declare."""
    uses = []
    for config in configs:
        if not _publishes(config):
            continue
        for name, entry in _pipelines(config):
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "npm" and entry.get("provenance") is True:
                uses.append(PublicOnlyUse(
                    f'npm pipeline "{name}" declares "provenance": true, and '
                    f"npm build provenance needs a public source repository.",
                    f'Set "provenance": false on that pipeline in '
                    f".rlsbl/config.json (or drop the pipeline).",
                ))
            elif kind == "go" and (
                entry.get("local") is True or entry.get("artifact") == "library"
            ):
                how = (
                    "notifies the Go module proxy from this machine"
                    if entry.get("local") is True
                    else "asks the Go module proxy for the new version from CI"
                )
                uses.append(PublicOnlyUse(
                    f'Go pipeline "{name}" {how}, which cannot fetch a private '
                    f"module and caches every version it is asked about "
                    f"permanently.",
                    'Set "publish_mode": "none" in .rlsbl/config.json: the '
                    "release then tags and creates GitHub Releases without "
                    "publishing, and consumers fetch the module with GOPRIVATE "
                    "set.",
                ))
    return uses


def _attests(step) -> bool:
    """Does this workflow step publish to PyPI with attestations on?"""
    uses = str(step.get("uses") or "")
    if not uses.startswith(PYPI_PUBLISH_ACTION):
        return False
    value = (step.get("with") or {}).get("attestations")
    return not (value is False or str(value).strip().lower() == "false")


def workflow_uses(workflows_dir) -> list[PublicOnlyUse]:
    """The PyPI publish steps under *workflows_dir* that would attest."""
    if not workflows_dir or not os.path.isdir(workflows_dir):
        return []
    uses = []
    yaml = YAML(typ="safe")
    for filename in sorted(os.listdir(workflows_dir)):
        if not filename.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(workflows_dir, filename)
        with open(path, encoding="utf-8") as f:
            workflow = yaml.load(f) or {}
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        for job_name, job in (jobs or {}).items():
            steps = (job or {}).get("steps") or []
            if any(isinstance(s, dict) and _attests(s) for s in steps):
                uses.append(PublicOnlyUse(
                    f".github/workflows/{filename} (job {job_name}) publishes to "
                    f"PyPI with attestations, which record this repository's "
                    f"name, workflow, and commit in a public transparency log.",
                    REGENERATE_WORKFLOWS,
                ))
    return uses


def public_only_uses(configs, workflows_dir) -> list[PublicOnlyUse]:
    """Everything in *configs* and *workflows_dir* that needs a public repository."""
    return config_uses(configs) + workflow_uses(workflows_dir)


class VisibilityUnknownError(Exception):
    """The repository's visibility could not be established."""


def repo_is_private(gh, gh_config) -> bool:
    """Ask GitHub whether the repository is private.

    *gh* is a ``run_gh``-style callable. Any failure raises
    :class:`VisibilityUnknownError`: an unanswered question is never read as
    "public".
    """
    try:
        data = json.loads(gh(["repo", "view", "--json", "isPrivate"], gh_config))
        value = data["isPrivate"]
    except Exception as exc:
        raise VisibilityUnknownError(
            f"'gh repo view --json isPrivate' failed ({exc})"
        ) from exc
    if not isinstance(value, bool):
        raise VisibilityUnknownError(
            f"'gh repo view --json isPrivate' returned an unexpected isPrivate "
            f"value ({value!r})"
        )
    return value


def refusal(uses) -> str:
    """The refusal for a private repository whose release would use *uses*."""
    lines = [
        "this repository is PRIVATE, and its release would use publishing "
        "that needs a public repository:",
    ]
    lines += [f"  - {use.sentence()}" for use in uses]
    lines.append(
        "  Making the repository public also clears every item above."
    )
    return "\n".join(lines)


def unknown_visibility(uses, exc) -> str:
    """The refusal when visibility could not be established for *uses*."""
    lines = [
        f"could not determine whether this repository is private ({exc}). "
        f"Its release would use publishing that needs a public GitHub "
        f"repository, so the answer is required:",
    ]
    lines += [f"  - {use.sentence()}" for use in uses]
    lines.append(
        "  If the repository is not on GitHub, apply the fixes above; if it is, "
        "check `gh auth status` and re-run."
    )
    return "\n".join(lines)


def workflows_dir_for(project_dir) -> str:
    """The ``.github/workflows`` directory of *project_dir*'s repository."""
    from .utils import run

    root = run("git", ["rev-parse", "--show-toplevel"], cwd=str(project_dir))
    return os.path.join(root.strip(), ".github", "workflows")

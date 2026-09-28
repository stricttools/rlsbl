"""The workflows a published GitHub Release starts, and whether they did.

rlsbl publishes through workflows that start when a GitHub Release is
published. GitHub starts nothing, and reports no error, when:

* the tagged commit's tree has no such workflow file (the file on the default
  branch does not count: a Release runs the workflow as it is at the tagged
  commit);
* the workflow is disabled, or GitHub Actions is disabled for the repository;
* the Release was created with a workflow's ``GITHUB_TOKEN``, whose events
  start no workflow run;
* the event is dropped or throttled, or a private repository has used up its
  Actions minutes.

Two instruments answer this. :func:`preflight` runs before a release pushes
anything: a project that publishes from CI must carry a release-triggered
workflow, Actions must be enabled, and every release-triggered workflow GitHub
already knows must be active. :func:`confirm_runs_started` runs after the
Release is created: every release-triggered workflow at the tagged commit must
show a run for the tag, or the release is a hard error naming what it found.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from ruamel.yaml import YAML

from .errors import RlsblError

WORKFLOWS_DIR = ".github/workflows"

#: How long a run may take to appear after the Release is published, and how
#: often to look. The same budget the release's CI wait gives a pushed commit.
DISCOVERY_SECONDS = 300
DISCOVERY_INTERVAL = 5

#: The events a run for a Release's tag can carry: the Release itself, or a
#: dispatch at the tag (``rlsbl release retry``).
_RUN_EVENTS = ("release", "workflow_dispatch")


class PublishWorkflowError(RlsblError):
    """A published Release would start, or did start, no publish run."""


@dataclass(frozen=True)
class ReleaseWorkflow:
    """A workflow file whose ``on:`` includes ``release``.

    Attributes:
        path: repository-relative path, ``.github/workflows/<file>``.
        types: the ``release`` activity types it filters on, or None for all.
    """

    path: str
    types: tuple[str, ...] | None

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    def starts_for(self, *, prerelease: bool) -> bool:
        """Does publishing a Release (pre-release or not) start this workflow?

        A published Release fires ``created`` and ``published``, then
        ``released`` or ``prereleased``. A job's ``if:`` never matters here:
        the run is created either way.
        """
        if self.types is None:
            return True
        fired = {"created", "published", "prereleased" if prerelease else "released"}
        return bool(fired & set(self.types))


def _release_types(on):
    """The ``release`` types an ``on:`` value subscribes to; False if none."""
    if isinstance(on, str):
        return None if on == "release" else False
    if isinstance(on, list):
        return None if "release" in on else False
    if isinstance(on, dict) and "release" in on:
        spec = on["release"]
        types = spec.get("types") if isinstance(spec, dict) else None
        if types is None:
            return None
        return tuple(types if isinstance(types, list) else [types])
    return False


def release_workflows(git_root, commit) -> list[ReleaseWorkflow]:
    """The release-triggered workflows in *commit*'s tree, by path."""
    from .utils import run

    listing = run(
        "git", ["ls-tree", "--name-only", commit, f"{WORKFLOWS_DIR}/"],
        cwd=str(git_root),
    )
    yaml = YAML(typ="safe")
    found = []
    for path in sorted(p for p in listing.splitlines() if p.strip()):
        if not path.endswith((".yml", ".yaml")):
            continue
        text = run("git", ["show", f"{commit}:{path}"], cwd=str(git_root))
        document = yaml.load(text) or {}
        if not isinstance(document, dict):
            continue
        # YAML 1.1 readers turn a bare `on` key into True.
        on = document.get("on", document.get(True))
        types = _release_types(on)
        if types is not False:
            found.append(ReleaseWorkflow(path=path, types=types))
    return found


def _api(gh, path, jq=None):
    # An explicit GET: the read form a dry run is allowed to perform.
    args = ["api", "--method", "GET", path]
    if jq:
        args += ["--jq", jq]
    return gh(args)


def _enable_actions_command(slug):
    return (
        f"gh api --method PUT repos/{slug or '{owner}/{repo}'}/actions/permissions "
        f"-F enabled=true"
    )


def actions_enabled(gh) -> bool:
    """Is GitHub Actions enabled for the repository? Raises when unanswered."""
    answer = json.loads(_api(gh, "repos/{owner}/{repo}/actions/permissions"))
    return answer["enabled"] is True


def workflow_state(gh, filename) -> str | None:
    """The workflow's state on GitHub, or None when GitHub does not know it.

    GitHub registers a workflow file when a push first brings it to the
    repository, so a workflow this release adds is unknown until the release
    pushes it.
    """
    try:
        return _api(gh, f"repos/{{owner}}/{{repo}}/actions/workflows/{filename}",
                    jq=".state").strip()
    except Exception as exc:
        if "404" in f"{exc} {getattr(exc, 'stderr', '')}":
            return None
        raise


def preflight(*, git_root, commit, gh, publishes, prerelease, slug):
    """Refuse a release whose Release would start no publish workflow.

    *publishes* is True when the project publishes from CI (``publish_mode``
    ``"ci"``): the tree at *commit* must then carry a workflow the Release
    starts. When any release-triggered workflow exists, GitHub Actions must be
    enabled and each workflow GitHub already knows must be active.
    """
    if git_root is None:
        raise PublishWorkflowError(
            "the project is not inside a git work tree, so the workflows its "
            "Release would start cannot be read."
        )
    workflows = release_workflows(git_root, commit)
    starting = [w for w in workflows if w.starts_for(prerelease=prerelease)]
    if publishes and not starting:
        raise PublishWorkflowError(
            f'publish_mode is "ci", but no workflow in {WORKFLOWS_DIR}/ at '
            f"{commit} starts when a GitHub Release is published, so nothing "
            f"would publish this release. Run `rlsbl scaffold` (`rlsbl monorepo "
            f"sync` in a monorepo) to generate the publish workflow, commit it, "
            f"and re-run the release."
        )
    if not starting:
        return
    try:
        enabled = actions_enabled(gh)
    except Exception as exc:
        raise PublishWorkflowError(
            f"could not read whether GitHub Actions is enabled for this "
            f"repository ({exc}). A release's publish workflows start only when "
            f"it is, so the answer is required. Check `gh auth status` (reading "
            f"it needs admin access to the repository) and re-run."
        ) from exc
    if not enabled:
        raise PublishWorkflowError(
            f"GitHub Actions is disabled for this repository, so the published "
            f"Release would start none of "
            f"{', '.join(w.path for w in starting)}. Enable it with "
            f"`{_enable_actions_command(slug)}` (or in the repository's "
            f"Settings > Actions) and re-run the release."
        )
    for workflow in starting:
        try:
            state = workflow_state(gh, workflow.filename)
        except Exception as exc:
            raise PublishWorkflowError(
                f"could not read the state of {workflow.path} on GitHub ({exc}). "
                f"A disabled workflow starts nothing, so the answer is "
                f"required. Check `gh auth status` and re-run."
            ) from exc
        if state is not None and state != "active":
            raise PublishWorkflowError(
                f"{workflow.path} is {state} on GitHub, so the published "
                f"Release would not start it. Enable it with `gh workflow "
                f"enable {workflow.filename}` and re-run the release."
            )


def runs_for_tag(gh, filename, *, tag, sha) -> list[dict]:
    """The runs of *filename* for *tag* at *sha*: the Release's, or a dispatch."""
    raw = _api(
        gh,
        f"repos/{{owner}}/{{repo}}/actions/workflows/{filename}/runs"
        f"?head_sha={sha}&per_page=100",
    )
    runs = json.loads(raw).get("workflow_runs") or []
    return [
        r for r in runs
        if r.get("head_branch") == tag and r.get("event") in _RUN_EVENTS
    ]


def _diagnosis(gh, tag, missing) -> list[str]:
    """What rlsbl can find out about why *missing* did not start."""
    found = []
    try:
        author = gh(["release", "view", tag, "--json", "author",
                     "--jq", ".author.login"]).strip()
    except Exception as exc:
        found.append(f"the Release's author could not be read ({exc})")
    else:
        if author.endswith("[bot]"):
            found.append(
                f"the Release was created by {author}: events created with a "
                f"workflow's GITHUB_TOKEN start no workflow run, so create the "
                f"Release with a personal or app token"
            )
    try:
        if not actions_enabled(gh):
            found.append("GitHub Actions is disabled for this repository")
    except Exception as exc:
        found.append(f"whether GitHub Actions is enabled could not be read ({exc})")
    for workflow in missing:
        try:
            state = workflow_state(gh, workflow.filename)
        except Exception as exc:
            found.append(f"the state of {workflow.path} could not be read ({exc})")
            continue
        if state is None:
            found.append(f"GitHub does not know {workflow.path}")
        elif state != "active":
            found.append(
                f"{workflow.path} is {state}: enable it with `gh workflow enable "
                f"{workflow.filename}`"
            )
    return found


def confirm_runs_started(*, tag, sha, prerelease, git_root, gh, log,
                         discovery_seconds=None, interval=None, sleep=None,
                         clock=None):
    """Require a run for *tag* of every workflow the Release starts.

    The workflows are the release-triggered ones in *sha*'s tree (the tagged
    commit). Each must show a run whose branch is *tag* -- from the Release,
    or from a dispatch at the tag -- within *discovery_seconds*. None
    expected, nothing asked. Raises :class:`PublishWorkflowError` naming each
    missing workflow and what rlsbl found about why.
    """
    expected = [
        w for w in release_workflows(git_root, sha)
        if w.starts_for(prerelease=prerelease)
    ]
    if not expected:
        return
    # Read at call time, so a budget set on the module is obeyed.
    if discovery_seconds is None:
        discovery_seconds = DISCOVERY_SECONDS
    if interval is None:
        interval = DISCOVERY_INTERVAL
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    names = ", ".join(w.path for w in expected)
    log(f"Confirming the Release started {names}...")
    deadline = clock() + discovery_seconds
    missing = list(expected)
    last_error = None
    while True:
        still = []
        for workflow in missing:
            try:
                runs = runs_for_tag(gh, workflow.filename, tag=tag, sha=sha)
            except Exception as exc:
                last_error = exc
                still.append(workflow)
                continue
            if not runs:
                still.append(workflow)
        missing = still
        if not missing:
            log(f"Publish runs started for {tag}: {names}")
            return
        if clock() >= deadline:
            break
        sleep(interval)

    lines = [
        f"{tag} is tagged and released, but no run of "
        f"{', '.join(w.path for w in missing)} started for it within "
        f"{discovery_seconds} seconds. A published Release starts "
        f"{'this workflow' if len(missing) == 1 else 'these workflows'}, and "
        f"GitHub reports no error when one does not start.",
    ]
    if last_error is not None:
        lines.append(f"  The last runs query failed: {last_error}")
    found = _diagnosis(gh, tag, missing)
    if found:
        lines.append("  Found:")
        lines += [f"    - {item}" for item in found]
    else:
        lines.append(
            "  Nothing rlsbl can read explains it: the event may have been "
            "dropped or throttled, and a private repository that used up its "
            "Actions minutes starts nothing."
        )
    lines.append(
        f"  The tag and the Release exist, so nothing needs re-releasing. Fix "
        f"the cause, start the workflows at the tag with `rlsbl release retry`, "
        f"and confirm with `rlsbl watch {sha}`."
    )
    raise PublishWorkflowError("\n".join(lines))


def confirm_or_exit(*, tag, sha, version, git_root, gh, log):
    """:func:`confirm_runs_started` for *version*, exiting 1 with its error."""
    import sys

    from .release_publication import is_prerelease

    try:
        confirm_runs_started(
            tag=tag, sha=sha, prerelease=is_prerelease(version),
            git_root=git_root, gh=gh, log=log,
        )
    except PublishWorkflowError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)

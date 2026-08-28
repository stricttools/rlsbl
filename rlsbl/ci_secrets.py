"""Does the repository carry the CI secrets its publish pipelines need?

A publish pipeline that authenticates with a repository secret fails at the
last possible moment when the secret is absent: the release has already tagged,
pushed and created the GitHub Release, and the publish job dies with
``ENEEDAUTH`` against the registry. The secret's presence is knowable long
before that, from the repository itself.

Only PRESENCE is read, never a value. ``gh`` answers whether a secret exists;
its value is not retrievable through the API at all, and rlsbl never puts a
credential on a pipe (see :mod:`rlsbl.observe_allowlist`).

Fail-closed: a probe that cannot answer -- no credential, no network, an API
error, a preview that recorded the call -- is "unknown", and the caller treats
unknown as an error. "We could not ask" is not evidence that the secret is
there, and a release that trusted it would discover otherwise after tagging.
"""

import subprocess

from . import effects

#: The secret an npm publish workflow authenticates with.
NPM_TOKEN = "NPM_TOKEN"


def probe_repo_secret(slug, name, *, timeout=15):
    """Does the GitHub repository *slug* have an Actions secret called *name*?

    Returns {"status": "present"}, {"status": "absent"}, or
    {"status": "unknown", "message": ...}. A 404 from this endpoint is the
    API's way of saying the secret does not exist; every other non-zero exit is
    unknown, because a permission or network failure must never read as
    absence (or as presence).
    """
    argv = [
        "api", "--method", "GET", f"repos/{slug}/actions/secrets/{name}",
    ]
    try:
        result = effects.gh(
            argv, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "message": str(exc) or "gh failed to run"}
    if effects.unsettled(result):
        return {"status": "unknown", "message": "the gh call was not performed"}
    if result.returncode == 0:
        return {"status": "present"}
    stderr = (result.stderr or "").strip()
    if "(HTTP 404)" in stderr or "Not Found" in stderr:
        return {"status": "absent"}
    return {
        "status": "unknown",
        "message": stderr.splitlines()[0] if stderr else
        f"gh exited {result.returncode}",
    }


def npm_ci_pipelines(config):
    """Names of the pipelines that publish to npm from CI.

    A ``local: true`` pipeline publishes from the developer's machine with the
    developer's own ``~/.npmrc``, so no repository secret is involved.
    """
    pipelines = (config or {}).get("pipelines")
    if not isinstance(pipelines, dict):
        return []
    return sorted(
        name for name, pipeline in pipelines.items()
        if isinstance(pipeline, dict)
        and pipeline.get("type") == "npm"
        and pipeline.get("local") is False
    )


def npm_token_remedy(slug):
    """The exact command that sets the secret from the local npm credential."""
    return (
        f'gh secret set {NPM_TOKEN} --repo {slug} '
        f'--body "$(grep _authToken ~/.npmrc | cut -d= -f2)"'
    )


class SecretVerdict:
    """Result of probing one repository for the secrets its pipelines need."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


def evaluate_npm_token_presence(config, slug, *, probe=probe_repo_secret):
    """The NPM_TOKEN secret must exist wherever CI publishes to npm."""
    from .config import get_publish_mode
    from .errors import ConfigError

    try:
        mode = get_publish_mode(config or {})
    except ConfigError as exc:
        return SecretVerdict(problems=[str(exc)])
    if mode != "ci":
        return SecretVerdict(
            skip_reason=f'publish_mode is "{mode}", so CI publishes nothing',
        )

    pipelines = npm_ci_pipelines(config)
    if not pipelines:
        return SecretVerdict(
            skip_reason="no pipeline publishes to npm from CI",
        )

    if not slug:
        return SecretVerdict(
            skip_reason=(
                "no GitHub repository is configured or resolvable from the "
                "origin remote, so there is no repository to read secrets from"
            ),
        )

    result = probe(slug, NPM_TOKEN)
    status = result.get("status")
    if status == "present":
        return SecretVerdict(notes=[
            f"{slug} carries {NPM_TOKEN} for pipeline(s) "
            f"{', '.join(pipelines)}"
        ])
    if status == "absent":
        return SecretVerdict(problems=[
            f"{slug} has no {NPM_TOKEN} secret, but pipeline(s) "
            f"{', '.join(pipelines)} publish to npm from CI -- the publish job "
            f"would fail with ENEEDAUTH after the release has already tagged "
            f"and pushed. Set it: {npm_token_remedy(slug)}"
        ])
    return SecretVerdict(problems=[
        f"{slug}: could not determine whether the {NPM_TOKEN} secret exists "
        f"({result.get('message') or 'probe failed'}). An unanswered probe is "
        f"not an answer -- fix the credential or the connection and re-run."
    ])

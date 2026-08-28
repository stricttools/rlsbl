"""Does the repository carry the CI secrets its publish pipelines need?

Which secrets those are is each PIPELINE's own answer -- ``ci_secret_names()``,
declared on the pipeline class beside the workflow templates that read the
secret -- so this module never tests a pipeline type by name. Today only the
npm pipeline declares one (``NPM_TOKEN``); a pypi pipeline declares none,
because its workflow authenticates through OIDC trusted publishing and
demanding a token there would be wrong.


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


def required_ci_secrets(config):
    """``{secret_name: [pipeline names]}`` for the configured CI pipelines.

    Which secret a pipeline's CI job authenticates with is the PIPELINE's own
    answer (``ci_secret_names``, declared beside the workflow templates that
    read it), never a type name tested here: a pipeline publishing through
    OIDC trusted publishing needs no secret at all, and a ``local: true``
    pipeline authenticates from the developer's own environment.
    """
    from .pipelines import load_pipelines

    required = {}
    for name, pipeline in (load_pipelines(config or {}) or {}).items():
        for secret in pipeline.ci_secret_names():
            required.setdefault(secret, []).append(name)
    return {secret: sorted(names) for secret, names in required.items()}


def secret_remedy(slug, secret):
    """The command that sets *secret* on *slug* from the local credential.

    ``NPM_TOKEN`` has a documented one-liner that reads the token out of the
    developer's own ``~/.npmrc``; any other secret gets the same command with
    the value left for the operator to supply, since rlsbl knows no source for
    it and must not invent one.
    """
    if secret == NPM_TOKEN:
        return (
            f'gh secret set {secret} --repo {slug} '
            f'--body "$(grep _authToken ~/.npmrc | cut -d= -f2)"'
        )
    return f"gh secret set {secret} --repo {slug}"


class SecretVerdict:
    """Result of probing one repository for the secrets its pipelines need."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


def evaluate_ci_secret_presence(config, slug, *, probe=probe_repo_secret):
    """Every secret the configured CI publish pipelines need must exist."""
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

    try:
        required = required_ci_secrets(config)
    except ConfigError as exc:
        return SecretVerdict(problems=[str(exc)])
    if not required:
        return SecretVerdict(
            skip_reason=(
                "no configured pipeline authenticates its CI publish with a "
                "repository secret"
            ),
        )

    if not slug:
        return SecretVerdict(
            skip_reason=(
                "no GitHub repository is configured or resolvable from the "
                "origin remote, so there is no repository to read secrets from"
            ),
        )

    problems = []
    notes = []
    for secret in sorted(required):
        pipelines = ", ".join(required[secret])
        result = probe(slug, secret)
        status = result.get("status")
        if status == "present":
            notes.append(f"{slug} carries {secret} for pipeline(s) {pipelines}")
            continue
        if status == "absent":
            problems.append(
                f"{slug} has no {secret} secret, but pipeline(s) {pipelines} "
                f"authenticate their CI publish with it -- the publish job "
                f"would fail with ENEEDAUTH after the release has already "
                f"tagged and pushed. Set it: {secret_remedy(slug, secret)}"
            )
            continue
        problems.append(
            f"{slug}: could not determine whether the {secret} secret exists "
            f"({result.get('message') or 'probe failed'}). An unanswered probe "
            f"is not an answer -- fix the credential or the connection and "
            f"re-run."
        )
    return SecretVerdict(problems=problems, notes=notes)

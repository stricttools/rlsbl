"""The npm token in ``~/.npmrc``, and whether repositories' ``NPM_TOKEN`` secrets carry it.

An npm publish in CI authenticates with the repository's ``NPM_TOKEN`` Actions
secret, which is a copy of the token the developer's own ``~/.npmrc`` holds.
When that token is replaced -- it expired, or was rotated -- every copy goes
stale at once, and the publish job of the next release fails against the
registry (``E404``/``ENEEDAUTH``) after the release has already tagged, pushed
and created the GitHub Release.

Two consumers share this module:

- ``rlsbl secrets sync-npm-token`` validates the local token against npm and
  writes it into the ``NPM_TOKEN`` secret of repositories that already carry
  one (:func:`sync_npm_token`).
- the ``npm-token-synced`` check refuses a release whose repository secret was
  set before the local token was created (:func:`evaluate_npm_token_sync`).

No token value is ever printed, logged, or put in an argv: the registry is
asked with an ``Authorization`` header, and ``gh secret set`` reads the token
from its stdin. Every probe that cannot answer is an error, never a pass.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime

from . import effects
from .ci_secrets import NPM_TOKEN, probe_repo_secret
from .errors import RlsblError

#: The ``~/.npmrc`` key npm writes the registry token under (``npm login``).
NPMRC_TOKEN_KEY = "//registry.npmjs.org/:_authToken"

#: The registry endpoint that names the user a bearer token authenticates.
WHOAMI_URL = "https://registry.npmjs.org/-/whoami"

#: The command that brings the repositories' secrets up to the local token.
SYNC_COMMAND = "rlsbl secrets sync-npm-token"


class NpmTokenError(RlsblError):
    """The local npm token, or a question about it, has no usable answer."""


def npmrc_path():
    """The developer's own ``~/.npmrc``."""
    return os.path.join(os.path.expanduser("~"), ".npmrc")


def read_npmrc_token(path=None):
    """The npm registry token ``~/.npmrc`` holds, or :class:`NpmTokenError`.

    Only the literal ``//registry.npmjs.org/:_authToken=<token>`` line counts.
    A value that names an environment variable (``${NPM_TOKEN}``) is not a
    token this machine holds, and is refused rather than expanded.
    """
    path = path or npmrc_path()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise NpmTokenError(f"cannot read {path}: {exc}") from exc

    values = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == NPMRC_TOKEN_KEY:
            values.append(value.strip())
    if not values or not values[-1]:
        raise NpmTokenError(
            f"{path} holds no `{NPMRC_TOKEN_KEY}=<token>` line, so this machine "
            f"has no npm token to check or copy. Log in with `npm login` first."
        )
    if len(values) > 1:
        raise NpmTokenError(
            f"{path} holds {len(values)} `{NPMRC_TOKEN_KEY}=` lines; npm reads "
            f"one token per registry. Remove all but the current one."
        )
    token = values[0]
    if token.startswith("${"):
        raise NpmTokenError(
            f"{path} names an environment variable in its `{NPMRC_TOKEN_KEY}=` "
            f"line instead of holding a token; there is no token on this "
            f"machine to check or copy."
        )
    return token


def npm_whoami(token, *, timeout=15):
    """The npm username *token* authenticates as, or :class:`NpmTokenError`.

    Asks ``GET /-/whoami`` with the token as a bearer credential. A 401 or 403
    is the registry refusing the token; anything else that yields no username
    is a question left unanswered, and says so.
    """
    request = urllib.request.Request(
        WHOAMI_URL, headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with effects.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise NpmTokenError(
                f"npm does not accept the token in ~/.npmrc (HTTP {exc.code} "
                f"from {WHOAMI_URL}): it expired or was revoked. Log in again "
                f"with `npm login`, then run `{SYNC_COMMAND}`."
            ) from exc
        raise NpmTokenError(
            f"could not ask npm whether it accepts the token in ~/.npmrc "
            f"(HTTP {exc.code} from {WHOAMI_URL})."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NpmTokenError(
            f"could not ask npm whether it accepts the token in ~/.npmrc "
            f"({exc})."
        ) from exc
    username = data.get("username") if isinstance(data, dict) else None
    if not username:
        raise NpmTokenError(
            f"{WHOAMI_URL} answered without a username, so whether npm accepts "
            f"the token in ~/.npmrc is unknown."
        )
    return username


def _listing_matches(token, shown):
    """Does the token npm lists as *shown* (``npm_abcd...wxyz``) match *token*?

    ``npm token list`` shows a token's first and last characters around
    ``...``; a display in any other shape matches nothing, because matching it
    would be a guess.
    """
    if not isinstance(shown, str) or "..." not in shown:
        return False
    prefix, _, suffix = shown.partition("...")
    return bool(prefix) and token.startswith(prefix) and token.endswith(suffix)


def parse_timestamp(value):
    """An ISO 8601 timestamp from npm or GitHub as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def token_created_at(token, *, timeout=60):
    """When npm created *token*, from ``npm token list --json``.

    The listing shows each token's first and last characters; exactly one
    unrevoked token must match *token*. No match, several matches, an
    unreadable listing, or a creation time that does not parse is an
    :class:`NpmTokenError`: the creation time cannot be determined.
    """
    argv = ["npm", "token", "list", "--json"]
    try:
        result = effects.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NpmTokenError(f"`npm token list --json` failed to run: {exc}") from exc
    if effects.unsettled(result):
        raise NpmTokenError("`npm token list --json` was not performed.")
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise NpmTokenError(
            f"`npm token list --json` exited {result.returncode}"
            + (f": {detail[0]}" if detail else "") + "."
        )
    try:
        listed = json.loads(result.stdout or "null")
    except ValueError as exc:
        raise NpmTokenError(
            f"`npm token list --json` printed something that is not JSON ({exc})."
        ) from exc
    if isinstance(listed, dict):
        listed = listed.get("objects")
    if not isinstance(listed, list):
        raise NpmTokenError("`npm token list --json` did not print a list of tokens.")

    matches = [
        entry for entry in listed
        if isinstance(entry, dict)
        and not entry.get("revoked")
        and _listing_matches(token, entry.get("token"))
    ]
    if len(matches) != 1:
        raise NpmTokenError(
            f"`npm token list --json` shows {len(matches)} unrevoked tokens "
            f"matching the one in ~/.npmrc, where exactly one is needed to "
            f"know when it was created."
        )
    created = parse_timestamp(matches[0].get("created"))
    if created is None:
        raise NpmTokenError(
            "`npm token list --json` gives the token in ~/.npmrc no creation "
            "time that parses."
        )
    return created


class SyncVerdict:
    """What the ``npm-token-synced`` check found for one repository."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


def evaluate_npm_token_sync(config, slug, *, npmrc=None, whoami=None,
                            created_at=None, probe=None):
    """Is the local npm token live, and does *slug*'s ``NPM_TOKEN`` carry it?

    Applies to a project whose configured CI publish pipelines authenticate
    with ``NPM_TOKEN``. Two findings, each an error:

    - npm does not accept the token in ``~/.npmrc`` (or cannot be asked);
    - the repository's ``NPM_TOKEN`` secret was last set before that token
      was created, so it holds an older token -- or the times cannot be
      determined at all, which is never read as a pass.

    *whoami*, *created_at* and *probe* default to this module's own
    :func:`npm_whoami`, :func:`token_created_at` and
    :func:`~rlsbl.ci_secrets.probe_repo_secret`, looked up at call time.
    """
    from .ci_secrets import required_ci_secrets
    from .config import get_publish_mode
    from .errors import ConfigError

    try:
        mode = get_publish_mode(config or {})
        required = required_ci_secrets(config) if mode == "ci" else {}
    except ConfigError as exc:
        return SyncVerdict(problems=[str(exc)])
    if mode != "ci":
        return SyncVerdict(
            skip_reason=f'publish_mode is "{mode}", so CI publishes nothing',
        )
    if NPM_TOKEN not in required:
        return SyncVerdict(
            skip_reason=(
                f"no configured CI publish pipeline authenticates with "
                f"{NPM_TOKEN}"
            ),
        )
    if not slug:
        return SyncVerdict(
            skip_reason=(
                "no GitHub repository is configured or resolvable from the "
                "origin remote, so there is no secret to compare"
            ),
        )

    whoami = whoami or npm_whoami
    created_at = created_at or token_created_at
    probe = probe or probe_repo_secret
    try:
        token = read_npmrc_token(npmrc)
        user = whoami(token)
        created = created_at(token)
    except NpmTokenError as exc:
        return SyncVerdict(problems=[f"{exc} Until then CI cannot publish to npm."])

    secret = probe(slug, NPM_TOKEN)
    status = secret.get("status")
    if status == "absent":
        return SyncVerdict(problems=[
            f"{slug} has no {NPM_TOKEN} secret, so its CI npm publish has no "
            f"token at all (`{SYNC_COMMAND}` only replaces an existing one; "
            f"ci-publish-secrets names the command that creates it)."
        ])
    if status != "present":
        return SyncVerdict(problems=[
            f"{slug}: could not read the {NPM_TOKEN} secret "
            f"({secret.get('message') or 'probe failed'}), so whether it holds "
            f"the current npm token is unknown. Fix the credential or the "
            f"connection and re-run, or run `{SYNC_COMMAND}` to set it."
        ])
    updated = parse_timestamp(secret.get("updated_at"))
    if updated is None:
        return SyncVerdict(problems=[
            f"{slug}: the GitHub API gives the {NPM_TOKEN} secret no update "
            f"time that parses, so whether it holds the current npm token is "
            f"unknown. Run `{SYNC_COMMAND}` to set it."
        ])
    if updated < created:
        return SyncVerdict(problems=[
            f"{slug}'s {NPM_TOKEN} secret was last set at {updated.isoformat()}, "
            f"before the npm token in ~/.npmrc (user {user}) was created at "
            f"{created.isoformat()}: CI would publish with an older token, "
            f"which npm refuses once it expires. Run `{SYNC_COMMAND}` to set "
            f"it from ~/.npmrc."
        ])
    return SyncVerdict(notes=[
        f"npm accepts the token in ~/.npmrc (user {user}), and {slug}'s "
        f"{NPM_TOKEN} secret was set after it was created"
    ])


# ---------------------------------------------------------------------------
# rlsbl secrets sync-npm-token
# ---------------------------------------------------------------------------


def _gh_lines(path, jq, *, paginate):
    """The lines ``gh api --method GET <path> --jq <jq>`` prints, or an error."""
    argv = ["api", "--method", "GET", path]
    if paginate:
        argv.append("--paginate")
    argv += ["--jq", jq]
    try:
        result = effects.gh(argv, capture_output=True, text=True, check=False,
                            timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NpmTokenError(f"`gh api {path}` failed to run: {exc}") from exc
    if effects.unsettled(result):
        raise NpmTokenError(f"`gh api {path}` was not performed.")
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise NpmTokenError(
            f"`gh api {path}` exited {result.returncode}"
            + (f": {detail[0]}" if detail else "")
            + ". The repositories this account can see cannot be listed."
        )
    return [line for line in (result.stdout or "").splitlines() if line.strip()]


def visible_repositories():
    """``[(owner/repo, archived)]`` for the authenticated ``gh`` account.

    Its own repositories and every repository of every organization it
    belongs to, each once, sorted.
    """
    login_lines = _gh_lines("user", ".login", paginate=False)
    if len(login_lines) != 1:
        raise NpmTokenError("`gh api user` named no single account login.")
    owner_paths = ["user/repos?affiliation=owner&per_page=100"]
    for org in _gh_lines("user/orgs?per_page=100", ".[].login", paginate=True):
        owner_paths.append(f"orgs/{org.strip()}/repos?per_page=100")

    repos = {}
    for path in owner_paths:
        for line in _gh_lines(
            path, '.[] | "\\(.full_name)\\t\\(.archived)"', paginate=True,
        ):
            name, _, archived = line.partition("\t")
            repos[name.strip()] = archived.strip() == "true"
    return sorted(repos.items())


def set_npm_token_secret(slug, token):
    """Set *slug*'s ``NPM_TOKEN`` to *token*, piped on ``gh``'s stdin.

    Returns ``("set" | "would set" | "failed", detail)``.
    """
    try:
        result = effects.gh(
            ["secret", "set", NPM_TOKEN, "--repo", slug],
            input=token, capture_output=True, text=True, check=False,
            timeout=60, grant="set-secret",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "failed", str(exc) or "gh failed to run"
    if effects.unsettled(result):
        return "would set", ""
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return "failed", detail[0] if detail else f"gh exited {result.returncode}"
    return "set", ""


def sync_npm_token(*, all_repositories, origin_repo, out, err, npmrc=None,
                   whoami=None, probe=None, list_repositories=None,
                   set_secret=None):
    """Write the token in ``~/.npmrc`` into repositories' ``NPM_TOKEN``.

    Targets: *origin_repo* (``owner/repo``), or with *all_repositories* every
    repository the ``gh`` account can see. Only a repository that already has
    an ``NPM_TOKEN`` secret is written; the secret is never created. Returns
    the exit code: 0 when every target was set, 1 otherwise.

    The injectable steps default to this module's own functions, looked up at
    call time.
    """
    whoami = whoami or npm_whoami
    probe = probe or probe_repo_secret
    list_repositories = list_repositories or visible_repositories
    set_secret = set_secret or set_npm_token_secret
    try:
        token = read_npmrc_token(npmrc)
        user = whoami(token)
    except NpmTokenError as exc:
        print(f"Error: {exc}", file=err)
        return 1
    print(f"npm accepts the token in ~/.npmrc (npm user: {user}).", file=out)

    failed = False
    if all_repositories:
        try:
            candidates = list_repositories()
        except NpmTokenError as exc:
            print(f"Error: {exc}", file=err)
            return 1
    else:
        if not origin_repo:
            print(
                "Error: this checkout has no GitHub origin remote, so there is "
                "no current repository to set the secret on. Run it inside a "
                "checkout, or pass --all.",
                file=err,
            )
            return 1
        candidates = [(origin_repo, False)]

    targets = []
    without = 0
    for slug, archived in candidates:
        if archived:
            # GitHub refuses every write to an archived repository, secrets
            # included, so it cannot be a target; it is named, not hidden.
            print(f"{slug}: skipped: archived, so GitHub refuses writes to it", file=out)
            continue
        found = probe(slug, NPM_TOKEN)
        status = found.get("status")
        if status == "present":
            targets.append(slug)
        elif status == "absent":
            without += 1
            if not all_repositories:
                print(
                    f"Error: {slug} has no {NPM_TOKEN} secret, and this command "
                    f"never creates one: it only replaces a secret a publish "
                    f"workflow already uses.",
                    file=err,
                )
                return 1
        else:
            failed = True
            print(
                f"{slug}: failed: could not read its secrets "
                f"({found.get('message') or 'probe failed'})",
                file=out,
            )

    for slug in targets:
        outcome, detail = set_secret(slug, token)
        if outcome == "failed":
            failed = True
            print(f"{slug}: failed: {detail}", file=out)
        elif outcome == "would set":
            print(f"{slug}: would set {NPM_TOKEN}", file=out)
        else:
            print(f"{slug}: {NPM_TOKEN} set", file=out)

    if all_repositories:
        print(
            f"{len(targets)} repositories carry {NPM_TOKEN}; {without} other "
            f"visible repositories have none and were left alone.",
            file=out,
        )
    return 1 if failed else 0

"""Does a ``go.mod``'s module path still name where the repository lives?

A Go module path is not decoration: it is the URL the toolchain fetches from.
When a repository is renamed, moved between owners, or absorbed into a monorepo
under a new subdirectory, every ``go.mod`` in it keeps declaring the OLD path
until someone rewrites it -- and a module published under a path that no longer
resolves is a module nobody can ``go get``. ``rlsbl rewrite go-module-path``
exists to perform that rewrite; this check is what notices it is owed.

What the expected path is
-------------------------

The repository's own origin remote, plus the module's directory inside the
repository::

    git@github.com:owner/repo.git  +  services/api
    -> github.com/owner/repo/services/api

Two spellings of the same remote (``https://github.com/owner/repo.git`` and
``git@github.com:owner/repo.git``) normalize to the same identity, so the
comparison never depends on how the remote was cloned.

A major-version suffix is part of the module path, not a mismatch: Go requires
``/v2`` and up on the module path itself, so ``github.com/owner/repo/v2`` is
accepted where ``github.com/owner/repo`` is expected.

What it refuses to guess
------------------------

* **No origin remote** -- there is no identity to compare against, so the check
  SKIPS and says so. Inventing one from the directory name is exactly the wrong
  answer, because the directory name is not the published identity.
* **An SSH host alias** (``git@gp:owner/repo.git``, where ``gp`` is a
  ``~/.ssh/config`` Host entry, not a domain) names no host that a module path
  could start with. Rather than guessing ``github.com``, the host segment is
  left unverified and only the OWNER/REPO/SUBDIRECTORY tail is compared -- the
  part that actually moves when a repository is renamed. The outcome message
  says the host was not verified, so a passing result never claims more than it
  checked.
"""

import os
import re
from dataclasses import dataclass

#: A trailing major-version element (``/v2`` and up), which Go requires on the
#: module path itself and which is therefore not a divergence from the repo.
_MAJOR_SUFFIX = re.compile(r"/v([2-9]|[1-9]\d+)$")

#: ``[user@]host:path`` -- the SCP-style remote spelling.
_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^@/:]+):(.+)$")

#: ``scheme://[user@]host[:port]/path``
_URL_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$")


@dataclass(frozen=True)
class RepoIdentity:
    """Where a repository lives, as its origin remote states it."""

    host: str
    path: str          # "owner/repo", with any .git suffix removed
    remote: str        # the remote URL as configured, for messages

    @property
    def host_is_a_domain(self):
        """True when the remote names something a module path can start with.

        An SSH alias (``gp``) has no dot; every real forge host does.
        """
        return "." in self.host


def parse_remote(url):
    """Return the :class:`RepoIdentity` a remote URL states, or None.

    Both spellings are normalized to the same ``(host, owner/repo)``: an SCP
    remote (``git@github.com:owner/repo.git``) and a URL remote
    (``https://github.com/owner/repo.git``, ``ssh://git@github.com/owner/repo``).
    """
    text = (url or "").strip()
    if not text:
        return None
    match = _URL_REMOTE.match(text) or _SCP_REMOTE.match(text)
    if match is None:
        return None
    host = match.group(1)
    path = match.group(2).strip("/").removesuffix(".git")
    if not host or not path:
        return None
    return RepoIdentity(host=host, path=path, remote=text)


def expected_module_path(identity, subdirectory):
    """The module path a module at *subdirectory* of this repository owns."""
    tail = identity.path
    rel = (subdirectory or "").replace(os.sep, "/").strip("/")
    if rel and rel != ".":
        tail = f"{tail}/{rel}"
    return f"{identity.host}/{tail}", tail


def strip_major_suffix(module_path):
    """*module_path* without its trailing ``/vN`` (N >= 2), if it has one."""
    return _MAJOR_SUFFIX.sub("", module_path or "")


def module_matches(actual, expected_full, expected_tail, *, host_verified):
    """Does *actual* declare the expected identity?

    When the host could not be derived from the remote (an SSH alias), the
    module's own first segment is accepted as the host and only the tail is
    compared -- see the module docstring.
    """
    base = strip_major_suffix(actual)
    if host_verified:
        return base == expected_full
    _host, _, rest = base.partition("/")
    return rest == expected_tail


class GoIdentityVerdict:
    """Result of comparing every Go module in one repository against origin."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


def read_module_line(go_mod_path):
    """The ``module`` directive of *go_mod_path*, or None when it has none."""
    try:
        with open(go_mod_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.split("//", 1)[0].strip()
                if stripped.startswith("module "):
                    return stripped[len("module "):].strip().strip('"')
    except (OSError, UnicodeDecodeError):
        return None
    return None


def evaluate_go_module_identity(repo_root, module_dirs, remote_url):
    """Compare each module's declared path against the repository's identity.

    *module_dirs* are absolute directories expected to contain a ``go.mod``.
    *remote_url* is the origin remote URL, or None when the repository has no
    origin -- which skips, rather than guessing an identity.
    """
    if not module_dirs:
        return GoIdentityVerdict(skip_reason="no Go module in this project")

    if not remote_url:
        return GoIdentityVerdict(
            skip_reason=(
                "no origin remote, so there is no published identity to "
                "compare the module path against"
            ),
        )
    identity = parse_remote(remote_url)
    if identity is None:
        return GoIdentityVerdict(
            skip_reason=(
                f"the origin remote {remote_url!r} does not parse as a "
                f"repository URL, so no module path can be derived from it"
            ),
        )

    host_verified = identity.host_is_a_domain
    problems = []
    checked = 0
    for directory in module_dirs:
        go_mod = os.path.join(directory, "go.mod")
        if not os.path.isfile(go_mod):
            continue
        rel = os.path.relpath(directory, str(repo_root))
        actual = read_module_line(go_mod)
        if not actual:
            problems.append(
                f"{_label(rel)}: go.mod declares no module path, so nothing "
                f"states what this module publishes as."
            )
            continue
        checked += 1
        expected_full, expected_tail = expected_module_path(identity, rel)
        if module_matches(
            actual, expected_full, expected_tail, host_verified=host_verified,
        ):
            continue
        suffix = _MAJOR_SUFFIX.search(actual)
        remedy_target = expected_full + (suffix.group(0) if suffix else "")
        if not host_verified:
            actual_host = actual.partition("/")[0]
            remedy_target = (
                actual_host + "/" + expected_tail
                + (suffix.group(0) if suffix else "")
            )
        problems.append(
            f"{_label(rel)}: go.mod declares module {actual}, but origin "
            f"({identity.remote}) puts this module at {remedy_target} -- a "
            f"module published under a path the repository no longer serves "
            f"cannot be fetched. Rewrite it with `rlsbl rewrite go-module-path "
            f"--from-module {actual} --to-module {remedy_target}`."
        )

    if not checked and not problems:
        return GoIdentityVerdict(skip_reason="no go.mod found in any Go target")

    notes = []
    if not problems:
        scope = f"{checked} Go module(s) match origin ({identity.remote})"
        if not host_verified:
            scope += (
                f"; the host segment was NOT verified because the remote names "
                f"the SSH alias '{identity.host}' rather than a domain"
            )
        notes.append(scope)
    return GoIdentityVerdict(problems=problems, notes=notes)


def _label(rel):
    return "go.mod" if rel in (".", "") else f"{rel}/go.mod"

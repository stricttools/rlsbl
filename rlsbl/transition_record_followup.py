"""What a recorded conversion still owes the outside world.

A transition record states what repository surgery DID; two of those facts imply
an obligation that lives outside this repository, and nothing verifies them
today:

* an **absorb** leaves the source repository standing. Until it is archived it
  keeps taking issues and pull requests, keeps appearing in searches, and keeps
  looking like the place to send a fix -- while the code moved here.
* a **go-module-path identity transition** leaves the OLD module path
  published. Until the old repository serves a ``// Deprecated:`` notice, every
  consumer resolving the old path gets a module with no sign that it moved.

Both checks are NETWORKED and both are fail-closed: an unanswered probe is a
hard error, never a pass. "We could not ask" is not evidence that the source
repo is archived or that the old module is deprecated, and reading it as such
is how a check reports a clean bill of health for a repository nobody looked
at.

Nothing here performs the remedy. rlsbl does not archive a repository it does
not own the release cycle of, and it does not push a deprecation commit into a
retired repo -- both findings print the exact command instead.
"""

import subprocess

from . import effects
from .transition_record import KIND_CONVERSION, KIND_IDENTITY_TRANSITION

#: The forge whose API these probes speak. A conversion source on any other
#: host is reported as unprobeable rather than guessed at.
GITHUB_HOST = "github.com"

#: The transition record facet whose transitions imply a Go deprecation.
GO_MODULE_FACET = "go-module-path"


class FollowupVerdict:
    """Findings, notes, and a skip reason for one transition-record-derived check."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------


def absorbed_sources(events):
    """``[(github_slug_or_None, stated_repo)]`` for every absorb in *events*.

    An extract's source is THIS repository, so only absorbs contribute: their
    source endpoint names a foreign repository that the absorb emptied.
    """
    sources = []
    for event in events:
        if event.KIND != KIND_CONVERSION or event.direction != "absorb":
            continue
        stated = (event.source.repo or "").strip()
        if not stated:
            continue
        sources.append((github_slug(stated), stated))
    return sources


def github_slug(repo):
    """``owner/repo`` when *repo* names a github.com repository, else None.

    A local path, a non-GitHub forge, and an SSH host alias (which names no
    host at all) each yield None: they are reported as unprobeable rather than
    assumed to be GitHub.
    """
    from .go_identity import parse_remote

    identity = parse_remote(repo)
    if identity is None or identity.host != GITHUB_HOST:
        return None
    parts = identity.path.split("/")
    if len(parts) != 2:
        return None
    return identity.path


def go_module_transitions(events):
    """``[(old, new, effective_version)]`` for every go-module-path move."""
    return [
        (event.old, event.new, event.effective_version)
        for event in events
        if event.KIND == KIND_IDENTITY_TRANSITION
        and event.facet == GO_MODULE_FACET
    ]


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_repo_archived(slug, *, timeout=15):
    """Is the GitHub repository *slug* archived?

    Returns {"status": "archived"}, {"status": "active"}, or
    {"status": "unknown", "message": ...}. Every unanswered probe -- a missing
    credential, a 404 that could equally be a deleted repo or a private one, a
    preview that recorded the call -- is "unknown", and the caller treats that
    as an error.
    """
    argv = ["api", "--method", "GET", f"repos/{slug}", "--jq", ".archived"]
    try:
        result = effects.gh(
            argv, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "message": str(exc) or "gh failed to run"}
    if effects.unsettled(result):
        return {"status": "unknown", "message": "the gh call was not performed"}
    if result.returncode != 0:
        return {
            "status": "unknown",
            "message": _gh_error(result) or f"gh exited {result.returncode}",
        }
    answer = (result.stdout or "").strip().lower()
    if answer == "true":
        return {"status": "archived"}
    if answer == "false":
        return {"status": "active"}
    return {
        "status": "unknown",
        "message": f"the API answered {answer!r} for .archived",
    }


def _gh_error(result):
    """The first meaningful line of gh's stderr."""
    for line in (result.stderr or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


def evaluate_old_repo_archived(events, *, probe=probe_repo_archived):
    """Every absorbed source repository should be archived."""
    sources = absorbed_sources(events)
    if not sources:
        return FollowupVerdict(
            skip_reason="the transition record contains no absorb conversion",
        )

    problems = []
    notes = []
    seen = set()
    archived = 0
    for slug, stated in sources:
        if slug is None:
            notes.append(
                f"{stated}: not a github.com repository, so its state cannot "
                f"be probed"
            )
            continue
        if slug in seen:
            continue
        seen.add(slug)
        result = probe(slug)
        status = result.get("status")
        if status == "archived":
            archived += 1
            continue
        if status == "active":
            problems.append(
                f"{slug} was absorbed into this repository but is still an "
                f"active repository, so it keeps collecting issues, pull "
                f"requests and clones for code that now lives here. Archive "
                f"it: `gh repo archive {slug}`."
            )
            continue
        problems.append(
            f"{slug}: could not determine whether the absorbed source "
            f"repository is archived ({result.get('message') or 'probe failed'}). "
            f"An unanswered probe is not an answer -- fix the connection (or "
            f"the credential) and re-run."
        )

    if not problems and archived:
        notes.insert(0, f"{archived} absorbed source repository(ies) archived")
    if not problems and not archived and not notes:
        return FollowupVerdict(
            skip_reason="no absorbed source names a repository to probe",
        )
    return FollowupVerdict(problems=problems, notes=notes)


def evaluate_go_deprecation_published(events, *, probe=None):
    """Every superseded Go module path should serve a deprecation notice."""
    if probe is None:
        from .registry import query_go_module_deprecation as probe

    transitions = go_module_transitions(events)
    if not transitions:
        return FollowupVerdict(
            skip_reason=(
                "the transition record contains no go-module-path identity "
                "transition"
            ),
        )

    problems = []
    notes = []
    seen = set()
    deprecated = 0
    for old, new, effective_version in transitions:
        if not old or old in seen:
            continue
        seen.add(old)
        result = probe(old)
        status = result.get("status")
        if status == "deprecated":
            deprecated += 1
            continue
        if status == "not_found":
            notes.append(
                f"{old}: the module proxy has never served this path, so there "
                f"is nothing published to deprecate"
            )
            continue
        if status == "not_deprecated":
            problems.append(
                f"{old} moved to {new} at {effective_version}, but the module "
                f"proxy still serves {result.get('version')} of the old path "
                f"with no deprecation notice, so `go get` on it reports "
                f"nothing. In the OLD repository add a `// Deprecated: moved "
                f"to {new}` comment above its `module` directive plus a "
                f"`retract` of its published versions, and release it."
            )
            continue
        problems.append(
            f"{old}: could not determine whether the superseded module path is "
            f"deprecated ({result.get('message') or 'proxy probe failed'}). An "
            f"unanswered probe is not an answer -- fix the connection to the "
            f"module proxy and re-run."
        )

    if not problems and deprecated:
        notes.insert(0, f"{deprecated} superseded module path(s) deprecated")
    return FollowupVerdict(problems=problems, notes=notes)

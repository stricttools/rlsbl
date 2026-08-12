"""The observe allowlist and the written standard every entry must satisfy.

An *observe* is a subprocess that really executes under ``--dry-run`` instead
of being recorded.  ``strictcli`` matches an argv against these prefixes
element-wise by string equality; a match means the run is performed for real
and is legal even inside a ``read_only`` command.  That makes this list the
one place where a preview is allowed to touch the outside world, so every
entry needs a reason that survives being read out loud.

The standard: no user-visible mutation
--------------------------------------

**An allowlisted program may not change anything a user would notice.**

Legal under the standard:

* **Reads of local state** -- the working tree, the object database, refs,
  config, the filesystem.
* **Reads of a remote** over the network -- an HTTP GET against a registry or
  the GitHub API changes nothing on the far side.
* **Cache writes** -- a package manager populating its own download cache is
  invisible plumbing: it changes no project state, no output, and no later
  decision.  Deleting the cache costs a re-download and nothing else.

Not legal under the standard:

* **Ref updates** -- writing any ref, including remote-tracking refs and
  ``FETCH_HEAD``.  A preview that moves refs has changed the repository.
* **Index writes** -- anything that takes ``index.lock``.  In a worktree
  shared by several sessions the lock is a real hazard, not a formality:
  a preview must not be able to make a concurrent commit fail.
* **Credential emission** -- printing a live token to stdout.  A preview
  must not put a secret on a pipe, into a captured buffer, or into a log,
  no matter what the caller intends to do with it.

Consequences of the standard, entry by entry
--------------------------------------------

* ``git fetch`` is pinned to the exact argv ``["git", "fetch", "origin",
  "--quiet"]`` rather than the two-token prefix it used to carry.  Fetching
  downloads objects and rewrites ``FETCH_HEAD`` and the remote-tracking refs,
  which is a ref update; the narrow prefix keeps the one call site the release
  flow needs while refusing ``--prune`` and ``--tags``, which the old prefix
  silently legalized.
* ``git status`` / ``git diff`` / ``git diff-index`` carry
  ``--no-optional-locks``, both here and at every call site.  Without it those
  commands refresh the index and take ``index.lock``.
* ``npm view`` and ``go list`` stay.  Both are registry/module reads whose
  only write is to the tool's own cache.
* ``gh auth token`` is **gone**.  It printed a live credential to stdout, so
  it was never observe-safe under any reading of the standard.  Its three
  former callers now let ``gh`` resolve and use the credential internally
  (``gh api``), so the token never transits an rlsbl pipe.

Purity, which this list also defines
------------------------------------

``rlsbl/data/checks.toml`` declares each check ``pure`` or not, and the rule
is stated in terms of this list: a pure check starts only programs whose argv
matches one of these prefixes.  Two consequences worth recording, because
they used to be accidents rather than decisions:

* ``local-tag`` shells out to ``git tag --list`` and ``config-schema`` can
  reach ``go list`` on its error path.  Both were declared pure under the
  older "starts no program" rule and were therefore misdeclared.  Under the
  standard above they are legitimately pure, and stay declared that way
  deliberately.
* Nine further checks that spawn only read-only local git flipped from impure
  to pure for the same reason.

Adding an entry
---------------

Every entry declares a category from :data:`OBSERVE_CATEGORIES` and a reason.
``tests/test_observe_allowlist.py`` asserts the shape (declared category,
non-empty reason, at least two tokens) and runs a corpus of known-mutating
argvs past every prefix.  A new entry that cannot be justified in one of the
three categories does not belong here.
"""

from collections import namedtuple

#: The closed set of categories an allowlist entry may declare, each with the
#: clause of the standard that admits it.  A category outside this mapping is
#: a test failure, so the standard cannot be widened by adding an entry.
OBSERVE_CATEGORIES = {
    "local-read": (
        "Reads local state (working tree, objects, refs, config, files) and "
        "writes nothing."
    ),
    "network-read": (
        "Reads a remote over the network. Nothing on the far side changes; a "
        "local tool cache may be populated, which the standard admits as "
        "invisible plumbing."
    ),
    "self-report": (
        "The tool reports its own version or authentication state. No project "
        "state and no remote state is read or written."
    ),
}

#: One allowlist entry: the argv prefix, its category, and why it is admitted.
ObserveEntry = namedtuple("ObserveEntry", ("argv", "category", "why"))


OBSERVE_ALLOWLIST = (
    # -- git reads ------------------------------------------------------
    ObserveEntry(("git", "rev-parse"), "local-read", "resolves refs and paths"),
    ObserveEntry(("git", "rev-list"), "local-read", "walks the commit graph"),
    ObserveEntry(
        ("git", "--no-optional-locks", "status"), "local-read",
        "reports worktree state; the flag keeps it off index.lock",
    ),
    ObserveEntry(("git", "log"), "local-read", "reads commit history"),
    ObserveEntry(("git", "show"), "local-read", "reads an object"),
    ObserveEntry(("git", "describe"), "local-read", "names a commit from tags"),
    ObserveEntry(
        ("git", "fetch", "origin", "--quiet"), "network-read",
        "the one fetch the release flow needs; pinned to this exact argv "
        "because fetching rewrites FETCH_HEAD and remote-tracking refs, so "
        "the broader forms (--prune, --tags) must not match",
    ),
    ObserveEntry(
        ("git", "--no-optional-locks", "diff"), "local-read",
        "compares trees; the flag keeps it off index.lock",
    ),
    ObserveEntry(("git", "diff-tree"), "local-read", "compares two trees, no index"),
    ObserveEntry(
        ("git", "--no-optional-locks", "diff-index"), "local-read",
        "compares a tree against the index; the flag keeps it from refreshing",
    ),
    ObserveEntry(("git", "ls-files"), "local-read", "lists tracked/ignored paths"),
    ObserveEntry(("git", "ls-remote"), "network-read", "lists a remote's refs"),
    ObserveEntry(("git", "ls-tree"), "local-read", "lists a tree's entries"),
    ObserveEntry(("git", "cat-file"), "local-read", "reads an object"),
    ObserveEntry(("git", "merge-base"), "local-read", "computes a merge base"),
    ObserveEntry(("git", "check-ignore"), "local-read", "tests paths against ignores"),
    ObserveEntry(("git", "for-each-ref"), "local-read", "lists refs"),
    ObserveEntry(("git", "symbolic-ref"), "local-read", "reads HEAD's target"),
    ObserveEntry(("git", "name-rev"), "local-read", "names a commit from refs"),
    ObserveEntry(("git", "shortlog"), "local-read", "summarizes history"),
    ObserveEntry(("git", "var"), "local-read", "reads a git variable"),
    ObserveEntry(("git", "config", "--get"), "local-read", "reads one config key"),
    ObserveEntry(("git", "config", "--get-all"), "local-read", "reads config values"),
    ObserveEntry(("git", "config", "--list"), "local-read", "lists config"),
    ObserveEntry(("git", "remote", "get-url"), "local-read", "reads a remote URL"),
    ObserveEntry(("git", "remote", "-v"), "local-read", "lists remotes"),
    ObserveEntry(("git", "branch", "--show-current"), "local-read", "reads the branch"),
    ObserveEntry(("git", "branch", "--contains"), "local-read", "lists containing branches"),
    ObserveEntry(("git", "branch", "-a"), "local-read", "lists branches"),
    ObserveEntry(("git", "branch", "--list"), "local-read", "lists branches"),
    ObserveEntry(("git", "tag", "-l"), "local-read", "lists tags"),
    ObserveEntry(("git", "tag", "--list"), "local-read", "lists tags"),
    ObserveEntry(("git", "tag", "--points-at"), "local-read", "lists tags at a commit"),
    ObserveEntry(("git", "stash", "list"), "local-read", "lists stash entries"),
    ObserveEntry(
        ("git", "merge-file", "-p"), "local-read",
        "three-way merges to stdout; -p is what keeps it from writing a file",
    ),
    ObserveEntry(("git", "--version"), "self-report", "prints git's version"),
    # -- gh reads (never `gh api` wholesale: it POSTs too) ---------------
    ObserveEntry(
        ("gh", "api", "--method", "GET"), "network-read",
        "GET-pinned API read; the bare `gh api` prefix would legalize POST",
    ),
    ObserveEntry(
        ("gh", "auth", "status"), "self-report",
        "reports whether a credential is present and valid; prints no token",
    ),
    ObserveEntry(("gh", "release", "view"), "network-read", "reads one Release"),
    ObserveEntry(("gh", "release", "list"), "network-read", "lists Releases"),
    ObserveEntry(("gh", "repo", "view"), "network-read", "reads repository metadata"),
    ObserveEntry(("gh", "run", "list"), "network-read", "lists workflow runs"),
    ObserveEntry(("gh", "run", "view"), "network-read", "reads one workflow run"),
    ObserveEntry(("gh", "pr", "list"), "network-read", "lists pull requests"),
    ObserveEntry(("gh", "workflow", "list"), "network-read", "lists workflows"),
    ObserveEntry(("gh", "--version"), "self-report", "prints gh's version"),
    # -- registry and toolchain reads ------------------------------------
    ObserveEntry(
        ("npm", "view"), "network-read",
        "registry metadata read; its only write is npm's own cache",
    ),
    ObserveEntry(
        ("go", "list"), "network-read",
        "module metadata read; its only write is the module cache",
    ),
    ObserveEntry(("uv", "--version"), "self-report", "prints uv's version"),
    ObserveEntry(("ruff", "--version"), "self-report", "prints ruff's version"),
    ObserveEntry(("safegit", "--version"), "self-report", "prints safegit's version"),
    ObserveEntry(("saferm", "--version"), "self-report", "prints saferm's version"),
    ObserveEntry(("selfdoc", "--version"), "self-report", "prints selfdoc's version"),
)


def prefixes():
    """The allowlist in the shape ``strictcli.App`` takes it."""
    return [list(entry.argv) for entry in OBSERVE_ALLOWLIST]

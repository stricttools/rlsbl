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
* **Scratch writes** -- loose objects added to an object database, and
  brand-new directories the running process owns.  A program qualifies only
  if it writes NO ref, NO index and NO worktree state in any pre-existing
  repository: loose objects are unreachable garbage until a ref names them,
  and a fresh scratch directory is not state anybody had before the preview
  started.  Deleting either costs a recomputation and nothing else, which is
  the same reasoning the cache clause rests on.

Not legal under the standard:

* **Ref updates** -- writing any ref, including remote-tracking refs and
  ``FETCH_HEAD``.  A preview that moves refs has changed the repository.
  One entry is retained against this ban by an explicit ruling rather than by
  the ban's own reading: the pinned ``git fetch origin --quiet``, reconciled
  below.
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

  **Why it is retained even though the ban names ref updates.**  This is a
  ruling, recorded here so nobody has to re-derive it: ``FETCH_HEAD`` and
  origin's remote-tracking refs are git-internal plumbing that no user workflow
  reads as state.  They are cache-like -- deleting them costs a re-fetch and
  nothing else -- which is the same reasoning the standard already applies to a
  package manager's download cache.  ``refs/heads`` and the index are not:
  those are what a user's work sits on, and writing either from a preview is
  refused with no exception.  The pin is what keeps the ruling narrow: the
  two-token prefix admitted the short mutating forms (``git fetch --prune``,
  ``git fetch --tags``, ``git fetch --all``), and those do reach refs a user
  reads.  Prefix matching cannot refuse a flag APPENDED to the pinned argv
  (``git fetch origin --quiet --prune`` still matches) -- rlsbl's own call
  sites are the only producer of these argvs, so what the pin buys is that a
  future call site cannot reach a mutating fetch by writing a shorter one.
* ``git status`` / ``git diff`` / ``git diff-index`` carry
  ``--no-optional-locks``, both here and at every call site.  Without it those
  commands refresh the index and take ``index.lock``.
* ``npm view`` stays: a registry read whose only write is to npm's own cache.
* ``go list`` is split into the two forms rlsbl actually issues -- ``go list
  -m ...`` (the module-proxy notification) and ``go list -e -f ...`` (package
  enumeration).  The bare two-token prefix also admitted ``go list -mod=mod
  ...``, which updates ``go.mod`` and ``go.sum`` in place: a manifest write a
  user would notice, reached through an entry written for a read.
* ``gh auth status`` is pinned to ``["gh", "auth", "status", "--hostname",
  "github.com"]``, and its one caller (``utils.check_gh_auth``) issues exactly
  that argv.  The two-token prefix it used to carry also matched ``gh auth
  status --show-token`` and its short form ``-t``, both of which print the live
  credential to stdout -- credential emission, admitted by a prefix that was
  written for the token-free form.  github.com is the only host rlsbl talks to,
  so naming it costs the check nothing.
* ``gh auth token`` is **gone**.  It printed a live credential to stdout, so
  it was never observe-safe under any reading of the standard.  Its three
  former callers now let ``gh`` resolve and use the credential internally
  (``gh api``), so the token never transits an rlsbl pipe.
* ``git subtree split --prefix`` is pinned with ``--prefix`` as its own token,
  and the mirror reconciler issues exactly that spelling (``["subtree",
  "split", "--prefix", path]``, not ``--prefix=path``) so the pin can name it.
  A branchless split prints a SHA and materializes the synthetic split ancestry
  as loose objects: no ref, no index, no worktree -- the scratch-write clause.
  Its residual hazard is the same class as the fetch's: ``-b <branch>`` DOES
  create a ref, and prefix matching cannot refuse a flag APPENDED after the
  pin.  What the pin buys is that ``-b`` cannot be reached by writing a
  SHORTER argv, since a ``-b`` in the third position no longer matches.
* ``git clone --quiet --single-branch --branch main`` is admitted for its
  DESTINATION, not for its flags: the mirror reconciler's inspection clone
  writes into a directory inside a temp dir the same observation just created
  (``effects.observe_scratch_dirs``), so the whole write is scratch the process
  owns and deletes.  Prefix matching cannot see a destination, so the entry is
  pinned to the exact spelling that one call site issues -- a future call site
  cloning somewhere durable would satisfy the prefix while breaking the reason,
  which is why the reason is recorded on the entry rather than assumed.

Purity, which this list also defines
------------------------------------

``rlsbl/data/checks.toml`` declares each check ``pure`` or not, and the rule
is stated in terms of this list: a pure check starts only programs whose argv
matches one of these prefixes.  Two consequences worth recording, because
they used to be accidents rather than decisions:

* ``config-schema`` can reach ``go list`` on its error path, and the retired
  ``local-tag`` shelled out to ``git tag --list``.  Both were declared pure
  under the older "starts no program" rule and were therefore misdeclared.
  Under the standard above they are legitimately pure, and ``config-schema``
  stays declared that way deliberately.
* Nine further checks that spawn only read-only local git flipped from impure
  to pure for the same reason.

Adding an entry
---------------

Every entry declares a category from :data:`OBSERVE_CATEGORIES` and a reason.
``tests/test_observe_allowlist.py`` asserts the shape (declared category,
non-empty reason, at least two tokens) and runs a corpus of known-mutating
argvs past every prefix.  A new entry that cannot be justified in one of the
declared categories does not belong here.

This list is also what :func:`rlsbl.preview_apply.no_writes` screens
``effects.run`` against: during a reconciler's observation, an argv that
matches no prefix here is refused outright.  So the list answers one question
in one place -- "may this program run while we are only looking?" -- instead of
being shadowed by a second, opposite-polarity denylist.
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
    "scratch-write": (
        "Writes only scratch: loose objects in an object database, or a "
        "brand-new directory the running process owns. No ref, no index and "
        "no worktree state of any pre-existing repository is touched, so "
        "nothing a user would notice changes."
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
        "the one fetch the release flow needs; retained by explicit ruling "
        "against the ref-update ban because FETCH_HEAD and origin's "
        "remote-tracking refs are cache-like git plumbing no user workflow "
        "reads as state, unlike refs/heads or the index; pinned to this exact "
        "argv so the short mutating forms (--prune, --tags, --all) cannot "
        "match",
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
    # -- git scratch writes (see the scratch-write clause of the standard) ---
    ObserveEntry(
        ("git", "subtree", "split", "--prefix"), "scratch-write",
        "the deterministic branchless split the mirror reconciler observes: it "
        "prints the resulting SHA and materializes the synthetic split ancestry "
        "as loose objects, creating no ref, taking no index lock and leaving "
        "the worktree alone. RESIDUAL, same class as the pinned fetch's: "
        "`-b <branch>` DOES create a ref and prefix matching cannot refuse a "
        "flag appended after the pin -- the pin buys that `-b` cannot be "
        "reached by a SHORTER argv, and rlsbl's two call sites issue this argv "
        "plus operands and nothing else: mirror_cmd.compute_split_sha appends "
        "the prefix path, and mirror_publication.split_commit_for appends the "
        "prefix path and one commit sha (the split AT that commit, which still "
        "creates no ref)",
    ),
    ObserveEntry(
        ("git", "clone", "--quiet", "--single-branch", "--branch", "main"),
        "scratch-write",
        "the mirror reconciler's inspection clone. Admitted for its "
        "DESTINATION, not its flags: the call site clones into a directory "
        "inside a temp dir the same observation created via "
        "effects.observe_scratch_dirs, so the entire write is scratch this "
        "process owns and deletes, and the remote is only read. Prefix "
        "matching cannot see a destination, so the pin is the exact spelling "
        "that call site issues -- a future call site cloning somewhere durable "
        "would satisfy the prefix while breaking this reason",
    ),
    # -- gh reads (never `gh api` wholesale: it POSTs too) ---------------
    ObserveEntry(
        ("gh", "api", "--method", "GET"), "network-read",
        "GET-pinned API read; the bare `gh api` prefix would legalize POST",
    ),
    ObserveEntry(
        ("gh", "auth", "status", "--hostname", "github.com"), "self-report",
        "reports whether a credential for github.com is present and valid; "
        "pinned to the one argv rlsbl issues because the bare `gh auth status` "
        "prefix also admitted `--show-token` and `-t`, which print the live "
        "credential to stdout",
    ),
    ObserveEntry(("gh", "release", "view"), "network-read", "reads one Release"),
    ObserveEntry(("gh", "release", "list"), "network-read", "lists Releases"),
    ObserveEntry(("gh", "repo", "view"), "network-read", "reads repository metadata"),
    ObserveEntry(("gh", "run", "list"), "network-read", "lists workflow runs"),
    ObserveEntry(("gh", "run", "view"), "network-read", "reads one workflow run"),
    ObserveEntry(
        ("gh", "run", "watch"), "network-read",
        "blocks polling one workflow run until it concludes; nothing on the "
        "far side changes, and a preview that RECORDED it would hand the "
        "caller a carrier and report a pass it never observed",
    ),
    ObserveEntry(("gh", "pr", "list"), "network-read", "lists pull requests"),
    ObserveEntry(("gh", "workflow", "list"), "network-read", "lists workflows"),
    ObserveEntry(("gh", "--version"), "self-report", "prints gh's version"),
    # -- registry and toolchain reads ------------------------------------
    ObserveEntry(
        ("npm", "view"), "network-read",
        "registry metadata read; its only write is npm's own cache",
    ),
    ObserveEntry(
        ("go", "list", "-m"), "network-read",
        "module metadata read (the proxy notification in pipelines/go.py); its "
        "only write is the module cache",
    ),
    ObserveEntry(
        ("go", "list", "-e", "-f"), "local-read",
        "package enumeration in go_introspect.py; -e keeps it off the network "
        "and the format string only shapes stdout",
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

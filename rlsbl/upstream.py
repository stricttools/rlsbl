"""A fork's upstream: its declaration, the refs holding upstream's history, and
the move of the tags a fork inherited from it.

A repository is a fork exactly when it declares its upstream in
``.strictmetadata/upstream/upstream.toml`` (strictspec's built-in ``upstream``
schema: ``host``, ``owner``, ``repo``, ``branch``, all required). A missing
file means the repository is not a fork. Nothing is ever inferred from a git
remote's name.

Two ref namespaces hold upstream's history, both under the upstream's
``<host>/<owner>/<repo>`` so two upstreams can never collide:

* ``refs/tags-of/<host>/<owner>/<repo>/<tag>`` -- a tag the fork inherited,
  moved out of ``refs/tags`` by ``rlsbl upstream adopt-tags`` with its object
  unchanged, and pushed to ``origin`` so it is not lost. A version tag in
  ``refs/tags`` is a claim that this repository released that version; an
  inherited one is upstream's release, not ours.
* ``refs/upstream/<host>/<owner>/<repo>/<branch>`` -- the declared branch as
  fetched from upstream. Nothing in rlsbl writes it: the operator fetches it
  with the command :func:`missing_history_message` prints.

Changelog coverage in a fork leaves out every commit reachable from either
(:func:`history_exclusions`): upstream's commits are not ours to describe.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

import strictspec

from . import effects
from .errors import RlsblError

#: Where a fork declares its upstream, relative to the repository root.
UPSTREAM_FILE = strictspec.UPSTREAM_FILE

#: The namespace inherited tags move to.
TAGS_OF_ROOT = "refs/tags-of"

#: The namespace the declared upstream branch is fetched into.
UPSTREAM_ROOT = "refs/upstream"

#: The remote the moved tags are deleted from and the kept refs pushed to.
REMOTE = "origin"

_TAGS = "refs/tags/"
_ZERO = "0" * 40


class UpstreamError(RlsblError):
    """A fork's upstream declaration or upstream refs that rlsbl refuses."""


@dataclass(frozen=True)
class Upstream:
    """A fork's declared upstream, and the names rlsbl derives from it."""

    host: str
    owner: str
    repo: str
    branch: str

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://{self.slug}"

    @property
    def tags_of_prefix(self) -> str:
        """The ref prefix this upstream's inherited tags live under (with a
        trailing slash)."""
        return f"{TAGS_OF_ROOT}/{self.slug}/"

    def kept_ref(self, tag: str) -> str:
        """The ref inherited tag *tag* moves to."""
        return f"{self.tags_of_prefix}{tag}"

    @property
    def branch_ref(self) -> str:
        """The ref holding the declared branch as fetched from upstream."""
        return f"{UPSTREAM_ROOT}/{self.slug}/{self.branch}"

    @property
    def branch_fetch_command(self) -> str:
        """The fetch that writes (or refreshes) :attr:`branch_ref`.

        ``--no-tags`` because git otherwise follows every upstream tag
        pointing into the fetched history into ``refs/tags`` -- re-creating
        the inherited tags ``rlsbl upstream adopt-tags`` moved out.
        """
        return (f"git fetch --no-tags {self.url} "
                f"+refs/heads/{self.branch}:{self.branch_ref}")

    @property
    def tags_of_fetch_command(self) -> str:
        """The fetch that restores this upstream's kept tags from origin."""
        spec = f"{self.tags_of_prefix}*"
        return f"git fetch --no-tags {REMOTE} '{spec}:{spec}'"


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def repository_root(start=None) -> str | None:
    """The git work-tree root enclosing *start* (default: the current
    directory), or None outside a repository -- the root the declaration
    lives at."""
    from .options import repository_root as _root

    return _root(start if start is not None else os.getcwd())


def load(repo_root) -> Upstream | None:
    """The upstream *repo_root* declares, or None when it is not a fork.

    Raises :class:`UpstreamError` for a declaration strictspec refuses, naming
    every diagnostic.
    """
    up, found, diags = strictspec.load_upstream(repo_root)
    if not found:
        return None
    if diags or up is None:
        lines = "\n".join(f"  {d.code} at {d.path}: {d.message}" for d in diags)
        raise UpstreamError(
            f"{UPSTREAM_FILE} is not a valid upstream declaration:\n{lines}\n"
            f"  It must hold format_version = 1 and the four strings host, "
            f"owner, repo and branch, e.g.:\n"
            f'    format_version = 1\n    host = "github.com"\n'
            f'    owner = "<owner>"\n    repo = "<repo>"\n    branch = "main"'
        )
    return Upstream(host=up.host, owner=up.owner, repo=up.repo, branch=up.branch)


def load_here(start=None) -> Upstream | None:
    """The upstream the repository enclosing *start* declares, or None."""
    root = repository_root(start)
    return None if root is None else load(root)


# ---------------------------------------------------------------------------
# Changelog coverage: upstream's history is not ours
# ---------------------------------------------------------------------------


def _git(argv, cwd, *, timeout=30):
    return effects.run(
        ["git", *argv], capture_output=True, text=True, timeout=timeout,
        cwd=cwd,
    )


def _ref_exists(ref, cwd) -> bool:
    result = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    return getattr(result, "returncode", 1) == 0


def missing_history_message(up: Upstream) -> str:
    """The refusal for a fork whose upstream branch ref is missing locally."""
    return (
        f"this repository is a fork of {up.url} (branch {up.branch}, declared "
        f"in {UPSTREAM_FILE}), but the ref holding upstream's history is "
        f"missing here: {up.branch_ref}\n"
        f"  Changelog coverage leaves out every commit reachable from that ref "
        f"and from the {up.tags_of_prefix}* refs -- upstream's commits are not "
        f"this repository's to describe -- so without it every inherited commit "
        f"would be asked for an entry, and rlsbl refuses instead.\n"
        f"  Fetch upstream's branch, and the inherited tags this repository "
        f"keeps on origin, then re-run:\n"
        f"    {up.branch_fetch_command}\n"
        f"    {up.tags_of_fetch_command}"
    )


def history_exclusions(start=None) -> list[str]:
    """The ``git log`` revisions that leave upstream's history out of a range.

    Empty for a repository that declares no upstream. In a fork, everything
    reachable from the declared branch's ref or from any kept inherited tag is
    excluded. The branch ref is required: a fork missing it is refused with
    the fetch commands that restore it (:func:`missing_history_message`).
    Kept tags are read when present -- a fork whose upstream has no tags, or
    that has not adopted them yet, has none.
    """
    root = repository_root(start)
    if root is None:
        return []
    up = load(root)
    if up is None:
        return []
    if not _ref_exists(up.branch_ref, root):
        raise UpstreamError(missing_history_message(up))
    # Each argument is a standalone negative revision, so the list can be
    # appended to any git log / rev-list argument list, whatever --not
    # toggles it already holds.
    kept = sorted(_local_refs(up.tags_of_prefix, root))
    return [f"^{ref}" for ref in (up.branch_ref, *kept)]


# ---------------------------------------------------------------------------
# Adopting inherited tags
# ---------------------------------------------------------------------------

#: A tag that moves: this run writes something for it.
INHERITED = "inherited"
#: A tag already moved everywhere: nothing to do.
ADOPTED = "adopted"
#: A tag with upstream's name at another object: the run is refused.
CONFLICT = "conflict"


@dataclass(frozen=True)
class TagPlan:
    """One tag name's observed refs and what the adoption does with it.

    ``inherited`` is the object the tag was inherited at: the kept ref's when
    an earlier adoption wrote one, else upstream's own ``refs/tags/<tag>``.
    The others are the object each ref holds, None when absent: ``local`` and
    ``origin`` are ``refs/tags/<tag>`` here and on origin, ``kept`` and
    ``origin_kept`` the ``refs/tags-of/...`` ref here and on origin.
    """

    tag: str
    state: str
    inherited: str
    local: str | None
    origin: str | None
    kept: str | None
    origin_kept: str | None
    problems: tuple[str, ...] = ()

    @property
    def create_kept(self) -> bool:
        return self.state == INHERITED and self.kept is None

    @property
    def push_kept(self) -> bool:
        return self.state == INHERITED and self.origin_kept is None

    @property
    def delete_origin(self) -> bool:
        return self.state == INHERITED and self.origin is not None

    @property
    def delete_local(self) -> bool:
        return self.state == INHERITED and self.local is not None


@dataclass(frozen=True)
class AdoptPlan:
    """Everything one adoption observed and would do."""

    upstream: Upstream
    repo_root: str
    tags: tuple[TagPlan, ...]
    ours: tuple[str, ...]
    #: ``(tag, only_kept_on_origin)`` for each inherited tag whose object is
    #: not in this repository.
    missing_objects: tuple[tuple[str, bool], ...] = field(default=())

    def in_state(self, state):
        return [t for t in self.tags if t.state == state]

    @property
    def conflicts(self):
        return self.in_state(CONFLICT)

    @property
    def work(self):
        return self.in_state(INHERITED)


def _ls_remote_tags(remote, patterns, cwd) -> dict[str, str]:
    """``refname -> object`` for the refs *remote* holds under *patterns*.

    Unpeeled (``--refs``): an annotated tag's value is its tag object, which
    is the object a tag keeps when it moves. A remote that cannot be read is
    an error -- an unreadable upstream would make every tag look like ours.
    """
    try:
        result = _git(["ls-remote", "--refs", remote, *patterns], cwd, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise UpstreamError(f"could not list the refs of {remote}: {exc}") from exc
    if result.returncode != 0:
        raise UpstreamError(
            f"could not list the refs of {remote}: git ls-remote exited "
            f"{result.returncode}: {(result.stderr or '').strip()}"
        )
    refs = {}
    for line in (result.stdout or "").splitlines():
        oid, _, name = line.strip().partition("\t")
        if oid and name:
            refs[name] = oid
    return refs


def _local_refs(prefix, cwd) -> dict[str, str]:
    result = _git(
        ["for-each-ref", "--format=%(objectname) %(refname)", prefix], cwd,
    )
    if result.returncode != 0:
        raise UpstreamError(
            f"could not list the local refs under {prefix}: "
            f"{(result.stderr or '').strip()}"
        )
    refs = {}
    for line in (result.stdout or "").splitlines():
        oid, _, name = line.strip().partition(" ")
        if oid and name:
            refs[name] = oid
    return refs


def _strip(refs, prefix) -> dict[str, str]:
    return {name[len(prefix):]: oid for name, oid in refs.items()
            if name.startswith(prefix)}


def _object_present(oid, cwd) -> bool:
    return _git(["cat-file", "-e", oid], cwd).returncode == 0


def _remote_configured(cwd) -> bool:
    return _git(["remote", "get-url", REMOTE], cwd).returncode == 0


def no_upstream_message() -> str:
    return (
        f"this repository declares no upstream: {UPSTREAM_FILE} does not "
        f"exist, so it is not a fork and has no inherited tags.\n"
        f"  A fork declares its upstream there (strictspec's upstream schema, "
        f"every field required), e.g.:\n"
        f'    format_version = 1\n    host = "github.com"\n'
        f'    owner = "<owner>"\n    repo = "<repo>"\n    branch = "main"\n'
        f"  then re-run this command."
    )


def observe(start=None) -> AdoptPlan:
    """Read everything an adoption decides on. Writes nothing.

    Reads the declaration, this repository's ``refs/tags`` and kept refs,
    origin's (``git ls-remote``), and upstream's tags (``git ls-remote`` on
    the declared URL -- never a package registry).
    """
    root = repository_root(start)
    if root is None:
        raise UpstreamError("not inside a git repository")
    up = load(root)
    if up is None:
        raise UpstreamError(no_upstream_message())
    if not _remote_configured(root):
        raise UpstreamError(
            f"this repository has no `{REMOTE}` remote. The inherited tags are "
            f"deleted from {REMOTE} and their {up.tags_of_prefix}* refs pushed "
            f"there so they are not lost, so the adoption needs it: add it "
            f"(git remote add {REMOTE} <url of this fork>), then re-run."
        )

    upstream_tags = _strip(_ls_remote_tags(up.url, ["refs/tags/*"], root), _TAGS)
    origin_refs = _ls_remote_tags(
        REMOTE, ["refs/tags/*", f"{up.tags_of_prefix}*"], root,
    )
    origin_tags = _strip(origin_refs, _TAGS)
    origin_kept = _strip(origin_refs, up.tags_of_prefix)
    local_tags = _strip(_local_refs("refs/tags/", root), _TAGS)
    local_kept = _strip(_local_refs(up.tags_of_prefix, root), up.tags_of_prefix)

    names = sorted(set(local_tags) | set(origin_tags) | set(local_kept)
                   | set(origin_kept))
    plans = []
    ours = []
    missing = []
    for tag in names:
        u = upstream_tags.get(tag)
        loc, org = local_tags.get(tag), origin_tags.get(tag)
        kept, okept = local_kept.get(tag), origin_kept.get(tag)
        # The object the tag was inherited at: a kept ref records it from an
        # earlier adoption (which checked it against upstream then, and is
        # never rewritten if upstream later moves or deletes its tag);
        # otherwise it is upstream's own object, or nothing.
        inherited = kept or okept or u
        if inherited is None:
            ours.append(tag)
            continue
        problems = []
        if kept is not None and okept is not None and kept != okept:
            problems.append(
                f"{up.kept_ref(tag)} is at {kept[:12]} here and at "
                f"{okept[:12]} on {REMOTE}"
            )
        source = ("upstream's" if kept is None and okept is None
                  else f"the inherited one ({up.kept_ref(tag)})")
        if loc is not None and loc != inherited:
            problems.append(
                f"refs/tags/{tag} here is at {loc[:12]}, {source} is at "
                f"{inherited[:12]}"
            )
        if org is not None and org != inherited:
            problems.append(
                f"refs/tags/{tag} on {REMOTE} is at {org[:12]}, {source} is at "
                f"{inherited[:12]}"
            )
        if problems:
            plans.append(TagPlan(tag, CONFLICT, inherited, loc, org, kept,
                                 okept, tuple(problems)))
            continue
        needs = (kept is None or okept is None or loc is not None
                 or org is not None)
        if not needs:
            plans.append(TagPlan(tag, ADOPTED, inherited, loc, org, kept, okept))
            continue
        if kept is None and loc is None and not _object_present(inherited, root):
            missing.append((tag, okept is not None))
        plans.append(TagPlan(tag, INHERITED, inherited, loc, org, kept, okept))
    return AdoptPlan(up, root, tuple(plans), tuple(ours), tuple(missing))


def refusal(plan: AdoptPlan) -> str | None:
    """Why *plan* may not be applied, or None when it may."""
    up = plan.upstream
    parts = []
    if plan.conflicts:
        lines = []
        for t in plan.conflicts:
            lines.extend(f"    {t.tag}: {p}" for p in t.problems)
        parts.append(
            f"{len(plan.conflicts)} tag(s) carry an inherited tag's name at a "
            f"different object. A tag is inherited only when upstream "
            f"({up.url}) has a tag of the same name at the same object, so "
            f"these are not, and rlsbl moves no tag while one remains and "
            f"will not guess which object the name should carry:\n"
            + "\n".join(lines)
        )
    if plan.missing_objects:
        fetches = []
        for tag, from_kept in plan.missing_objects:
            if from_kept:
                if up.tags_of_fetch_command not in fetches:
                    fetches.append(up.tags_of_fetch_command)
            else:
                fetches.append(f"git fetch {REMOTE} tag {tag}")
        names = ", ".join(tag for tag, _ in plan.missing_objects)
        parts.append(
            f"{len(plan.missing_objects)} inherited tag(s) exist only on "
            f"{REMOTE} ({names}), and their objects are not in this "
            f"repository, so their {up.tags_of_prefix}* refs cannot be "
            f"written here. Fetch them, then re-run:\n"
            + "\n".join(f"    {f}" for f in fetches)
        )
    return "\n".join(parts) if parts else None


def render(plan: AdoptPlan, out) -> None:
    """Print *plan*: one line per tag it moved or would move, then the tags it
    leaves alone."""
    up = plan.upstream
    print(
        f"Upstream: {up.url} (declared in {UPSTREAM_FILE}); inherited tags "
        f"move to {up.tags_of_prefix}<tag>.",
        file=out,
    )
    for t in plan.tags:
        if t.state == INHERITED:
            steps = []
            if t.create_kept:
                steps.append(f"write {up.kept_ref(t.tag)}")
            if t.push_kept:
                steps.append(f"push it to {REMOTE}")
            if t.delete_origin:
                steps.append(f"delete refs/tags/{t.tag} on {REMOTE}")
            if t.delete_local:
                steps.append(f"delete refs/tags/{t.tag} here")
            print(f"  {t.tag}: inherited at {t.inherited[:12]}: "
                  f"{', '.join(steps)}", file=out)
        elif t.state == ADOPTED:
            print(f"  {t.tag}: adopted: already in {up.kept_ref(t.tag)} here "
                  f"and on {REMOTE}", file=out)
        else:
            print(f"  {t.tag}: conflict: {'; '.join(t.problems)}", file=out)
    if plan.ours:
        print(
            f"  {len(plan.ours)} tag(s) upstream does not have stay in "
            f"refs/tags: {', '.join(plan.ours)}",
            file=out,
        )


def apply(plan: AdoptPlan, *, push_timeout=120) -> None:
    """Perform *plan*'s moves. Call only on a plan :func:`refusal` accepts.

    Order is what makes a crash re-runnable: the kept refs are written here
    first; then ONE atomic push creates them on origin and deletes origin's
    inherited ``refs/tags`` (each guarded by a lease on the object observed);
    only then are the local tags deleted, each guarded by its object too.
    """
    up = plan.upstream
    root = plan.repo_root
    work = plan.work
    for t in work:
        if t.create_kept:
            result = _git(["update-ref", up.kept_ref(t.tag), t.inherited, _ZERO],
                          root)
            _require_ok(result, f"write {up.kept_ref(t.tag)}")

    leases, refspecs = [], []
    for t in work:
        if t.push_kept:
            leases.append(f"--force-with-lease={up.kept_ref(t.tag)}:")
            refspecs.append(f"{up.kept_ref(t.tag)}:{up.kept_ref(t.tag)}")
        if t.delete_origin:
            leases.append(f"--force-with-lease={_TAGS}{t.tag}:{t.origin}")
            refspecs.append(f":{_TAGS}{t.tag}")
    if refspecs:
        result = _git(
            ["push", "--atomic", "--no-verify", REMOTE, *leases, *refspecs],
            root, timeout=push_timeout,
        )
        _require_ok(result, f"push to {REMOTE}")

    for t in work:
        if t.delete_local:
            result = _git(["update-ref", "-d", f"{_TAGS}{t.tag}", t.local], root)
            _require_ok(result, f"delete refs/tags/{t.tag}")


def _require_ok(result, what):
    if effects.unsettled(result):
        return
    if result.returncode != 0:
        raise UpstreamError(
            f"could not {what}: git exited {result.returncode}: "
            f"{(result.stderr or '').strip()}\n"
            f"  Nothing already done is undone, and every step is guarded by "
            f"the object it expects: re-run `rlsbl upstream adopt-tags` to "
            f"finish."
        )

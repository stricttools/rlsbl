"""The command-neutral observe -> preview -> apply skeleton.

Several rlsbl commands are reconcilers: they look at some subject (a mirror
remote, a converted repository, a published version), decide which of a fixed
set of states it is in, report that decision as a plan, and -- only when the
caller asked for it -- perform the writes the plan named.  The shape was first
written out inside ``monorepo mirror``; this module is that shape with the
mirror taken out of it.

The three pieces
----------------

* :class:`VerdictItem` -- one subject's verdict: a **key** naming the subject,
  a **state** naming which classification it fell into, the observed **facts**
  behind that classification, and the **actions** an apply would take.  A
  reconciler that judges one whole repository emits a one-item preview; one
  that judges every package in a workspace emits an item per package.
* :class:`Preview` -- the ordered list of those items, and nothing else.  The
  order is the reconciler's own, preserved verbatim.
* :func:`render_preview` -- the ONE renderer.  Every reconciler's plan output
  is produced here, so the plan of a command written next year reads like the
  plan of the one written today.

The no-writes line
------------------

:func:`reconcile` is the entry skeleton, and its body is deliberately short
enough to read in one glance: observation runs first, inside :func:`no_writes`,
and every write happens after the ``dry_run`` branch.  Observation is not
merely *documented* as read-only -- for the duration of the observe call this
module swaps rlsbl's mutation entry points (:mod:`rlsbl.effects`) for versions
that raise :class:`ObserveWriteError`, and screens ``effects.run`` argv for
mutating git subcommands.  A reconciler whose "observation" quietly pushes a
branch fails loudly at the attempt instead of silently making ``--dry-run`` a
lie.

What the guard does NOT cover, stated plainly so nobody assumes otherwise:

* **Scratch space.**  ``effects.mkdtemp`` and ``effects.rmtree`` stay live:
  observing a remote means cloning it somewhere, and the clone has to be
  cleaned up.  Those touch a temp directory this process created, never the
  project.
* **``effects.gh``.**  Distinguishing a GitHub read from a GitHub write needs a
  verb list of its own; until a consumer needs one, gh calls are unscreened.
* **Grandchildren.**  Only the argv rlsbl itself launches is screened.  A
  command like ``git subtree split`` spawns its own git processes, which the
  screen never sees (and does not need to: it writes no refs).

All writes -- guarded phase or not -- go through :mod:`rlsbl.effects`, which is
what makes them previewable and recordable under strictcli's effects regime.
"""

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import effects


class ObserveWriteError(RuntimeError):
    """A mutation was attempted during observation, above the no-writes line."""


# ---------------------------------------------------------------------------
# The preview: an ordered list of keyed verdict items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictItem:
    """One subject's verdict in a preview.

    Args:
        key: names the subject judged (a project name, a package name, a
            version).  Unique within a preview; the renderer can show it.
        state: the classification, in ``snake_case``.  The set of states is
            the reconciler's own closed vocabulary.
        summary: the one-line headline printed after the state label.
        label: overrides the printed state label.  Defaults to *state* with
            underscores turned into hyphens, which is what a reader expects to
            see on a command line.
        facts: observed facts behind the verdict, one per rendered line,
            indented under the headline.
        actions: what an apply WOULD do, one per rendered line, indented under
            the facts.  Empty for a state that needs no action.
        detail: a free-form block rendered verbatim (its own indentation is
            preserved).  For guidance too long to be a fact line.
        data: the reconciler's own observation record, carried untouched from
            observe to apply so the apply step never re-derives it.
    """

    key: str
    state: str
    summary: str = ""
    label: str | None = None
    facts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    detail: str = ""
    data: object = None

    @property
    def state_label(self) -> str:
        """The label the renderer prints for this item's state."""
        if self.label is not None:
            return self.label
        return self.state.replace("_", "-")


@dataclass(frozen=True)
class Preview:
    """An ordered list of :class:`VerdictItem`, and nothing else."""

    items: tuple[VerdictItem, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))
        seen = set()
        for item in self.items:
            if item.key in seen:
                raise ValueError(f"duplicate preview key: {item.key!r}")
            seen.add(item.key)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(item.state for item in self.items)

    def by_key(self, key: str) -> VerdictItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def only(self) -> VerdictItem:
        """The single item of a one-subject preview (hard error otherwise)."""
        if len(self.items) != 1:
            raise ValueError(
                f"only() needs a one-item preview, got {len(self.items)}"
            )
        return self.items[0]


def single(item: VerdictItem) -> Preview:
    """The one-item preview -- a whole-repository verdict is this case."""
    return Preview((item,))


# ---------------------------------------------------------------------------
# The one renderer
# ---------------------------------------------------------------------------


def render_preview(preview: Preview, *, show_keys: bool, out=None) -> None:
    """Print *preview* as a human-readable plan.

    Args:
        preview: the plan to render.
        show_keys: prefix each headline with the item's key.  A reconciler
            that judges one subject passes False (the key would be noise);
            one that judges many passes True.  No default: which one a
            command wants is a decision it must state.
        out: stream to print to.  Resolved at call time so a caller (or
            pytest's capture) can rebind ``sys.stdout``.
    """
    stream = sys.stdout if out is None else out
    for item in preview.items:
        prefix = f"{item.key}: " if show_keys else ""
        headline = f"{prefix}{item.state_label}"
        if item.summary:
            headline = f"{headline}: {item.summary}"
        print(headline, file=stream)
        for fact in item.facts:
            print(f"  {fact}", file=stream)
        for action in item.actions:
            print(f"  {action}", file=stream)
        if item.detail:
            print(item.detail, file=stream)


# ---------------------------------------------------------------------------
# The no-writes line
# ---------------------------------------------------------------------------

# Mutation entry points on rlsbl.effects that observation may not call.
# ``mkdtemp``/``rmtree``/``temp_file``/``observe_scratch_files`` are absent on
# purpose: they touch scratch space this process owns, which observing a remote
# genuinely needs.  The lock helpers are absent for the same reason.
FORBIDDEN_DURING_OBSERVE = (
    "open_write",
    "open_exclusive",
    "write_text",
    "append_text",
    "write_bytes",
    "atomic_write_text",
    "makedirs",
    "mkdir",
    "rename",
    "replace",
    "remove",
    "rmdir",
    "removedirs",
    "chmod",
    "copy_file",
    "copytree",
    "spawn",
)

# git subcommands that write refs, the index, or the working tree.  Anything
# not listed here (ls-remote, rev-list, diff, show, log, clone, subtree split,
# merge-base) reads, or writes only into scratch space.
MUTATING_GIT_SUBCOMMANDS = frozenset({
    "add",
    "am",
    "apply",
    "branch",
    "checkout",
    "cherry-pick",
    "commit",
    "filter-branch",
    "filter-repo",
    "gc",
    "merge",
    "mv",
    "prune",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
    "update-ref",
    "worktree",
})

# git global options that take a separate value token, so the scan does not
# mistake the value for the subcommand.
_GIT_OPTS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})


def git_subcommand(argv) -> str | None:
    """The git subcommand in *argv*, or None when *argv* is not a git call."""
    if isinstance(argv, str) or not argv:
        return None
    tokens = [str(t) for t in argv]
    head = tokens[0].rsplit("/", 1)[-1]
    if head != "git" and not head.startswith("git."):
        return None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in _GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _refusal(name):
    def refuse(*args, **kwargs):
        raise ObserveWriteError(
            f"effects.{name} was called during observation: observation is "
            f"read-only, and every write belongs after the preview/apply "
            f"branch point."
        )

    return refuse


@contextmanager
def no_writes():
    """Refuse rlsbl's mutation entry points for the length of the block.

    Swaps the names in :data:`FORBIDDEN_DURING_OBSERVE` on
    :mod:`rlsbl.effects` for raisers and screens ``effects.run`` argv against
    :data:`MUTATING_GIT_SUBCOMMANDS`, restoring everything on the way out
    (including when the block raises).
    """
    saved = {name: getattr(effects, name) for name in FORBIDDEN_DURING_OBSERVE}
    real_run = effects.run

    def guarded_run(argv, **kwargs):
        sub = git_subcommand(argv)
        if sub in MUTATING_GIT_SUBCOMMANDS:
            raise ObserveWriteError(
                f"`git {sub}` was run during observation: observation is "
                f"read-only, and every write belongs after the preview/apply "
                f"branch point."
            )
        return real_run(argv, **kwargs)

    for name, fn in saved.items():
        setattr(effects, name, _refusal(name))
    effects.run = guarded_run
    try:
        yield
    finally:
        effects.run = real_run
        for name, fn in saved.items():
            setattr(effects, name, fn)


# ---------------------------------------------------------------------------
# The entry skeleton
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reconciler:
    """A reconciler: how to observe, how to apply, and how to render.

    Args:
        observe: called with no arguments, returns a :class:`Preview`.  Runs
            under :func:`no_writes`.
        apply_item: called once per item, in preview order, only outside a
            dry run.  Every write the reconciler performs happens here.
        show_keys: passed straight to :func:`render_preview`.
    """

    observe: object
    apply_item: object
    show_keys: bool


def reconcile(reconciler: Reconciler, *, dry_run: bool, out=None) -> Preview:
    """Observe, then either render the plan or apply it.

    Returns the preview either way, so a caller can inspect what was judged.
    """
    # ---- observation: NO WRITES ABOVE THIS LINE ---------------------------
    with no_writes():
        preview = reconciler.observe()
    if not isinstance(preview, Preview):
        raise TypeError(
            f"reconciler.observe() must return a Preview, got "
            f"{type(preview).__name__}"
        )

    if dry_run:
        render_preview(preview, show_keys=reconciler.show_keys, out=out)
        return preview

    # ---- apply: EVERY WRITE BELOW THIS LINE -------------------------------
    for item in preview.items:
        reconciler.apply_item(item)
    return preview

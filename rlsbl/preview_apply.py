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
that raise :class:`ObserveWriteError`, and screens every ``effects.run`` argv
against :data:`rlsbl.observe_allowlist.OBSERVE_ALLOWLIST`.  A reconciler whose
"observation" quietly pushes a branch fails loudly at the attempt instead of
silently making ``--dry-run`` a lie.

**One authority, not two.**  The screen is an allowlist because the question
"may this program run while we are only looking?" already has an answer, and it
is the observe allowlist -- the same list strictcli consults to decide which
argv really executes under ``--dry-run``.  This guard used to carry a private
denylist of mutating git subcommands instead, and an opposite-polarity second
authority answers the same question differently the moment either side moves:
``git subtree push``, ``git clean -fdx``, ``git config --global``, ``git remote
add``, ``git fetch --prune``, ``git init`` and any non-git argv at all were all
absent from that denylist and therefore ran.

What the guard does NOT cover, stated plainly so nobody assumes otherwise:

* **Scratch directories.**  ``effects.mkdtemp`` stays live and, inside
  :func:`effects.observe_scratch_dirs` (which :func:`no_writes` enters),
  creates a REAL directory even under a preview -- observing a remote means
  cloning it somewhere, and an allowlisted clone that really runs needs a
  parent that really exists.  ``effects.rmtree`` is restricted to those tracked
  paths: deleting anything else during observation raises.
* **``effects.gh``.**  gh calls go through ``effects.run`` and are screened by
  the same prefixes, so a gh verb absent from the allowlist is refused rather
  than allowed.  What is still missing is a real read/write classification of
  gh's verbs; until the publication reconciler needs one, the allowlist's
  handful of gh reads is the whole vocabulary an observation may use.
* **Grandchildren.**  Only the argv rlsbl itself launches is screened.  A
  command like ``git subtree split`` spawns its own git processes, which the
  screen never sees (and does not need to: it writes no refs).

All writes -- guarded phase or not -- go through :mod:`rlsbl.effects`, which is
what makes them previewable and recordable under strictcli's effects regime.
"""

import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from . import effects
from .observe_allowlist import OBSERVE_ALLOWLIST


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
# ``mkdtemp``/``temp_file``/``observe_scratch_files`` are absent on purpose:
# they touch scratch space this process owns, which observing a remote genuinely
# needs.  The lock helpers are absent for the same reason.  ``rmtree`` is
# absent from this list but NOT unguarded: it is wrapped separately so it can
# delete tracked scratch and nothing else.
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


def observe_allowed(argv) -> bool:
    """True when *argv* matches an observe-allowlist prefix.

    Element-wise string equality against each entry's prefix -- exactly how
    strictcli matches its ``proc_observe_allowlist``, so "runs during
    observation" and "really executes under --dry-run" are the same set.
    A shell string (``shell=True``) matches nothing: the allowlist is about
    argv, and a shell line is not one.
    """
    if isinstance(argv, str) or not argv:
        return False
    tokens = [str(t) for t in argv]
    for entry in OBSERVE_ALLOWLIST:
        n = len(entry.argv)
        if len(tokens) >= n and tuple(tokens[:n]) == entry.argv:
            return True
    return False


def _run_refusal(argv):
    sub = git_subcommand(argv)
    if sub is not None:
        what = f"`git {sub}`"
    elif isinstance(argv, str):
        what = f"the shell command {argv!r}"
    else:
        what = f"`{' '.join(str(t) for t in argv)}`"
    return ObserveWriteError(
        f"{what} was run during observation: only argv on rlsbl's observe "
        f"allowlist (rlsbl/observe_allowlist.py) may run above the no-writes "
        f"line, and every write belongs after the preview/apply branch point."
    )


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

    Three things, restored on the way out (including when the block raises):

    * the names in :data:`FORBIDDEN_DURING_OBSERVE` become raisers;
    * ``effects.run`` is screened by :func:`observe_allowed`, so an argv that
      is not on the observe allowlist raises instead of running;
    * ``effects.rmtree`` is restricted to scratch this block's own
      ``effects.mkdtemp`` created, which is also what makes those directories
      real under a preview (see :func:`effects.observe_scratch_dirs`).
    """
    saved = {name: getattr(effects, name) for name in FORBIDDEN_DURING_OBSERVE}
    real_run = effects.run
    real_rmtree = effects.rmtree

    def guarded_run(argv, **kwargs):
        if not observe_allowed(argv):
            raise _run_refusal(argv)
        return real_run(argv, **kwargs)

    def guarded_rmtree(path, **kwargs):
        if not effects.observe_scratch_owns(path):
            raise ObserveWriteError(
                f"effects.rmtree({path!r}) was called during observation: an "
                f"observation may delete only the scratch directories it "
                f"created itself, and every other deletion belongs after the "
                f"preview/apply branch point."
            )
        return real_rmtree(path, **kwargs)

    for name in saved:
        setattr(effects, name, _refusal(name))
    effects.run = guarded_run
    effects.rmtree = guarded_rmtree
    with ExitStack() as stack:
        stack.callback(lambda: [setattr(effects, n, f) for n, f in saved.items()])
        stack.callback(lambda: setattr(effects, "rmtree", real_rmtree))
        stack.callback(lambda: setattr(effects, "run", real_run))
        stack.enter_context(effects.observe_scratch_dirs())
        yield


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

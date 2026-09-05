"""``rlsbl transition record``: the typed door onto the operator-declared facts.

Most transition record events are written by the surgery that produced them --
an extract writes its conversion, a rewrite writes its commit remap, a rename
writes its boundary alias.  Two kinds have no such writer, because they are not
things a command DID.  They are declarations an operator makes about a
repository they read:

``non-version-tag``
    This tag stands outside the version model on purpose -- a nightly marker,
    an upstream vendor tag imported with a history.  Nothing can derive that; a
    human decides it, and the readers of the tag namespace
    (:mod:`rlsbl.tag_explanation`, hence ``rlsbl release backfill`` and
    ``rlsbl release reconcile``) then stop reporting it forever.

``release-history-closed``
    This member's or releasable's release history is deliberately over, so the
    version file, changelog directory and archives it leaves behind are a
    record rather than residue.

Until this command existed the only way to write either was the Python snippet
``rlsbl release backfill``'s own refusal spelled out.  A fact worth recording
in a committed store is worth a typed door.

WHERE IT WRITES: the repository-scoped record --
``<root>/.rlsbl-monorepo/transitions.jsonl`` in a workspace, and
``<root>/.rlsbl/transitions.jsonl`` in a standalone repository, which is
:func:`rlsbl.release_backfill.repository_transition_record`'s answer and
therefore exactly the file the backfill's refusal names and reads back.  Both
kinds go there rather than into a releasable's own state directory: a tag
namespace belongs to the repository, and a releasable whose release history
just closed may be a releasable whose state directory is about to leave with
it.

WHAT IT REFUSES: an empty subject or reason (the schema calls both non-empty,
and a declaration with no stated reason is not an audit trail), a second
declaration of the same kind about the same subject (the record is
append-only, so a duplicate would stand beside the first forever with no way
to say which one is meant), and -- defensively -- a kind outside the two.  The
choice flag admits only those two, so no argv reaches that last refusal; it
exists so that widening the choice without teaching this router is a hard
error rather than an event written with a shape nobody checked.
"""

import os
import sys

from .. import effects
from ..release_backfill import repository_transition_record
from ..transition_record import (
    KIND_NON_VERSION_TAG,
    KIND_RELEASE_HISTORY_CLOSED,
    NonVersionTagEvent,
    ReleaseHistoryClosedEvent,
    append_event,
    read_events,
    serialize_event,
)
from ..utils import commit_files

#: The kinds this door writes, each with the flag that elects it and the name
#: of the event field its value goes into. The two operator-declared kinds and
#: nothing else: every other kind is written by the surgery that produced it.
OPERATOR_KINDS = {
    KIND_NON_VERSION_TAG: ("--non-version-tag", "tag"),
    KIND_RELEASE_HISTORY_CLOSED: ("--release-history-closed", "subject"),
}


def _fail(message):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _build_event(kind, subject, reason):
    if kind == KIND_NON_VERSION_TAG:
        return NonVersionTagEvent(tag=subject, reason=reason)
    return ReleaseHistoryClosedEvent(subject=subject, reason=reason)


def _subject_of(event):
    """The one string an event of either kind declares its fact about."""
    return event.tag if event.KIND == KIND_NON_VERSION_TAG else event.subject


def run_cmd(flags, *, ctx):
    """Record one operator-declared transition record fact.

    Exits non-zero through :func:`sys.exit` on every refusal, so a failure is
    the process's rather than a value a caller could ignore.
    """
    kind = flags.get("kind")
    subject = (flags.get("subject") or "").strip()
    reason = (flags.get("reason") or "").strip()
    dry_run = flags.get("dry-run", False)
    auto_commit = flags.get("auto-commit", True)

    if kind not in OPERATOR_KINDS:
        _fail(
            f"{kind!r} is not an operator-declared transition record kind. "
            f"This door writes only "
            f"{', '.join(sorted(OPERATOR_KINDS))}; every other kind is written "
            f"by the operation that performed the surgery."
        )
    flag_name, field = OPERATOR_KINDS[kind]

    if not subject:
        _fail(f"{flag_name} requires a non-empty value")
    if not reason:
        _fail(
            f"--reason requires a non-empty value: the record's {field} field "
            f"is the fact, and the reason is why an operator declared it"
        )

    # The whole repository, not one member: the record this writes is the one
    # the readers of the repository's tag namespace consult.
    repo = str(ctx.workspace_root or ctx.project_root)
    path = repository_transition_record(repo)

    for existing in read_events(path, kinds=[kind]):
        if _subject_of(existing) != subject:
            continue
        _fail(
            f"{kind} {subject!r} is already declared in "
            f"{os.path.relpath(path, repo)}: event {existing.id}, recorded "
            f"{existing.recorded_at}, reason {existing.reason!r}. The record "
            f"is append-only, so a second declaration would stand beside the "
            f"first forever with nothing to say which one is meant. Amend the "
            f"reason by editing that line yourself, or leave it as it stands."
        )

    event = _build_event(kind, subject, reason)

    if dry_run:
        # The plan IS the output, so the framework's would-do header goes above
        # it rather than under an empty list at the end of the dispatch.
        effects.render_would_do_log()
        print(f"Would append to {os.path.relpath(path, repo)}:")
        print(f"  {serialize_event(event)}")
        print("\nDry run: nothing was written.")
        return

    written = append_event(path, event)
    relative = os.path.relpath(path, repo)
    print(f"Recorded {kind} {subject!r} in {relative} (event {written.id}).")
    if auto_commit:
        commit_files(
            f"transition record: {kind} {subject}", [relative],
            autogenerated=True, cwd=repo,
        )

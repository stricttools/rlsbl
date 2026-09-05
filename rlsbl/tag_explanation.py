"""Is this tag explained? -- the one consultation, for every reader of a tag namespace.

A repository's tag namespace is writable by anything, and two rlsbl commands
have to walk it and decide, per tag, whether this repository can account for it:

* ``rlsbl release backfill`` lists every tag it cannot explain FIRST and refuses
  to write anything while one remains;
* ``rlsbl release reconcile`` fires its publication tripwire on a ref origin
  holds that no record explains.

Both used to answer from their own reading.  This module is the answer, and the
consumers differ only in what they DO with it.

The three explanations
----------------------

``archived-version``
    The tag is the spelling this repository's own tag scheme produces for a
    version its release archives record.  The caller supplies that mapping --
    it is the caller's scheme (``expected_refs`` for reconcile, the scope's
    ``tag_format`` for the backfill), and this module never guesses one.

``shipped-as``
    An archive records ``shipped_as = "<tag>"``: the historical spelling that
    version ACTUALLY shipped under, from before a rename or a repository
    boundary moved.  Without this, a renamed project's whole published tag
    history reads as unexplained.

``non-version-tag``
    The transition record carries a ``non-version-tag`` event naming it: a tag
    deliberately outside the version model, recorded by an operator so that
    everything walking the namespace can account for it instead of reporting it
    forever.

Anything else is unexplained, and what that costs is the consumer's own
decision -- a hard refusal in the backfill, a tripwire in reconcile.

Reading archives tolerantly, on purpose
---------------------------------------

``shipped_as`` is read with :mod:`tomllib` rather than through
:func:`rlsbl.release_file.read_release_file`.  The strict reader requires the
strictspec ``format_version`` gate, and the archives the backfill exists to
repair are exactly the ones written before that gate -- asking the strict
reader would refuse to look at the file whose one field is being read.  Only
that field is taken; nothing here validates an archive or acts on one.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from .release_file import SHIPPED_AS_FIELD, archive_version
from .transition_record import KIND_NON_VERSION_TAG, read_events

#: The tag is the current scheme's spelling for an archived version.
SOURCE_ARCHIVED_VERSION = "archived-version"
#: An archive records this tag as the historical spelling it shipped under.
SOURCE_SHIPPED_AS = "shipped-as"
#: The transition record declares this tag outside the version model.
SOURCE_NON_VERSION_TAG = "non-version-tag"


@dataclass(frozen=True)
class TagExplanation:
    """Why a tag is accounted for.

    ``version`` names the version the tag belongs to for the two archive-backed
    sources, and is None for a non-version tag -- which belongs to no version by
    definition.  ``reason`` carries the operator's own words from a
    ``non-version-tag`` event.
    """

    tag: str
    source: str
    version: str | None = None
    reason: str = ""

    def describe(self) -> str:
        """One line naming the explanation, for a plan or an error."""
        if self.source == SOURCE_ARCHIVED_VERSION:
            return f"the archived version {self.version}"
        if self.source == SOURCE_SHIPPED_AS:
            return (
                f"the archived version {self.version}, which records "
                f"{SHIPPED_AS_FIELD} = {self.tag!r}"
            )
        return f"recorded as a tag outside the version model: {self.reason}"


@dataclass(frozen=True)
class TagExplanations:
    """The explanations available in one repository, keyed by tag."""

    by_tag: dict = field(default_factory=dict)

    def explain(self, tag: str) -> TagExplanation | None:
        """The explanation for *tag*, or None when nothing accounts for it."""
        return self.by_tag.get(tag)

    def explains(self, tag: str) -> bool:
        return tag in self.by_tag

    @property
    def non_version_tags(self) -> tuple[str, ...]:
        """Every tag the transition record puts outside the version model."""
        return tuple(
            tag for tag, e in sorted(self.by_tag.items())
            if e.source == SOURCE_NON_VERSION_TAG
        )


def shipped_as_index(releases_dir: str) -> dict:
    """``tag -> version`` for every archive in *releases_dir* recording one.

    An archive that does not parse, or that carries no ``shipped_as``,
    contributes nothing: this is a lookup over a field, not a validation pass.
    """
    index: dict = {}
    try:
        names = os.listdir(releases_dir)
    except OSError:
        return index
    for name in sorted(names):
        version = archive_version(name)
        if version is None:
            continue
        try:
            with open(os.path.join(releases_dir, name), "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        tag = data.get(SHIPPED_AS_FIELD)
        if isinstance(tag, str) and tag.strip():
            index[tag.strip()] = version
    return index


def non_version_tag_index(transition_record_paths) -> dict:
    """``tag -> reason`` for every recorded ``non-version-tag`` event.

    :func:`rlsbl.transition_record.read_events` is the read-for-use site and
    raises on a malformed record, which is the intended behavior: a namespace
    judged against a record that cannot be read in full would be judged against
    part of one.
    """
    index: dict = {}
    for path in transition_record_paths or ():
        for event in read_events(path, kinds=[KIND_NON_VERSION_TAG]):
            index[event.tag] = event.reason
    return index


def build(*, version_tags=None, releases_dirs=(), transition_record_paths=()) -> TagExplanations:
    """Assemble the explanations available in one repository.

    *version_tags* is the caller's own ``tag -> version`` mapping for the
    spellings its scheme produces.  The two other sources are read here: every
    ``shipped_as`` across *releases_dirs*, and every ``non-version-tag`` event
    across *transition_record_paths*.

    Precedence is the order of the sources above, which is the order of
    decreasing specificity about a RELEASE: a tag the current scheme names is
    that version's, a tag an archive claims historically is that version's, and
    only a tag no archive claims can be one an operator put outside the model.
    """
    by_tag: dict = {}
    for tag, reason in non_version_tag_index(transition_record_paths).items():
        by_tag[tag] = TagExplanation(
            tag=tag, source=SOURCE_NON_VERSION_TAG, reason=reason,
        )
    for releases_dir in releases_dirs or ():
        for tag, version in shipped_as_index(releases_dir).items():
            by_tag[tag] = TagExplanation(
                tag=tag, source=SOURCE_SHIPPED_AS, version=version,
            )
    for tag, version in (version_tags or {}).items():
        by_tag[tag] = TagExplanation(
            tag=tag, source=SOURCE_ARCHIVED_VERSION, version=version,
        )
    return TagExplanations(by_tag=by_tag)

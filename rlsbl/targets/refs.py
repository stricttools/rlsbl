"""The value types ``expected_refs`` speaks: what a version's ref set is, and everything needed to derive it.

``expected_refs`` is the single authority for the git refs one released version
owns -- its primary tag, the companion tags its ecosystem requires, and the
alias tags a rename or a conversion recorded for it. Before it existed the
answer was assembled independently at four sites (the release tag step, the
dry-run preview, ``release undo``, and the tag/Release checks), each of which
knew about a different subset.

The three sources, and why each is where it is:

* **The primary tag** is a naming decision. A releasable owns its own
  ``tag_format``, a monorepo package gets the target's ``monorepo_tag_format``,
  and a standalone repository gets the target's ``tag_format``. The context
  states which of the three applies; the target renders it.
* **Companion tags** are an ecosystem requirement. Go's module proxy resolves
  ``{path}/v{version}``, so a releasable whose primary tag is *not* in that form
  needs one companion per publishing Go member. Both rules the collector
  carried -- skip when the primary tag is already Go-compatible, skip
  publish-suppressed members -- live in :meth:`BaseTarget._companion_refs`.
* **Recorded aliases** are a repository FACT, read from the transition record
  rather than recomputed. A rename creates ``new@v1.2.3`` beside the existing
  ``old@v1.2.3``; a conversion does the same at its boundary. Both write a
  ``boundary-alias`` event, and both tags address the same version, so both
  belong to that version's ref set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RefContext:
    """Everything ``expected_refs`` needs that is not the version itself.

    Built by :func:`ref_context` rather than by hand, so the transition records to
    consult are derived in one place instead of at every call site.

    Attributes:
        repo_root: absolute path of the git repository (the workspace root in a
            monorepo). Every relative path here is relative to it.
        project_path: repo-relative directory of the project being released,
            or None for a standalone repository whose project IS the root.
        monorepo_name: the workspace project's name, when the release is a
            monorepo package release. None for a standalone repository.
        primary_tag_format: the releasable's ``tag_format``
            (``"{name}@v{version}"``) when a releasable owns the naming. None
            means the target's own tag format decides.
        releasable_name: the releasable's name, substituted into
            ``primary_tag_format``.
        member_package_paths: repo-relative member directories of the
            releasable being released. None -- not an empty tuple -- means this
            is not a releasable release, which is exactly the condition under
            which companion tags were never collected.
        releasable_config_dir: the releasable's state directory, for the
            member-config inheritance the publish-mode rule reads.
        transition_record_paths: the transition records that may carry aliases for this
            project's versions, in read order.
    """

    repo_root: str
    project_path: str | None = None
    monorepo_name: str | None = None
    primary_tag_format: str | None = None
    releasable_name: str | None = None
    member_package_paths: tuple[str, ...] | None = None
    releasable_config_dir: str | None = None
    transition_record_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectedRefs:
    """The full git ref set one released version owns.

    The three groups are kept apart because they fail differently: a missing
    primary tag means the release never tagged, a missing companion means an
    ecosystem cannot resolve the module, and a missing alias means a recorded
    fact has no ref behind it. :attr:`tags` is the flat, deduplicated,
    primary-first order anything that creates or pushes them uses.
    """

    version: str
    primary: str
    companions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def tags(self) -> tuple[str, ...]:
        """Every ref, primary first, in declaration order, deduplicated."""
        seen: set[str] = set()
        ordered: list[str] = []
        for tag in (self.primary, *self.companions, *self.aliases):
            if tag not in seen:
                seen.add(tag)
                ordered.append(tag)
        return tuple(ordered)


def ref_context(
    *,
    repo_root,
    project_path=None,
    monorepo_name=None,
    primary_tag_format=None,
    releasable_name=None,
    member_package_paths=None,
    releasable_config_dir=None,
) -> RefContext:
    """Build a :class:`RefContext`, deriving which transition records to consult.

    Exactly ONE record is consulted, and which one follows the project's release
    identity: a releasable's own record when a releasable owns the versioning,
    otherwise the project's standalone record. The workspace-scoped record is
    deliberately NOT consulted -- it holds facts about the repository (which tag
    globs departed), and an alias recorded there for a different releasable
    could carry the same version number as this one.
    """
    from ..transition_record import get_transition_record_path

    root = str(repo_root)
    if releasable_config_dir:
        paths = (get_transition_record_path(root, releasable_dir=str(releasable_config_dir)),)
    else:
        project_dir = os.path.join(root, project_path) if project_path else root
        paths = (get_transition_record_path(project_dir),)

    return RefContext(
        repo_root=root,
        project_path=project_path,
        monorepo_name=monorepo_name,
        primary_tag_format=primary_tag_format,
        releasable_name=releasable_name,
        member_package_paths=(
            None if member_package_paths is None
            else tuple(member_package_paths)
        ),
        releasable_config_dir=(
            str(releasable_config_dir) if releasable_config_dir else None
        ),
        transition_record_paths=paths,
    )


def recorded_aliases(context: RefContext, version: str) -> tuple[str, ...]:
    """Alias tags the transition records attribute to *version*.

    A ``boundary-alias`` event names two tags -- the alias created and the tag
    it duplicates -- and BOTH address the version they carry, so both join that
    version's ref set. Which version a tag carries is
    :func:`~rlsbl.tag_glob.parse_version_tag`'s answer, not a substring test:
    the three schemes it recognizes (``v1.2.3``, ``name@v1.2.3``,
    ``path/v1.2.3``) are exactly the ones rlsbl writes, and a tag under none of
    them carries no version rather than a guessed one.

    A missing record yields nothing; a MALFORMED one raises, because
    :func:`~rlsbl.transition_record.read_events` is the read-for-use site and a record
    that cannot be read in full cannot be trusted in part.
    """
    from ..transition_record import KIND_BOUNDARY_ALIAS, read_events
    from ..tag_glob import TagMode, parse_version_tag

    def carries(tag):
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        return parsed is not None and parsed.version == version

    found: list[str] = []
    for path in context.transition_record_paths:
        for event in read_events(path, kinds=[KIND_BOUNDARY_ALIAS]):
            for alias in event.aliases:
                for tag in (alias.alias_tag, alias.aliased_tag):
                    if carries(tag) and tag not in found:
                        found.append(tag)
    return tuple(found)

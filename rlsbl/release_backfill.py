"""The release-archive backfill engine: bring a repository's archives into the fate model.

A release archive (``.rlsbl/releases/v{X.Y.Z}.toml``, and the releasable-level
equivalent) is the authoritative record of what a version shipped: the bump
type, the description and context every later changelog regeneration reads back,
and the RELEASE COMMIT -- the ``candidate_sha`` the version shipped from and the
``tree_hashes`` each released path carried.  Repositories that predate any of
that carry archives with no release commit, archives missing required fields,
archives written before the strictspec ``format_version`` gate existed, released
versions with no archive at all, and version tags no archive accounts for.

This module is what ``rlsbl release backfill`` runs, and what
``scripts/migrate_workspace_model.py`` calls in-process after it rewrites a
workspace.  It observes one repository and decides, per subject, exactly one
verdict:

``unexplained-tag``
    A tag in the namespace that nothing accounts for.  Listed FIRST, and a
    single one refuses the whole apply -- see :func:`unexplained_error`.
``adopt``
    A tag that is one of the refs some version of this repository would own --
    asked of ``expected_refs``, never rendered here -- for a version no archive
    and no changelog file records.  It gets an archive recording the release the
    tag is evidence of.
``materialize``
    A version the changelog records as released, with no archive at all.
``repair``
    An archive that exists and is incomplete: missing required fields, missing
    the ``format_version`` gate, missing its fate, or all three.
``settled``
    Nothing is proposed.  An archive that already records one of the three
    fates and carries every required field is done -- which is what makes the
    pass idempotent.

The fate model, and the one fate this pass will not derive
----------------------------------------------------------

The three fates are ``recorded`` (a release commit), ``unrecoverable`` (it
shipped, from a commit nothing can name) and ``never_released`` (the version
NUMBER exists, no release does).  This pass derives the first two, and NEVER the
third: a never-released version has no tag and no version-bump commit by
construction, which is indistinguishable from a released version whose commit is
gone.  Only an operator knows which it is, so ``never_released`` is DECLARED --
by writing the archive with ``never_released = true`` before running the pass.
An archive that already declares it is settled and is never written to again.

The recovery chain
------------------

A reconstructed description comes from the first of these that yields one, and
the archive records which:

1. the version's GitHub Release body -- unless it carries no substantive
   content, which auto-generated compare-link boilerplate is not;
2. the version's CHANGELOG.md section;
3. the commit subjects in the version's tag range;
4. a placeholder that names the recovery obligation.

An operator who has reviewed the descriptions supplies them instead, through
``--overrides`` (see :func:`read_overrides`), which is applied before any of the
chain runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field

import tomlkit

from . import effects
from .changelog.files import list_versioned_files
from .errors import RlsblError
from .git_util import tree_rev_spec
from .preview_apply import Preview, VerdictItem, render_preview
from .release_file import (
    NEVER_RELEASED_FIELD,
    RELEASE_COMMIT_FIELDS,
    SHIPPED_AS_FIELD,
    UNRECOVERABLE_FIELD,
    archive_sort_key,
    archive_version,
    archived_release_path,
    write_archived_release_file,
    write_release_commit,
    write_unrecoverable_marker,
    writable_release_file,
)
from .tag_explanation import SOURCE_NON_VERSION_TAG, build as build_tag_explanations
from .tag_glob import TagMode, parse_version_tag
from .transition_record import (
    get_transition_record_path,
    repository_transition_record_path,
)
from .utils import commit_files, extract_changelog_entry_from_text


class BackfillError(RlsblError):
    """The repository cannot be backfilled as it stands.

    Raised for a condition an operator must resolve before the pass can write
    anything: a workspace file that exists but does not load, an unexplained
    tag, an overrides file naming a version this repository does not have, a
    stash sitting in the repository.  Nothing is written when one is raised.
    """


# Every subprocess states its own timeout: a backfill that hangs on a git or gh
# call in a repository with an unusual object store is worse than one that fails.
GIT_TIMEOUT = 60

# The strictspec gate, stamped verbatim onto archives written before the gate
# existed. Prepended as text rather than through a tomlkit round-trip so every
# other byte of the operator's file is preserved exactly.
FORMAT_VERSION_STAMP = (
    "# strictspec document version gate (do not remove)\nformat_version = 1\n"
)

# The header comment block on an archive this pass materialized. It says the
# file was written after the fact, which the reader of a 0444 archive otherwise
# has no way to know, and it enumerates where each reconstructed value can have
# come from.
MATERIALIZED_HEADER = [
    "Materialized by `rlsbl release backfill`: this version shipped before",
    "rlsbl archived a release file per version, so no archive existed.",
    "Every reconstructed value names its source on the field below it. A",
    "description is recovered from the first source that yields one: an",
    "operator-reviewed --overrides file, then the version's GitHub Release",
    "body, then its CHANGELOG.md section, then the commit subjects in its tag",
    "range, and otherwise a placeholder naming the recovery obligation. bump",
    "is derived by version arithmetic against the predecessor, and include",
    "reflects the targets detected at backfill time (the historical target set",
    "is not recoverable).",
]

# What a materialized archive says when no description could be recovered. It
# names the obligation rather than pretending the version had no summary.
PLACEHOLDER_DESCRIPTION = (
    "RECOVERY OBLIGATION: no description was recoverable for this version "
    "(neither the GitHub Release notes, nor the CHANGELOG.md section, nor the "
    "commit subjects in its tag range carried one). Author a real description "
    "from this version's changelog entries and regenerate."
)

# The fields a release document must carry to be readable at all. `bump`,
# `include`, `exclude` and `description` are the schema's required set;
# `format_version` is the gate in front of it.
REQUIRED_FIELDS = ("format_version", "bump", "include", "exclude", "description")

# How many commit subjects a reconstructed description quotes. A handful says
# what the release was about; the whole range is the changelog's job.
_SUBJECT_LIMIT = 5

STATE_UNEXPLAINED = "unexplained_tag"
STATE_ADOPT = "adopt"
STATE_MATERIALIZE = "materialize"
STATE_REPAIR = "repair"
STATE_SETTLED = "settled"


# ---------------------------------------------------------------------------
# git plumbing -- every read goes through the effect seam, so an observation
# running under preview_apply.no_writes is screened by the observe allowlist.
# ---------------------------------------------------------------------------


def _git(repo, args, *, timeout=GIT_TIMEOUT):
    """Run a read-only git command in *repo*; returns stdout stripped, or ""."""
    try:
        result = effects.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout,
            cwd=repo,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    if effects.unsettled(result) or getattr(result, "returncode", 1) != 0:
        return ""
    return (result.stdout or "").strip()


def rev_parse(repo, spec):
    """Resolve *spec* to an object hash, or None when it does not resolve."""
    return _git(repo, ["rev-parse", "--verify", "--quiet", spec]) or None


def all_tags(repo):
    return sorted(t for t in _git(repo, ["tag", "-l"]).splitlines() if t.strip())


def stash_entries(repo):
    """The repository's stash entries, one per line."""
    return [line for line in _git(repo, ["stash", "list"]).splitlines() if line.strip()]


def bump_commit_messages(scope, version):
    """Every whole commit message a release of *scope* writes for *version*.

    The release flow commits the version bump under one of two shapes, and which
    one is not a guess:

    * a RELEASABLE's bump commit names the releasable --
      ``{name}: release v{version}`` (see
      ``rlsbl.commands.release``'s commit-message step);
    * everything else commits the release's own TAG STRING, which for a
      standalone repository is ``v{version}`` and for a member is that member's
      spelling -- so the scope's own ref set supplies them.

    The bare version is accepted too, because older flows wrote it. Every
    message is matched WHOLE, so ``core: release v1.2.3`` can never be found by
    a sibling releasable looking for its own bump commit.
    """
    messages = [f"v{version}", version, *scope.tag_candidates(version)]
    releasable = getattr(scope.ref_ctx, "releasable_name", None)
    if releasable:
        messages.append(f"{releasable}: release v{version}")
    return list(dict.fromkeys(messages))


def find_bump_commits(repo, messages):
    """Commits whose whole message is one of *messages*.

    That commit IS the release candidate even when the tag that should point at
    it is missing.
    """
    alternatives = "|".join(re.escape(m) for m in messages if m)
    if not alternatives:
        return []
    out = _git(
        repo,
        ["log", "--all", "--extended-regexp", f"--grep=^({alternatives})$",
         "--format=%H"],
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_subjects(repo, sha, predecessor_sha):
    """The subjects of the commits *sha* added over *predecessor_sha*.

    Without a predecessor the range is open on the left, so the read is bounded
    by ``--max-count`` instead: an initial release would otherwise quote the
    entire history.
    """
    args = ["log", f"--max-count={_SUBJECT_LIMIT + 1}", "--format=%s"]
    args.append(f"{predecessor_sha}..{sha}" if predecessor_sha else sha)
    return [line.strip() for line in _git(repo, args).splitlines() if line.strip()]


def tree_hashes_at(repo, sha, released_paths):
    """Tree hashes for the released paths at *sha*. Returns ``(trees, notes)``.

    A declared path that does not exist at that commit is dropped with a note --
    a workspace's member directories did not exist during its standalone era,
    and recording a tree for a path that was not there would be a fabrication.
    When nothing resolves, the root tree under ``"."`` is the honest record of
    what that commit released.
    """
    trees = {}
    notes = []
    for path in released_paths:
        tree = rev_parse(repo, tree_rev_spec(sha, path))
        if tree:
            trees[path] = tree
        elif path != ".":
            notes.append(f"path {path!r} did not exist at {sha[:8]}")
    if not trees:
        root = rev_parse(repo, tree_rev_spec(sha, "."))
        if root:
            trees["."] = root
            notes.append(
                'no declared path existed at this commit; recorded the root '
                'tree as "."'
            )
    return trees, notes


# ---------------------------------------------------------------------------
# Scopes: one per independently-versioned release-state directory
# ---------------------------------------------------------------------------


@dataclass
class Scope:
    """One independently-versioned release-state location in a repository.

    ``released_paths`` are the repo-relative paths whose trees the release
    commit records: ``["."]`` for a standalone repository, one entry per member
    directory for a workspace releasable.

    ``target`` and ``ref_ctx`` are what this scope asks the ref question with.
    They are not a second opinion about tag naming: they are the very inputs the
    release flow builds for a release of this scope, so
    :meth:`tag_candidates` and the refs a release creates are one derivation.
    """

    label: str
    releases_dir: str  # absolute
    changes_dir: str  # absolute
    changelog_md: str  # absolute
    released_paths: list
    target: object  # the BaseTarget subclass that answers ref questions here
    ref_ctx: object  # rlsbl.targets.refs.RefContext
    transition_record_path: str = ""
    #: Reasons a part of this scope's ref set could not be derived, recorded as
    #: they are hit and reported with the plan. Empty is the normal state.
    undecidable: list = field(default_factory=list)
    _tags: dict = field(default_factory=dict, repr=False, compare=False)

    def tag_candidates(self, version):
        """Every tag spelling *version* is addressable under, in ref order.

        ``expected_refs`` is the single authority for a version's ref set --
        primary tag, the companion tags its members' ecosystems require, and the
        aliases this repository's own records attribute to it -- and this pass
        asks it rather than rendering a tag format of its own. A private
        rendering saw only the primary spelling, so every Go companion tag a
        monorepo release creates read as unexplained, and one unexplained tag
        refuses the whole apply.

        Cached per version: the answer resolves each member's config, and the
        pass asks it several times per version.
        """
        if version not in self._tags:
            self._tags[version] = list(self._expected(version))
        return list(self._tags[version])

    def _expected(self, version):
        """``expected_refs`` for *version*, minus any part that is underivable.

        COMPANION tags are the one part that can be unanswerable: they are
        collected from the members' own effective configs, and a repository this
        pass exists to repair may have a member (commonly a root member, which
        may not carry a ``.rlsbl/`` of its own at all) whose config names no
        targets and no publish mode. The primary tag and the recorded aliases
        never depend on that.

        So the question is asked again with NO member set -- the same authority,
        told there are no members to consult -- and the reason is recorded on
        :attr:`undecidable`, which the plan prints and the unexplained-tag
        refusal repeats. Nothing is guessed and nothing is silent: a companion
        tag whose derivation failed is then reported as an unexplained tag, with
        the three resolutions and the reason its scope could not account for it.
        """
        from dataclasses import replace

        try:
            return self.target.expected_refs(version, self.ref_ctx).tags
        except RlsblError as exc:
            reason = (
                f"{self.label}: the companion tags its members' ecosystems "
                f"require cannot be derived ({exc})"
            )
            if reason not in self.undecidable:
                self.undecidable.append(reason)
            return self.target.expected_refs(
                version, replace(self.ref_ctx, member_package_paths=None),
            ).tags

    def tag_spellings(self):
        """The scope's ref set rendered as format strings, for the plan header.

        Asked at the literal ``{version}`` placeholder rather than at a real
        version: every spelling any target produces is a plain concatenation
        around the version, so rendering at the placeholder yields exactly the
        format strings -- from the same authority as every decision below,
        instead of a display-only rendering that could disagree with them. No
        alias joins in, because ``{version}`` parses as no version.
        """
        return self._expected("{version}")


def scope_target(dir_path, *, releasable_config_dir=None):
    """The target that answers ref questions for the project at *dir_path*.

    The first detected target, exactly as ``rlsbl release reconcile`` resolves
    it. A directory with no detectable target still gets an answer rather than a
    refusal, and the answer is not a guess: ref naming has a defined default
    (``v{version}``, :class:`~rlsbl.targets.base.BaseTarget`'s own
    ``tag_format``) that holds independently of any ecosystem.

    A config that cannot be read yields the same default. This is an
    observation of a repository whose state predates the current model -- the
    pass exists BECAUSE the project's records are incomplete -- so an
    unreadable config must not stop it from reading the rest of the namespace.
    """
    from .errors import ConfigError
    from .targets import TARGETS, detect_targets
    from .targets.base import BaseTarget

    try:
        entries = detect_targets(dir_path, releasable_config_dir=releasable_config_dir)
    except (ConfigError, OSError):
        entries = []
    if not entries:
        return BaseTarget()
    return TARGETS.get(entries[0].name) or BaseTarget()


def discover_scopes(repo):
    """Enumerate the repository's release-state scopes.

    Two repository shapes, and no third: a repository with no workspace file is
    a STANDALONE one with a single scope at the root; a WORKSPACE yields one
    scope per releasable, plus one per member that stands outside every
    releasable and still keeps its own ``.rlsbl/releases/`` or
    ``.rlsbl/changes/``. A workspace file that exists but does not load is a
    hard error carrying the loader's own message.
    """
    from .targets.refs import ref_context

    workspace_file = os.path.join(repo, ".rlsbl-monorepo", "workspace.toml")
    if not os.path.isfile(workspace_file):
        return [
            Scope(
                label="standalone",
                releases_dir=os.path.join(repo, ".rlsbl", "releases"),
                changes_dir=os.path.join(repo, ".rlsbl", "changes"),
                changelog_md=os.path.join(repo, "CHANGELOG.md"),
                released_paths=["."],
                target=scope_target(repo),
                ref_ctx=ref_context(repo_root=repo),
                transition_record_path=get_transition_record_path(repo),
            )
        ]

    from .workspace import (
        WorkspaceError,
        load_releasables,
        load_workspace,
        members_of,
    )

    # A workspace file that is there but does not load is a hard error carrying
    # the loader's own message. It used to be swallowed into an empty
    # releasable list labelled "implicit mode", which was already wrong when it
    # was written and is now impossible: a workspace with no [[releasables]]
    # section is refused by ``load_workspace`` itself, so every failure this
    # caught was a BROKEN workspace being quietly downgraded to a repository
    # with no releasables -- whose archives the pass would then leave
    # unrepaired while reporting that it had nothing to do.
    try:
        projects = load_workspace(repo)
        releasables = load_releasables(repo, projects)
    except (OSError, ValueError, WorkspaceError) as exc:
        raise BackfillError(f"{workspace_file}: {exc}") from exc

    scopes = []
    releasable_members = set()

    for rel in releasables:
        members = members_of(rel.name, projects)
        for m in members:
            releasable_members.add(m.path)
        rel_dir = os.path.join(repo, ".rlsbl-monorepo", "releasables", rel.name)
        member_paths = [m.path for m in members]
        paths = member_paths or ["."]
        scopes.append(
            Scope(
                label=rel.name,
                releases_dir=os.path.join(rel_dir, "releases"),
                changes_dir=os.path.join(rel_dir, "changes"),
                changelog_md=os.path.join(repo, "CHANGELOG.md"),
                released_paths=paths,
                target=scope_target(
                    os.path.join(repo, paths[0]), releasable_config_dir=rel_dir,
                ),
                # Exactly the context a release of this releasable builds: the
                # releasable owns the naming, and its members are what the
                # companion tags are collected from.
                ref_ctx=ref_context(
                    repo_root=repo,
                    project_path=member_paths[0] if member_paths else None,
                    primary_tag_format=rel.effective_tag_format,
                    releasable_name=rel.name,
                    member_package_paths=member_paths,
                    releasable_config_dir=rel_dir,
                ),
                transition_record_path=get_transition_record_path(
                    repo, releasable_dir=rel_dir,
                ),
            )
        )

    for proj in projects:
        if proj.path in releasable_members:
            continue
        proj_dir = os.path.join(repo, proj.path)
        releases_dir = os.path.join(proj_dir, ".rlsbl", "releases")
        changes_dir = os.path.join(proj_dir, ".rlsbl", "changes")
        if not os.path.isdir(releases_dir) and not os.path.isdir(changes_dir):
            continue
        scopes.append(
            Scope(
                label=proj.name,
                releases_dir=releases_dir,
                changes_dir=changes_dir,
                changelog_md=os.path.join(proj_dir, "CHANGELOG.md"),
                released_paths=[proj.path],
                target=scope_target(proj_dir),
                # A member outside every releasable is its own release unit, so
                # it is its own member set: its primary tag is the target's
                # monorepo spelling, and a Go member still owes the module proxy
                # the ``{path}/v{version}`` companion the ecosystem resolves by.
                ref_ctx=ref_context(
                    repo_root=repo,
                    project_path=proj.path,
                    monorepo_name=proj.name,
                    member_package_paths=[proj.path],
                ),
                transition_record_path=get_transition_record_path(proj_dir),
            )
        )

    return scopes


# ---------------------------------------------------------------------------
# Reading what is already there
# ---------------------------------------------------------------------------


def archived_versions(scope):
    """Map version -> archive path for every ``v{X}.toml`` in the scope."""
    result = {}
    if not os.path.isdir(scope.releases_dir):
        return result
    for name in os.listdir(scope.releases_dir):
        # rlsbl.release_file owns both the archive-name grammar and the order
        # archives stand in; this pass reads the very directories the release
        # record reads, so a second opinion here about which files in them are
        # archives (or which of two prereleases came first) would make the pass
        # repair files the release record ignores and derive a bump from the
        # wrong predecessor.
        version = archive_version(name)
        if version is not None:
            result[version] = os.path.join(scope.releases_dir, name)
    return result


def changelog_versions(scope):
    """Map version -> JSONL path for every finalized changelog file."""
    return {v: p for v, p in list_versioned_files(scope.changes_dir)}


@dataclass
class ArchiveState:
    """What an existing archive already carries, without validating its shape.

    tomllib rather than the strictspec reader on purpose: the archives this pass
    repairs are exactly the ones the reader would reject (no ``format_version``
    gate, a missing required field), so asking the reader first would refuse to
    look at the file the pass exists to fix.
    """

    present: set
    recorded: bool
    unrecoverable: bool
    never_released: bool
    shipped_as: str | None
    description: str
    context: str
    candidate_sha: str

    @property
    def missing(self):
        return [f for f in REQUIRED_FIELDS if f not in self.present]

    @property
    def settled_fate(self):
        return self.recorded or self.unrecoverable or self.never_released


def read_archive_state(path):
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise BackfillError(f"{path}: unparseable TOML: {exc}") from exc
    description = data.get("description")
    context = data.get("context")
    return ArchiveState(
        present={k for k in REQUIRED_FIELDS if k in data},
        recorded=any(f in data for f in RELEASE_COMMIT_FIELDS),
        unrecoverable=bool(data.get(UNRECOVERABLE_FIELD)),
        # The third fate. A never-released version has no tag and no
        # version-bump commit BY CONSTRUCTION -- that is what "no release was
        # published under this number" means -- so a pass that reads only the
        # first two fates sees an unsettled archive, plans the unrecoverable
        # marker for it, and turns a correct one-fate archive into a two-fate
        # document. It is read here so the pass can recognize the fate as
        # settled and leave it alone.
        never_released=bool(data.get(NEVER_RELEASED_FIELD)),
        shipped_as=data.get(SHIPPED_AS_FIELD),
        description=description if isinstance(description, str) else "",
        # Read for the same reason the description is: an override that
        # restates what the archive already says is not a change, and a
        # re-plan of it on every run would make the pass never settle.
        context=context if isinstance(context, str) else "",
        candidate_sha=(
            data["candidate_sha"] if isinstance(data.get("candidate_sha"), str)
            else ""
        ),
    )


# ---------------------------------------------------------------------------
# The recovery chain
# ---------------------------------------------------------------------------

# An auto-generated Release body's compare link. It is the one thing GitHub
# writes into a body nobody authored, so a body carrying only that is a body
# with nothing in it.
_COMPARE_LINK_RE = re.compile(r"^\**\s*full changelog\s*\**\s*:", re.I)

_BULLET_RE = re.compile(r"^[-*+]\s+")


def _is_boilerplate(line):
    return (
        not line
        or line.startswith("<!--")
        or line.startswith("#")
        or _COMPARE_LINK_RE.match(line) is not None
    )


def body_is_substantive(body):
    """Does *body* carry content, as opposed to auto-generated boilerplate?

    Bullets, prose and blockquote openings are content. A heading with nothing
    under it, an HTML comment and the ``**Full Changelog**: ...`` compare link
    GitHub generates are not -- a body made of only those is ABSENT for the
    recovery chain's purposes and falls through to the next source.
    """
    for raw in (body or "").splitlines():
        if not _is_boilerplate(raw.strip()):
            return True
    return False


def description_from_body(body):
    """A one-line description from a Release body, or None.

    Prefers the prose paragraph the notes open with. A body that opens straight
    into its bullets (the common shape for generated-then-edited notes) yields
    its first bullet instead, and one that opens with a blockquote yields that
    -- both are content by the same rule :func:`body_is_substantive` applies.

    A body whose only content is a MARKDOWN TABLE -- an asset matrix, a
    platform list -- deliberately yields None even though
    :func:`body_is_substantive` calls it content. The two answer different
    questions: the body carries something (so it is not the boilerplate a body
    nobody authored consists of), but a table cell is data rather than a
    sentence about the release, and quoting one as the version's description
    would put a filename where a summary belongs. The chain then continues to
    the CHANGELOG.md section, and the archive records THAT as its source, so
    what the reader is told is what actually answered.
    """
    if not body_is_substantive(body):
        return None
    lines = [raw.strip() for raw in body.splitlines()]
    prose = []
    for line in lines:
        if _is_boilerplate(line):
            if prose:
                break
            continue
        if _BULLET_RE.match(line) or line.startswith(("<details", "|", ">")):
            break
        prose.append(line)
    text = " ".join(prose).strip()
    if text:
        return text
    for line in lines:
        if _BULLET_RE.match(line):
            return _BULLET_RE.sub("", line).strip() or None
    for line in lines:
        if line.startswith(">"):
            return line.lstrip("> ").strip() or None
    return None


def lead_paragraph(markdown):
    """The prose paragraph a CHANGELOG.md version section opens with, or None.

    A version section is a description paragraph (when it has one) followed by
    ``### Features`` / bullet groups. Everything from the first heading, bullet
    or details block onward is the generated part, so only the leading prose is
    a recovered description.
    """
    if not markdown:
        return None
    lines = []
    for raw in markdown.strip().splitlines():
        line = raw.strip()
        if line.startswith(("#", "-", "*", "<details", "<!--", "|", ">")):
            break
        if not line and lines:
            break
        if line:
            lines.append(line)
    text = " ".join(lines).strip()
    return text or None


def _subject_description(subjects):
    """A description built from a version's commit subjects, or None."""
    quoted = [s for s in subjects[:_SUBJECT_LIMIT] if s]
    if not quoted:
        return None
    more = " (and later commits)" if len(subjects) > _SUBJECT_LIMIT else ""
    return f"Reconstructed from this version's commit subjects: " + "; ".join(quoted) + f".{more}"


@dataclass
class Recovery:
    """Where a reconstructed description may come from.

    ``gh`` is the runner :func:`rlsbl.release_publication.read_release_body`
    drives, injected so the Release source can be exercised (and refused)
    without a network. ``use_gh`` False skips the source entirely, for an
    offline pass.
    """

    use_gh: bool = True
    gh: object = None

    def release_body(self, tag):
        """The Release body for *tag*, or None when it cannot be read.

        Fails soft in every direction -- no ``gh``, not authenticated, no
        Release for the tag, a network failure -- because it is the FIRST of
        three sources and an unavailable source must fall through to the next
        rather than abort the pass.
        """
        if not (self.use_gh and tag):
            return None
        from .release_publication import read_release_body
        from .utils import run_gh

        try:
            return read_release_body(tag, gh=self.gh or run_gh)
        except Exception:
            return None


def recover_description(repo, scope, version, tag, *, recovery, sha,
                        predecessor_sha):
    """Recover a version's description. Returns ``(description, source)``."""
    body = recovery.release_body(tag)
    text = description_from_body(body)
    if text:
        return text, f"github-release:{tag}"
    if os.path.isfile(scope.changelog_md):
        with open(scope.changelog_md, "r", encoding="utf-8") as f:
            content = f.read()
        text = lead_paragraph(extract_changelog_entry_from_text(content, version))
        if text:
            return text, "changelog-md"
    if sha:
        text = _subject_description(commit_subjects(repo, sha, predecessor_sha))
        if text:
            span = f"{predecessor_sha[:8]}..{sha[:8]}" if predecessor_sha else sha[:8]
            return text, f"commit-subjects:{span}"
    return PLACEHOLDER_DESCRIPTION, "placeholder"


def derive_bump(version, predecessor):
    """Derive the bump type from version arithmetic against the predecessor.

    The HIGHEST-ORDER component that differs names the bump, and the SIZE of the
    difference is never read: 0.1.0 -> 0.4.0 is a ``minor`` exactly as 0.1.0 ->
    0.2.0 is, because a gap in the archived history says nothing about how many
    releases crossed it. A version with no predecessor is measured against
    ``0.0.0``, so a first release of ``0.1.0`` derives ``minor``. ``infra`` is a
    patch increment and therefore indistinguishable here -- a derived ``patch``
    is the honest answer, not a guess at intent.
    """
    cur = [int(p) for p in version.partition("-")[0].split(".")[:3]]
    prev = [int(p) for p in (predecessor or "0.0.0").partition("-")[0].split(".")[:3]]
    if cur[0] != prev[0]:
        return "major"
    if cur[1] != prev[1]:
        return "minor"
    return "patch"


def detect_include(repo, scope):
    """Target names for a materialized archive, detected at backfill time.

    A soft source: the historical target set is not recoverable, so a project
    whose config cannot answer contributes an empty list rather than stopping
    the pass. What it absorbs is exactly that -- a config that does not resolve
    and a directory that cannot be read. Every other failure propagates,
    ``ObserveWriteError`` above all: that one is the effects layer refusing a
    write attempted during an observation, a defect report about rlsbl itself,
    and swallowing it here would hide the very thing the no-writes screen
    exists to surface.
    """
    from .errors import ConfigError
    from .targets import detect_targets

    if scope.released_paths == ["."]:
        proj_dir = repo
    else:
        proj_dir = os.path.join(repo, scope.released_paths[0])
    try:
        return [t.name for t in detect_targets(proj_dir)]
    except (ConfigError, OSError):
        return []


# ---------------------------------------------------------------------------
# The operator's reviewed descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Override:
    description: str
    context: str = ""


def read_overrides(path):
    """Read an ``--overrides`` file: ``[versions."X.Y.Z"]`` tables.

    Shape, and nothing else accepted::

        [versions."0.1.0"]
        description = "What this release was."
        context = "Optional, multiline, why."

    Every refusal is a hard error naming the offending key: an overrides file is
    reviewed text an operator wrote deliberately, so a typo that silently
    applied to nothing would be worse than one that stops the pass.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as exc:
        raise BackfillError(f"no overrides file at {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackfillError(f"{path}: {exc}") from exc

    unknown = [k for k in data if k != "versions"]
    if unknown:
        raise BackfillError(
            f"{path}: unknown top-level key(s) {sorted(unknown)}; an overrides "
            f'file holds one [versions."X.Y.Z"] table per version and nothing '
            f"else"
        )
    versions = data.get("versions")
    if versions is None:
        raise BackfillError(
            f'{path}: no [versions] table; write one [versions."X.Y.Z"] table '
            f"per version whose description you have reviewed"
        )
    if not isinstance(versions, dict):
        raise BackfillError(f"{path}: [versions] must be a table of version tables")

    out = {}
    for version, entry in versions.items():
        where = f'{path}: [versions."{version}"]'
        if not isinstance(entry, dict):
            raise BackfillError(f"{where} must be a table")
        extra = [k for k in entry if k not in ("description", "context")]
        if extra:
            raise BackfillError(
                f"{where}: unknown key(s) {sorted(extra)}; only description and "
                f"context are accepted"
            )
        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            raise BackfillError(
                f"{where}: description must be a non-empty string"
            )
        context = entry.get("context", "")
        if not isinstance(context, str):
            raise BackfillError(f"{where}: context must be a string")
        out[version] = Override(
            description=description.strip(), context=context.strip(),
        )
    return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class VersionPlan:
    """What this pass will do to one version's archive."""

    scope: Scope
    version: str
    archive_path: str
    archive_exists: bool
    state: str = STATE_SETTLED
    actions: list = field(default_factory=list)
    tag: str | None = None
    probed_tags: list = field(default_factory=list)
    candidate_sha: str | None = None
    recorded_from: str = ""  # "tag" | "shipped-as" | "bump-commit" | ""
    tree_hashes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    stamp_format_version: bool = False
    fill_fields: dict = field(default_factory=dict)  # field -> (value, source)
    unrecoverable: bool = False
    bump: str = ""
    description: str = ""
    description_source: str = ""
    include: list = field(default_factory=list)
    context: str = ""
    shipped_as: str | None = None

    @property
    def changed(self):
        return bool(self.actions)

    @property
    def key(self):
        return f"{self.scope.label} {self.version}"


@dataclass
class UnexplainedTag:
    """A tag in the namespace that nothing in this repository accounts for."""

    tag: str
    scheme: str = ""
    version: str = ""
    probed: list = field(default_factory=list)

    @property
    def key(self):
        return f"tag {self.tag}"


@dataclass
class Plan:
    repo: str
    scopes: list
    versions: list = field(default_factory=list)
    unexplained: list = field(default_factory=list)
    outside_the_model: list = field(default_factory=list)
    stash: list = field(default_factory=list)
    transition_record_path: str = ""

    @property
    def changed_versions(self):
        return [v for v in self.versions if v.changed]


def _scope_for_tag(scopes, tag):
    """The scope one of whose refs IS *tag*, with the version. Or None.

    Answered by CONSTRUCTION -- deriving each scope's whole ref set at the
    version the tag parses as, and comparing -- rather than by pattern-matching
    the tag, so a scope can never claim a spelling it would not itself write.
    """
    parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
    if parsed is None:
        return None, None
    for scope in scopes:
        if tag in scope.tag_candidates(parsed.version):
            return scope, parsed.version
    return None, None


def build_plan(repo, *, use_gh=True, gh=None, overrides=None):
    """Inspect the repository and decide every action, writing nothing."""
    scopes = discover_scopes(repo)
    recovery = Recovery(use_gh=use_gh, gh=gh)
    overrides = dict(overrides or {})
    plan = Plan(
        repo=repo, scopes=scopes,
        transition_record_path=repository_transition_record_path(repo),
    )

    known_tags = {}   # tag -> version, for every scope spelling that resolves
    in_scope = set()  # every version any scope accounts for

    # Pass one: the versions the archives and changelog files already name,
    # plus the tags they claim.
    per_scope_versions = {}
    for scope in scopes:
        archives = archived_versions(scope)
        changelogs = changelog_versions(scope)
        states = {
            v: read_archive_state(path) for v, path in archives.items()
        }
        versions = sorted(set(archives) | set(changelogs), key=archive_sort_key)
        per_scope_versions[scope.label] = (archives, changelogs, states, versions)
        in_scope.update(versions)
        for version in versions:
            state = states.get(version)
            # EVERY spelling that resolves, not just the first. A version can
            # stand under several live spellings at once -- reconcile's own
            # repair MINTS the current scheme's tag beside the historical one an
            # archive names in shipped_as, and a Go member's companion tag
            # stands beside its releasable's primary -- and a spelling this pass
            # stops at is a spelling nothing explains on the next run.
            for candidate in _probe_order(scope, version, state):
                if rev_parse(repo, f"{candidate}^{{commit}}"):
                    known_tags[candidate] = version

    # Pass two: version tags that belong to some scope's own ref set and that
    # NO store records. They are evidence of a release nothing wrote down, so they
    # are adopted rather than reported.
    adopted = []
    adopting_tag = {}  # (scope label, version) -> the spelling adopted from
    for tag in all_tags(repo):
        if tag in known_tags:
            continue
        scope, version = _scope_for_tag(scopes, tag)
        if scope is None:
            continue
        archives, changelogs, states, versions = per_scope_versions[scope.label]
        if version in versions:
            continue
        # A version adopted under one spelling is adopted once. Every FURTHER
        # spelling of it that exists is explained by the same release, so it
        # joins ``known_tags`` without proposing a second archive.
        key = (scope.label, version)
        if key not in adopting_tag:
            adopting_tag[key] = tag
            adopted.append((scope, version, tag))
        known_tags[tag] = version
        in_scope.add(version)

    unknown_overrides = sorted(set(overrides) - in_scope)
    if unknown_overrides:
        raise BackfillError(
            f"the overrides file names version(s) this pass does not have: "
            f"{', '.join(unknown_overrides)}. Every override must name a "
            f"version some scope of this repository records (an archive, a "
            f"finalized changelog file, or a version tag under its own "
            f"scheme); nothing is silently ignored."
        )

    for scope in scopes:
        archives, changelogs, states, versions = per_scope_versions[scope.label]
        adopted_here = sorted(
            (v for s, v, _t in adopted if s is scope),
            key=archive_sort_key,
        )
        all_versions = sorted(set(versions) | set(adopted_here), key=archive_sort_key)
        adopted_tags = {v: t for s, v, t in adopted if s is scope}
        include = None  # detected lazily, only when something is written
        for index, version in enumerate(all_versions):
            predecessor = all_versions[index - 1] if index else None
            vp = _plan_version(
                repo, scope, version,
                predecessor=predecessor,
                archives=archives, changelogs=changelogs, states=states,
                adopted_tag=adopted_tags.get(version),
                recovery=recovery,
                override=overrides.get(version),
                include_hint=include,
                plan=plan,
            )
            if vp.include:
                include = vp.include
            plan.versions.append(vp)

    explanations = build_tag_explanations(
        version_tags=known_tags,
        releases_dirs=[s.releases_dir for s in scopes],
        transition_record_paths=_record_paths(scopes, plan),
    )
    for tag in all_tags(repo):
        explanation = explanations.explain(tag)
        if explanation is not None:
            if explanation.source == SOURCE_NON_VERSION_TAG:
                plan.outside_the_model.append(tag)
            continue
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        probed = []
        if parsed is not None:
            for scope in scopes:
                probed.extend(
                    f"{scope.label}: {spelling}"
                    for spelling in scope.tag_candidates(parsed.version)
                )
        plan.unexplained.append(UnexplainedTag(
            tag=tag,
            scheme=parsed.scheme if parsed else "",
            version=parsed.version if parsed else "",
            probed=probed,
        ))

    plan.stash = stash_entries(repo)
    return plan


def _record_paths(scopes, plan):
    paths = [plan.transition_record_path]
    paths.extend(s.transition_record_path for s in scopes if s.transition_record_path)
    return list(dict.fromkeys(p for p in paths if p))


def _probe_order(scope, version, state):
    """Tag spellings to try for *version*, historical spelling first.

    An archive recording ``shipped_as`` names the spelling the version ACTUALLY
    shipped under, from before a rename or a repository boundary moved it. It is
    tried first because it is a FACT about this version, where the scheme's
    spelling is only what today's scheme would produce.
    """
    order = []
    if state is not None and state.shipped_as:
        order.append(state.shipped_as)
    order.extend(scope.tag_candidates(version))
    return list(dict.fromkeys(order))


def _plan_version(repo, scope, version, *, predecessor, archives, changelogs,
                  states, adopted_tag, recovery, override, include_hint, plan):
    archive_path = archives.get(
        version, archived_release_path(scope.releases_dir, version),
    )
    exists = version in archives
    state = states.get(version)

    vp = VersionPlan(
        scope=scope, version=version, archive_path=archive_path,
        archive_exists=exists,
    )

    if version not in changelogs:
        vp.notes.append("no changelog JSONL for this version")

    # A settled NEVER-RELEASED archive is done, and the pass proposes nothing
    # for it -- not a commit, not a field, not an override. Everything below
    # derives facts about a version that SHIPPED, and a version no release ever
    # used has no tag and no version-bump commit by construction, so the
    # derivation would conclude "unrecoverable" and plan a marker beside the one
    # already there.
    if state is not None and state.never_released:
        if override is not None:
            raise BackfillError(
                f"the overrides file names {version}, whose archive records "
                f"{NEVER_RELEASED_FIELD} = true ({archive_path}). That version "
                f"number exists but no release does, so it has no description "
                f"to review. Remove it from the overrides file, or -- if the "
                f"version really did ship -- unlock the archive (chmod 644), "
                f"delete the {NEVER_RELEASED_FIELD} line, relock it "
                f"(chmod 444), and re-run."
            )
        vp.notes.append(
            f"archive records {NEVER_RELEASED_FIELD} = true (the version "
            f"number exists, no release does); the fate is settled and nothing "
            f"is proposed"
        )
        return vp

    probed = _probe_order(scope, version, state)
    tag = None
    sha = None
    if adopted_tag:
        tag, sha = adopted_tag, rev_parse(repo, f"{adopted_tag}^{{commit}}")
    else:
        for candidate in probed:
            resolved = rev_parse(repo, f"{candidate}^{{commit}}")
            if resolved:
                tag, sha = candidate, resolved
                break
    vp.tag = tag
    vp.probed_tags = probed
    if tag:
        vp.recorded_from = (
            "shipped-as"
            if state is not None and state.shipped_as == tag
            else "tag"
        )
        if vp.recorded_from == "shipped-as":
            vp.notes.append(
                f"recorded from the historical spelling {tag} that the archive "
                f"names in {SHIPPED_AS_FIELD}"
            )

    already_recorded = state is not None and (state.recorded or state.unrecoverable)

    if not tag and not already_recorded:
        bump_commits = find_bump_commits(
            repo, bump_commit_messages(scope, version),
        )
        if bump_commits:
            sha = bump_commits[0]
            vp.recorded_from = "bump-commit"
            vp.notes.append(
                f"no tag (probed {', '.join(probed)}); recorded from the "
                f"version-bump commit {sha[:8]}"
            )
            if len(bump_commits) > 1:
                vp.notes.append(
                    f"{len(bump_commits)} commits carry this bump message; "
                    f"took the first ({', '.join(s[:8] for s in bump_commits)})"
                )
            vp.notes.append(
                f"if {version} was NEVER ACTUALLY RELEASED, this pass must not "
                f"record a commit for it: declare the fate first by writing "
                f"{archive_path} with {NEVER_RELEASED_FIELD} = true, and run "
                f"the backfill again. The declaration IS the archive -- there "
                f"is no flag and no input file for it, because only you know "
                f"whether a release happened."
            )
        else:
            vp.unrecoverable = True
            vp.notes.append(
                f"no tag (probed {', '.join(probed)}) and no version-bump "
                f"commit in history: unrecoverable"
            )

    if sha and not vp.unrecoverable and not already_recorded:
        vp.candidate_sha = sha
        trees, notes = tree_hashes_at(repo, sha, scope.released_paths)
        vp.tree_hashes = trees
        vp.notes.extend(notes)
        if not trees:
            vp.unrecoverable = True
            vp.candidate_sha = None
            vp.notes.append(f"commit {sha[:8]} yielded no tree for any released path")

    predecessor_sha = None  # resolved lazily, only for the commit-subject source

    def _describe():
        nonlocal predecessor_sha
        if override is not None:
            return override.description, "overrides-file"
        if predecessor and predecessor_sha is None:
            # Under EVERY spelling the predecessor owns, its recorded aliases
            # included: a predecessor that shipped before a rename stands under
            # the spelling its archive names in shipped_as, and resolving it
            # under today's scheme alone would leave the range open on the left
            # and quote earlier versions' commits as if they were this one's.
            for candidate in scope.tag_candidates(predecessor):
                predecessor_sha = rev_parse(repo, f"{candidate}^{{commit}}")
                if predecessor_sha:
                    break
        # The commit whose subjects describe this version: the one this pass
        # is about to record, else the one an already-recorded archive names,
        # else the tag's own commit.
        subject_sha = vp.candidate_sha or (
            state.candidate_sha if state is not None else ""
        ) or sha
        return recover_description(
            repo, scope, version, tag, recovery=recovery,
            sha=subject_sha, predecessor_sha=predecessor_sha,
        )

    if not exists:
        vp.state = STATE_ADOPT if adopted_tag else STATE_MATERIALIZE
        vp.include = list(include_hint if include_hint is not None
                          else detect_include(repo, scope))
        vp.bump = derive_bump(version, predecessor)
        vp.description, vp.description_source = _describe()
        vp.context = override.context if override is not None else ""
        # A tag whose spelling is not what this scope's scheme produces today
        # is the historical one, and the archive records it so the version is
        # never counted unexplained again.
        if tag and tag not in scope.tag_candidates(version):
            vp.shipped_as = tag
        if adopted_tag:
            vp.notes.append(
                f"the tag {adopted_tag} records a release no archive and no "
                f"changelog file named; adopting it as released"
            )
        vp.actions.append(
            f"write the archive (bump={vp.bump} from predecessor "
            f"{predecessor or '0.0.0'}, description from "
            f"{vp.description_source}, include={vp.include})"
        )
        if vp.unrecoverable:
            vp.actions.append(f"write {UNRECOVERABLE_FIELD} = true")
        else:
            vp.actions.append(
                f"record candidate_sha={vp.candidate_sha[:12]} "
                f"tree_hashes={ {k: v[:12] for k, v in vp.tree_hashes.items()} }"
            )
        return vp

    # An existing archive: complete every required field it lacks, then settle
    # its fate if it has none.
    vp.state = STATE_REPAIR
    missing = state.missing
    if "format_version" in missing:
        vp.stamp_format_version = True
        vp.actions.append("stamp format_version = 1")
    if "bump" in missing:
        vp.bump = derive_bump(version, predecessor)
        vp.fill_fields["bump"] = (
            vp.bump, f"version arithmetic against {predecessor or '0.0.0'}",
        )
    if "include" in missing:
        vp.include = list(include_hint if include_hint is not None
                          else detect_include(repo, scope))
        vp.fill_fields["include"] = (
            vp.include, "the targets detected at backfill time",
        )
    if "exclude" in missing:
        vp.fill_fields["exclude"] = ([], "the default: nothing excluded")
    if "description" in missing:
        vp.description, vp.description_source = _describe()
        vp.fill_fields["description"] = (vp.description, vp.description_source)
    elif override is not None and state.description != override.description:
        vp.description = override.description
        vp.description_source = "overrides-file"
        vp.fill_fields["description"] = (vp.description, "overrides-file")
    if (
        override is not None
        and override.context
        and state.context.strip() != override.context
    ):
        vp.context = override.context
        vp.fill_fields["context"] = (override.context, "overrides-file")
    for name, (value, source) in vp.fill_fields.items():
        vp.actions.append(f"write {name} (from {source})")

    if already_recorded:
        # Said only where there is other work, so it explains why the fate is
        # untouched. On an archive with nothing to do it would be one line of
        # noise per released version.
        if vp.actions:
            vp.notes.append(
                "already marked unrecoverable; the fate is left alone"
                if state.unrecoverable
                else "already recorded; the fate is left alone"
            )
    elif vp.unrecoverable:
        vp.actions.append(f"write {UNRECOVERABLE_FIELD} = true")
    else:
        vp.actions.append(
            f"record candidate_sha={vp.candidate_sha[:12]} "
            f"tree_hashes={ {k: v[:12] for k, v in vp.tree_hashes.items()} }"
        )
    if not vp.changed:
        vp.state = STATE_SETTLED
    return vp


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


def _unexplained_item(entry, plan):
    facts = []
    if entry.version:
        facts.append(
            f"parses under the {entry.scheme} scheme as version {entry.version}"
        )
        if entry.probed:
            facts.append(f"probed: {'; '.join(entry.probed)}")
    else:
        facts.append("parses under no recognized version-tag scheme")
    return VerdictItem(
        key=entry.key,
        state=STATE_UNEXPLAINED,
        summary="nothing in this repository accounts for this tag",
        facts=tuple(facts),
        detail=_resolutions(entry, plan),
        data=entry,
    )


def _resolutions(entry, plan):
    """The three cheap resolutions, spelled out so each can be performed."""
    return (
        f"  Resolve it in one of three ways, then re-run:\n"
        f"    1. ADOPT IT AS RELEASED. A tag that is one of the refs some\n"
        f"       version here would own is adopted automatically, and this one\n"
        f"       is not -- so it is spelled the way an older scheme spelled it.\n"
        f"       Record that version's archive with\n"
        f'       {SHIPPED_AS_FIELD} = "{entry.tag}", and the pass records its\n'
        f"       commit from this tag.\n"
        f"    2. RECORD IT AS A NON-VERSION TAG, if it is not a release at all\n"
        f"       (a nightly marker, a vendor tag imported with the history):\n"
        f"         rlsbl transition record --non-version-tag {entry.tag} \\\n"
        f"             --reason \"<why>\"\n"
        f"       The declaration is appended to\n"
        f"       {plan.transition_record_path}.\n"
        f"    3. DELETE IT, on your own explicit decision:\n"
        f"         git tag -d {entry.tag}\n"
        f"       (and on origin too, if it was ever pushed).\n"
        f"  rlsbl does not guess which of the three this is."
    )


def _version_item(vp):
    facts = tuple(f"note: {n}" for n in vp.notes)
    if vp.state == STATE_SETTLED:
        summary = "nothing to do"
    elif vp.state == STATE_ADOPT:
        summary = f"a released version only the tag {vp.tag} records"
    elif vp.state == STATE_MATERIALIZE:
        summary = "released, with no archive at all"
    else:
        summary = "the archive is incomplete"
    return VerdictItem(
        key=vp.key, state=vp.state, summary=summary,
        facts=(f"archive: {vp.archive_path}",) + facts,
        actions=tuple(f"-> {a}" for a in vp.actions),
        data=vp,
    )


def build_preview(plan):
    """The plan as a :class:`~rlsbl.preview_apply.Preview`.

    Unexplained tags come FIRST, because they are what refuses the apply: an
    operator reading the plan top-down sees the blocker before the work.
    """
    items = [_unexplained_item(e, plan) for e in plan.unexplained]
    # A settled version with nothing to say is not rendered: a repository with
    # two hundred healthy archives would otherwise bury its handful of findings
    # under two hundred lines saying "nothing to do".
    items.extend(
        _version_item(v) for v in plan.versions
        if v.changed or v.notes
    )
    return Preview(tuple(items))


def undecidable_reasons(plan):
    """Every part of a scope's ref set that could not be derived, in order.

    Empty for a repository whose members all resolve, which is the normal
    state. When it is not empty, a tag reported unexplained may be one of the
    spellings the underivable part would have accounted for, so the refusal
    repeats these reasons.
    """
    reasons = []
    for scope in plan.scopes:
        for reason in scope.undecidable:
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def unexplained_error(plan):
    """The message an unexplained tag refuses the whole apply with."""
    lines = [
        f"Refusing to backfill: {len(plan.unexplained)} tag(s) in this "
        f"repository's namespace are not accounted for.",
        "",
    ]
    for entry in plan.unexplained:
        lines.append(f"  {entry.tag}")
        lines.append(_resolutions(entry, plan))
        lines.append("")
    for reason in undecidable_reasons(plan):
        lines.append(
            f"  NOTE {reason}\n"
            f"       A tag above may be one of the spellings that part would "
            f"have accounted for."
        )
        lines.append("")
    lines.append(
        "NOTHING has been written -- not the unexplained tags and not the "
        "archives the plan above would repair. A backfill that wrote around an "
        "unexplained tag would record a release history it knows to be "
        "incomplete."
    )
    return "\n".join(lines)


def stash_error(plan):
    """The message a present stash refuses the apply with."""
    listed = "\n".join(f"    {line}" for line in plan.stash)
    return (
        f"Refusing to backfill: this repository has {len(plan.stash)} stash "
        f"entry/entries.\n{listed}\n"
        f"  The pass unlocks, rewrites and relocks archived release files, and "
        f"commits them.\n"
        f"  A stash is uncommitted work with no branch of its own, and nothing "
        f"here can tell\n"
        f"  what it belongs to. Drop it first -- inspect it with `git stash "
        f"show -p`, land it\n"
        f"  where it belongs, then `git stash drop` -- and re-run."
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _completion_comment(name, source):
    return f"{name} reconstructed by `rlsbl release backfill` from {source}"


def apply_version(vp):
    """Write one version's archive. Returns the path written."""
    if not vp.archive_exists:
        write_archived_release_file(
            vp.scope.releases_dir,
            vp.version,
            bump=vp.bump,
            include=vp.include,
            exclude=[],
            description=vp.description,
            context=vp.context,
            candidate_sha=None if vp.unrecoverable else vp.candidate_sha,
            tree_hashes=None if vp.unrecoverable else vp.tree_hashes,
            unrecoverable=vp.unrecoverable,
            shipped_as=vp.shipped_as,
            # The chain is enumerated in the header; the last line says which
            # link of it actually answered for THIS file, so a reader of the
            # 0444 archive never has to guess.
            header_comments=MATERIALIZED_HEADER + [
                f"This archive's description came from: {vp.description_source}.",
            ],
        )
        return vp.archive_path

    with writable_release_file(vp.archive_path):
        if vp.stamp_format_version:
            with open(vp.archive_path, "r", encoding="utf-8") as f:
                content = f.read()
            effects.atomic_write_text(
                vp.archive_path, FORMAT_VERSION_STAMP + content,
            )
        if vp.fill_fields:
            with open(vp.archive_path, "r", encoding="utf-8") as f:
                doc = tomlkit.loads(f.read())
            for name, (value, source) in vp.fill_fields.items():
                if name in doc:
                    doc[name] = value
                    continue
                doc.add(tomlkit.comment(_completion_comment(name, source)))
                doc.add(name, value)
            effects.atomic_write_text(vp.archive_path, tomlkit.dumps(doc))
        if vp.unrecoverable:
            write_unrecoverable_marker(vp.archive_path)
        elif vp.candidate_sha:
            write_release_commit(
                vp.archive_path,
                candidate_sha=vp.candidate_sha,
                tree_hashes=vp.tree_hashes,
            )
    return vp.archive_path


def apply_item(item):
    """Perform one preview item. Only version items have anything to do."""
    if not isinstance(item.data, VersionPlan):
        return None
    if not item.data.changed:
        return None
    return apply_version(item.data)


def apply_plan(plan):
    """Write every planned archive. Returns the repo-relative paths written."""
    written = []
    for vp in plan.changed_versions:
        written.append(os.path.relpath(apply_version(vp), plan.repo))
    return written


def commit_message(plan, written):
    """One commit per run, naming the repo-relative scope it touched."""
    dirs = sorted({os.path.dirname(p) for p in written})
    return (
        f"Backfill release archives in {', '.join(dirs)} "
        f"({len(written)} archive(s))"
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def render(plan, out=None):
    """Print the plan, unexplained tags first, then the per-version verdicts."""
    stream = sys.stdout if out is None else out
    print(f"Repository: {plan.repo}", file=stream)
    for scope in plan.scopes:
        print(
            f"  scope {scope.label}: "
            f"releases={os.path.relpath(scope.releases_dir, plan.repo)} "
            f"changes={os.path.relpath(scope.changes_dir, plan.repo)} "
            f"released_paths={scope.released_paths} "
            f"tag_spellings={list(scope.tag_spellings())}",
            file=stream,
        )
    for reason in undecidable_reasons(plan):
        print(f"  NOTE {reason}", file=stream)
    print("", file=stream)
    render_preview(build_preview(plan), show_keys=True, out=stream)
    if plan.outside_the_model:
        print(
            f"\nTags recorded outside the version model (accounted for, "
            f"nothing owed): {', '.join(plan.outside_the_model)}",
            file=stream,
        )
    changed = plan.changed_versions
    print(
        f"\nTOTAL: {len(changed)} archive(s) to write, "
        f"{len(plan.unexplained)} unexplained tag(s).",
        file=stream,
    )


def run(repo, *, dry_run, use_gh=True, auto_commit=True, out=None, gh=None,
        overrides=None):
    """Observe, then render the plan or perform it. Returns an exit status.

    0 when the repository is (or has been brought) fully accounted for, 1 when
    unexplained tags remain. Raises :class:`BackfillError` for a condition that
    stops the pass outright.

    This is the entry ``scripts/migrate_workspace_model.py`` calls in-process
    after it rewrites a workspace, so the two halves share one output stream,
    one exit status and one dry-run decision.
    """
    stream = sys.stdout if out is None else out
    plan = build_plan(repo, use_gh=use_gh, gh=gh, overrides=overrides)
    render(plan, out=stream)

    if dry_run:
        print("\n--dry-run: nothing written.", file=stream)
        return 1 if plan.unexplained else 0

    if plan.unexplained:
        raise BackfillError(unexplained_error(plan))
    if plan.stash:
        raise BackfillError(stash_error(plan))

    if not plan.changed_versions:
        print(
            "\nNothing to do: every archive records a fate and carries every "
            "required field.",
            file=stream,
        )
        return 0

    written = apply_plan(plan)
    print(f"\nWrote {len(written)} archive(s).", file=stream)
    if auto_commit:
        commit_files(
            commit_message(plan, written), written, autogenerated=True, cwd=repo,
        )
    return 0

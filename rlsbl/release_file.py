"""Release file reader and validator for file-based releases, parsing .rlsbl/releases/unreleased.toml for bump type and release metadata.

Instead of passing bump type on the CLI, the user creates
.rlsbl/releases/unreleased.toml describing the release.
This module reads and validates that file's internal consistency.
"""

import contextlib
import os
import re
import sys
from dataclasses import dataclass, field

import tomlkit

from .errors import ReleaseFileError
from . import effects


VALID_BUMP_TYPES = ("patch", "minor", "major", "infra", "prerelease")

VALID_PREIDS = ("alpha", "beta", "rc", "stable")

VALID_TARGET_MODES = ("ota", "build")

# The anchor: the fields the release flow itself writes into the ARCHIVED
# release file (v{X.Y.Z}.toml) at the archive step, recording which commit and
# which tree the version shipped from. Authored by the flow and by nothing
# else -- the editable unreleased.toml carrying one is refused at release
# validation, and `release undo` strips them when it restores an archive as the
# editable file. This tuple is the one place the field names are stated; the
# refusal, the strip and the writer all read it.
ANCHOR_FIELDS = ("candidate_sha", "tree_hashes")

# The permanent record that anchoring a version FAILED. An archive carries
# either the anchor or this marker, never both and never neither: a version
# whose commit could not be recovered (no tag under any recognized scheme, no
# version-bump commit in history) says so in its own file rather than being
# silently skipped. Written by the backfill pass, never by the release flow --
# the flow always knows its own candidate.
UNANCHORABLE_FIELD = "unanchorable"

# A git object name (commit or tree). The same shape the schema's
# GitObjectHash refinement states -- and the same the lineage schema's GitSha
# states -- restated here because the writer refuses a malformed anchor BEFORE
# it reaches the file, rather than producing an archive the reader will later
# reject.
_GIT_OBJECT_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")

# The archive filename: ``v{X.Y.Z}.toml``, optionally with a pre-release
# suffix. Strict, and anchored on both ends, so nothing else that happens to
# live in a releases directory (``unreleased.toml``, ``in-progress.json``, a
# batch plan sidecar) can be mistaken for a released version.
#
# The grammar is rlsbl's OWN version vocabulary, not semver at large, and the
# preid alternation is derived from :data:`VALID_PREIDS` rather than restated:
# the release flow is the only thing that names a version, it produces
# ``X.Y.Z`` or ``X.Y.Z-{preid}.{N}``, and ``"stable"`` is the promotion
# instruction that STRIPS the suffix rather than a suffix itself. The backfill
# pass materializes archives only for versions it discovered from an existing
# archive or a finalized changelog file, both of which carry the same
# vocabulary -- so a file named outside it was never written by rlsbl and is
# not an archive here, in the backfill, or in the changelog file lister.
_SUFFIX_PREIDS = tuple(preid for preid in VALID_PREIDS if preid != "stable")
_ARCHIVE_NAME_RE = re.compile(
    r"^v(\d+)\.(\d+)\.(\d+)(?:-("
    + "|".join(_SUFFIX_PREIDS)
    + r")\.(\d+))?\.toml$"
)

# Pre-release channel ranks, for ordering archives. A stable version sorts
# after every pre-release of the same base -- the same ordering the changelog
# file lister uses -- and within a channel the counter is compared as a NUMBER,
# so alpha.10 follows alpha.2.
_PREID_RANK = {preid: rank for rank, preid in enumerate(_SUFFIX_PREIDS)}


def archived_release_path(releases_dir: str, version: str) -> str:
    """Return the archive path for *version* -- ``<releases_dir>/v{version}.toml``.

    The one place the archive filename is spelled; the writer, the reader and
    the ledger all go through it.
    """
    return os.path.join(releases_dir, f"v{version}.toml")


def archive_version(name: str) -> str | None:
    """The version *name* archives, or None when *name* is not an archive name.

    The one recognizer for "is this file in a releases directory an archive?".
    Exported because the backfill pass reads the same directories and must
    agree with the ledger about which files in them are archives -- it used to
    carry a looser pattern of its own, so it discovered, sorted and repaired
    "archives" the ledger then ignored entirely.
    """
    if _ARCHIVE_NAME_RE.match(name) is None:
        return None
    return name[1:-len(".toml")]


def is_release_version(version: str) -> bool:
    """True when *version* is a version rlsbl's release flow could have named.

    The version-level half of :func:`archive_version`, for callers holding a
    version string rather than an archive filename -- the JSONL changelog file
    lister, whose files are named ``{version}.jsonl`` by the same flow. It is
    what makes :func:`archive_sort_key` askable without catching its refusal:
    check first, then order.
    """
    return _ARCHIVE_NAME_RE.match(f"v{version}.toml") is not None


def archive_sort_key(version: str):
    """Ascending order key for an archived *version*.

    The one ordering: by numeric ``major.minor.patch``, then every pre-release
    of that base before the base itself, then by channel (alpha, beta, rc) and
    finally by the counter compared as a number.

    Raises ``ValueError`` for a string that is not an archivable version. A
    caller sorts versions it discovered through :func:`archive_version` or the
    changelog file lister, both of which speak this same vocabulary, so an
    unparsable version there is a bug to surface rather than an ordering to
    guess at.
    """
    m = _ARCHIVE_NAME_RE.match(f"v{version}.toml")
    if m is None:
        raise ValueError(f"not an archivable version: {version!r}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    preid = m.group(4)
    if preid is None:
        return (major, minor, patch, 1, 0, 0)
    return (major, minor, patch, 0, _PREID_RANK[preid], int(m.group(5)))


def list_archived_versions(releases_dir: str) -> list[str]:
    """List the versions archived in *releases_dir*, HIGHEST FIRST.

    A pure filename scan: no archive is opened, so enumerating a repository's
    whole release history costs one ``listdir``. This is deliberate -- the
    ledger reads archives lazily, walking this list from the top and opening
    only the ones it actually has to answer with.

    A missing or unreadable directory yields an empty list: "no releases are
    recorded here" is a real state (a project before its first release), not
    an error. Errors belong to the callers that READ an entry, not to the scan.
    """
    try:
        names = os.listdir(releases_dir)
    except OSError:
        return []
    versions = [v for v in (archive_version(name) for name in names) if v is not None]
    versions.sort(key=archive_sort_key, reverse=True)
    return versions


# Project identification: "slug" is the machine identifier (URL-safe, lowercase,
# hyphens) used in config keys and paths; "name" is the human-readable display
# name used in UI and documentation.
@dataclass
class ReleaseConfig:
    bump: str  # "patch", "minor", "major", "infra", "prerelease"
    include: list[str]  # target names to release
    exclude: list[str]  # target names to skip
    targets: dict[str, dict] = field(default_factory=dict)  # per-target config
    description: str = ""  # short description of this release
    context: str = ""  # optional context explaining why these changes were made
    preid: str = ""  # pre-release identifier: "alpha", "beta", "rc", or "stable"
    blog: bool = False
    # --- the anchor (archived files only; None means the field is ABSENT) ---
    # Absence is None rather than "" / {}: a release file that carries no
    # anchor and one that carries an empty anchor are different documents, and
    # only the second is a hand-authored anchor the flow must refuse.
    candidate_sha: str | None = None  # the commit CI verified for this version
    tree_hashes: dict[str, str] | None = None  # released path -> git tree object
    # True on an archive whose commit could not be recovered at all. Absence is
    # None, not False: "never asked" and "asked and failed" are different facts.
    unanchorable: bool | None = None


def get_releases_dir(project_dir: str = ".", *, releasable_dir: str | None = None) -> str:
    """Return the directory holding release files (unreleased.toml family).

    ``releasable_dir`` is the releasable's state directory
    (``.rlsbl-monorepo/releasables/<name>/``); when given, release files
    live in its ``releases/`` subdirectory — the same home as
    in-progress.json/scrub-result.json — instead of the
    project's ``.rlsbl/releases/``. This is the single derivation for the
    releases dir; the release state module delegates here.
    """
    if releasable_dir:
        return os.path.join(releasable_dir, "releases")
    return os.path.join(project_dir, ".rlsbl", "releases")


def get_release_file_path(project_dir: str = ".", *, releasable_dir: str | None = None) -> str:
    """Return the path to the release file (unreleased.toml).

    Standalone projects: ``<project_dir>/.rlsbl/releases/unreleased.toml``.
    Releasable releases: pass ``releasable_dir`` — the file is at
    ``.rlsbl-monorepo/releasables/<name>/releases/unreleased.toml``.
    """
    return os.path.join(
        get_releases_dir(project_dir, releasable_dir=releasable_dir),
        "unreleased.toml",
    )


def _field_is_blank(value) -> bool:
    """True if a release-file field is absent or an empty/whitespace string.

    The scaffolder writes ``bump = ""`` and ``description = ""``; an operator
    "fills in" the file by setting a real value. Anything non-string and
    non-None (e.g. a number) counts as filled.
    """
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


def is_pristine_release_file(content: str) -> bool:
    """True if ``content`` is a still-pristine single-project release scaffold.

    Pristine means the operator has not filled in the release: either the file
    is empty/whitespace-only, or it parses as TOML with a blank ``bump`` and a
    blank ``description``. Any filled bump/description -- or content that fails
    to parse as TOML -- is treated as operator data and reported non-pristine
    so ``release init`` refuses to clobber it.
    """
    if content.strip() == "":
        return True
    try:
        data = tomlkit.loads(content)
    except Exception:
        return False
    return _field_is_blank(data.get("bump")) and _field_is_blank(data.get("description"))


def is_pristine_batch_release_file(content: str) -> bool:
    """True if ``content`` is a still-pristine batch (monorepo) release scaffold.

    Pristine means every ``[packages.<name>]`` / ``[releasables.<name>]``
    section has a blank ``bump`` and blank ``description`` (the scaffold state).
    Empty/whitespace-only content is pristine. Any filled section, a
    non-table section entry, or unparseable content is non-pristine so
    ``monorepo release init`` refuses to clobber it.
    """
    if content.strip() == "":
        return True
    try:
        data = tomlkit.loads(content)
    except Exception:
        return False
    for section_key in ("packages", "releasables"):
        section = data.get(section_key)
        if not isinstance(section, dict):
            continue
        for _name, entry in section.items():
            if not isinstance(entry, dict):
                return False
            if not (_field_is_blank(entry.get("bump")) and _field_is_blank(entry.get("description"))):
                return False
    return True


def check_legacy_release_file(project_dir: str, releasable_dir: str | None) -> None:
    """Hard-error if a release file sits at the legacy member location.

    Releasable release files used to live under the representative
    member's ``.rlsbl/releases/``. A file found there in releasable mode
    must never be silently ignored (it would be skipped by the relocated
    read path and left behind as per-package residue).

    Raises ReleaseFileError with a migration hint. No-op when
    ``releasable_dir`` is None (a standalone project).
    """
    if not releasable_dir:
        return
    legacy_path = get_release_file_path(project_dir)
    if os.path.isfile(legacy_path):
        raise ReleaseFileError(
            f"found a release file at the legacy location {legacy_path}. "
            f"Releasable release files now live at "
            f"{get_release_file_path(project_dir, releasable_dir=releasable_dir)}. "
            f"Move the file to the new location (or delete it if stale) and "
            f"re-run."
        )


def _validate_release_config(data: dict, prefix: str = "") -> ReleaseConfig:
    """Validate release config fields from a parsed TOML dict.

    Shared validation for both single-project and batch (per-package) release
    configs. The prefix is prepended to all error messages -- empty string for
    single-project, "[packages.<name>] " for batch.

    Raises ReleaseFileError for schema/validation failures.
    Returns a ReleaseConfig on success.
    """
    def err(msg: str) -> ReleaseFileError:
        return ReleaseFileError(f"{prefix}{msg}")

    # --- bump ---
    if "bump" not in data:
        raise err("missing required field: bump")
    bump = data["bump"]
    if not isinstance(bump, str) or bump not in VALID_BUMP_TYPES:
        raise err(
            f"bump must be set to a valid value: invalid bump {bump!r} "
            f"(must be one of {VALID_BUMP_TYPES})"
        )

    # --- include ---
    if "include" not in data:
        raise err("missing required field: include")
    include = data["include"]
    if not isinstance(include, list) or not all(isinstance(s, str) for s in include):
        raise err("include must be a list of strings")

    # --- exclude ---
    if "exclude" not in data:
        raise err("missing required field: exclude")
    exclude = data["exclude"]
    if not isinstance(exclude, list) or not all(isinstance(s, str) for s in exclude):
        raise err("exclude must be a list of strings")

    # --- include ∩ exclude must be empty ---
    overlap = set(include) & set(exclude)
    if overlap:
        raise err(
            f"targets appear in both include and exclude: {sorted(overlap)}"
        )

    # --- targets section ---
    targets_raw = data.get("targets", {})
    targets = {}
    if targets_raw:
        if not isinstance(targets_raw, dict):
            raise err("targets must be a table of per-target configurations")
        include_set = set(include)
        for name, cfg in targets_raw.items():
            if name not in include_set:
                raise err(
                    f"target config for {name!r} but it is not in include"
                )
            if not isinstance(cfg, dict):
                raise err(f"target config for {name!r} must be a table")
            # Validate known fields
            for key, value in cfg.items():
                if key == "mode":
                    if value not in VALID_TARGET_MODES:
                        raise err(
                            f"invalid mode for target {name!r}: {value!r} "
                            f"(must be one of {VALID_TARGET_MODES})"
                        )
                else:
                    raise err(
                        f"unknown field {key!r} in target config for {name!r}"
                    )
            targets[name] = dict(cfg)

    # Flutter target requires a mode field in its per-target config
    for name in include:
        if name == "flutter":
            if name not in targets or "mode" not in targets[name]:
                raise err(
                    f"Flutter target {name!r} requires a [targets.{name}] section "
                    f"with mode = \"ota\" or mode = \"build\""
                )

    # --- description (required) ---
    if "description" not in data:
        raise err("missing required field: description")
    description = data["description"]
    if not isinstance(description, str):
        raise err("description must be a string")
    if not description.strip():
        raise err("description must be set (a short summary of this release)")

    # --- context (optional) ---
    context = data.get("context", "")
    if not isinstance(context, str):
        raise err("context must be a string")

    # --- preid (optional) ---
    preid = data.get("preid", "")
    if not isinstance(preid, str):
        raise err("preid must be a string")
    preid = preid.strip()
    if preid:
        if preid not in VALID_PREIDS:
            raise err(
                f"invalid preid {preid!r} "
                f"(must be one of {VALID_PREIDS} or empty)"
            )
        if bump == "infra":
            raise err("infra releases cannot use preid (infra releases cannot be pre-releases)")
        if preid == "stable" and bump != "prerelease":
            raise err(
                f'preid "stable" is only valid with bump = "prerelease" '
                f'(got bump = {bump!r})'
            )

    # --- blog (optional) ---
    blog = data.get("blog", False)
    if not isinstance(blog, bool):
        raise err("blog must be a boolean")

    return ReleaseConfig(
        bump=bump,
        include=list(include),
        exclude=list(exclude),
        targets=targets,
        description=description.strip(),
        context=context.strip(),
        preid=preid,
        blog=blog,
    )


def _render_release_diags(diags) -> str:
    """Render strictspec diagnostics in rlsbl's release-file error style.

    Each diagnostic contributes its rendered path, message, and stable code so
    the operator sees exactly which field failed and why.
    """
    lines = ["release file failed strictspec validation:"]
    for d in diags:
        lines.append(f"  {d.path}: {d.message}  [{d.code}]")
    return "\n".join(lines)


def _strictspec_validate_release_document(raw: bytes) -> None:
    """Validate the raw release-file document shape via the generated validator.

    strictspec owns the DOCUMENT SHAPE: the ``format_version`` gate, field
    types, the ``bump``/``preid``/``mode`` enums, required fields, unknown-key
    rejection, include/exclude disjointness, the ``[targets.<name>]`` ⊆
    ``include`` reference, and the preid/bump couplings. Raises
    ``ReleaseFileError`` (rlsbl's native error style) when any diagnostic fires.

    Consumer-native refinements that strictspec cannot express (whitespace-only
    ``description``, the Flutter required-``mode`` gate) stay in
    :func:`_bind_release_config`. There is no dual validation: any property
    strictspec owns is not re-checked natively on this path.
    """
    from .strictspec_gen import release_file_validator as _rfv

    _root, diags = _rfv.validate_bytes(raw, "toml")
    if diags:
        raise ReleaseFileError(_render_release_diags(diags))


def _bind_release_config(data: dict) -> ReleaseConfig:
    """Build a ReleaseConfig from a shape-validated release document.

    Assumes :func:`_strictspec_validate_release_document` already validated the
    document shape, so this applies only the consumer-native refinements and
    the field normalization (``.strip()``) before constructing the dataclass.
    """
    bump = data["bump"]
    include = list(data["include"])
    exclude = list(data["exclude"])
    targets = {name: dict(cfg) for name, cfg in data.get("targets", {}).items()}

    # Native refinement: the Flutter target requires a mode field (an
    # array-contains-literal gate strictspec does not express).
    for name in include:
        if name == "flutter" and (name not in targets or "mode" not in targets[name]):
            raise ReleaseFileError(
                f"Flutter target {name!r} requires a [targets.{name}] section "
                f'with mode = "ota" or mode = "build"'
            )

    description = data["description"]
    # Native refinement: whitespace-only description (non_empty passes "   ").
    if not description.strip():
        raise ReleaseFileError(
            "description must be set (a short summary of this release)"
        )

    context = data.get("context", "")

    # Native preid validation: strictspec owns only the string type. Membership,
    # the empty-string-means-unset semantics, and the infra/stable couplings are
    # consumer-native because rlsbl treats preid = "" / whitespace as UNSET,
    # which a strictspec enum + forbidden-when cannot express.
    preid = data.get("preid", "").strip()
    if preid:
        if preid not in VALID_PREIDS:
            raise ReleaseFileError(
                f"invalid preid {preid!r} "
                f"(must be one of {VALID_PREIDS} or empty)"
            )
        if bump == "infra":
            raise ReleaseFileError(
                "infra releases cannot use preid (infra releases cannot be pre-releases)"
            )
        if preid == "stable" and bump != "prerelease":
            raise ReleaseFileError(
                f'preid "stable" is only valid with bump = "prerelease" '
                f'(got bump = {bump!r})'
            )

    blog = data.get("blog", False)

    # The anchor: bound only when the document actually carries it. `in` rather
    # than `.get(..., default)` so an absent field arrives as None and an empty
    # one (candidate_sha = "", [tree_hashes] with no entries) arrives as itself
    # -- the refusal at release validation must be able to tell them apart.
    candidate_sha = data["candidate_sha"] if "candidate_sha" in data else None
    if "tree_hashes" in data:
        tree_hashes = {str(k): str(v) for k, v in data["tree_hashes"].items()}
    else:
        tree_hashes = None
    unanchorable = data[UNANCHORABLE_FIELD] if UNANCHORABLE_FIELD in data else None

    return ReleaseConfig(
        bump=bump,
        include=include,
        exclude=exclude,
        targets=targets,
        description=description.strip(),
        context=context.strip(),
        preid=preid,
        blog=blog,
        candidate_sha=candidate_sha,
        tree_hashes=tree_hashes,
        unanchorable=unanchorable,
    )


@contextlib.contextmanager
def _errors_name(path: str):
    """Name *path* in every release-file error raised inside the block.

    A diagnostic that says which FIELD failed but not which FILE is
    unactionable: a workspace holds one editable release file per releasable
    plus one archive per released version, and the reader always has the path
    in hand. The prefix is applied once, at the read boundary, so no raising
    site has to repeat it and no message carries it twice.
    """
    try:
        yield
    except ReleaseFileError as exc:
        raise ReleaseFileError(f"{path}: {exc}") from exc


def read_release_file(path: str) -> ReleaseConfig:
    """Read and validate a single-project release TOML file.

    The raw document shape is validated by the strictspec-generated validator
    (which requires a ``format_version`` gate) BEFORE tomlkit parsing;
    consumer-native refinements and dataclass construction happen after. Batch
    (monorepo) release files keep the native :func:`_validate_release_config`
    path -- their document shape is different and not yet strictspec-modeled.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError, naming *path*, for schema/validation failures.
    """
    with open(path, "rb") as f:
        raw = f.read()

    with _errors_name(path):
        _strictspec_validate_release_document(raw)
        data = tomlkit.loads(raw.decode("utf-8"))
        return _bind_release_config(data)


def write_archived_release_file(
    releases_dir: str,
    version: str,
    *,
    bump: str,
    include,
    exclude=(),
    description: str,
    context: str = "",
    preid: str = "",
    blog: bool = False,
    candidate_sha: str | None,
    tree_hashes: dict | None,
    unanchorable: bool = False,
    header_comments=None,
) -> str:
    """Write ``v{version}.toml`` for a release that had no ``unreleased.toml``.

    A standalone release finalizes by RENAMING its release file to
    ``v{version}.toml``, and every later changelog regeneration reads the
    version's description, context and bump type back out of that archive.

    A batch release has no per-member release file -- its members' metadata
    lives in the workspace-level batch TOML, archived under a different name --
    so nothing was ever written here, and regeneration silently stripped the
    description and context from the version's ``.md`` and its ``CHANGELOG.md``
    section.  Materializing the archive puts the metadata exactly where every
    reader already looks, rather than teaching each reader a second source.

    The result is a complete, schema-valid release document (``read_release_file``
    accepts it, which matters because ``rlsbl release undo`` restores it as
    ``unreleased.toml``), and read-only like every other archived release file.

    ``candidate_sha`` and ``tree_hashes`` are the anchor: an archive that does
    not say which commit and tree the version shipped from is not a record of
    the release, so they are written with the file and the archive is anchored
    from the instant it exists. The ONE way to write an unanchored archive is
    ``unanchorable=True`` with both anchor arguments ``None`` -- the backfill
    case where a version's commit could not be recovered from any source; the
    file then carries the permanent ``unanchorable = true`` record instead. An
    archive is never both anchored and unanchorable, and never neither.

    ``header_comments`` replaces the leading comment block (one list element per
    comment line) for a writer other than the release flow -- the backfill pass
    states there that the file was materialized after the fact.

    Returns the path written.
    """
    if unanchorable:
        if candidate_sha is not None or tree_hashes is not None:
            raise ReleaseFileError(
                "an unanchorable archive carries no anchor: pass "
                "candidate_sha=None and tree_hashes=None with unanchorable=True"
            )
    else:
        _check_anchor(candidate_sha, tree_hashes)
    doc = tomlkit.document()
    if header_comments is None:
        header_comments = [
            "Archived by rlsbl at release time. This release had no unreleased.toml",
            "(a batch member takes its metadata from the batch release file); the",
            "archive is written so later changelog regenerations keep the",
            "description and context.",
        ]
    for line in header_comments:
        doc.add(tomlkit.comment(line))
    doc.add(tomlkit.comment("strictspec document version gate (do not remove)"))
    doc.add("format_version", 1)
    doc.add("bump", bump)
    doc.add("include", list(include))
    doc.add("exclude", list(exclude))
    doc.add("description", description)
    if context:
        doc.add("context", context)
    if preid:
        doc.add("preid", preid)
    if blog:
        doc.add("blog", True)
    if unanchorable:
        doc.add(UNANCHORABLE_FIELD, True)
    else:
        doc.add("candidate_sha", candidate_sha)
        doc.add("tree_hashes", _anchor_tree_table(tree_hashes))

    effects.makedirs(releases_dir, exist_ok=True)
    path = archived_release_path(releases_dir, version)
    # file_mode, not a write-then-chmod: the archive is immutable from the
    # instant it exists, exactly like the renamed-and-chmodded standalone one.
    effects.atomic_write_text(path, tomlkit.dumps(doc), file_mode=0o444)
    return path


def _check_anchor(candidate_sha: str, tree_hashes: dict) -> None:
    """Refuse a malformed anchor before it reaches a file.

    The schema rejects one on the way back IN; this rejects it on the way OUT,
    so a release never produces a read-only archive its own reader refuses.
    """
    if not isinstance(candidate_sha, str) or not _GIT_OBJECT_HASH_RE.match(candidate_sha):
        raise ReleaseFileError(
            f"release anchor candidate_sha must be a git commit hash "
            f"(7 to 40 hex characters), got {candidate_sha!r}"
        )
    if not isinstance(tree_hashes, dict) or not tree_hashes:
        raise ReleaseFileError(
            "release anchor tree_hashes must name at least one released path "
            "(\".\" for a standalone repository, one entry per member "
            f"directory for a releasable), got {tree_hashes!r}"
        )
    for path, tree in tree_hashes.items():
        if not isinstance(tree, str) or not _GIT_OBJECT_HASH_RE.match(tree):
            raise ReleaseFileError(
                f"release anchor tree_hashes[{path!r}] must be a git tree hash "
                f"(7 to 40 hex characters), got {tree!r}"
            )


def _anchor_tree_table(tree_hashes: dict):
    """Render the tree-hash map as a TOML table with quoted path keys."""
    table = tomlkit.table()
    for path in sorted(tree_hashes):
        table.add(tomlkit.key(path), tree_hashes[path])
    return table


def write_release_anchor(path: str, *, candidate_sha: str, tree_hashes: dict) -> None:
    """Author the release anchor into an already-written release file.

    Used on the finalization path, where the archive is the operator's own
    ``unreleased.toml`` renamed to ``v{version}.toml``: the anchor is added
    after the rename and BEFORE the file is chmodded read-only, so the archive
    is never observable as a writable anchored file, and never as a locked
    unanchored one.

    The document is otherwise preserved as written -- tomlkit round-trips the
    operator's comments, ordering and formatting -- so the archive still reads
    as the file the operator authored, plus the two fields the flow owns.
    """
    _check_anchor(candidate_sha, tree_hashes)
    with open(path, "r", encoding="utf-8") as f:
        doc = tomlkit.loads(f.read())
    for f_name in ANCHOR_FIELDS:
        if f_name in doc:
            del doc[f_name]
    # No explanatory comment block: tomlkit appends comments after the last
    # element, which in a file carrying a [targets.<name>] section reads as a
    # comment ON that section. The anchor's meaning is stated where it belongs
    # -- the schema field descriptions and docs/release-workflow.md.
    doc.add("candidate_sha", candidate_sha)
    doc.add("tree_hashes", _anchor_tree_table(tree_hashes))
    effects.atomic_write_text(path, tomlkit.dumps(doc))


@contextlib.contextmanager
def writable_release_file(path: str):
    """Temporarily clear the read-only bit on an archived release file.

    Archives are chmodded 0o444 the instant they exist, so any later edit --
    the backfill pass that anchors a version released before anchoring existed,
    or stamps the strictspec gate onto one written before the gate existed --
    has to unlock, write, and relock. Restores the original permissions on the
    way out even when the body raises, and is a no-op on an already-writable
    file (the editable ``unreleased.toml`` takes the same path without being
    locked behind it).
    """
    from .changelog.files import is_read_only

    was_ro = is_read_only(path)
    if was_ro:
        effects.chmod(path, 0o644)
    try:
        yield path
    finally:
        if was_ro:
            effects.chmod(path, 0o444)


def write_unanchorable_marker(path: str) -> None:
    """Record on an already-written archive that its commit is unrecoverable.

    The counterpart to :func:`write_release_anchor` for the backfill pass: a
    released version with no tag under any recognized scheme and no version-bump
    commit in history cannot be anchored, and the archive says so permanently
    rather than being passed over in silence. Refuses an archive that already
    carries an anchor -- an anchored version is by definition not unanchorable.
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = tomlkit.loads(f.read())
    present = [name for name in ANCHOR_FIELDS if name in doc]
    if present:
        raise ReleaseFileError(
            f"refusing to mark {path} unanchorable: it already carries "
            f"{', '.join(present)}"
        )
    doc[UNANCHORABLE_FIELD] = True
    effects.atomic_write_text(path, tomlkit.dumps(doc))


def strip_release_anchor(path: str) -> bool:
    """Remove the anchor fields from a release file. True if anything changed.

    The inverse of :func:`write_release_anchor`, for ``release undo``: the
    archive it restores as ``unreleased.toml`` must come back as an EDITABLE
    release file, and an editable file carrying an anchor is refused at the
    next release validation (the anchor is the flow's to author, never the
    operator's). The ``unanchorable`` marker goes with them: it is a statement
    about a version that already shipped, meaningless on a file describing the
    next one.
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = tomlkit.loads(f.read())
    present = [name for name in (*ANCHOR_FIELDS, UNANCHORABLE_FIELD) if name in doc]
    if not present:
        return False
    for name in present:
        del doc[name]
    effects.atomic_write_text(path, tomlkit.dumps(doc))
    return True


def unfinalize_release_file(releases_dir: str, version: str) -> list[str]:
    """Reverse a release-file finalization: restore vX.Y.Z.toml to unreleased.toml.

    Inverse of the finalization step in `release run`, which renames
    unreleased.toml to vX.Y.Z.toml and chmods it read-only (0o444).

    1. No-op (returns []) if the versioned file doesn't exist.
    2. If unreleased.toml exists with content that differs from the versioned
       file, warns on stderr and skips -- nothing is deleted.
    3. Otherwise removes any stale unreleased.toml, makes the versioned file
       writable, renames it back to unreleased.toml, and STRIPS the anchor the
       release wrote into it. The restored file is an editable pre-release file
       again, and one carrying an anchor is refused at the next release
       validation -- so leaving the anchor on would block the re-release of the
       very version the undo just freed.

    Returns the list of changed file paths (for committing).
    """
    versioned = os.path.join(releases_dir, f"v{version}.toml")
    unreleased = os.path.join(releases_dir, "unreleased.toml")

    if not os.path.isfile(versioned):
        return []

    if os.path.isfile(unreleased):
        with open(unreleased, "r", encoding="utf-8") as f:
            unreleased_content = f.read()
        if unreleased_content != "":
            with open(versioned, "r", encoding="utf-8") as f:
                versioned_content = f.read()
            if unreleased_content != versioned_content:
                print(
                    f"warning: {unreleased} has user content that differs "
                    f"from {versioned}; leaving both files in place. Restore "
                    f"the release file manually if needed.",
                    file=sys.stderr,
                )
                return []
        effects.remove(unreleased)

    effects.chmod(versioned, 0o644)
    effects.rename(versioned, unreleased)
    strip_release_anchor(unreleased)
    changed = [unreleased, versioned]

    # Also reverse blog body file archival if present
    versioned_md = os.path.join(releases_dir, f"v{version}.md")
    unreleased_md = os.path.join(releases_dir, "unreleased.md")
    if os.path.isfile(versioned_md):
        if os.path.isfile(unreleased_md):
            print(
                f"warning: {unreleased_md} already exists; leaving {versioned_md} in place.",
                file=sys.stderr,
            )
        else:
            effects.chmod(versioned_md, 0o644)
            effects.rename(versioned_md, unreleased_md)
            changed.extend([unreleased_md, versioned_md])

    return changed


@dataclass
class BatchReleaseConfig:
    """Configuration from a batch release TOML file (monorepo).

    ``packages`` maps releasable names to their release configs, one entry
    per ``[releasables.<name>]`` section of the batch release file.
    """

    packages: dict[str, ReleaseConfig]  # releasable name -> config


def get_batch_release_file_path(workspace_root: str = ".") -> str:
    """Return the path to .rlsbl-monorepo/releases/unreleased.toml."""
    return os.path.join(workspace_root, ".rlsbl-monorepo", "releases", "unreleased.toml")


def read_batch_release_file(path: str) -> BatchReleaseConfig:
    """Read and validate a batch release TOML file.

    Sections are ``[releasables.<name>]``, one per releasable being released.

    Each section has the same fields as a single ReleaseConfig (bump, include,
    exclude, optional targets, description, context).

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError, naming *path*, for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    with _errors_name(path):
        return _bind_batch_release_config(data)


def _bind_batch_release_config(data) -> BatchReleaseConfig:
    """Validate a parsed batch release document and build its config."""
    if "packages" in data:
        raise ReleaseFileError(
            "batch release file has a [packages] section. A batch release "
            "names releasables, not packages: rewrite the sections as "
            "[releasables.<name>], one per releasable, and re-run "
            "`rlsbl monorepo release init` if you want them scaffolded."
        )

    if "releasables" not in data:
        raise ReleaseFileError("missing required section: [releasables]")

    section_key = "releasables"
    raw = data[section_key]
    if not isinstance(raw, dict):
        raise ReleaseFileError(f"[{section_key}] must be a table of configurations")

    if not raw:
        raise ReleaseFileError(
            f"[{section_key}] is empty -- at least one entry is required"
        )

    entries = {}
    for name, entry_data in raw.items():
        if not isinstance(entry_data, dict):
            raise ReleaseFileError(
                f"[{section_key}.{name}] must be a table"
            )

        entries[name] = _validate_release_config(
            entry_data, prefix=f"[{section_key}.{name}] "
        )

    return BatchReleaseConfig(packages=entries)


# ---------------------------------------------------------------------------
# Retry file
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """Configuration from a retry TOML file (.rlsbl/releases/retry.toml)."""

    version: str  # version to retry (mandatory)
    dispatch: list[str]  # workflow filenames to dispatch, e.g. ["publish.yml"]
    ref: str  # git ref for CI dispatch, defaults to tag
    tag: str  # release tag (e.g. "v1.2.3"), passed as workflow_dispatch input


def discard_invalid_retry_file(retry_path: str) -> None:
    """Delete a retry file that failed to parse.

    An unparseable retry.toml is not recoverable state: leaving it on disk
    dirties the working tree and blocks the next `rlsbl release run`. Lives
    beside the retry-file readers (and out of the command registration module,
    which must stay free of effect calls for the effects-bypass lint).
    """
    if os.path.exists(retry_path):
        effects.remove(retry_path)


def get_retry_file_path(project_dir: str = ".", *, releasable_dir: str | None = None) -> str:
    """Return the path to retry.toml (same releases-dir home as unreleased.toml).

    Releasable releases (explicit monorepo mode): pass ``releasable_dir``
    so the file lives under the releasable's own releases dir instead of
    the member's ``.rlsbl/releases/``.
    """
    return os.path.join(
        get_releases_dir(project_dir, releasable_dir=releasable_dir),
        "retry.toml",
    )


def read_retry_file(path: str) -> RetryConfig:
    """Read and validate a retry TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ReleaseFileError for schema/validation failures.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    # --- version ---
    if "version" not in data:
        raise ReleaseFileError("missing required field: version")
    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise ReleaseFileError("version must be a non-empty string")

    # --- dispatch ---
    if "dispatch" not in data:
        raise ReleaseFileError("missing required field: dispatch")
    dispatch = data["dispatch"]
    if not isinstance(dispatch, list) or not all(isinstance(s, str) for s in dispatch):
        raise ReleaseFileError("dispatch must be a list of strings")
    if not dispatch:
        raise ReleaseFileError("dispatch must be non-empty")

    # --- ref ---
    if "ref" not in data:
        raise ReleaseFileError("missing required field: ref")
    ref = data["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise ReleaseFileError("ref must be set in retry.toml (e.g. a tag like v1.2.3 or a branch like main)")

    # --- tag (optional, defaults to ref) ---
    tag = data.get("tag", "")
    if isinstance(tag, str) and tag.strip():
        tag = tag.strip()
    else:
        tag = ref.strip()  # default: tag = ref

    return RetryConfig(
        version=version.strip(),
        dispatch=list(dispatch),
        ref=ref.strip(),
        tag=tag,
    )

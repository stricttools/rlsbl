#!/usr/bin/env python3
"""Backfill release-archive anchors, format_version gates, and missing archives.

Release archives (``.rlsbl/releases/v{X.Y.Z}.toml``, and the releasable-level
equivalent) are the authoritative record of what a version shipped: the bump
type, the description and context every later changelog regeneration reads back,
and -- since anchoring exists -- the ``candidate_sha`` / ``tree_hashes`` anchor
naming the commit and the released trees. Repositories that predate any of that
carry archives with no anchor, archives with no ``format_version`` gate, and
released versions with no archive at all.

This pass repairs one repository (``--repo``, default: the process cwd), and it
sorts every released version and every git tag into four buckets, all of which
appear in the plan and in the run output:

  (a) anchorable from a tag -- the version's tag resolves under one of the
      repository's recognized tag spellings, and its commit becomes the anchor.
  (b) TAGLESS -- no tag under any spelling. Recovery is attempted from history:
      a commit whose message is the version-bump message (``v1.2.3``) is the
      commit the release built, and anchoring from it is recorded as such. Only
      when that also fails does the archive get the permanent ``unanchorable =
      true`` marker. A version is never silently skipped.
  (c) FOREIGN tags -- a tag that parses under a recognized scheme but matches no
      released version of any scope. The script does not guess what it is: it
      reports the tag, the spellings it probed, and exits non-zero so the
      operator resolves it.
  (d) unrecognizable tags -- listed, untouched, non-fatal.

The pass is idempotent: an archive that already carries an anchor (or the
unanchorable marker) and the format_version gate is proposed for no change, so a
second run plans nothing and commits nothing.

Usage:
    uv run python scripts/backfill_release_anchors.py --dry-run
    uv run python scripts/backfill_release_anchors.py [--repo PATH]

The plan is printed either way; ``--dry-run`` stops before writing anything.
Exit status: 0 when everything is anchored and no foreign tag is present, 1 when
foreign tags need operator input (the rest of the work is still done first).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Import rlsbl from the repository this script ships in, not from the repo being
# backfilled: the writers and the tag parser are this tool's, and the target repo
# may be any rlsbl-managed project.
_SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SCRIPT_ROOT)

from rlsbl.changelog.files import list_versioned_files  # noqa: E402
from rlsbl.release_file import (  # noqa: E402
    ANCHOR_FIELDS,
    UNANCHORABLE_FIELD,
    write_archived_release_file,
    write_release_anchor,
    write_unanchorable_marker,
    writable_release_file,
)
from rlsbl.tag_glob import TagMode, parse_version_tag  # noqa: E402
from rlsbl.utils import commit_files, extract_changelog_entry_from_text  # noqa: E402

# Every subprocess in this script states its own timeout: a backfill that hangs
# on a git or gh call in a repository with an unusual object store is worse than
# one that fails.
GIT_TIMEOUT = 60
GH_TIMEOUT = 30

# Archive filename: v{semver}.toml, prerelease suffix included.
_ARCHIVE_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\.toml$")

# The strictspec gate, stamped verbatim onto archives written before the gate
# existed. Prepended as text rather than through a tomlkit round-trip so every
# other byte of the operator's file is preserved exactly.
FORMAT_VERSION_STAMP = (
    "# strictspec document version gate (do not remove)\nformat_version = 1\n"
)

# The header comment block on an archive this pass materialized. It says the
# file was written after the fact, which the reader of a 0444 archive otherwise
# has no way to know.
MATERIALIZED_HEADER = [
    "Materialized by scripts/backfill_release_anchors.py: this version shipped",
    "before rlsbl archived a release file per version, so no archive existed.",
    "The description below was recovered from the sources named in the run's",
    "plan; bump is derived by version arithmetic against the predecessor, and",
    "include reflects the targets detected at backfill time (the historical",
    "target set is not recoverable).",
]

# What a materialized archive says when no description could be recovered. It
# names the obligation rather than pretending the version had no summary.
PLACEHOLDER_DESCRIPTION = (
    "RECOVERY OBLIGATION: no description was recoverable for this version "
    "(neither the GitHub Release notes nor the CHANGELOG.md section carried "
    "one). Author a real description from this version's changelog entries and "
    "regenerate."
)


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def git(repo: str, args: list[str], *, check: bool = True) -> str:
    """Run a git command in *repo* and return its stdout, stripped."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def rev_parse(repo: str, spec: str) -> str | None:
    """Resolve *spec* to an object hash, or None when it does not resolve."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", spec],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    out = result.stdout.strip()
    return out or None


def all_tags(repo: str) -> list[str]:
    out = git(repo, ["tag", "-l"])
    return sorted(t for t in out.splitlines() if t.strip())


def find_bump_commits(repo: str, version: str) -> list[str]:
    """Commits whose whole message is this version's bump message.

    The release flow commits the version bump with the tag string as the message
    (``v1.2.3``), so that commit IS the release candidate even when the tag that
    should point at it is missing. Both the tagged spelling and the bare version
    are accepted, because older flows wrote either.
    """
    pattern = f"^v?{re.escape(version)}$"
    out = git(
        repo,
        ["log", "--all", "--extended-regexp", f"--grep={pattern}", "--format=%H"],
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Scopes: one per independently-versioned release-state directory
# ---------------------------------------------------------------------------


@dataclass
class Scope:
    """One independently-versioned release-state location in a repository.

    ``released_paths`` are the repo-relative paths whose trees the anchor
    records: ``["."]`` for a standalone repository, one entry per member
    directory for a workspace releasable. ``tag_formats`` are the tag spellings
    this scope's versions may be tagged under, as format strings taking
    ``{version}``.
    """

    label: str
    releases_dir: str  # absolute
    changes_dir: str  # absolute
    changelog_md: str  # absolute
    released_paths: list[str]
    tag_formats: list[str]

    def tag_candidates(self, version: str) -> list[str]:
        return [fmt.format(version=version) for fmt in self.tag_formats]


def discover_scopes(repo: str) -> list[Scope]:
    """Enumerate the repository's release-state scopes.

    A workspace yields one scope per releasable (explicit mode) plus one per
    package that keeps its own ``.rlsbl/releases/`` (implicit mode). Anything
    else is a standalone repository with a single scope at the root.
    """
    workspace_file = os.path.join(repo, ".rlsbl-monorepo", "workspace.toml")
    if not os.path.isfile(workspace_file):
        return [
            Scope(
                label="standalone",
                releases_dir=os.path.join(repo, ".rlsbl", "releases"),
                changes_dir=os.path.join(repo, ".rlsbl", "changes"),
                changelog_md=os.path.join(repo, "CHANGELOG.md"),
                released_paths=["."],
                tag_formats=["v{version}"],
            )
        ]

    from rlsbl.workspace import load_releasables, load_workspace, members_of

    projects = load_workspace(repo)
    scopes: list[Scope] = []
    releasable_members: set[str] = set()

    try:
        releasables = load_releasables(repo, projects)
    except Exception:
        releasables = []  # implicit mode: no [[releasables]] section

    for rel in releasables:
        members = members_of(rel.name, projects)
        for m in members:
            releasable_members.add(m.path)
        rel_dir = os.path.join(repo, ".rlsbl-monorepo", "releasables", rel.name)
        paths = [m.path for m in members] or ["."]
        tag_format = rel.effective_tag_format.replace("{name}", rel.name)
        scopes.append(
            Scope(
                label=rel.name,
                releases_dir=os.path.join(rel_dir, "releases"),
                changes_dir=os.path.join(rel_dir, "changes"),
                changelog_md=os.path.join(repo, "CHANGELOG.md"),
                released_paths=paths,
                tag_formats=[tag_format],
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
                tag_formats=[
                    proj.name + "@v{version}",
                    proj.path + "/v{version}",
                ],
            )
        )

    return scopes


# ---------------------------------------------------------------------------
# Version discovery and metadata recovery
# ---------------------------------------------------------------------------


def _semver_key(version: str):
    """Sort key: (major, minor, patch, is_stable, prerelease-string)."""
    core, _, pre = version.partition("-")
    parts = core.split(".")
    nums = tuple(int(p) for p in parts[:3])
    return (*nums, 0 if pre else 1, pre)


def archived_versions(scope: Scope) -> dict[str, str]:
    """Map version -> archive path for every ``v{X}.toml`` in the scope."""
    result: dict[str, str] = {}
    if not os.path.isdir(scope.releases_dir):
        return result
    for name in os.listdir(scope.releases_dir):
        m = _ARCHIVE_RE.match(name)
        if m:
            result[m.group(1)] = os.path.join(scope.releases_dir, name)
    return result


def changelog_versions(scope: Scope) -> dict[str, str]:
    """Map version -> JSONL path for every finalized changelog file."""
    return {v: p for v, p in list_versioned_files(scope.changes_dir)}


def read_archive_state(path: str) -> dict:
    """What an existing archive already carries, without validating its shape.

    tomllib rather than the strictspec reader on purpose: the archives this pass
    repairs are exactly the ones the reader would reject (no ``format_version``
    gate), so asking the reader first would refuse to look at the file the pass
    exists to fix.
    """
    import tomllib

    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"{path}: unparseable TOML: {exc}") from exc
    return {
        "format_version": "format_version" in data,
        "anchored": any(f in data for f in ANCHOR_FIELDS),
        "unanchorable": bool(data.get(UNANCHORABLE_FIELD)),
        "bump": data.get("bump", ""),
        "description": data.get("description", ""),
    }


def lead_paragraph(markdown: str | None) -> str | None:
    """The prose paragraph a version's notes open with, or None.

    A version section is a description paragraph (when it has one) followed by
    ``### Features`` / bullet groups. Everything from the first heading, bullet
    or details block onward is the generated part, so only the leading prose is
    a recovered description.
    """
    if not markdown:
        return None
    lines: list[str] = []
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


def gh_release_body(repo: str, tag: str) -> str | None:
    """The GitHub Release body for *tag*, or None when unavailable.

    Fails soft in every direction -- no ``gh``, not authenticated, no release
    for the tag, a network failure -- because it is the FIRST of three
    description sources and an unavailable source must fall through to the next
    one rather than abort the pass.
    """
    try:
        result = subprocess.run(
            ["gh", "release", "view", tag, "--json", "body", "-q", ".body"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def recover_description(
    repo: str, scope: Scope, version: str, tag: str | None, *, use_gh: bool
) -> tuple[str, str]:
    """Recover a version's description. Returns ``(description, source)``."""
    if use_gh and tag:
        body = gh_release_body(repo, tag)
        text = lead_paragraph(body)
        if text:
            return text, f"github-release:{tag}"
    if os.path.isfile(scope.changelog_md):
        with open(scope.changelog_md, "r", encoding="utf-8") as f:
            content = f.read()
        text = lead_paragraph(extract_changelog_entry_from_text(content, version))
        if text:
            return text, "changelog-md"
    return PLACEHOLDER_DESCRIPTION, "placeholder"


def derive_bump(version: str, predecessor: str | None) -> str:
    """Derive the bump type from version arithmetic against the predecessor.

    A version with no predecessor is measured against ``0.0.0``, so a first
    release of ``0.1.0`` derives ``minor``. ``infra`` is a patch increment and
    therefore indistinguishable here -- a derived ``patch`` is the honest answer,
    not a guess at intent.
    """
    cur = [int(p) for p in version.partition("-")[0].split(".")[:3]]
    prev = [int(p) for p in (predecessor or "0.0.0").partition("-")[0].split(".")[:3]]
    if cur[0] != prev[0]:
        return "major"
    if cur[1] != prev[1]:
        return "minor"
    return "patch"


def detect_include(repo: str, scope: Scope) -> list[str]:
    """Target names for a materialized archive, detected at backfill time."""
    from rlsbl.targets import detect_targets

    if scope.released_paths == ["."]:
        proj_dir = repo
    else:
        proj_dir = os.path.join(repo, scope.released_paths[0])
    try:
        return [t.name for t in detect_targets(proj_dir)]
    except Exception:
        return []


def tree_hashes_at(repo: str, sha: str, released_paths: list[str]) -> tuple[dict, list[str]]:
    """Tree hashes for the released paths at *sha*. Returns ``(trees, notes)``.

    A declared path that does not exist at that commit is dropped with a note --
    a workspace's member directories did not exist during its standalone era,
    and recording a tree for a path that was not there would be a fabrication.
    When nothing resolves, the root tree under ``"."`` is the honest record of
    what that commit released.
    """
    trees: dict[str, str] = {}
    notes: list[str] = []
    for path in released_paths:
        if path == ".":
            root = rev_parse(repo, f"{sha}^{{tree}}")
            if root:
                trees["."] = root
            continue
        tree = rev_parse(repo, f"{sha}:{path}")
        if tree:
            trees[path] = tree
        else:
            notes.append(f"path {path!r} did not exist at {sha[:8]}")
    if not trees:
        root = rev_parse(repo, f"{sha}^{{tree}}")
        if root:
            trees["."] = root
            notes.append("no declared path existed at this commit; recorded the root tree as \".\"")
    return trees, notes


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class VersionPlan:
    """What this pass will do to one version's archive."""

    scope: Scope
    version: str
    bucket: str  # "anchorable" | "tagless"
    archive_path: str
    archive_exists: bool
    actions: list[str] = field(default_factory=list)
    tag: str | None = None
    probed_tags: list[str] = field(default_factory=list)
    candidate_sha: str | None = None
    anchored_from: str = ""  # "tag" | "bump-commit" | ""
    tree_hashes: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stamp_format_version: bool = False
    materialize: bool = False
    unanchorable: bool = False
    bump: str = ""
    description: str = ""
    description_source: str = ""
    include: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass
class ForeignTag:
    tag: str
    scheme: str
    version: str
    probed: list[str]


@dataclass
class Plan:
    repo: str
    scopes: list[Scope]
    versions: list[VersionPlan] = field(default_factory=list)
    foreign_tags: list[ForeignTag] = field(default_factory=list)
    unrecognized_tags: list[str] = field(default_factory=list)

    @property
    def changed_versions(self) -> list[VersionPlan]:
        return [v for v in self.versions if v.changed]


def build_plan(repo: str, *, use_gh: bool) -> Plan:
    """Inspect the repository and decide every action, writing nothing."""
    scopes = discover_scopes(repo)
    plan = Plan(repo=repo, scopes=scopes)

    claimed_tags: set[str] = set()

    for scope in scopes:
        archives = archived_versions(scope)
        changelogs = changelog_versions(scope)
        versions = sorted(set(archives) | set(changelogs), key=_semver_key)
        include = None  # detected lazily, only when something is materialized

        for index, version in enumerate(versions):
            predecessor = versions[index - 1] if index else None
            archive_path = archives.get(
                version, os.path.join(scope.releases_dir, f"v{version}.toml")
            )
            exists = version in archives
            state = read_archive_state(archive_path) if exists else None

            probed = scope.tag_candidates(version)
            tag = None
            sha = None
            for candidate in probed:
                resolved = rev_parse(repo, f"{candidate}^{{commit}}")
                if resolved:
                    tag = candidate
                    sha = resolved
                    break
            if tag:
                claimed_tags.add(tag)

            vp = VersionPlan(
                scope=scope,
                version=version,
                bucket="anchorable" if tag else "tagless",
                archive_path=archive_path,
                archive_exists=exists,
                tag=tag,
                probed_tags=probed,
            )

            if version not in changelogs:
                vp.notes.append("no changelog JSONL for this version")

            already_anchored = bool(state and (state["anchored"] or state["unanchorable"]))

            if not tag:
                bump_commits = find_bump_commits(repo, version)
                if bump_commits:
                    sha = bump_commits[0]
                    vp.anchored_from = "bump-commit"
                    vp.notes.append(
                        f"no tag (probed {', '.join(probed)}); anchored from the "
                        f"version-bump commit {sha[:8]}"
                    )
                    if len(bump_commits) > 1:
                        vp.notes.append(
                            f"{len(bump_commits)} commits carry this bump message; "
                            f"took the first ({', '.join(s[:8] for s in bump_commits)})"
                        )
                else:
                    vp.unanchorable = True
                    vp.notes.append(
                        f"no tag (probed {', '.join(probed)}) and no version-bump "
                        f"commit in history: unrecoverable"
                    )
            else:
                vp.anchored_from = "tag"

            if sha and not vp.unanchorable:
                vp.candidate_sha = sha
                trees, notes = tree_hashes_at(repo, sha, scope.released_paths)
                vp.tree_hashes = trees
                vp.notes.extend(notes)
                if not trees:
                    vp.unanchorable = True
                    vp.candidate_sha = None
                    vp.notes.append(
                        f"commit {sha[:8]} yielded no tree for any released path"
                    )

            if not exists:
                if include is None:
                    include = detect_include(repo, scope)
                vp.materialize = True
                vp.include = list(include)
                vp.bump = derive_bump(version, predecessor)
                vp.description, vp.description_source = recover_description(
                    repo, scope, version, tag, use_gh=use_gh
                )
                vp.actions.append(
                    f"materialize archive (bump={vp.bump} from predecessor "
                    f"{predecessor or '0.0.0'}, description from "
                    f"{vp.description_source}, include={vp.include})"
                )
                if vp.unanchorable:
                    vp.actions.append("write unanchorable = true")
                else:
                    vp.actions.append(
                        f"anchor candidate_sha={vp.candidate_sha[:12]} "
                        f"tree_hashes={ {k: v[:12] for k, v in vp.tree_hashes.items()} }"
                    )
            else:
                if not state["format_version"]:
                    vp.stamp_format_version = True
                    vp.actions.append("stamp format_version = 1")
                if already_anchored:
                    vp.notes.append("already anchored; left alone")
                elif vp.unanchorable:
                    vp.actions.append("write unanchorable = true")
                else:
                    vp.actions.append(
                        f"anchor candidate_sha={vp.candidate_sha[:12]} "
                        f"tree_hashes={ {k: v[:12] for k, v in vp.tree_hashes.items()} }"
                    )

            plan.versions.append(vp)

    for tag in all_tags(repo):
        if tag in claimed_tags:
            continue
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is None:
            plan.unrecognized_tags.append(tag)
            continue
        probed = []
        for scope in scopes:
            probed.extend(
                f"{scope.label}: {spelling}"
                for spelling in scope.tag_candidates(parsed.version)
            )
        plan.foreign_tags.append(
            ForeignTag(
                tag=tag, scheme=parsed.scheme, version=parsed.version, probed=probed
            )
        )

    return plan


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan(plan: Plan, out=sys.stdout) -> None:
    """Print the full plan: every scope, every bucket, every action."""
    print(f"Repository: {plan.repo}", file=out)
    for scope in plan.scopes:
        print(
            f"  scope {scope.label}: releases={os.path.relpath(scope.releases_dir, plan.repo)} "
            f"changes={os.path.relpath(scope.changes_dir, plan.repo)} "
            f"released_paths={scope.released_paths} "
            f"tag_spellings={scope.tag_formats}",
            file=out,
        )
    print("", file=out)

    for bucket, title in (
        ("anchorable", "(a) anchorable from a tag"),
        ("tagless", "(b) TAGLESS versions"),
    ):
        rows = [v for v in plan.versions if v.bucket == bucket]
        changed = [v for v in rows if v.changed]
        print(f"{title}: {len(rows)} version(s), {len(changed)} needing work", file=out)
        for vp in rows:
            if not vp.changed and not vp.notes:
                continue
            head = f"  {vp.scope.label} {vp.version}"
            if vp.tag:
                head += f"  tag={vp.tag}"
            print(head, file=out)
            for note in vp.notes:
                print(f"      note: {note}", file=out)
            for action in vp.actions:
                print(f"      -> {action}", file=out)
        print("", file=out)

    print(
        f"(c) FOREIGN tags -- OPERATOR INPUT REQUIRED: {len(plan.foreign_tags)}",
        file=out,
    )
    for ft in plan.foreign_tags:
        print(
            f"  {ft.tag}: parses under the {ft.scheme} scheme as version "
            f"{ft.version}, but no scope claims that spelling.",
            file=out,
        )
        print(f"      probed: {'; '.join(ft.probed) or '(no scope)'}", file=out)
        print(
            "      the script does not guess: resolve it by hand (identify the "
            "commit and content, then anchor the right version or remove nothing).",
            file=out,
        )
    print("", file=out)

    print(f"(d) unrecognizable tags: {len(plan.unrecognized_tags)}", file=out)
    for tag in plan.unrecognized_tags:
        print(f"  {tag}  (left untouched)", file=out)
    print("", file=out)

    changed = plan.changed_versions
    print(
        f"TOTAL: {len(changed)} archive(s) to write "
        f"({sum(1 for v in changed if v.materialize)} materialized, "
        f"{sum(1 for v in changed if v.stamp_format_version)} stamped, "
        f"{sum(1 for v in changed if v.unanchorable)} unanchorable)",
        file=out,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_version(vp: VersionPlan) -> str:
    """Write one version's archive. Returns the path written."""
    if vp.materialize:
        write_archived_release_file(
            vp.scope.releases_dir,
            vp.version,
            bump=vp.bump,
            include=vp.include,
            exclude=[],
            description=vp.description,
            candidate_sha=None if vp.unanchorable else vp.candidate_sha,
            tree_hashes=None if vp.unanchorable else vp.tree_hashes,
            unanchorable=vp.unanchorable,
            header_comments=MATERIALIZED_HEADER,
        )
        return vp.archive_path

    with writable_release_file(vp.archive_path):
        if vp.stamp_format_version:
            with open(vp.archive_path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(vp.archive_path, "w", encoding="utf-8") as f:
                f.write(FORMAT_VERSION_STAMP + content)
        if vp.unanchorable:
            write_unanchorable_marker(vp.archive_path)
        elif vp.candidate_sha:
            write_release_anchor(
                vp.archive_path,
                candidate_sha=vp.candidate_sha,
                tree_hashes=vp.tree_hashes,
            )
    return vp.archive_path


def apply_plan(plan: Plan) -> list[str]:
    """Write every planned archive. Returns the repo-relative paths written."""
    written: list[str] = []
    for vp in plan.changed_versions:
        path = apply_version(vp)
        written.append(os.path.relpath(path, plan.repo))
    return written


def commit_message(plan: Plan, written: list[str]) -> str:
    """One commit per run, naming the repo-relative scope it touched."""
    dirs = sorted({os.path.dirname(p) for p in written})
    return (
        f"Backfill release anchors in {', '.join(dirs)} "
        f"({len(written)} archive(s))"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(repo: str, *, dry_run: bool, use_gh: bool, auto_commit: bool, out=sys.stdout) -> int:
    plan = build_plan(repo, use_gh=use_gh)
    render_plan(plan, out=out)

    if dry_run:
        print("\n--dry-run: nothing written.", file=out)
    elif not plan.changed_versions:
        print("\nNothing to do: every archive is anchored and gated.", file=out)
    else:
        written = apply_plan(plan)
        print(f"\nWrote {len(written)} archive(s).", file=out)
        if auto_commit:
            commit_files(
                commit_message(plan, written),
                written,
                autogenerated=True,
                cwd=repo,
            )

    if plan.foreign_tags:
        rest = "planned above" if dry_run else "above is complete"
        print(
            f"\nOPERATOR INPUT REQUIRED: {len(plan.foreign_tags)} foreign tag(s) "
            f"({', '.join(ft.tag for ft in plan.foreign_tags)}). Every other "
            f"action {rest}; resolve these by hand.",
            file=out,
        )
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill release-archive anchors, format_version gates, and missing archives.",
    )
    parser.add_argument(
        "--repo", default=".", help="repository to operate on (default: cwd)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the full plan and write nothing",
    )
    parser.add_argument(
        "--no-gh",
        action="store_true",
        help="skip the GitHub Release description source (offline runs)",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="write the archives but leave them uncommitted",
    )
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2
    return run(
        repo,
        dry_run=args.dry_run,
        use_gh=not args.no_gh,
        auto_commit=not args.no_commit,
    )


if __name__ == "__main__":
    sys.exit(main())

"""Monorepo mirror reconciler.

The subtree mirror of a monorepo project is a TOOL-OWNED, derived artifact.
Nothing is ever authored on it by hand -- it is regenerated from the monorepo
whenever the project's history advances. Force-push (with lease) is the routine
write, not an exceptional one.

The command is an observe-then-converge reconciler:

* ``rlsbl monorepo mirror <project>``            -- observe, then converge (apply).
* ``rlsbl monorepo mirror <project> --dry-run``  -- observe and report a plan only
  (zero writes beyond the loose objects a branchless ``git subtree split`` leaves
  in the monorepo object store).

Desired state of the mirror's ``main``:

* its tip is exactly one scaffold commit atop the CURRENT split-lineage commit,
  where the split-lineage commit equals the deterministic branchless subtree
  split of the project's current history, and
* the scaffold commit touches only scaffold-owned paths.

A tripwire enforces the contract with no heuristics: the remote tip must be
EITHER a bare split-lineage commit (the current split SHA or an older one --
covers pre-scaffold-layer mirrors) OR exactly one commit atop a split-lineage
commit whose changed paths are all scaffold-owned. Anything else is a foreign
commit -- a contract violation -- and is a hard error that touches nothing.
"""

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field

from ...git_util import validate_subtree_remote_ssh_host
from ...workspace import find_workspace_root, load_workspace
from ... import effects


# ---------------------------------------------------------------------------
# Scaffold-owned path set (the tripwire's allow-list)
# ---------------------------------------------------------------------------
#
# A mirror commit is a legitimate scaffold layer only if every path it changes
# is scaffold-owned. The owned set is the union of:
#
#   1. the keys of the clone's ``.rlsbl/managed-files.json`` manifest
#      (template-derived files tracked for orphan detection -- typically CI
#      workflows under ``.github/``); plus
#   2. the pinned prefixes and root files below.
#
# The pinned set is derived from what ``rlsbl scaffold`` writes (see
# rlsbl/commands/init_cmd.py: apply_plans / _post_scaffold, and rlsbl/tagging.py
# for manifest keyword tagging). Everything the tool owns lives under
# ``.rlsbl/`` (config.json, managed-files.json, version, bases/, changes/,
# hooks/, lint/) or under ``.github/`` (workflows), plus a handful of root
# files. The root files are:
#   * CHANGELOG.md, .gitignore, .npmignore -- template-derived (also in
#     managed-files.json, listed here so the set holds even without a manifest).
#   * LICENSE                              -- scaffolded license file.
#   * package.json, pyproject.toml         -- the ONLY manifests scaffold
#     rewrites, and only to inject the "rlsbl" keyword (rlsbl/tagging.py:
#     ensure_npm_keyword / ensure_pypi_keyword). No other manifest is touched.
# Because the mirror is tool-owned, these directories are entirely scaffold
# territory -- nothing else is ever authored there. Any changed path outside
# this set belongs to the project's own source tree and therefore marks a
# foreign commit.
#
# Pin the set HERE, in one place, and update it if scaffold's outputs change.
SCAFFOLD_OWNED_PREFIXES = (".rlsbl/", ".github/")
SCAFFOLD_OWNED_FILES = frozenset({
    "CHANGELOG.md",
    ".gitignore",
    ".npmignore",
    "LICENSE",
    "package.json",
    "pyproject.toml",
})

# Substrings in ``git ls-remote`` stderr that indicate an authentication or
# authorization failure (as opposed to a genuinely missing repository).
_AUTH_MARKERS = (
    "authentication failed",
    "permission denied",
    "could not read username",
    "could not read password",
    "access denied",
    "403 forbidden",
    "terminal prompts disabled",
    "fatal: authentication",
)


class MirrorError(Exception):
    """A hard error in the mirror reconciler (contract violation, auth, etc.)."""


@dataclass
class MirrorPlan:
    """The observed state of a mirror relative to its monorepo source.

    ``state`` is one of:
      * ``"converged"``          -- scaffold commit atop the current split; nothing to do.
      * ``"behind"``             -- a scaffold layer exists atop an OLDER split; a new
                                    split is available.
      * ``"scaffold_missing"``   -- the tip is a bare split-lineage commit (no scaffold
                                    layer). May also be behind (older split).
      * ``"contract_violated"``  -- a foreign commit exists on the mirror.
      * ``"virgin"``             -- the remote is missing or empty.
    """

    state: str
    split_sha: str
    remote_tip: str | None = None
    split_lineage_sha: str | None = None
    behind: bool = False
    foreign_commits: list = field(default_factory=list)  # list of (sha, [paths])
    remote_detail: str = ""

    @property
    def split_push_needed(self) -> bool:
        """Whether converging requires pushing a fresh bare split to ``main``."""
        if self.state == "virgin":
            return True
        return self.split_lineage_sha != self.split_sha


# ---------------------------------------------------------------------------
# Low-level git helpers (fine-grained exit-code control)
# ---------------------------------------------------------------------------


def _git(args, cwd=None, timeout=180):
    """Run a git command, returning the CompletedProcess (never raises)."""
    return effects.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_ok(args, cwd=None, timeout=180):
    """Run a git command, returning stdout stripped; raise MirrorError on failure."""
    r = _git(args, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        raise MirrorError(
            f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout.strip()


def is_ancestor(ancestor, descendant, cwd):
    """True iff ``ancestor`` is an ancestor of (or equal to) ``descendant``.

    Returns False when either object is unknown to the repo at ``cwd`` (git
    exits non-zero), which is exactly the "not part of our lineage" signal the
    tripwire needs.
    """
    r = _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Observation layer (mutation-free apart from loose split objects)
# ---------------------------------------------------------------------------


def classify_remote(remote, cwd):
    """Classify the remote via ``git ls-remote``.

    Returns a tuple ``(kind, tip, detail)`` where ``kind`` is one of:
      * ``"missing"``   -- ``ls-remote`` failed and stderr does not look like auth.
      * ``"auth"``      -- ``ls-remote`` failed with an authentication/authorization error.
      * ``"empty"``     -- ``ls-remote`` succeeded but the remote has no refs.
      * ``"no_main"``   -- the remote has refs but no ``refs/heads/main``.
      * ``"populated"`` -- ``refs/heads/main`` exists; ``tip`` is its SHA.
    """
    r = _git(["ls-remote", remote], cwd=cwd)
    if r.returncode != 0:
        stderr = (r.stderr or "").lower()
        if any(m in stderr for m in _AUTH_MARKERS):
            return ("auth", None, r.stderr.strip())
        return ("missing", None, r.stderr.strip())

    refs = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    if not refs:
        return ("empty", None, "")
    tip = refs.get("refs/heads/main")
    if tip is None:
        return ("no_main", None, "")
    return ("populated", tip, "")


def compute_split_sha(root, project_path):
    """Deterministic branchless subtree split of ``project_path``.

    Runs ``git subtree split --prefix=<path>`` WITHOUT ``-b``: it prints the
    resulting commit SHA to stdout, creates no refs, and materializes the whole
    synthetic split lineage as loose objects in the monorepo (so later
    ancestry checks against older split commits resolve locally).
    """
    r = _git(["subtree", "split", f"--prefix={project_path}"], cwd=root)
    if r.returncode != 0:
        raise MirrorError(
            f"git subtree split failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        raise MirrorError("git subtree split produced no SHA")
    return lines[-1]


def _clone_main(remote, dest):
    """Full single-branch clone of ``main`` so the whole tip layer is inspectable."""
    r = _git(
        ["clone", "--quiet", "--single-branch", "--branch", "main", remote, dest]
    )
    if r.returncode != 0:
        raise MirrorError(
            f"failed to fetch mirror for inspection: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )


def _first_parent_chain(clone_dir, tip):
    """First-parent commit chain from ``tip`` (newest first)."""
    out = _git_ok(["rev-list", "--first-parent", tip], cwd=clone_dir)
    return [c for c in out.splitlines() if c.strip()]


def _changed_paths(clone_dir, base, tip):
    """Paths changed between ``base`` and ``tip`` (name-only)."""
    out = _git_ok(["diff", "--name-only", base, tip], cwd=clone_dir)
    return [p for p in out.splitlines() if p.strip()]


def _commit_paths(clone_dir, commit):
    """Paths changed by a single ``commit`` (vs its first parent)."""
    r = _git(["show", "--first-parent", "--name-only", "--format=", commit],
             cwd=clone_dir)
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.splitlines() if p.strip()]


def _load_owned_predicate(clone_dir):
    """Build the ``is scaffold-owned?`` predicate for this mirror.

    Reads the clone's ``.rlsbl/managed-files.json`` (if present) and unions its
    keys with the pinned prefixes/files.
    """
    managed = set()
    manifest = os.path.join(clone_dir, ".rlsbl", "managed-files.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            managed = set(data.get("files", {}).keys())
        except (json.JSONDecodeError, OSError):
            managed = set()

    def owned(path):
        if path in managed:
            return True
        if path in SCAFFOLD_OWNED_FILES:
            return True
        return any(path.startswith(pref) for pref in SCAFFOLD_OWNED_PREFIXES)

    return owned


def observe(remote, root, project_path):
    """Observe the mirror and return a :class:`MirrorPlan`.

    Mutation-free apart from the loose objects the branchless split leaves in
    the monorepo object store.
    """
    split_sha = compute_split_sha(root, project_path)

    kind, tip, detail = classify_remote(remote, root)
    if kind == "auth":
        raise MirrorError(
            f"authentication failed reaching subtree remote: {detail or remote}"
        )
    if kind == "missing" or kind == "empty":
        return MirrorPlan(state="virgin", split_sha=split_sha, remote_detail=detail)
    if kind == "no_main":
        raise MirrorError(
            f"mirror remote has refs but no 'main' branch: {remote}. "
            "Reset the mirror (delete stray refs) and re-run."
        )

    # Populated: inspect the tip's shape in an isolated temp clone.
    tmpdir = tempfile.mkdtemp(prefix="rlsbl-mirror-observe-")
    try:
        clone_dir = os.path.join(tmpdir, "mirror")
        _clone_main(remote, clone_dir)

        # Walk the first-parent chain from the tip down to the SPLIT BOUNDARY:
        # the nearest commit that is part of the split lineage (an ancestor of
        # the current split, i.e. an old or current bare split commit). Every
        # commit strictly above the boundary is the mirror's "layer".
        chain = _first_parent_chain(clone_dir, tip)
        boundary = None
        above = []  # commits above the boundary, newest first
        for commit in chain:
            if is_ancestor(commit, split_sha, cwd=root):
                boundary = commit
                break
            above.append(commit)

        if boundary is None:
            # No split lineage anywhere in the tip's history -> wholly foreign.
            foreign = [(c, _commit_paths(clone_dir, c)) for c in above]
            return MirrorPlan(
                state="contract_violated",
                split_sha=split_sha,
                remote_tip=tip,
                split_lineage_sha=None,
                foreign_commits=foreign,
            )

        # (a) No layer: tip is a bare split-lineage commit.
        if not above:
            return MirrorPlan(
                state="scaffold_missing",
                split_sha=split_sha,
                remote_tip=tip,
                split_lineage_sha=boundary,
                behind=boundary != split_sha,
            )

        owned = _load_owned_predicate(clone_dir)

        # (b) Exactly one commit atop the boundary, all scaffold-owned -> the
        #     legitimate scaffold layer.
        if len(above) == 1:
            changed = _changed_paths(clone_dir, boundary, tip)
            foreign_paths = [p for p in changed if not owned(p)]
            if not foreign_paths:
                if boundary == split_sha:
                    return MirrorPlan(
                        state="converged",
                        split_sha=split_sha,
                        remote_tip=tip,
                        split_lineage_sha=boundary,
                    )
                return MirrorPlan(
                    state="behind",
                    split_sha=split_sha,
                    remote_tip=tip,
                    split_lineage_sha=boundary,
                    behind=True,
                )

        # (c) Anything else (multiple commits, or a single non-scaffold commit)
        #     -> foreign layer. Report each commit above the boundary with its
        #     non-scaffold-owned paths (falling back to all its paths).
        foreign = []
        for commit in above:
            paths = _commit_paths(clone_dir, commit)
            non_owned = [p for p in paths if not owned(p)]
            foreign.append((commit, non_owned or paths))
        return MirrorPlan(
            state="contract_violated",
            split_sha=split_sha,
            remote_tip=tip,
            split_lineage_sha=boundary,
            foreign_commits=foreign,
        )
    finally:
        effects.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Plan reporting (dry-run)
# ---------------------------------------------------------------------------


def _remediation(plan, project_path):
    lines = []
    for sha, paths in plan.foreign_commits:
        if paths is None:
            lines.append(
                f"  - commit {sha[:12]} is not a split-lineage commit and is not "
                f"a single scaffold commit atop one."
            )
        else:
            shown = ", ".join(paths[:8]) + (" ..." if len(paths) > 8 else "")
            lines.append(
                f"  - commit {sha[:12]} touches non-scaffold paths: {shown}"
            )
    body = "\n".join(lines)
    return (
        f"{body}\n"
        f"The mirror is tool-owned and must never be authored on directly.\n"
        f"Remediation: port the change(s) into the monorepo (under '{project_path}') "
        f"and re-run, OR reset the mirror branch (a fresh forced split discards the "
        f"foreign work) and re-run."
    )


def print_plan(plan, remote, project_path):
    """Print a human-readable plan for ``--dry-run`` (zero writes)."""
    if plan.state == "converged":
        print(f"converged: mirror is up to date (split {plan.split_sha[:12]}, "
              f"scaffold layer present).")
    elif plan.state == "behind":
        print(f"behind: a new split is available.")
        print(f"  old split: {plan.split_lineage_sha[:12]}")
        print(f"  new split: {plan.split_sha[:12]}")
        print("  apply would force-push the new split (with lease) and re-scaffold.")
    elif plan.state == "scaffold_missing":
        if plan.behind:
            print(f"scaffold-missing (and behind): tip is a bare split commit "
                  f"{plan.split_lineage_sha[:12]}, older than current split "
                  f"{plan.split_sha[:12]}.")
            print("  apply would force-push the new split (with lease) and scaffold.")
        else:
            print(f"scaffold-missing: tip is the current bare split commit "
                  f"{plan.split_sha[:12]} with no scaffold layer.")
            print("  apply would add the scaffold commit and push.")
    elif plan.state == "contract_violated":
        print("contract-violated: foreign commit(s) detected on the mirror.")
        print(_remediation(plan, project_path))
    elif plan.state == "virgin":
        print(f"remote-missing-or-empty: mirror at {remote} is virgin.")
        print(f"  apply would push split {plan.split_sha[:12]} and scaffold CI.")
    else:  # pragma: no cover - defensive
        print(f"unknown state: {plan.state}")


# ---------------------------------------------------------------------------
# Convergence (apply)
# ---------------------------------------------------------------------------


def _push_bare_split(remote, split_sha, expected_tip, root):
    """Push the bare split commit to ``main``.

    Uses force-with-lease against ``expected_tip`` when the branch already
    exists; a plain push when creating the branch on a virgin remote.
    """
    # --no-verify: the reconciler's writes to a tool-owned mirror are not
    # subject to the monorepo's changelog pre-push guard (an irrelevant check
    # for a derived artifact). This is the tool bypassing its own hook on an
    # internal operation, not a user-facing escape hatch.
    args = ["push", "--no-verify"]
    if expected_tip is not None:
        args += [f"--force-with-lease=main:{expected_tip}"]
    args += [remote, f"{split_sha}:refs/heads/main"]
    r = _git(args, cwd=root)
    if r.returncode != 0:
        raise MirrorError(
            f"failed to push split to mirror: {r.stderr.strip() or r.stdout.strip()}"
        )


def _run_scaffold(clone_dir, sub_config_path):
    """Copy the project's ``.rlsbl/config.json`` into the clone and scaffold.

    Runs ``rlsbl scaffold --no-auto-commit`` so the reconciler owns the commit.
    A non-zero scaffold exit is a HARD ERROR (no warn-and-continue).
    """
    config = {}
    if os.path.isfile(sub_config_path):
        with open(sub_config_path, encoding="utf-8") as f:
            config = json.load(f)

    clone_rlsbl_dir = os.path.join(clone_dir, ".rlsbl")
    effects.makedirs(clone_rlsbl_dir, exist_ok=True)
    with effects.open_write(os.path.join(clone_rlsbl_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    result = effects.run(
        # -P: suppress CWD injection from ``python -m`` run in the mirror clone dir
        # (a root module shadowing a stdlib/dep name would break rlsbl imports).
        [sys.executable, "-P", "-m", "rlsbl", "scaffold", "--no-auto-commit"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MirrorError(
            "rlsbl scaffold failed in mirror clone "
            f"(exit {result.returncode}):\n{result.stderr or result.stdout}"
        )


def _converge(plan, remote, root, project_path, sub_config_path):
    """Bring the mirror to the desired state. Idempotent; interrupted runs heal."""
    split_sha = plan.split_sha

    # 1. If the remote's split lineage is not the current split, force the new
    #    bare split onto main (with lease). This temporarily strips the scaffold
    #    layer; step 3 re-adds it -- an interrupted run re-observes as
    #    scaffold-missing and heals.
    if plan.split_push_needed:
        expected = None if plan.state == "virgin" else plan.remote_tip
        print(f"Pushing split {split_sha[:12]} to mirror main...")
        _push_bare_split(remote, split_sha, expected, root)

    # 2. Fresh clone at main (== split_sha now).
    tmpdir = tempfile.mkdtemp(prefix="rlsbl-mirror-apply-")
    try:
        clone_dir = os.path.join(tmpdir, "mirror")
        r = _git(["clone", "--quiet", "--single-branch", "--branch", "main",
                  remote, clone_dir])
        if r.returncode != 0:
            raise MirrorError(
                f"failed to clone mirror for scaffolding: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )

        # 3. Scaffold (hard error on failure), then commit + push the layer.
        print("Scaffolding CI in mirror...")
        _run_scaffold(clone_dir, sub_config_path)

        _git_ok(["add", "-A"], cwd=clone_dir)
        status = _git_ok(["status", "--porcelain"], cwd=clone_dir)
        if not status:
            print(f"Mirror converged (scaffold produced no changes): {remote}")
            return
        _git_ok(["commit", "-m", "chore: scaffold rlsbl CI"], cwd=clone_dir)

        # Fast-forward child of split_sha; force-with-lease guards against races.
        # --no-verify: the scaffold clone has an rlsbl pre-push hook whose
        # changelog-coverage guard is irrelevant to this tool-owned mirror push.
        r = _git(["push", "--no-verify", f"--force-with-lease=main:{split_sha}",
                  "origin", "main"], cwd=clone_dir)
        if r.returncode != 0:
            raise MirrorError(
                f"failed to push scaffold layer to mirror: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )
        print(f"Mirror converged: {remote}")
    finally:
        effects.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def _cmd_mirror(flags, project_root):
    """Observe-then-converge reconciler for a project's subtree mirror.

    ``flags["project"]``  -- workspace project name.
    ``flags["dry-run"]``  -- plan only (no writes).
    """
    project_name = flags["project"]
    dry_run = bool(flags.get("dry-run", False))

    root = find_workspace_root(str(project_root))
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    project = None
    for p in projects:
        if p.name == project_name:
            project = p
            break
    if project is None:
        available = ", ".join(p.name for p in projects)
        print(
            f"Error: project '{project_name}' not found in workspace. "
            f"Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    subtree_remote = project.subtree_remote
    if not subtree_remote:
        print(f"Error: project '{project_name}' has no subtree_remote configured.", file=sys.stderr)
        print("Set it with: rlsbl monorepo add --subtree-remote <url> <path>", file=sys.stderr)
        sys.exit(1)

    # SSH host consistency (hard error on mismatch).
    validate_subtree_remote_ssh_host(subtree_remote, root)

    project_path = project.path
    sub_config_path = os.path.join(root, project_path, ".rlsbl", "config.json")

    try:
        plan = observe(subtree_remote, root, project_path)
    except MirrorError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print_plan(plan, subtree_remote, project_path)
        return

    # Apply.
    if plan.state == "contract_violated":
        print("Error: contract-violated -- the mirror has foreign commit(s); "
              "nothing was touched.", file=sys.stderr)
        print(_remediation(plan, project_path), file=sys.stderr)
        sys.exit(1)

    if plan.state == "converged":
        print(f"Already converged: {subtree_remote}")
        return

    try:
        _converge(plan, subtree_remote, root, project_path, sub_config_path)
    except MirrorError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

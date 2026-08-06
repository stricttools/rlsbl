"""Phase A of a release: a plan the builder derives, and an executor that issues it.

Phase A is everything from ``VERSION_BUMPED`` through ``BRANCH_PUSHED`` -- the
half of a release that happens before CI has judged anything, and therefore the
half a preview can show in full.  It is split in two:

* :func:`build_phase_a_plan` -- the BUILDER.  It reads (git, the working tree,
  the target manifests, the release-state file), derives every operand, decides
  every idempotency skip, and returns a :class:`PhaseAPlan`: an ordered list of
  typed :class:`PlanStep` records whose payloads are plain data.
* :func:`execute_phase_a_plan` -- the EXECUTOR.  It walks the plan and issues
  the declared effect for each step.  It derives nothing and asks nothing:
  every value it needs is either already in the plan or arrives through a
  DECLARED result capture (below).

Why the split
-------------

Under ``--dry-run`` every mutation is recorded on ``ctx.effects`` rather than
performed, and -- by the contract -- an *observe* issued after the first
recorded mutation returns the framework's stale carrier instead of a real
answer.  The old Phase A interleaved reads and writes freely (``git status``
after the version bump, ``git log -1`` before the commit, ``git rev-parse
HEAD`` after it, ``git ls-remote`` before the push), so the very first recorded
write poisoned every question that followed.  That is why ``release run
--dry-run`` used to print an empty would-do body: there was no arrangement of
those calls that could survive its own preview.

Hoisting all ~20 of those reads into the builder -- which runs before anything
is recorded -- removes the problem at the root rather than branching around it.
The executor then issues a linear sequence of effects that record cleanly, so
the preview walks Phase A end to end and stops exactly where the release itself
stops being knowable: at the candidate push, whose verdict CI has not given yet.

Value threading
---------------

Exactly one value crosses the seam: ``candidate_sha``, the commit the release
publishes as its candidate.  The commit step PRODUCES it; the state-save step
and the push step CONSUME it, as :class:`StepRef` placeholders the builder
plants in their payloads.  Three threads, one producer and two consumers, and
the executor resolves them through :meth:`_Executor._resolve`.

A :class:`StepRef` is resolved from a per-step DECLARED result capture: an
observe the builder names alongside the step (``["git", "rev-parse", "HEAD"]``
for the commit).  In live mode the executor runs it and threads the real value.
In preview mode it runs NOTHING and threads the carrier the step's own recorded
effect returned, so the push renders as ``git push ... «step N output»`` --
the framework's own brand for "the output of the step above".

Result captures are the executor's SOLE permitted reads.  ``tests/
test_release_phase_a_seam.py`` scans this module and fails if any other read
appears in it.

Rollback is NOT a plan step
---------------------------

A step that fails does not schedule its own undo.  Phase-A execution runs
inside the caller's existing revert handler (``git reset --hard`` to the
pre-release pin plus orphan-artifact cleanup), which is an executor-level
concern and stays exactly where it was.  A preview executes nothing and so
needs no rollback at all.
"""

import dataclasses
import os

from ... import effects


# The one value that crosses the Phase-A seam.
CANDIDATE_SHA = "candidate_sha"

# Sentinel: in preview mode this step's threaded value is the carrier its own
# recorded effect returned, not a captured string.
CARRIER = object()


# ---------------------------------------------------------------------------
# Step kinds
# ---------------------------------------------------------------------------

WRITE_RELEASABLE_VERSION = "write-releasable-version"
WRITE_TARGET_VERSION = "write-target-version"
WRITE_MEMBER_VERSIONS = "write-member-versions"
BUMP_SELFDOC = "bump-selfdoc"
ENSURE_KEYWORD = "ensure-keyword"
SYNC_LOCKFILE = "sync-lockfile"
WRITE_MARKER = "write-marker"
CLEAN_ARTIFACTS = "clean-artifacts"
BUILD = "build"
SECRET_SCAN = "secret-scan"
GUARD_UNEXPECTED_FILES = "guard-unexpected-files"
COMMIT = "commit"
SNAPSHOT = "snapshot"
GUARD_FOREIGN_COMMITS = "guard-foreign-commits"
GUARD_CANDIDATE_WINDOW = "guard-candidate-window"
RECORD_CANDIDATE = "record-candidate"
PUSH_CANDIDATE = "push-candidate"

# Steps whose work is a guard rather than a mutation. A preview executes
# nothing, so there is nothing for them to guard: they are declared in the plan
# (and rendered) but issue no effect.
_GUARD_KINDS = frozenset({
    GUARD_UNEXPECTED_FILES, GUARD_FOREIGN_COMMITS, GUARD_CANDIDATE_WINDOW,
})


# The substitution point inside a StepRef template. A literal token rather
# than ``str.format``'s braces because one of the templates IS a JSON document,
# whose own braces would have to be escaped to survive formatting.
SLOT = "@@RLSBL_STEP_VALUE@@"


@dataclasses.dataclass(frozen=True)
class StepRef:
    """A symbolic placeholder for a value a not-yet-executed step will yield.

    ``name`` is the threaded value's name (only :data:`CANDIDATE_SHA` today).
    ``template`` is text with :data:`SLOT` marking where the resolved value
    goes -- the push step wants ``<sha>:refs/heads/main``, not a bare SHA, and
    the state-save step wants a whole JSON document with the SHA inside it.
    In preview mode the template is NOT applied: the operand forwarded into the
    effect is the framework's carrier, which renders as its own brand.
    """

    name: str
    template: str = SLOT

    def render(self):
        """How this reference reads in the plan table."""
        return self.template.replace(SLOT, f"<{self.name}>")


@dataclasses.dataclass(frozen=True)
class PlanStep:
    """One typed, data-only record of work Phase A will do.

    ``payload`` carries plain operands (paths, argv, content) and may contain
    :class:`StepRef` placeholders.  ``capture`` is the step's DECLARED result
    capture: ``(name, argv, cwd)``, an observe the executor runs in live mode
    to resolve the value this step ``produces``.  ``summary`` is what the
    preview's plan table shows.
    """

    kind: str
    release_step: str
    summary: str
    payload: dict = dataclasses.field(default_factory=dict)
    produces: str | None = None
    capture: tuple | None = None
    # Release-state markers to record once this step has been issued.
    marks: tuple = ()


@dataclasses.dataclass
class PhaseAPlan:
    """The ordered plan for one release's Phase A, plus what Phase B needs."""

    steps: list
    files_to_commit: list
    # Markers the builder proved already satisfied (nothing left to issue).
    presatisfied: tuple = ()
    # True when the builder resolved the candidate from a prior attempt's
    # record instead of scheduling a commit + push (resume past the CI gate,
    # or a batch member the orchestrator already gated).
    candidate_sha: str | None = None
    # Set when the plan stops at the commit: batch pass 1 defers the push and
    # the CI gate to the orchestrator.
    defers_push: bool = False

    @property
    def produced_names(self):
        """Names of the values this plan's steps produce."""
        return [s.produces for s in self.steps if s.produces]

    def consumers_of(self, name):
        """Steps whose payload references the threaded value *name*."""
        return [s for s in self.steps if name in _refs_in(s.payload)]


def _git_answer(argv, *, cwd):
    """A read-only git answer, or None when the builder cannot get one.

    The builder runs AFTER the preflight -- deliberately: the preflight is not
    part of the plan, it executes its observes and records its mutations
    exactly as it always has, and its recorded effects render first. But that
    means a preview reaches the builder with mutations already recorded, and
    from that point the framework answers every observe with a stale carrier
    rather than a fact.

    None is that case, and every call site here declares what it assumes when
    it cannot get an answer. The assumption is always the SAME one: that the
    release does the full piece of work rather than skipping it. A preview
    therefore shows the whole plan, and idempotency skips -- "the version is
    already bumped", "the remote is already at the candidate" -- stay what they
    have always been: live-mode facts about a release already partly done.
    """
    from . import run

    try:
        # The release flow's own late-bound ``run``, not ``effects.run``
        # directly: the builder's reads are release-flow reads, and tests that
        # stub the release's git calls must see these too.
        out = run("git", list(argv), cwd=cwd)
    except Exception:
        return None
    if effects.unsettled(out) or not isinstance(out, str):
        # Not a string is not an answer: the carrier a preview gets back, or
        # anything a stubbed runner hands over that is not command output.
        return None
    return out


def _predicted_version_files(target, path):
    """The version files a target's writer is expected to touch.

    A LOWER BOUND, deliberately: only the target adapter knows the full set
    (pypi also rewrites ``__version__`` in the package source, and which file
    that is depends on the project layout).  The builder predicts what it can
    so the plan table and the concurrent-change guard's expected set are right
    before anything is written; the executor widens both with the paths the
    writer actually reports (see :meth:`_Executor._do_write_target_version`).
    """
    try:
        vfile = target.version_file(path)
    except Exception:
        return []
    return [vfile] if vfile else []


def _refs_in(payload):
    """Every threaded-value name referenced anywhere in a payload."""
    names = set()

    def walk(value):
        if isinstance(value, StepRef):
            names.add(value.name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(payload)
    return names


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan_table(plan, *, indent="  "):
    """Render a Phase-A plan as a table, one line per step."""
    lines = []
    for i, step in enumerate(plan.steps, 1):
        suffix = ""
        if step.produces:
            suffix = f"  -> {step.produces}"
        lines.append(
            f"{indent}{i:>2}. {step.release_step:<20} {step.summary}{suffix}"
        )
    return "\n".join(lines)


def _render_operand(value):
    """Render a payload operand for a plan summary."""
    if isinstance(value, StepRef):
        return value.render()
    return str(value)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BuildInputs:
    """Everything the builder reads from, resolved by the caller.

    A plain record rather than a pile of keyword arguments: the builder's whole
    contract is "derive the plan from these inputs and the state of the world",
    and naming the inputs in one place is what makes that contract checkable.
    """

    state: object            # ReleaseState
    ctx: object              # ProjectContext
    log: object              # callable
    project_dir: str
    git_root: str
    monorepo_root: object
    releasable_cfg_dir: str | None
    rep_is_private: bool
    registry: str
    primary_path: str
    target_paths: dict
    secondary_targets: dict
    state_path: str
    completed: set
    pin_sha: str | None
    baseline_dirty: set
    commit_msg: str
    lock_dir: str


def build_phase_a_plan(inp: BuildInputs) -> PhaseAPlan:
    """Derive the Phase-A plan.  Reads freely; mutates nothing.

    Every read the old inline Phase A interleaved with its writes happens here,
    before the first effect is issued -- which is what lets a preview record the
    whole of Phase A instead of truncating at the first question asked after a
    recorded mutation.
    """
    from . import TARGETS, should_tag
    from .execute import (
        _bump_selfdoc_version_content,
        _rel_to_git_root,
        _sync_member_package_versions_plan,
        _target_lockfile_syncs,
    )
    state = inp.state
    log = inp.log
    completed = inp.completed
    project_dir = inp.project_dir
    git_root = inp.git_root
    registry = inp.registry
    target_paths = inp.target_paths
    new_version = state.new_version
    current_version = state.current_version
    reg = TARGETS[registry]

    steps: list = []
    files_to_commit: list = []
    presatisfied: list = []

    def vpath(filename):
        return _rel_to_git_root(os.path.join(project_dir, filename), git_root)

    def target_vpath(t_path, filename):
        return _rel_to_git_root(os.path.join(t_path, filename), git_root)

    # ---- VERSION_BUMPED ---------------------------------------------------
    version_already_bumped = False
    if "VERSION_BUMPED" in completed:
        version_already_bumped = True
        log("Skipping version bump (already done)")
    elif new_version != current_version:
        try:
            if reg.read_version(inp.primary_path) == new_version:
                version_already_bumped = True
                presatisfied.append("VERSION_BUMPED")
                log("Skipping version bump (version already matches)")
        except Exception:
            pass  # read_version may fail if the target has no manifest yet

    if not version_already_bumped and new_version != current_version:
        if state.releasable_name and inp.monorepo_root:
            from ...workspace import get_releasable_version_path

            ver_path = get_releasable_version_path(
                str(inp.monorepo_root), state.releasable_name,
            )
            ver_rel = _rel_to_git_root(ver_path, git_root)
            files_to_commit.append(ver_rel)
            steps.append(PlanStep(
                kind=WRITE_RELEASABLE_VERSION,
                release_step="VERSION_BUMPED",
                summary=f"write {ver_rel} = {new_version}",
                payload={
                    "workspace_root": str(inp.monorepo_root),
                    "releasable": state.releasable_name,
                    "version": new_version,
                    "rel_path": ver_rel,
                },
            ))

        if inp.rep_is_private:
            log("Skipping representative manifest bump (publish-suppressed member)")
        else:
            for rel in _predicted_version_files(reg, inp.primary_path):
                files_to_commit.append(target_vpath(inp.primary_path, rel))
            steps.append(PlanStep(
                kind=WRITE_TARGET_VERSION,
                release_step="VERSION_BUMPED",
                summary=(
                    f"bump {registry} version in {inp.primary_path} "
                    f"-> {new_version}"
                ),
                payload={
                    "target": registry, "path": inp.primary_path,
                    "version": new_version, "primary": True,
                },
            ))
            for t_name, t_path in target_paths.items():
                if t_name == registry:
                    continue
                other_reg = TARGETS.get(t_name)
                if other_reg and other_reg.check_project_exists(t_path):
                    for rel in _predicted_version_files(other_reg, t_path):
                        files_to_commit.append(target_vpath(t_path, rel))
                    steps.append(PlanStep(
                        kind=WRITE_TARGET_VERSION,
                        release_step="VERSION_BUMPED",
                        summary=(
                            f"sync {t_name} version in {t_path} -> {new_version}"
                        ),
                        payload={
                            "target": t_name, "path": t_path,
                            "version": new_version, "primary": False,
                        },
                    ))

        if state.releasable_name and state.member_package_paths and inp.monorepo_root:
            member_plan = _sync_member_package_versions_plan(
                state.member_package_paths, inp.monorepo_root, new_version,
                git_root, exclude_path=state.monorepo_project_path,
                releasable_config_dir=inp.releasable_cfg_dir,
            )
            for entry in member_plan:
                for fpath in entry["files"]:
                    if fpath not in files_to_commit:
                        files_to_commit.append(fpath)
            if member_plan:
                steps.append(PlanStep(
                    kind=WRITE_MEMBER_VERSIONS,
                    release_step="VERSION_BUMPED",
                    summary=(
                        f"sync {len(member_plan)} member package(s) "
                        f"-> {new_version}"
                    ),
                    payload={"entries": member_plan, "version": new_version},
                ))

        selfdoc_content = _bump_selfdoc_version_content(project_dir, new_version)
        if selfdoc_content is not None:
            fpath = vpath("selfdoc.json")
            if fpath not in files_to_commit:
                files_to_commit.append(fpath)
            steps.append(PlanStep(
                kind=BUMP_SELFDOC,
                release_step="VERSION_BUMPED",
                summary=f"write {fpath} (version {new_version})",
                payload={
                    "path": os.path.join(project_dir, "selfdoc.json"),
                    "content": selfdoc_content,
                },
            ))

    if not version_already_bumped:
        # Either the plan bumps the version above, or the version is unchanged
        # (a first release) and there is nothing to bump. Either way the marker
        # is owed so completeness is provable at the epilogue.
        presatisfied.append("VERSION_BUMPED")

    # ---- Ecosystem keyword tagging ---------------------------------------
    if should_tag(state.flags, inp.ctx.config) and not inp.rep_is_private:
        for kind, manifest in (("npm", "package.json"), ("pypi", "pyproject.toml")):
            kind_path = target_paths.get(kind, project_dir)
            try:
                exists = TARGETS[kind].check_project_exists(kind_path)
            except Exception:
                exists = False
            if not exists:
                continue
            manifest_rel = target_vpath(kind_path, manifest)
            steps.append(PlanStep(
                kind=ENSURE_KEYWORD,
                release_step="VERSION_BUMPED",
                summary=f"add the 'rlsbl' keyword to {manifest_rel}",
                payload={
                    "kind": kind, "path": kind_path,
                    "manifest_path": manifest_rel,
                },
            ))
            # The manifest joins the commit whether or not the keyword is
            # actually missing: it costs nothing when unchanged, and leaving it
            # out made a resume that skipped the version bump (nothing else put
            # the manifest in the list) abort on its own keyword write as an
            # "unexpected modified file".
            if manifest_rel not in files_to_commit:
                files_to_commit.append(manifest_rel)

    # ---- Lockfile syncs ---------------------------------------------------
    lock_targets = [dict(target_paths)]
    if state.releasable_name and state.member_package_paths and inp.monorepo_root:
        from ...member_context import resolve_member_context as _rmc_lock

        for mp_path in state.member_package_paths:
            mp_abs = os.path.join(str(inp.monorepo_root), mp_path)
            if not os.path.isdir(mp_abs):
                continue
            member = _rmc_lock(
                mp_abs, releasable_config_dir=inp.releasable_cfg_dir,
            )
            if member.publish_mode == "none":
                continue
            if member.target_paths:
                lock_targets.append(dict(member.target_paths))
    if inp.monorepo_root:
        lock_targets.append({"workspace_root": str(inp.monorepo_root)})

    for paths in lock_targets:
        for sync in _target_lockfile_syncs(paths, log):
            steps.append(PlanStep(
                kind=SYNC_LOCKFILE,
                release_step="VERSION_BUMPED",
                summary=f"run {' '.join(sync['cmd'])} in {sync['cwd']}",
                payload=sync,
            ))
            norm = os.path.normpath(sync["lockfile_path"])
            if norm not in files_to_commit:
                files_to_commit.append(norm)

    if inp.monorepo_root:
        # Workspace-root lockfiles are included unconditionally: a lockfile can
        # already be stale from the version bump (npm bug #5967) without the
        # sync touching it, and the release commit must capture that.
        from .execute import _LOCKFILE_SPECS

        for ws_lockfile_name, _, _, ws_guard in _LOCKFILE_SPECS:
            if ws_guard and not os.path.exists(
                os.path.join(str(inp.monorepo_root), ws_guard)
            ):
                continue
            ws_lockfile = os.path.join(str(inp.monorepo_root), ws_lockfile_name)
            if os.path.exists(ws_lockfile):
                norm = os.path.normpath(ws_lockfile)
                if norm not in files_to_commit:
                    files_to_commit.append(norm)
                    log(f"Workspace lockfile included: {ws_lockfile_name}")

    # ---- .rlsbl/version marker -------------------------------------------
    marker_rel = vpath(os.path.join(".rlsbl", "version"))
    scaffold_meta_present = any(
        os.path.exists(os.path.join(project_dir, ".rlsbl", meta))
        for meta in ("managed-files.json",)
    )
    if inp.releasable_cfg_dir is None and scaffold_meta_present:
        from ... import __version__ as rlsbl_ver

        steps.append(PlanStep(
            kind=WRITE_MARKER,
            release_step="VERSION_BUMPED",
            summary=f"write {marker_rel} (rlsbl {rlsbl_ver})",
            payload={"path": marker_rel, "content": rlsbl_ver + "\n"},
        ))
        if marker_rel not in files_to_commit:
            files_to_commit.append(marker_rel)

    # ---- Generated CHANGELOG.md files ------------------------------------
    from ...changelog.home import get_changelog_home, get_workspace_changelog_path

    canonical_changelog = get_changelog_home(
        project_dir, releasable_dir=inp.releasable_cfg_dir,
    )
    changelog_commit_files = []
    # Existence OR "this release generates one": the entry point materializes
    # CHANGELOG.md just before the mutating phase, and under a preview that
    # write is recorded rather than performed -- so asking the filesystem
    # whether the file is there would drop it from the previewed commit while
    # the real release commits it. A project with no changes dir (a dev node)
    # has no changelog either way.
    if os.path.exists(canonical_changelog) or state.changes_dir:
        changelog_commit_files.append(
            _rel_to_git_root(canonical_changelog, git_root)
        )
    if inp.releasable_cfg_dir and inp.monorepo_root:
        root_changelog = get_workspace_changelog_path(str(inp.monorepo_root))
        if os.path.exists(root_changelog) or state.changes_dir:
            changelog_commit_files.append(
                _rel_to_git_root(root_changelog, git_root)
            )
    for cl_file in changelog_commit_files:
        if cl_file not in files_to_commit:
            files_to_commit.append(cl_file)

    # ---- Hook-generated files --------------------------------------------
    for hf in sorted(state.hook_generated or ()):
        if hf not in files_to_commit:
            files_to_commit.append(hf)
            log(f"Including hook-generated file: {hf}")

    # ---- Build + secret scan ---------------------------------------------
    steps.append(PlanStep(
        kind=CLEAN_ARTIFACTS,
        release_step="VERSION_BUMPED",
        summary="clear stale build artifacts from dist/",
        payload={"project_dir": project_dir, "target_paths": dict(target_paths)},
    ))
    steps.append(PlanStep(
        kind=BUILD,
        release_step="VERSION_BUMPED",
        summary=f"build {registry} in {inp.primary_path}",
        payload={
            "target": registry, "path": inp.primary_path,
            "version": new_version,
        },
    ))
    for sec_name in sorted(inp.secondary_targets):
        if TARGETS.get(sec_name) is None:
            continue
        steps.append(PlanStep(
            kind=BUILD,
            release_step="VERSION_BUMPED",
            summary=f"build {sec_name} in {inp.secondary_targets[sec_name]}",
            payload={
                "target": sec_name, "path": inp.secondary_targets[sec_name],
                "version": new_version,
            },
        ))
    steps.append(PlanStep(
        kind=SECRET_SCAN,
        release_step="VERSION_BUMPED",
        summary="scan the built artifacts for secrets (gitleaks)",
        payload={"project_dir": project_dir, "target_paths": dict(target_paths)},
    ))

    # ---- The concurrent-change guard -------------------------------------
    steps.append(PlanStep(
        kind=GUARD_UNEXPECTED_FILES,
        release_step="VERSION_BUMPED",
        summary="refuse if files outside the release's own set were modified",
        payload={
            "expected": _expected_dirty_files(inp, files_to_commit),
            "baseline": set(inp.baseline_dirty or ()),
        },
        capture=("dirty", ["git", "status", "--porcelain"], None),
        marks=("VERSION_BUMPED",),
    ))

    # ---- COMMITTED --------------------------------------------------------
    commit_needed = _commit_is_needed(inp, files_to_commit, log)
    if commit_needed:
        steps.append(PlanStep(
            kind=COMMIT,
            release_step="COMMITTED",
            summary=f"commit {len(files_to_commit)} file(s) as {inp.commit_msg!r}",
            payload={
                "message": inp.commit_msg,
                "files": list(files_to_commit),
                "cwd": git_root,
                "autogenerated": False,
            },
            marks=("COMMITTED",),
            capture=(CANDIDATE_SHA, ["git", "rev-parse", "HEAD"], git_root),
            produces=CANDIDATE_SHA,
        ))
    else:
        presatisfied.append("COMMITTED")

    # ---- SNAPSHOT_REGENERATED --------------------------------------------
    snapshot_step = _snapshot_step(inp, log)
    if snapshot_step is None:
        presatisfied.append("SNAPSHOT_REGENERATED")
    else:
        steps.append(snapshot_step)

    # The candidate is whatever the LAST commit-producing step yields. When the
    # snapshot commits too, it -- not the release commit -- is the tip CI sees.
    plan_commits = _retarget_candidate(steps)

    if state.flags.get("ci-defer"):
        # Batch pass 1: COMMIT ONLY. The orchestrator publishes every member's
        # candidate in ONE push and gates that single commit, so the plan stops
        # here and the batch's own plan carries the push.
        return PhaseAPlan(
            steps=steps, files_to_commit=files_to_commit,
            presatisfied=tuple(presatisfied), defers_push=True,
        )

    # ---- BRANCH_PUSHED ----------------------------------------------------
    push_plan = _candidate_push_plan(inp, plan_commits=plan_commits)

    steps.append(PlanStep(
        kind=RECORD_CANDIDATE,
        release_step="BRANCH_PUSHED",
        summary=f"record the candidate in {os.path.basename(inp.state_path)}",
        payload={"path": inp.state_path, "ref": StepRef(CANDIDATE_SHA)},
    ))
    steps.append(PlanStep(
        kind=GUARD_FOREIGN_COMMITS,
        release_step="BRANCH_PUSHED",
        summary="refuse if a concurrent session's commits rode onto the branch",
        payload={"phase": "candidate push"},
        capture=("foreign", ["git", "rev-list", "<pin>..HEAD"], git_root),
    ))
    steps.append(PlanStep(
        kind=GUARD_CANDIDATE_WINDOW,
        release_step="BRANCH_PUSHED",
        summary="refuse a candidate whose diff window cannot trigger this project's CI",
        payload=dict(push_plan["window"]),
        capture=("window", ["git", "diff", "--name-only", "<base>..<candidate>"], git_root),
    ))
    if push_plan["needs_push"]:
        argv = list(push_plan["argv_prefix"]) + [
            StepRef(CANDIDATE_SHA, template=push_plan["refspec_template"]),
        ]
        steps.append(PlanStep(
            kind=PUSH_CANDIDATE,
            release_step="BRANCH_PUSHED",
            summary="run " + " ".join(_render_operand(a) for a in argv),
            payload={"argv": argv, "timeout": push_plan["timeout"]},
            marks=("BRANCH_PUSHED",),
        ))
    else:
        log("Skipping candidate push (remote already at the candidate)")
        presatisfied.append("BRANCH_PUSHED")

    return PhaseAPlan(
        steps=steps, files_to_commit=files_to_commit,
        presatisfied=tuple(presatisfied),
        candidate_sha=push_plan["local_head"] if not plan_commits else None,
    )


def _retarget_candidate(steps):
    """Move the ``candidate_sha`` production onto the LAST commit step.

    The snapshot commit, when there is one, lands after the release commit --
    so it, not the release commit, is the branch tip the push publishes and CI
    judges. Exactly one step ever produces the value.

    Returns True when the plan creates a commit at all.
    """
    commit_steps = [s for s in steps if s.kind in (COMMIT, SNAPSHOT)]
    if not commit_steps:
        return False
    last = commit_steps[-1]
    for i, step in enumerate(steps):
        if step.kind in (COMMIT, SNAPSHOT):
            steps[i] = dataclasses.replace(
                step, produces=(CANDIDATE_SHA if step is last else None),
            )
    return True


def _expected_dirty_files(inp, files_to_commit):
    """Paths the release itself is allowed to have dirtied, git-relative."""
    from .release_state import SCRUB_RESULT_FILENAME
    from .execute import _rel_to_git_root
    from ...changelog import get_changes_dir

    git_root = inp.git_root
    expected = {
        os.path.relpath(os.path.abspath(f), git_root) if os.path.isabs(f) else f
        for f in files_to_commit
    }
    expected.add(_rel_to_git_root(
        os.path.join(inp.project_dir, inp.lock_dir, "lock"), git_root,
    ))
    state_abs = os.path.abspath(inp.state_path)
    expected.add(os.path.relpath(state_abs, git_root))
    state_home = os.path.dirname(state_abs)
    expected.add(os.path.relpath(
        os.path.join(state_home, SCRUB_RESULT_FILENAME), git_root,
    ))
    expected.add(os.path.relpath(state_home, git_root) + "/")
    changes_dir = inp.state.changes_dir or get_changes_dir(inp.project_dir)
    validated_raw = os.path.normpath(os.path.join(changes_dir, ".validated"))
    expected.add(
        os.path.relpath(validated_raw, git_root)
        if os.path.isabs(validated_raw) else validated_raw
    )
    if inp.state.pre_existing_dirty:
        expected |= set(inp.state.pre_existing_dirty)
    return expected


def _commit_is_needed(inp, files_to_commit, log):
    """Decide -- by reading -- whether Phase A still owes a release commit."""
    if "COMMITTED" in inp.completed:
        log("Skipping commit (already done)")
        return False
    head_msg = _git_answer(["log", "-1", "--format=%s"], cwd=inp.git_root)
    if head_msg is not None and head_msg.strip() == inp.commit_msg:
        log("Skipping commit (HEAD already matches)")
        return False
    if not files_to_commit:
        log("No changes to commit")
        return False
    if inp.state.new_version == inp.state.current_version:
        # Nothing to bump, so the commit is owed only if one of the release's
        # own files actually differs.
        changed = False
        for path in files_to_commit:
            for argv in (
                ["diff", "--name-only", "--", path],
                ["status", "--porcelain", "--", path],
            ):
                answer = _git_answer(argv, cwd=inp.git_root)
                if answer is None or answer.strip():
                    changed = True
                    break
            if changed:
                break
        if not changed:
            log("No changes to commit")
            return False
    return True


def _snapshot_step(inp, log):
    """The monorepo-snapshot step, or None when the slot is not owed."""
    state = inp.state
    if "SNAPSHOT_REGENERATED" in inp.completed:
        log("Skipping snapshot regeneration (already done)")
        return None
    if "BRANCH_PUSHED" in inp.completed or "TAGGED" in inp.completed:
        # The pre-push slot is forfeit: a commit cannot be inserted before one
        # that is already on the remote. The post-hoc fallback regenerates it.
        log("Snapshot pre-push slot forfeit (candidate already pushed); "
            "will regenerate post-hoc")
        return None
    if not state.monorepo_name:
        return None
    return PlanStep(
        kind=SNAPSHOT,
        release_step="SNAPSHOT_REGENERATED",
        summary="regenerate and commit the monorepo snapshot",
        payload={"workspace_root": str(inp.monorepo_root)},
        marks=("SNAPSHOT_REGENERATED",),
        capture=(CANDIDATE_SHA, ["git", "rev-parse", "HEAD"], inp.git_root),
    )


def _candidate_push_plan(inp, *, plan_commits):
    """Derive the candidate push: whether it is owed, its argv, its window.

    Every question here is asked BEFORE the plan is issued -- what the remote
    branch points at, whether it exists at all, what the local tip is -- so the
    executor's push is a single recorded effect with no probing around it.
    """
    from . import get_push_timeout, DEFAULT_PUSH_TIMEOUT

    branch = inp.state.branch
    timeout = get_push_timeout(
        inp.ctx.config, override=inp.state.flags.get("push-timeout"),
    )
    if timeout != DEFAULT_PUSH_TIMEOUT:
        inp.log(
            f"Push timeout: {timeout}s (from --push-timeout or the "
            f"push_timeout config key)"
        )

    ls_out = _git_answer(
        ["ls-remote", "origin", f"refs/heads/{branch}"], cwd=inp.git_root,
    )
    # Three distinct answers, and they mean different things. A SHA: the branch
    # is on the remote at that commit. Empty: git answered, and the branch is
    # not there -- the push has to create it. None: unanswerable (a preview
    # past its first recorded mutation), so nothing is known about the remote.
    remote_head = ls_out.split()[0] if ls_out and ls_out.strip() else None
    remote_branch_absent = ls_out is not None and not ls_out.strip()

    local_head = _git_answer(["rev-parse", "HEAD"], cwd=inp.git_root)
    local_head = local_head.strip() if local_head else None

    # The candidate is the tip AFTER this plan's commits. A plan that commits
    # nothing leaves the tip where it is, so a remote already at that tip has
    # the candidate and the push is not owed.
    needs_push = plan_commits or not (
        remote_head and local_head and remote_head == local_head
    )

    argv_prefix = ["git", "push", "--no-verify"]
    if remote_branch_absent:
        argv_prefix.append("-u")
    argv_prefix.append("origin")

    return {
        "needs_push": needs_push,
        "argv_prefix": argv_prefix,
        "refspec_template": SLOT + ":refs/heads/" + branch,
        "timeout": timeout,
        "local_head": local_head,
        "window": {"remote_head": remote_head, "branch": branch},
    }


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def execute_phase_a_plan(plan, inp, *, preview):
    """Issue every step of *plan*.  Derives nothing; reads only through captures.

    Returns the resolved ``candidate_sha`` (a real SHA in live mode, the
    framework's carrier in preview mode, or None when the plan defers the push).

    Rollback is deliberately absent: a failing step raises, and the caller's
    existing revert handler -- ``git reset --hard`` to the pre-release pin plus
    orphan-artifact cleanup -- owns the undo. A preview executed nothing and
    needs none.
    """
    return _Executor(plan, inp, preview=preview).run()


class _Executor:
    """Walks a :class:`PhaseAPlan` and issues each step's declared effect."""

    def __init__(self, plan, inp, *, preview):
        self._plan = plan
        self._inp = inp
        self._preview = preview
        self._values: dict = {}
        if plan.candidate_sha:
            # A plan that commits nothing has no producing step; the candidate
            # is the tip the builder already resolved.
            self._values[CANDIDATE_SHA] = plan.candidate_sha
        self._issued: list = []
        # Per-step progress logging is LIVE-ONLY. In a preview those lines
        # ("Committed: v1.2.0", "Pushed the release candidate ...") would
        # narrate work that was recorded rather than done; the plan table the
        # preview prints instead says the same things in the past-conditional
        # they belong in.
        self._log = (lambda _msg: None) if preview else inp.log
        # Paths a target's writer REPORTED touching. Not a read of the world:
        # a writer's return value is part of the effect it just issued, and it
        # is the only statement of the files the builder could only predict a
        # lower bound for (see :func:`_predicted_version_files`).
        self._written: list = []

    # -- the executor's ONLY read ------------------------------------------

    def _capture(self, step):
        """Run a step's DECLARED result-capture observe and return its output.

        This is the executor's sole permitted read, and it exists because a
        step's own result -- the SHA a commit creates, the porcelain a guard
        judges -- cannot be known before the step runs. In preview mode nothing
        is captured: the step issued a recorded effect, not a real one, so the
        honest stand-in is the carrier that effect returned (see
        :meth:`_resolve`).
        """
        from . import run

        name, argv, cwd = step.capture
        try:
            out = run(argv[0], list(argv[1:]), cwd=cwd)
        except Exception:
            return None
        if effects.unsettled(out) or not isinstance(out, str):
            return None
        return out

    # -- value threading ----------------------------------------------------

    def _resolve(self, operand):
        """Resolve a payload operand, threading :class:`StepRef` placeholders."""
        if isinstance(operand, StepRef):
            value = self._values.get(operand.name)
            if effects.unsettled(value):
                # Preview: forward the carrier whole. The framework renders it
                # as «step N output», which is exactly what it is.
                return value
            return operand.template.replace(SLOT, value or "")
        if isinstance(operand, list):
            return [self._resolve(item) for item in operand]
        return operand

    # -- the walk -----------------------------------------------------------

    def run(self):
        from .release_state import save_step

        for step in self._plan.steps:
            issued = self._issue(step)
            self._issued.append(step)
            self._settle(step, issued)
            for mark in step.marks:
                save_step(self._inp.state_path, mark)
                self._inp.completed.add(mark)
        for mark in self._plan.presatisfied:
            save_step(self._inp.state_path, mark)
            self._inp.completed.add(mark)
        return self._values.get(CANDIDATE_SHA)

    def _settle(self, step, issued):
        """Resolve a step's declared capture and thread whatever it produced.

        Guard steps run their own capture inside their handler (the guard IS
        the reading), so they are settled there and skipped here.
        """
        if step.capture is None or step.kind in _GUARD_KINDS:
            return
        if self._preview:
            if step.produces:
                # Nothing ran, so nothing was captured: the honest stand-in is
                # the carrier the step's own recorded effect returned.
                self._values[step.produces] = issued
            return
        captured = (self._capture(step) or "").strip()
        if step.produces:
            self._values[step.produces] = captured
        if step.kind in (COMMIT, SNAPSHOT):
            # The drift guard's trail. ``_track_release_commit`` resolves HEAD
            # itself, through the same direct ``effects.run`` the guard uses to
            # read the range -- the two must name commits the same way or the
            # release's own commit looks foreign to its own guard.
            from .execute import _track_release_commit

            _track_release_commit(self._inp.state_path)

    def _issue(self, step):
        """Issue one step's effect. Returns the carrier/result it produced."""
        handler = _HANDLERS[step.kind]
        if step.kind in _GUARD_KINDS and self._preview:
            # A preview executed nothing, so there is nothing to guard.
            return CARRIER
        return handler(self, step)

    # -- per-kind implementations ------------------------------------------

    def _do_write_releasable_version(self, step):
        from ...workspace import write_releasable_version

        p = step.payload
        write_releasable_version(p["workspace_root"], p["releasable"], p["version"])
        self._log(
            f"Updated releasable version: {p['releasable']} -> {p['version']}"
        )
        return None

    def _record_written(self, base_path, relatives):
        """Remember the files a writer reported touching, git-root-relative."""
        from .execute import _rel_to_git_root

        for rel in relatives or ():
            fpath = _rel_to_git_root(
                os.path.join(base_path, rel), self._inp.git_root,
            )
            if fpath not in self._written:
                self._written.append(fpath)

    def _do_write_target_version(self, step):
        from . import TARGETS

        p = step.payload
        modified = TARGETS[p["target"]].write_version(
            p["path"], p["version"], ctx=self._inp.ctx,
        )
        self._record_written(p["path"], modified)
        if modified:
            verb = "Updated version in" if p["primary"] else "Synced version to"
            self._log(f"{verb} {', '.join(modified)}")
        return None

    def _do_write_member_versions(self, step):
        from . import TARGETS

        for entry in step.payload["entries"]:
            modified = TARGETS[entry["target"]].write_version(
                entry["path"], step.payload["version"], ctx=self._inp.ctx,
            )
            self._record_written(entry["path"], modified)
            if modified:
                self._log(
                    f"Synced version to member {entry['package_path']}: "
                    f"{', '.join(modified)}"
                )
        return None

    def _do_bump_selfdoc(self, step):
        effects.atomic_write_text(
            step.payload["path"], step.payload["content"], file_mode=0o600,
        )
        self._log("Synced version to selfdoc.json")
        return None

    def _do_ensure_keyword(self, step):
        from ...tagging import ensure_npm_keyword, ensure_pypi_keyword
        from ...utils import warn_exception

        p = step.payload
        fn = ensure_npm_keyword if p["kind"] == "npm" else ensure_pypi_keyword
        try:
            fn(p["path"], quiet=self._inp.state.quiet,
               project_root=self._inp.ctx.project_root)
        except Exception as e:
            warn_exception(f"{p['kind']} ecosystem tagging failed", e)
        return None

    def _do_sync_lockfile(self, step):
        from . import subprocess as _subprocess

        p = step.payload
        try:
            effects.run(
                p["cmd"], cwd=p["cwd"], timeout=p["timeout"],
                check=True, capture_output=True,
            )
        except (_subprocess.CalledProcessError, _subprocess.TimeoutExpired,
                OSError) as e:
            self._log(f"Warning: {p['lockfile']} sync failed: {e}")
        return None

    def _do_write_marker(self, step):
        from ...utils import warn_exception

        try:
            with effects.open_write(step.payload["path"], "w") as f:
                f.write(step.payload["content"])
        except Exception as e:
            warn_exception("writing .rlsbl/version marker failed", e)
        return None

    def _do_clean_artifacts(self, step):
        from ...secret_scan import clean_stale_artifacts

        clean_stale_artifacts(
            step.payload["project_dir"], log=self._inp.log,
            target_paths=step.payload["target_paths"],
        )
        return None

    def _do_build(self, step):
        from . import TARGETS

        p = step.payload
        TARGETS[p["target"]].build(
            p["path"], p["version"],
            config=self._inp.ctx.config if self._inp.ctx else None,
        )
        return None

    def _do_secret_scan(self, step):
        from .execute import ReleaseAbortError
        from ...secret_scan import scan_artifacts_for_secrets, SecretScanError

        if self._preview:
            # The build above was RECORDED, not run, so there are no artifacts
            # of this release to scan. Unpacking whatever a previous real build
            # left in dist/ would be a preview inventing its own inputs, and
            # writing the unpacked tree would be a preview writing to disk.
            self._log(
                "Secret scan: not previewable "
                "(it scans artifacts the recorded build did not produce)"
            )
            return None
        try:
            scan_artifacts_for_secrets(
                step.payload["project_dir"], log=self._inp.log,
                target_paths=step.payload["target_paths"],
            )
        except SecretScanError as e:
            raise ReleaseAbortError(str(e))
        return None

    def _do_guard_unexpected_files(self, step):
        from .execute import ReleaseAbortError
        from .validate import parse_porcelain_paths

        porcelain = self._capture(step)
        if not porcelain:
            return None
        dirty = parse_porcelain_paths(porcelain)
        expected = set(step.payload["expected"]) | set(self._written)
        unexpected = dirty - expected - step.payload["baseline"]
        if unexpected:
            raise ReleaseAbortError(
                f"Unexpected modified files detected (possible concurrent "
                f"change): {', '.join(sorted(unexpected))}. Aborting release."
            )
        return None

    def _do_commit(self, step):
        from . import commit_files

        p = step.payload
        files = list(p["files"])
        for extra in self._written:
            if extra not in files:
                files.append(extra)
        result = commit_files(
            p["message"], files, cwd=p["cwd"],
            autogenerated=p["autogenerated"], return_result=True,
        )
        self._log(f"Committed: {p['message']}")
        return result

    def _do_snapshot(self, step):
        from . import commit_files_if_changed, load_workspace
        from ...snapshot import generate_snapshot, write_snapshot
        from ...workspace_graph import WorkspaceGraph

        root = step.payload["workspace_root"]
        projects = load_workspace(root)
        graph = WorkspaceGraph(root, projects)
        rel_path = write_snapshot(root, generate_snapshot(root, projects, graph))
        result = None
        if self._preview:
            # commit_files_if_changed's emptiness probe is an observe, and the
            # write above was recorded rather than performed -- so the probe
            # would answer about a file that was never written. The commit is
            # recorded unconditionally instead.
            from . import commit_files

            result = commit_files(
                "snapshot", [rel_path], autogenerated=True, cwd=root,
                return_result=True,
            )
        else:
            commit_files_if_changed(
                "snapshot", [rel_path], skip_message="Snapshot unchanged.",
                autogenerated=True, cwd=root,
            )
        self._log(f"Regenerated monorepo snapshot: {rel_path}")
        return result

    def _do_guard_foreign_commits(self, step):
        from .execute import _guard_foreign_commits

        _guard_foreign_commits(
            self._inp.pin_sha, self._inp.state_path, cwd=self._inp.git_root,
            phase=step.payload["phase"],
        )
        return None

    def _do_guard_candidate_window(self, step):
        from .execute import _guard_empty_candidate_window

        state = self._inp.state
        _guard_empty_candidate_window(
            candidate_sha=self._values.get(CANDIDATE_SHA),
            remote_head=step.payload["remote_head"],
            needs_push=True,
            state_path=self._inp.state_path,
            monorepo_root=self._inp.monorepo_root,
            monorepo_name=state.monorepo_name,
            releasable_name=state.releasable_name,
            version=state.new_version, tag=state.tag,
            branch=step.payload["branch"],
            cwd=self._inp.git_root, log=self._inp.log,
        )
        return None

    def _do_record_candidate(self, step):
        """Write the candidate SHA into the release-state file.

        The document is assembled as a TEMPLATE with :data:`SLOT` where the SHA
        goes, so the SHA reaches the write as a single operand. Under a preview
        that operand is the commit step's carrier, and the whole document is
        recorded as ``write: <state file> («step N output»)`` -- a write that
        names the value it is waiting on instead of inventing one.
        """
        import json

        from .release_state import load_release_state

        path = step.payload["path"]
        state_dict = load_release_state(path) or {}
        state_dict["candidate_sha"] = SLOT
        template = json.dumps(state_dict, indent=2) + "\n"
        content = self._resolve(
            dataclasses.replace(step.payload["ref"], template=template)
        )
        effects.atomic_write_text(path, content, file_mode=0o600)
        return None

    def _do_push_candidate(self, step):
        """Publish the candidate to the release branch.

        The one step where the StepRef seam is visible from both sides. With a
        settled SHA the push goes through :func:`~rlsbl.utils.push_if_needed`,
        which keeps its own idempotent skip (a remote already at the candidate
        is not pushed again). With the carrier -- a preview, where the commit
        that would produce the SHA was recorded rather than made -- there is
        nothing to compare it against, so the push is issued straight onto the
        chokepoint with the carrier as its own argv operand. It records as
        ``git push --no-verify origin «step N output»``: the push of a commit
        that does not exist, named by the step that would have created it.
        """
        from . import push_if_needed

        argv = self._resolve(step.payload["argv"])
        sha = argv[-1]
        if effects.unsettled(sha):
            effects.run(
                argv, timeout=step.payload["timeout"], cwd=self._inp.git_root,
            )
        else:
            push_if_needed(
                self._inp.state.branch, config=self._inp.ctx.config,
                cwd=self._inp.project_dir,
                sha=self._values.get(CANDIDATE_SHA),
            )
        self._log(
            f"Pushed the release candidate to origin/{self._inp.state.branch} "
            f"(untagged)"
        )
        return None


_HANDLERS = {
    WRITE_RELEASABLE_VERSION: _Executor._do_write_releasable_version,
    WRITE_TARGET_VERSION: _Executor._do_write_target_version,
    WRITE_MEMBER_VERSIONS: _Executor._do_write_member_versions,
    BUMP_SELFDOC: _Executor._do_bump_selfdoc,
    ENSURE_KEYWORD: _Executor._do_ensure_keyword,
    SYNC_LOCKFILE: _Executor._do_sync_lockfile,
    WRITE_MARKER: _Executor._do_write_marker,
    CLEAN_ARTIFACTS: _Executor._do_clean_artifacts,
    BUILD: _Executor._do_build,
    SECRET_SCAN: _Executor._do_secret_scan,
    GUARD_UNEXPECTED_FILES: _Executor._do_guard_unexpected_files,
    COMMIT: _Executor._do_commit,
    SNAPSHOT: _Executor._do_snapshot,
    GUARD_FOREIGN_COMMITS: _Executor._do_guard_foreign_commits,
    GUARD_CANDIDATE_WINDOW: _Executor._do_guard_candidate_window,
    RECORD_CANDIDATE: _Executor._do_record_candidate,
    PUSH_CANDIDATE: _Executor._do_push_candidate,
}

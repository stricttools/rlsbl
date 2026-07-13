"""Resolved-plan sidecar for monorepo batch releases.

A batch release file (``.rlsbl-monorepo/releases/unreleased.toml``) records
only *bump intents* -- it does not persist the pre-batch base version of each
item. That is fatal for idempotent, resumable batch releases: once an item has
been released (its version bumped from V0 to V1 and tag(V1) created), it is
observationally identical to a *pending* item that happens to sit at V1 with
its prior tag. No function of (live version, existing tags, bump type) can tell
the two apart. See the investigation notes: the base version must be persisted.

This module persists a *resolved plan* alongside the batch file:
``.rlsbl-monorepo/releases/unreleased.plan.json``. It is computed exactly once,
at the start of the first ``monorepo release run`` against a given
``unreleased.toml`` (before any item is released), and captures for every item:

- ``name`` -- releasable or package name
- ``base_version`` -- the live version at plan time
- ``target_version`` -- the version the release will produce (bump applied)
- ``tag`` -- the exact git tag string the release will create
- ``registry`` -- the primary target registry (drives live-version reads)
- ``bump`` -- the bump intent (used to validate reuse against the batch file)

On any subsequent run the plan is *validated and reused*, never regenerated:
regenerating would recompute base versions from drifted state -- the exact bug
this design closes. The per-item skip predicate and the archive-as-repair gate
are both computed against the persisted plan.
"""

import json
import os
import stat
from dataclasses import dataclass

from ...utils import tag_exists_locally


PLAN_FILENAME = "unreleased.plan.json"


class BatchPlanError(Exception):
    """Raised when a persisted plan is malformed or inconsistent with the
    current batch release file (item set / bump intent mismatch)."""


@dataclass
class PlanItem:
    """A single item in a resolved batch plan, capturing the frozen
    base version, target version, tag, registry, and bump intent."""

    name: str
    base_version: str
    target_version: str
    tag: str
    registry: str
    bump: str


@dataclass
class BatchPlan:
    """The full resolved plan for a batch release, containing the
    section type and a mapping of item names to their PlanItem entries."""

    section_type: str  # "packages" or "releasables"
    items: dict[str, PlanItem]  # name -> PlanItem


def get_batch_plan_path(workspace_root: str = ".") -> str:
    """Return the path to .rlsbl-monorepo/releases/unreleased.plan.json."""
    return os.path.join(
        workspace_root, ".rlsbl-monorepo", "releases", PLAN_FILENAME
    )


def plan_exists(workspace_root: str) -> bool:
    """Return True if a resolved plan sidecar exists on disk."""
    return os.path.exists(get_batch_plan_path(workspace_root))


def write_batch_plan(path: str, plan: BatchPlan) -> None:
    """Atomically write the resolved plan to ``path`` (tmp file + rename)."""
    payload = {
        "section_type": plan.section_type,
        "items": [
            {
                "name": it.name,
                "base_version": it.base_version,
                "target_version": it.target_version,
                "tag": it.tag,
                "registry": it.registry,
                "bump": it.bump,
            }
            for it in plan.items.values()
        ],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def read_batch_plan(path: str) -> BatchPlan:
    """Read and parse a resolved plan JSON file.

    Raises BatchPlanError on malformed content.
    """
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise BatchPlanError(f"batch plan file {path} is not valid JSON: {e}")

    if not isinstance(data, dict):
        raise BatchPlanError(f"batch plan file {path} must be a JSON object")

    section_type = data.get("section_type")
    if section_type not in ("packages", "releasables"):
        raise BatchPlanError(
            f"batch plan file {path} has invalid section_type {section_type!r}"
        )

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise BatchPlanError(f"batch plan file {path} 'items' must be a list")

    items: dict[str, PlanItem] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise BatchPlanError(f"batch plan item must be an object: {entry!r}")
        for key in ("name", "base_version", "target_version", "tag", "registry", "bump"):
            if key not in entry:
                raise BatchPlanError(
                    f"batch plan item missing required field {key!r}: {entry!r}"
                )
        items[entry["name"]] = PlanItem(
            name=entry["name"],
            base_version=entry["base_version"],
            target_version=entry["target_version"],
            tag=entry["tag"],
            registry=entry["registry"],
            bump=entry["bump"],
        )

    return BatchPlan(section_type=section_type, items=items)


def _noop_log(_msg):
    pass


def compute_batch_plan(workspace_root, batch_config, projects):
    """Resolve base/target/tag for every batch item and return a BatchPlan.

    Delegates to the same ``compute_release_version`` used by the real release
    flow, so the plan captures exactly what a release started *now* would
    produce. In particular, ``compute_release_version`` raises
    ``ReleaseValidationError`` when a target tag already exists -- the caller
    treats that as evidence of a partially-executed plan-less batch.
    """
    from ..release.validate import compute_release_version
    from ..release.execute import resolve_target_paths
    from ...targets import TARGETS

    section_type = batch_config.section_type

    if section_type == "releasables":
        from ...workspace import load_releasables, members_of
        from ...workspace_types import get_releasable_dir

        releasables = load_releasables(workspace_root, projects)
        releasable_by_name = {r.name: r for r in releasables}

        items: dict[str, PlanItem] = {}
        for name, rc in batch_config.packages.items():
            rel = releasable_by_name[name]
            member_projs = members_of(name, projects)
            representative = member_projs[0]
            project_dir = os.path.join(workspace_root, representative["path"])
            registry = rc.include[0]
            target = TARGETS[registry]
            rel_cfg_dir = get_releasable_dir(workspace_root, name)
            target_paths = resolve_target_paths(
                project_dir, releasable_config_dir=rel_cfg_dir
            )
            primary_path = target_paths.get(registry, project_dir)
            current, new, _bump_type, tag = compute_release_version(
                target, primary_path, rc.bump,
                None, None, _noop_log,
                workspace_root=workspace_root, releasable_name=name,
                releasable_tag_fmt=rel.tag_format, preid=rc.preid,
            )
            items[name] = PlanItem(
                name=name, base_version=current, target_version=new,
                tag=tag, registry=registry, bump=rc.bump,
            )
        return BatchPlan(section_type=section_type, items=items)

    # package mode (implicit)
    project_by_name = {p["name"]: p for p in projects}
    items = {}
    for name, rc in batch_config.packages.items():
        project = project_by_name[name]
        project_dir = os.path.join(workspace_root, project["path"])
        registry = rc.include[0]
        target = TARGETS[registry]
        target_paths = resolve_target_paths(project_dir)
        primary_path = target_paths.get(registry, project_dir)
        current, new, _bump_type, tag = compute_release_version(
            target, primary_path, rc.bump,
            project["name"], project["path"], _noop_log,
            preid=rc.preid,
        )
        items[name] = PlanItem(
            name=name, base_version=current, target_version=new,
            tag=tag, registry=registry, bump=rc.bump,
        )
    return BatchPlan(section_type=section_type, items=items)


def validate_plan_against_config(plan: BatchPlan, batch_config) -> None:
    """Validate a persisted plan matches the current batch file's intent.

    The plan is reused across runs and must never be regenerated mid-flight.
    Reuse is only valid when the plan describes the same set of items with the
    same bump intents as the batch file. Base versions may legitimately differ
    from live state (an item may already be partially released), so those are
    NOT compared here.

    Raises BatchPlanError on any mismatch.
    """
    if plan.section_type != batch_config.section_type:
        raise BatchPlanError(
            f"batch plan section_type {plan.section_type!r} does not match "
            f"batch file section_type {batch_config.section_type!r}"
        )

    plan_names = set(plan.items.keys())
    config_names = set(batch_config.packages.keys())
    if plan_names != config_names:
        only_plan = sorted(plan_names - config_names)
        only_config = sorted(config_names - plan_names)
        details = []
        if only_config:
            details.append(f"in batch file but not in plan: {', '.join(only_config)}")
        if only_plan:
            details.append(f"in plan but not in batch file: {', '.join(only_plan)}")
        raise BatchPlanError(
            "batch plan item set does not match the batch release file "
            f"({'; '.join(details)}). The plan is never regenerated mid-flight. "
            "Resolve by finishing the in-flight batch, or delete the plan and "
            "batch file and re-run `rlsbl monorepo release init`."
        )

    for name, rc in batch_config.packages.items():
        if plan.items[name].bump != rc.bump:
            raise BatchPlanError(
                f"batch plan bump for {name!r} ({plan.items[name].bump!r}) does "
                f"not match the batch release file ({rc.bump!r}). The plan is "
                "never regenerated mid-flight. Delete the plan and batch file "
                "and re-run `rlsbl monorepo release init` to change bump intents."
            )


def read_live_version(workspace_root, item: PlanItem, projects, section_type):
    """Read the current on-disk version for a plan item.

    Returns the version string, or None if it cannot be read (e.g. a manifest
    is missing). A None result makes the skip predicate treat the item as not
    yet released, so it proceeds through the normal release path.
    """
    try:
        if section_type == "releasables":
            from ...workspace import read_releasable_version
            return read_releasable_version(workspace_root, item.name)

        from ..release.execute import resolve_target_paths
        from ...targets import TARGETS

        project_by_name = {p["name"]: p for p in projects}
        project = project_by_name[item.name]
        project_dir = os.path.join(workspace_root, project["path"])
        target = TARGETS[item.registry]
        target_paths = resolve_target_paths(project_dir)
        primary_path = target_paths.get(item.registry, project_dir)
        # read_version is part of the concrete target protocol; BaseTarget does
        # not declare it (same pattern as compute_release_version).
        return target.read_version(primary_path)  # type: ignore[attr-defined]
    except Exception:
        return None


def item_is_released(workspace_root, item: PlanItem, projects, section_type) -> bool:
    """Skip predicate: True iff the item's release provably already happened.

    An item is released iff its live version equals the plan's target_version
    AND the plan's tag exists locally. Any other state (version mismatch, tag
    absent) means the item is not verifiably released and must proceed -- a
    genuinely inconsistent intermediate state will then fail loudly downstream.
    """
    live = read_live_version(workspace_root, item, projects, section_type)
    if live is None:
        return False
    return live == item.target_version and tag_exists_locally(item.tag)


def plan_all_released(workspace_root, plan: BatchPlan, projects) -> bool:
    """True iff every plan item satisfies the skip predicate."""
    return all(
        item_is_released(workspace_root, it, projects, plan.section_type)
        for it in plan.items.values()
    )


def archive_plan_file(plan_path: str, versioned_stem: str) -> list[str]:
    """Archive the plan sidecar next to the archived batch file.

    ``versioned_stem`` is the batch file's archived stem (e.g.
    ``batch-20260713-101500``); the plan is renamed to
    ``<stem>.plan.json`` and chmod'd read-only. Returns the list of changed
    paths (for committing), or [] if no plan file exists.
    """
    if not os.path.exists(plan_path):
        return []
    releases_dir = os.path.dirname(plan_path)
    versioned_plan = os.path.join(releases_dir, f"{versioned_stem}.plan.json")
    os.rename(plan_path, versioned_plan)
    os.chmod(versioned_plan, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 444
    return [os.path.normpath(versioned_plan), os.path.normpath(plan_path)]

"""Resolution of a member's release surface into ``ResolvedTarget`` records.

Targets and pipelines are configured separately-but-linked (Phase 6.1): each
pipeline entry declares an explicit ``target`` link (a target name, or ``null``
for a target-less publisher such as a docs/site deploy). This module turns that
linked configuration into the flat, per-pipeline view the release flow wants.

A :class:`ResolvedTarget` is one publishable (target, pipeline) pair:

- A target served by N pipelines produces N ResolvedTargets -- the release flow
  publishes once per pipeline, so per-pair records let it iterate directly
  without re-deriving the target for each pipeline.
- A pipeline-less target (e.g. ``plain`` / ``spec``, which version-bump but do
  not publish through a pipeline) produces a single ResolvedTarget with
  ``pipeline=None``.

Target-less pipelines (``target: null``) are NOT targets and must never be
faked into a ResolvedTarget. They are surfaced separately as ``deploys`` by
:func:`partition_pipelines` (and ``MemberContext.deploy_pipelines``).

Kept dependency-light: imports only :mod:`rlsbl.errors`. The caller supplies
already-detected targets and already-loaded pipelines, so this module never
touches disk or the pipeline registry.
"""

from dataclasses import dataclass
from typing import Any, Optional

from .errors import ConfigError


@dataclass(frozen=True)
class ResolvedTarget:
    """One publishable (target, pipeline) pair.

    Fields:
        target: the detected target entry (a ``TargetEntry``).
        path: the target's resolved directory path (same value as
            ``MemberContext.target_paths[name]`` -- carried here so consumers
            need not re-resolve it).
        pipeline: the linked pipeline object, or ``None`` for a pipeline-less
            target (version-bump-only targets like ``plain`` / ``spec``).
        publish_mode: the member's effective ``publish_mode`` string, carried
            verbatim from config (``"ci"`` / ``"none"``) -- never collapsed to a
            private boolean.
        artifact_kind: the pipeline config's ``artifact`` value where present
            (e.g. Go's ``library`` / ``binary``, wired in a later phase), or
            ``None``. Carried as the raw config value.
    """

    target: Any
    path: str
    pipeline: Optional[Any]
    publish_mode: str
    artifact_kind: Optional[Any]

    @property
    def name(self) -> str:
        """The target's name (convenience for ``self.target.name``)."""
        return self.target.name


def _pipeline_target_link(pipeline) -> Optional[str]:
    """The pipeline's explicit target link (Phase 6.1's ``.target`` attribute).

    ``load_pipelines`` sets ``instance.target`` from the entry's ``target`` key
    (a target name string, or ``None`` for a target-less publisher). This is the
    single point of coupling to that wiring; ``getattr`` keeps the resolver from
    crashing un-actionably on a pipeline built outside ``load_pipelines``.
    """
    return getattr(pipeline, "target", None)


def partition_pipelines(pipelines: dict) -> tuple[dict, list]:
    """Split loaded pipelines by their target link.

    Returns ``(by_target, deploys)`` where:

    - ``by_target`` maps a target name to the list of pipelines linking it
      (insertion order preserved -- a target served by multiple pipelines keeps
      its pipelines in config order).
    - ``deploys`` is the list of target-less pipelines (``target: null``), in
      config order.

    Args:
        pipelines: the dict returned by ``load_pipelines`` (name -> pipeline).
    """
    by_target: dict[str, list] = {}
    deploys: list = []
    for pipeline in pipelines.values():
        link = _pipeline_target_link(pipeline)
        if link is None:
            deploys.append(pipeline)
        else:
            by_target.setdefault(link, []).append(pipeline)
    return by_target, deploys


def resolve_targets(targets: list, pipelines: dict, publish_mode: str) -> list:
    """Resolve targets + linked pipelines into a flat list of ResolvedTargets.

    One :class:`ResolvedTarget` is produced per (target, linked pipeline) pair.
    A pipeline-less target yields a single record with ``pipeline=None``.
    Target-less pipelines are excluded here (see ``partition_pipelines`` /
    ``deploys``).

    Args:
        targets: detected target entries (list of ``TargetEntry``).
        pipelines: loaded pipelines (dict name -> pipeline, from
            ``load_pipelines``).
        publish_mode: the member's effective ``publish_mode`` string, carried
            verbatim onto every produced record.

    Raises:
        ConfigError: if a pipeline links a target name absent from ``targets``
            (a dangling reference). ``validate_pipeline_target_links`` catches
            this earlier at config-validation time; this is a loud backstop so
            the resolver never produces a silently-incomplete view from
            unvalidated input.
    """
    by_target, _deploys = partition_pipelines(pipelines)

    target_names = {t.name for t in targets}
    for link in by_target:
        if link not in target_names:
            known = ", ".join(sorted(target_names)) or "(none configured)"
            offenders = ", ".join(p.name for p in by_target[link])
            raise ConfigError(
                f"pipeline(s) [{offenders}] link a target '{link}' that is not "
                f"among the configured targets. Configured targets: {known}. "
                f"Add '{link}' to targets, correct the link, or set the "
                'pipeline\'s "target": null if it publishes no release target.'
            )

    resolved: list = []
    for target in targets:
        linked = by_target.get(target.name, [])
        if not linked:
            resolved.append(
                ResolvedTarget(
                    target=target,
                    path=target.path,
                    pipeline=None,
                    publish_mode=publish_mode,
                    artifact_kind=None,
                )
            )
            continue
        for pipeline in linked:
            artifact_kind = getattr(pipeline, "config", {}).get("artifact")
            resolved.append(
                ResolvedTarget(
                    target=target,
                    path=target.path,
                    pipeline=pipeline,
                    publish_mode=publish_mode,
                    artifact_kind=artifact_kind,
                )
            )
    return resolved


__all__ = ["ResolvedTarget", "partition_pipelines", "resolve_targets"]

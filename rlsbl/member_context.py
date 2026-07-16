"""Shared resolution of a member package's effective config and targets, merging releasable-level and per-package overrides for release pipelines and hooks.

In explicit releasable mode, a member package's effective configuration is
the releasable-level config.json merged with the per-package config.json
(per-package wins), and target detection must use the same inheritance so
releasable-level ``publish_mode`` / ``targets`` apply to members that don't
set them. This module is the single source of truth for that resolution --
every code path that decides whether a member is published (version sync,
companion tags, workspace checks, primary path resolution) must go through
it so they all agree on the member set.

Kept dependency-light (only rlsbl.config and rlsbl.targets, both imported
lazily) so it is importable from both rlsbl.checks.* and
rlsbl.commands.release.* without circular imports.
"""

from functools import cached_property


class MemberContext:
    """A member package's effective (releasable-inherited) config and targets.

    ``targets`` is computed lazily: detect_targets() can raise ConfigError
    (config file present but no targets key anywhere), and consumers that
    skip publish-suppressed members must be able to check ``publish_mode``
    without triggering target detection.
    """

    def __init__(self, member_dir, releasable_config_dir=None):
        self.member_dir = member_dir
        self.releasable_config_dir = releasable_config_dir

        from .config import read_project_config

        self.config = read_project_config(
            member_dir, releasable_config_dir=releasable_config_dir,
        )

    @property
    def publish_mode(self) -> str:
        """The member's effective publish_mode (one of ``"ci"`` / ``"none"``).

        Required-read: raises :class:`ConfigError` when the key is absent or
        invalid (with releasable-level inheritance already applied). Consumers
        derive the old boolean via ``publish_mode == "none"``.
        """
        from .config import get_publish_mode

        return get_publish_mode(self.config)

    @cached_property
    def targets(self) -> list:
        """Detected targets (list of TargetEntry), with releasable inheritance."""
        from .targets import detect_targets

        return detect_targets(
            self.member_dir, releasable_config_dir=self.releasable_config_dir,
        )

    @property
    def target_paths(self) -> dict:
        """Dict mapping target name -> resolved directory path."""
        return {e.name: e.path for e in self.targets}

    @cached_property
    def pipelines(self) -> dict:
        """Loaded publish pipelines (name -> pipeline), from the merged config.

        Each pipeline carries an explicit ``target`` link (Phase 6.1): a target
        name, or ``None`` for a target-less publisher (deploy). Empty dict when
        the config declares no ``pipelines``.
        """
        from .pipelines import load_pipelines

        return load_pipelines(self.config)

    @cached_property
    def resolved_targets(self) -> list:
        """List of :class:`ResolvedTarget`, one per (target, linked pipeline).

        Pipeline-less targets resolve with ``pipeline=None``; a target served by
        multiple pipelines produces one record per pipeline (the release flow
        publishes per pipeline). Target-less pipelines are excluded -- see
        :attr:`deploy_pipelines`.

        Additive: existing consumers keep using ``targets`` / ``target_paths`` /
        ``publish_mode`` unchanged.
        """
        from .resolved_target import resolve_targets

        return resolve_targets(self.targets, self.pipelines, self.publish_mode)

    @property
    def deploy_pipelines(self) -> list:
        """Target-less publishers (``target: null``), in config order.

        These are deploys (e.g. a docs/site publish), not release targets, so
        they are surfaced separately -- never faked into a ResolvedTarget.
        """
        from .resolved_target import partition_pipelines

        _by_target, deploys = partition_pipelines(self.pipelines)
        return deploys


def resolve_member_context(member_dir, releasable_config_dir=None) -> MemberContext:
    """Resolve a member directory's effective merged config and detected targets.

    Args:
        member_dir: path to the member package directory.
        releasable_config_dir: optional path to the releasable's state
            directory (e.g. ``.rlsbl-monorepo/releasables/<name>/``) for
            config inheritance. None for standalone/implicit-mode projects.
    """
    return MemberContext(member_dir, releasable_config_dir=releasable_config_dir)

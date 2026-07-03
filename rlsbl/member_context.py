"""Shared resolution of a member package's effective config and targets, merging releasable-level and per-package overrides for release pipelines and hooks.

In explicit releasable mode, a member package's effective configuration is
the releasable-level config.json merged with the per-package config.json
(per-package wins), and target detection must use the same inheritance so
releasable-level ``private`` / ``targets`` apply to members that don't set
them. This module is the single source of truth for that resolution --
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
    skip private members must be able to check ``is_private`` without
    triggering target detection.
    """

    def __init__(self, member_dir, releasable_config_dir=None):
        self.member_dir = member_dir
        self.releasable_config_dir = releasable_config_dir

        from .config import read_project_config

        self.config = read_project_config(
            member_dir, releasable_config_dir=releasable_config_dir,
        )

    @property
    def is_private(self) -> bool:
        """Whether the member is private (defaults to True when unset)."""
        return bool(self.config.get("private", True))

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


def resolve_member_context(member_dir, releasable_config_dir=None) -> MemberContext:
    """Resolve a member directory's effective merged config and detected targets.

    Args:
        member_dir: path to the member package directory.
        releasable_config_dir: optional path to the releasable's state
            directory (e.g. ``.rlsbl-monorepo/releasables/<name>/``) for
            config inheritance. None for standalone/implicit-mode projects.
    """
    return MemberContext(member_dir, releasable_config_dir=releasable_config_dir)

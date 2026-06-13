"""Custom exception hierarchy for rlsbl, providing typed error classes for config, workspace, changelog, version, and release file failures."""


class RlsblError(Exception):
    """Base exception for all rlsbl errors."""


class ConfigError(RlsblError):
    """Invalid config.json, pipeline, or deploy configuration."""


class WorkspaceError(RlsblError):
    """Invalid workspace.toml or layers configuration."""


class ChangelogError(RlsblError):
    """Invalid JSONL changelog entry or schema."""


class VersionError(RlsblError):
    """Version not found in manifest or semver parse failure."""


class ReleaseFileError(RlsblError):
    """Invalid unreleased.toml, retry.toml, or batch release file."""

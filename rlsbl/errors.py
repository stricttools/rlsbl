"""Custom exception hierarchy for rlsbl, providing typed error classes for config, workspace, changelog, version, and release file failures."""


class RlsblError(Exception):
    """Base exception for all rlsbl errors."""


class ConfigError(RlsblError):
    """Invalid config.json, pipeline, or deploy configuration."""


class WorkspaceError(RlsblError):
    """Invalid workspace.toml or layers configuration."""


class MixedTagSchemeError(RlsblError):
    """A member's targets span both monorepo tag schemes.

    Raised when a releasable's tag format is being derived from a member whose
    targets tag under both the ``{name}@v{version}`` and the ``{path}/v{version}``
    scheme: there is no single format to derive, and the operator must state one.
    """


class ChangelogError(RlsblError):
    """Invalid JSONL changelog entry or schema."""


class VersionError(RlsblError):
    """Version not found in manifest or semver parse failure."""


class ReleaseFileError(RlsblError):
    """Invalid unreleased.toml, retry.toml, or batch release file."""


class LedgerError(RlsblError):
    """The release ledger could not be read for use.

    Raised for the three ways an archived release file fails the reader that
    needs its anchor: the version's tag disagrees with the anchor, the anchor's
    ancestry cannot be determined, or the archive carries neither an anchor nor
    the ``unanchorable`` marker.
    """


class GitError(RlsblError):
    """Git infrastructure failure (detached HEAD, shallow clone, push timeout)."""


class PostReleaseError(RlsblError):
    """Post-mutation failure where the error is already printed and no rollback is possible."""

"""Publication probe protocol for registry-aware version checking.

Targets that support publication probing can determine whether a specific
version of a package has been published to a registry. This is used by
``rlsbl release yank`` to decide which registry-specific removal actions
to take, and by ``rlsbl release undo`` to gate non-latest undo operations.
"""

import enum


class PublicationStatus(enum.Enum):
    """Tri-state result of a publication probe.

    PUBLISHED: the version exists on the registry and is publicly available.
    UNPUBLISHED: the version does not exist on the registry (safe to skip
        registry-level yank actions).
    UNPROBEABLE: the target cannot probe the registry (e.g. no API, no
        package name configured). Hard error for operations that require
        certainty.
    """
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    UNPROBEABLE = "unprobeable"


class PublicationProbeResult:
    """Result of a publication probe including status and optional details.

    Attributes:
        status: The tri-state publication status.
        registry: Name of the registry that was probed (e.g. "npm", "pypi").
        version: The version that was probed.
        message: Optional human-readable detail (e.g. error message for
            UNPROBEABLE, or the found version for PUBLISHED).
    """

    __slots__ = ("status", "registry", "version", "message")

    def __init__(self, status, registry, version, message=""):
        self.status = status
        self.registry = registry
        self.version = version
        self.message = message

    def __repr__(self):
        return (
            f"PublicationProbeResult(status={self.status!r}, "
            f"registry={self.registry!r}, version={self.version!r}, "
            f"message={self.message!r})"
        )

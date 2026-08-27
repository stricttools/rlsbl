"""Structured outcomes returned by per-target protocol methods.

These types exist so a target can say "I do not implement this" *explicitly*.
The dispatch these replaced fell through a chain of name comparisons to a bare
``return True`` or a stderr line, which made an unsupported target
indistinguishable from a successful one.
"""

from enum import Enum
from typing import NamedTuple


class SuiteRunStatus(Enum):
    """Result of a target's built-in test run."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SuiteRunOutcome(NamedTuple):
    """Outcome of ``ReleaseTarget.run_tests``.

    ``message`` always names the target, so a SKIPPED outcome renders a step
    summary line a reader can act on rather than silence.
    """

    status: SuiteRunStatus
    message: str

    @property
    def passed(self) -> bool:
        """True only when the suite actually ran and passed."""
        return self.status is SuiteRunStatus.PASSED

    @property
    def skipped(self) -> bool:
        """True when this target has no built-in test runner."""
        return self.status is SuiteRunStatus.SKIPPED


class YankStatus(Enum):
    """Result of a target's registry-removal action."""

    DONE = "done"
    """The removal was performed (or, under dry run, described)."""

    INCOMPLETE = "incomplete"
    """The target implements yank but could not complete it (e.g. no name)."""

    UNSUPPORTED = "unsupported"
    """This target has no registry-removal action at all."""


class YankOutcome(NamedTuple):
    """Outcome of ``ReleaseTarget.yank``."""

    status: YankStatus
    message: str

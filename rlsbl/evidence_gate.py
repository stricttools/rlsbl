"""Layered evidence gate that checks multiple sources to determine whether a release was published, producing CLEARED or BLOCKED verdicts.

The gate checks multiple evidence sources in order and produces a verdict:
CLEARED (safe to undo) or BLOCKED (may have been published).

Evidence sources are extensible -- new sources can be added by implementing
the EvidenceSource protocol and registering them in the sources list.

Current sources:
- RegistryProbeSource: uses publication_probe() from target implementations
  (npm, pypi, go)
- GoModuleProxySource: asks the Go module proxy directly, because the Go
  target's own probe asks the git remote instead (see the class docstring)

Future sources (not yet implemented):
- CIPublishRunSource: checks GitHub Actions workflow conclusions
- LocalStateSource: checks in-progress.json completed_steps
- GitHubReleaseAssetSource: checks for uploaded assets
"""

import enum
import json
import os
import time
from typing import Protocol, runtime_checkable
from . import effects
from .errors import RlsblError


class Verdict(enum.Enum):
    """Result of the evidence gate."""
    CLEARED = "cleared"
    BLOCKED = "blocked"


class EvidenceKind(enum.Enum):
    """What kind of evidence a source provides."""
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    INCONCLUSIVE = "inconclusive"


class Evidence:
    """A single piece of evidence from one source about one target.

    Attributes:
        source: Name of the evidence source (e.g. "registry_probe").
        target: Name of the target (e.g. "npm", "pypi").
        kind: Whether this evidence says PUBLISHED, UNPUBLISHED, or INCONCLUSIVE.
        message: Human-readable detail.
    """

    __slots__ = ("source", "target", "kind", "message")

    def __init__(self, source, target, kind, message=""):
        self.source = source
        self.target = target
        self.kind = kind
        self.message = message

    def to_dict(self):
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "message": self.message,
        }


@runtime_checkable
class EvidenceSource(Protocol):
    """Protocol for evidence sources."""

    @property
    def name(self) -> str:
        """Unique name for this evidence source."""
        ...

    def gather(self, targets, project_dir, version, ctx=None) -> list[Evidence]:
        """Gather evidence for the given targets and version.

        Args:
            targets: list of target objects from TARGETS registry.
            project_dir: path to the project directory.
            version: the version string (without 'v' prefix).
            ctx: optional project context.

        Returns:
            list of Evidence objects, one per target checked.
        """
        ...


class RegistryProbeSource:
    """Evidence source using target.publication_probe()."""

    @property
    def name(self):
        return "registry_probe"

    def gather(self, targets, project_dir, version, ctx=None):
        from .publication_probe import PublicationStatus

        evidence = []
        for target in targets:
            if not target.supports_publication_probe:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.INCONCLUSIVE,
                    message=f"target '{target.name}' does not support publication probing",
                ))
                continue

            result = target.publication_probe(project_dir, version, ctx)
            if result.status == PublicationStatus.PUBLISHED:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.PUBLISHED,
                    message=result.message,
                ))
            elif result.status == PublicationStatus.UNPUBLISHED:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.UNPUBLISHED,
                    message=result.message,
                ))
            else:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.INCONCLUSIVE,
                    message=result.message,
                ))
        return evidence


class GoModuleProxySource:
    """Evidence source asking the Go module proxy about one version.

    The Go target's own ``publication_probe`` asks the GIT REMOTE whether the
    version's tag exists, not the proxy. That is the right question for "did we
    tag this?", and the wrong one for "is this out in the world?": the proxy
    caches a module version PERMANENTLY the first time anyone resolves it, so a
    tag that was deleted after someone fetched it is gone from the remote and
    still served by ``proxy.golang.org`` forever. Reading the tag's absence as
    "never published" is how a destructive operation gets cleared for a version
    that consumers can still download today.

    This source answers only two ways, never three:

    * **PUBLISHED** -- the proxy serves the version's ``go.mod``. Under the
      gate's rule that alone blocks, which is the whole point.
    * **INCONCLUSIVE** -- the proxy does not serve it, or could not be asked.

    Absence on the proxy is deliberately NOT reported as UNPUBLISHED. The proxy
    indexes lazily: a genuinely published version nobody has fetched yet is
    absent from it, so proxy lag alone must never be the evidence that clears a
    deletion. Something that really did observe the version's absence -- the
    tag probe -- has to say so.
    """

    @property
    def name(self):
        return "go_module_proxy"

    def gather(self, targets, project_dir, version, ctx=None):
        from .registry import query_go_mod
        from .utils import read_go_module_path

        evidence = []
        for target in targets:
            if target.name != "go":
                continue
            module_path = read_go_module_path(project_dir)
            if not module_path:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.INCONCLUSIVE,
                    message=(
                        f"no module path could be read from go.mod in "
                        f"{project_dir}, so the module proxy could not be asked"
                    ),
                ))
                continue

            result = query_go_mod(module_path, f"v{version}")
            status = result.get("status")
            if status == "found":
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.PUBLISHED,
                    message=(
                        f"proxy.golang.org serves {module_path}@v{version}; "
                        f"the module proxy is permanent, so this version "
                        f"remains downloadable whatever happens to the tag"
                    ),
                ))
            elif status == "not_found":
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.INCONCLUSIVE,
                    message=(
                        f"proxy.golang.org does not serve {module_path}@"
                        f"v{version}, which is not evidence that it was never "
                        f"published: the proxy indexes lazily and a version "
                        f"nobody has fetched is absent from it"
                    ),
                ))
            else:
                evidence.append(Evidence(
                    source=self.name,
                    target=target.name,
                    kind=EvidenceKind.INCONCLUSIVE,
                    message=(
                        f"the module proxy could not be asked about "
                        f"{module_path}@v{version}: "
                        f"{result.get('message') or 'unknown error'}"
                    ),
                ))
        return evidence


# Default evidence sources -- extensible by appending to this list
DEFAULT_SOURCES = [
    RegistryProbeSource(),
    GoModuleProxySource(),
]


class GateResult:
    """Result of running the evidence gate.

    Attributes:
        verdict: CLEARED or BLOCKED.
        evidence: all evidence gathered from all sources.
        reason: human-readable explanation of the verdict.
    """

    __slots__ = ("verdict", "evidence", "reason")

    def __init__(self, verdict, evidence, reason):
        self.verdict = verdict
        self.evidence = evidence
        self.reason = reason

    def to_dict(self):
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def run_evidence_gate(targets, project_dir, version, ctx=None, sources=None):
    """Run the layered evidence gate to determine if a release is safe to undo.

    Decision rule:
    - CLEARED: at least one authoritative source says UNPUBLISHED and none says PUBLISHED.
    - BLOCKED: any source says PUBLISHED.
    - BLOCKED (hard error): no authoritative evidence at all (all INCONCLUSIVE).

    Args:
        targets: list of target objects from TARGETS registry.
        project_dir: path to the project directory.
        version: the version string (without 'v' prefix).
        ctx: optional project context.
        sources: list of EvidenceSource objects; defaults to DEFAULT_SOURCES.

    Returns:
        GateResult with verdict and evidence.
    """
    if sources is None:
        sources = DEFAULT_SOURCES

    all_evidence = []
    for source in sources:
        evidence = source.gather(targets, project_dir, version, ctx)
        all_evidence.extend(evidence)

    # Decision rule
    has_published = any(e.kind == EvidenceKind.PUBLISHED for e in all_evidence)
    has_unpublished = any(e.kind == EvidenceKind.UNPUBLISHED for e in all_evidence)
    all_inconclusive = all(e.kind == EvidenceKind.INCONCLUSIVE for e in all_evidence)

    if has_published:
        published_targets = [e for e in all_evidence if e.kind == EvidenceKind.PUBLISHED]
        target_names = ", ".join(e.target for e in published_targets)
        return GateResult(
            verdict=Verdict.BLOCKED,
            evidence=all_evidence,
            reason=f"published on: {target_names}",
        )

    if all_inconclusive or not all_evidence:
        return GateResult(
            verdict=Verdict.BLOCKED,
            evidence=all_evidence,
            reason="no authoritative evidence -- cannot determine publication status",
        )

    if has_unpublished:
        return GateResult(
            verdict=Verdict.CLEARED,
            evidence=all_evidence,
            reason="at least one registry confirms unpublished, none confirms published",
        )

    # Should not reach here, but be defensive
    return GateResult(
        verdict=Verdict.BLOCKED,
        evidence=all_evidence,
        reason="unexpected evidence state",
    )


def write_undo_audit(audit_dir, version, tag, gate_result, operator_context=None):
    """Write an audit record for a non-latest undo operation.

    Creates ``undo-audit.json`` in the given directory with per-target
    evidence, verdict, and operator context.

    Args:
        audit_dir: directory to write the audit file to (e.g. .rlsbl/ or
            the releasable dir).
        version: the version that was undone.
        tag: the git tag that was deleted.
        gate_result: the GateResult from run_evidence_gate().
        operator_context: optional dict with additional context.

    Returns:
        path to the written audit file.

    Raises:
        RlsblError: an existing ``undo-audit.json`` that cannot be parsed. The
            append is a whole-file rewrite, so continuing would replace the
            records already in it.
    """
    effects.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "undo-audit.json")

    record = {
        "version": version,
        "tag": tag,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "verdict": gate_result.verdict.value,
        "reason": gate_result.reason,
        "evidence": [e.to_dict() for e in gate_result.evidence],
    }
    if operator_context:
        record["operator_context"] = operator_context

    # Append to the existing audit file if present. The whole file is
    # read-modify-written, so an existing file that cannot be parsed must NOT
    # be treated as an empty list: that silently overwrote every record the
    # file held with the single new one -- data loss in the one file whose
    # purpose is to survive a destructive operation. A malformed file is a hard
    # error naming it, and the file is left exactly as found for recovery.
    existing = []
    if os.path.isfile(audit_path):
        with open(audit_path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            existing = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RlsblError(
                f"the undo audit file {audit_path} is not readable JSON "
                f"({exc}), so appending to it would overwrite whatever record "
                f"it holds. It is left untouched: repair or move it aside "
                f"(its contents are the record of past undo operations), then "
                f"re-run."
            )
        # A valid single record (an older single-object file) is readable, so
        # it is wrapped and appended to rather than refused.
        if not isinstance(existing, list):
            existing = [existing]

    existing.append(record)

    with effects.open_write(audit_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")

    return audit_path

"""Tests for rlsbl.evidence_gate -- layered evidence gate for undo safety."""

import json
import os
import tempfile

import pytest
from unittest.mock import MagicMock

from rlsbl.evidence_gate import (
    Evidence,
    EvidenceKind,
    GateResult,
    RegistryProbeSource,
    Verdict,
    run_evidence_gate,
    write_undo_audit,
)
from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus


class TestEvidence:
    def test_to_dict(self):
        e = Evidence("registry_probe", "npm", EvidenceKind.PUBLISHED, "found on npm")
        d = e.to_dict()
        assert d["source"] == "registry_probe"
        assert d["target"] == "npm"
        assert d["kind"] == "published"
        assert d["message"] == "found on npm"


class TestRegistryProbeSource:
    def test_gather_published(self):
        target = MagicMock()
        target.name = "npm"
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.PUBLISHED

    def test_gather_unpublished(self):
        target = MagicMock()
        target.name = "pypi"
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "pypi", "1.0.0", "not found"
        )

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.UNPUBLISHED

    def test_gather_unprobeable(self):
        target = MagicMock()
        target.name = "pypi"
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPROBEABLE, "pypi", "1.0.0", "error"
        )

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.INCONCLUSIVE

    def test_gather_no_probe_capability(self):
        target = MagicMock()
        target.name = "plain"
        target.capabilities = frozenset()

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.INCONCLUSIVE

    def test_gather_multiple_targets(self):
        npm_target = MagicMock()
        npm_target.name = "npm"
        npm_target.capabilities = frozenset({"publication_probe"})
        npm_target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        pypi_target = MagicMock()
        pypi_target.name = "pypi"
        pypi_target.capabilities = frozenset({"publication_probe"})
        pypi_target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "pypi", "1.0.0", "not found"
        )

        source = RegistryProbeSource()
        evidence = source.gather([npm_target, pypi_target], "/fake", "1.0.0")
        assert len(evidence) == 2
        assert all(e.kind == EvidenceKind.UNPUBLISHED for e in evidence)


class TestRunEvidenceGate:
    def test_cleared_when_unpublished(self):
        target = MagicMock()
        target.name = "npm"
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.CLEARED

    def test_blocked_when_published(self):
        target = MagicMock()
        target.name = "npm"
        target.capabilities = frozenset({"publication_probe"})
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED
        assert "published" in result.reason

    def test_blocked_when_all_inconclusive(self):
        target = MagicMock()
        target.name = "plain"
        target.capabilities = frozenset()

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED
        assert "no authoritative" in result.reason

    def test_blocked_when_no_targets(self):
        result = run_evidence_gate([], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED

    def test_cleared_mixed_unpublished_inconclusive(self):
        """One unpublished + one inconclusive = CLEARED (has_unpublished, not all_inconclusive)."""
        npm = MagicMock()
        npm.name = "npm"
        npm.capabilities = frozenset({"publication_probe"})
        npm.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        plain = MagicMock()
        plain.name = "plain"
        plain.capabilities = frozenset()

        result = run_evidence_gate([npm, plain], "/fake", "1.0.0")
        assert result.verdict == Verdict.CLEARED

    def test_blocked_published_overrides_unpublished(self):
        """One published + one unpublished = BLOCKED."""
        npm = MagicMock()
        npm.name = "npm"
        npm.capabilities = frozenset({"publication_probe"})
        npm.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )

        pypi = MagicMock()
        pypi.name = "pypi"
        pypi.capabilities = frozenset({"publication_probe"})
        pypi.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "pypi", "1.0.0", "not found"
        )

        result = run_evidence_gate([npm, pypi], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED

    def test_gate_result_to_dict(self):
        e = Evidence("registry_probe", "npm", EvidenceKind.UNPUBLISHED, "not found")
        result = GateResult(Verdict.CLEARED, [e], "test reason")
        d = result.to_dict()
        assert d["verdict"] == "cleared"
        assert d["reason"] == "test reason"
        assert len(d["evidence"]) == 1


class TestWriteUndoAudit:
    def test_creates_new_file(self, tmp_path):
        gate = GateResult(
            Verdict.CLEARED,
            [Evidence("registry_probe", "npm", EvidenceKind.UNPUBLISHED, "not found")],
            "test",
        )
        audit_path = write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)
        assert os.path.isfile(audit_path)
        with open(audit_path) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["version"] == "1.0.0"
        assert data[0]["tag"] == "v1.0.0"
        assert data[0]["verdict"] == "cleared"

    def test_appends_to_existing(self, tmp_path):
        gate = GateResult(
            Verdict.CLEARED,
            [Evidence("registry_probe", "npm", EvidenceKind.UNPUBLISHED, "not found")],
            "test",
        )
        write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)
        write_undo_audit(str(tmp_path), "0.9.0", "v0.9.0", gate)

        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        with open(audit_path) as f:
            data = json.load(f)
        assert len(data) == 2

    def test_handles_corrupt_existing_file(self, tmp_path):
        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        with open(audit_path, "w") as f:
            f.write("not json")

        gate = GateResult(Verdict.CLEARED, [], "test")
        write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)

        with open(audit_path) as f:
            data = json.load(f)
        assert len(data) == 1

    def test_includes_operator_context(self, tmp_path):
        gate = GateResult(Verdict.CLEARED, [], "test")
        write_undo_audit(
            str(tmp_path), "1.0.0", "v1.0.0", gate,
            operator_context={"reason": "bad build"},
        )

        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        with open(audit_path) as f:
            data = json.load(f)
        assert data[0]["operator_context"]["reason"] == "bad build"

    def test_creates_directory(self, tmp_path):
        nested = os.path.join(str(tmp_path), "a", "b")
        gate = GateResult(Verdict.CLEARED, [], "test")
        audit_path = write_undo_audit(nested, "1.0.0", "v1.0.0", gate)
        assert os.path.isfile(audit_path)


class TestNonLatestUndoIntegration:
    """Integration-level tests for the --version undo path."""

    def test_blocked_undo_exits_with_error(self):
        """Non-latest undo should exit 1 when the gate blocks."""
        from unittest.mock import patch
        from io import StringIO
        from rlsbl.commands.undo import run_cmd

        ctx = MagicMock()
        ctx.project_root = "/fake"
        ctx.config = {}

        mock_member = MagicMock(targets=[])

        with patch("rlsbl.commands.undo.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.undo.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.undo.is_clean_tree", return_value=True), \
             patch("rlsbl.commands.undo.find_workspace_root", return_value=None), \
             patch("rlsbl.member_context.resolve_member_context", return_value=mock_member), \
             patch("rlsbl.evidence_gate.run_evidence_gate") as mock_gate, \
             patch("sys.stderr", new_callable=StringIO) as err:

            mock_gate.return_value = GateResult(
                Verdict.BLOCKED,
                [Evidence("registry_probe", "npm", EvidenceKind.PUBLISHED, "found")],
                "published on: npm",
            )

            with pytest.raises(SystemExit) as exc:
                run_cmd(None, [], {"version": "1.0.0", "yes": True}, ctx=ctx)
            assert exc.value.code == 1
            assert "cannot undo" in err.getvalue()

"""Tests for rlsbl.evidence_gate -- layered evidence gate for undo safety."""

import json
import os
import tempfile

import pytest
from unittest.mock import MagicMock

from rlsbl.errors import RlsblError
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
        target.supports_publication_probe = True
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
        target.supports_publication_probe = True
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
        target.supports_publication_probe = True
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPROBEABLE, "pypi", "1.0.0", "error"
        )

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.INCONCLUSIVE

    def test_gather_target_that_cannot_probe(self):
        """The non-prober branch: no probe is run and the reason is stated.

        ``supports_publication_probe`` is set explicitly because a bare
        MagicMock answers every unset attribute with a truthy auto-attribute.
        Leaving it unset sent this target down the prober branch, where the
        mock's ``publication_probe`` return value fell through to INCONCLUSIVE
        anyway -- the assertion below held while testing nothing.
        """
        target = MagicMock()
        target.name = "plain"
        target.supports_publication_probe = False

        source = RegistryProbeSource()
        evidence = source.gather([target], "/fake", "1.0.0")
        assert len(evidence) == 1
        assert evidence[0].kind == EvidenceKind.INCONCLUSIVE
        assert evidence[0].message == (
            "target 'plain' does not support publication probing"
        )
        target.publication_probe.assert_not_called()

    def test_gather_multiple_targets(self):
        npm_target = MagicMock()
        npm_target.name = "npm"
        npm_target.supports_publication_probe = True
        npm_target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        pypi_target = MagicMock()
        pypi_target.name = "pypi"
        pypi_target.supports_publication_probe = True
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
        target.supports_publication_probe = True
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.CLEARED

    def test_blocked_when_published(self):
        target = MagicMock()
        target.name = "npm"
        target.supports_publication_probe = True
        target.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED
        assert "published" in result.reason

    def test_blocked_when_all_inconclusive(self):
        """A lone non-prober leaves the gate with no authoritative evidence."""
        target = MagicMock()
        target.name = "plain"
        target.supports_publication_probe = False

        result = run_evidence_gate([target], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED
        assert "no authoritative" in result.reason
        assert result.evidence[0].message == (
            "target 'plain' does not support publication probing"
        )
        target.publication_probe.assert_not_called()

    def test_blocked_when_no_targets(self):
        result = run_evidence_gate([], "/fake", "1.0.0")
        assert result.verdict == Verdict.BLOCKED

    def test_cleared_mixed_unpublished_inconclusive(self):
        """One unpublished + one inconclusive = CLEARED (has_unpublished, not all_inconclusive)."""
        npm = MagicMock()
        npm.name = "npm"
        npm.supports_publication_probe = True
        npm.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.UNPUBLISHED, "npm", "1.0.0", "not found"
        )

        plain = MagicMock()
        plain.name = "plain"
        plain.supports_publication_probe = False

        result = run_evidence_gate([npm, plain], "/fake", "1.0.0")
        assert result.verdict == Verdict.CLEARED
        # The inconclusive half really came from the non-prober branch.
        plain.publication_probe.assert_not_called()
        assert {e.kind for e in result.evidence} == {
            EvidenceKind.UNPUBLISHED, EvidenceKind.INCONCLUSIVE,
        }

    def test_blocked_published_overrides_unpublished(self):
        """One published + one unpublished = BLOCKED."""
        npm = MagicMock()
        npm.name = "npm"
        npm.supports_publication_probe = True
        npm.publication_probe.return_value = PublicationProbeResult(
            PublicationStatus.PUBLISHED, "npm", "1.0.0", "found"
        )

        pypi = MagicMock()
        pypi.name = "pypi"
        pypi.supports_publication_probe = True
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

    def test_corrupt_existing_file_is_a_hard_error(self, tmp_path):
        """A malformed audit file is never silently discarded.

        The audit trail is the record of what an undo destroyed, and this
        function read-modify-writes the WHOLE file. Treating an unparseable
        existing file as an empty list overwrote every past record with the
        new one -- silent data loss, in the one file whose purpose is to not
        lose anything. It is now a hard error naming the file.
        """
        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        with open(audit_path, "w") as f:
            f.write("not json")

        gate = GateResult(Verdict.CLEARED, [], "test")
        with pytest.raises(RlsblError, match="undo-audit.json"):
            write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)

        # The unreadable file is left exactly as found -- nothing overwritten.
        with open(audit_path) as f:
            assert f.read() == "not json"

    def test_truncated_existing_file_is_a_hard_error(self, tmp_path):
        # A half-written array (an interrupted previous write) is the realistic
        # shape of the corruption, and it must not cost the records it holds.
        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        truncated = '[{"version": "0.9.0", "tag": "v0.9.0"}'
        with open(audit_path, "w") as f:
            f.write(truncated)

        gate = GateResult(Verdict.CLEARED, [], "test")
        with pytest.raises(RlsblError):
            write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)

        # The half-written file is left byte-for-byte as found: the records it
        # still holds are recoverable by hand, which is the whole point of
        # refusing rather than rewriting.
        with open(audit_path) as f:
            assert f.read() == truncated

    def test_non_list_existing_record_is_wrapped_not_refused(self, tmp_path):
        # Valid JSON that is a single record (an older single-object file) is
        # readable, so it is wrapped and appended to -- not an error.
        audit_path = os.path.join(str(tmp_path), "undo-audit.json")
        with open(audit_path, "w") as f:
            json.dump({"version": "0.9.0", "tag": "v0.9.0"}, f)

        gate = GateResult(Verdict.CLEARED, [], "test")
        write_undo_audit(str(tmp_path), "1.0.0", "v1.0.0", gate)

        with open(audit_path) as f:
            data = json.load(f)
        assert [r["version"] for r in data] == ["0.9.0", "1.0.0"]

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
                run_cmd(None, [], {"version": "1.0.0"}, ctx=ctx)
            assert exc.value.code == 1
            assert "cannot undo" in err.getvalue()


class TestCachedRegistrySource:
    """The registry-side second opinion, and why it never clears.

    A target's primary ``publication_probe`` does not have to ask the registry.
    The Go target's asks the git REMOTE whether the version's tag exists -- the
    right question for "did we tag this?" and the wrong one for "is this out in
    the world?": the proxy caches a module version permanently the first time
    anyone resolves it, so a tag deleted after someone fetched it is gone from
    the remote and served by ``proxy.golang.org`` forever.

    Which targets have the second probe is the TARGET's answer, not a name this
    source knows.
    """

    def _go_target(self):
        from rlsbl.targets import TARGETS

        return TARGETS["go"]

    def _gather(self, proxy_answer, monkeypatch, *, module_path="example.com/m"):
        from rlsbl.evidence_gate import CachedRegistrySource

        monkeypatch.setattr(
            "rlsbl.registry.query_go_mod",
            lambda path, version: proxy_answer,
        )
        # The Go target binds read_go_module_path at import time, so the
        # patch has to name it where the target reads it.
        monkeypatch.setattr(
            "rlsbl.targets.go.read_go_module_path", lambda d: module_path,
        )
        return CachedRegistrySource().gather(
            [self._go_target()], "/fake", "1.0.0",
        )

    def test_the_target_declares_whether_it_has_one(self):
        from rlsbl.targets import TARGETS

        assert TARGETS["go"].supports_cached_registry_probe is True
        assert TARGETS["npm"].supports_cached_registry_probe is False

    def test_a_served_version_is_published(self, monkeypatch):
        evidence = self._gather(
            {"status": "found", "text": "module example.com/m\n"}, monkeypatch,
        )
        assert [e.kind for e in evidence] == [EvidenceKind.PUBLISHED]
        assert "permanent" in evidence[0].message

    def test_absence_on_the_proxy_is_inconclusive_never_unpublished(
        self, monkeypatch,
    ):
        evidence = self._gather({"status": "not_found"}, monkeypatch)
        assert [e.kind for e in evidence] == [EvidenceKind.INCONCLUSIVE]
        assert "lazily" in evidence[0].message

    def test_an_unanswerable_proxy_is_inconclusive(self, monkeypatch):
        evidence = self._gather(
            {"status": "error", "message": "HTTP 503"}, monkeypatch,
        )
        assert [e.kind for e in evidence] == [EvidenceKind.INCONCLUSIVE]
        assert "503" in evidence[0].message

    def test_an_unreadable_module_path_is_inconclusive(self, monkeypatch):
        evidence = self._gather(
            {"status": "found", "text": ""}, monkeypatch, module_path=None,
        )
        assert [e.kind for e in evidence] == [EvidenceKind.INCONCLUSIVE]

    def test_a_target_without_a_second_probe_produces_nothing(self):
        from rlsbl.evidence_gate import CachedRegistrySource
        from rlsbl.targets import TARGETS

        assert CachedRegistrySource().gather(
            [TARGETS["npm"]], "/fake", "1.0.0",
        ) == []


class TestTheTwoSourcesCombine:
    """Fail-closed: either source saying PUBLISHED blocks; proxy lag never clears."""

    def _sources(self, tag_status, proxy_status):
        class FakeTagProbe:
            name = "registry_probe"

            def gather(self, targets, project_dir, version, ctx=None):
                return [Evidence("registry_probe", "go", tag_status, "tag probe")]

        class FakeProxy:
            name = "cached_registry"

            def gather(self, targets, project_dir, version, ctx=None):
                if proxy_status is None:
                    return []
                return [Evidence("cached_registry", "go", proxy_status, "proxy")]

        return [FakeTagProbe(), FakeProxy()]

    def test_the_proxy_alone_can_block_a_deleted_tag(self):
        """The tag is gone; the proxy still serves it. That must block."""
        result = run_evidence_gate(
            [], "/fake", "1.0.0",
            sources=self._sources(
                EvidenceKind.UNPUBLISHED, EvidenceKind.PUBLISHED,
            ),
        )
        assert result.verdict == Verdict.BLOCKED
        assert "go" in result.reason

    def test_proxy_lag_alone_can_never_clear_a_deletion(self):
        """An inconclusive proxy plus an inconclusive tag probe stays blocked.

        This is the property the source's INCONCLUSIVE-not-UNPUBLISHED
        answer exists to guarantee: if the proxy could report absence as
        "unpublished", a version nobody has fetched yet would be cleared for
        deletion on the strength of the proxy not having indexed it.
        """
        result = run_evidence_gate(
            [], "/fake", "1.0.0",
            sources=self._sources(
                EvidenceKind.INCONCLUSIVE, EvidenceKind.INCONCLUSIVE,
            ),
        )
        assert result.verdict == Verdict.BLOCKED
        assert "no authoritative evidence" in result.reason

    def test_the_tag_probe_still_clears_on_its_own(self):
        """Adding the proxy source must not block what used to be cleared."""
        result = run_evidence_gate(
            [], "/fake", "1.0.0",
            sources=self._sources(
                EvidenceKind.UNPUBLISHED, EvidenceKind.INCONCLUSIVE,
            ),
        )
        assert result.verdict == Verdict.CLEARED

    def test_the_proxy_source_is_wired_into_the_defaults(self):
        from rlsbl.evidence_gate import DEFAULT_SOURCES

        assert "cached_registry" in [s.name for s in DEFAULT_SOURCES]

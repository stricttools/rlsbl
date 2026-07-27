"""Tests for the strictspec certificate deploy gate (rlsbl.strictspec_gate).

Covers the grade-gate rule (decision 25): violated blocks, corpus-supported is
green, an unsupported claim needs adjudication discharge, a configured-but-
missing certificate is a hard error, and an unconfigured project is untouched.
"""

import json

import pytest

from rlsbl.errors import ConfigError
from rlsbl.strictspec_gate import evaluate_certificate_gate


def _write_cert(tmp_path, claims, name="cert.json"):
    cert = {
        "certificate_format_version": 1,
        "schema_id": "rlsbl-release-file",
        "old_format_version": 1,
        "new_format_version": 2,
        "corpus": {"declared_glob": "corpus/*", "resolved_file_count": 3,
                   "content_hash": "abc"},
        "claims": claims,
        "strictspec_release": "0.1.0",
    }
    p = tmp_path / name
    p.write_text(json.dumps(cert))
    return p


def _config(tmp_path, certificate="cert.json", adjudication=None):
    section = {"certificate": certificate}
    if adjudication is not None:
        section["adjudication"] = adjudication
    return {"publish_mode": "ci", "strictspec_gate": section}


class TestUnconfigured:
    def test_absent_section_skips(self, tmp_path):
        verdict = evaluate_certificate_gate({"publish_mode": "ci"}, str(tmp_path))
        assert verdict.skipped
        assert verdict.ok


class TestGreen:
    def test_corpus_supported_passes(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "corpus-supported",
             "statement": "every doc valid at N re-validates at N+1"},
        ])
        verdict = evaluate_certificate_gate(_config(tmp_path), str(tmp_path))
        assert verdict.ok
        assert not verdict.skipped

    def test_proven_passes(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "proven", "statement": "s"},
        ])
        assert evaluate_certificate_gate(_config(tmp_path), str(tmp_path)).ok


class TestViolatedBlocks:
    def test_violated_blocks(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "violated",
             "statement": "narrowing without a bump",
             "counterexamples": [{"document_path": "corpus/bad.toml",
                                  "diagnostics": []}]},
        ])
        verdict = evaluate_certificate_gate(_config(tmp_path), str(tmp_path))
        assert not verdict.ok
        assert any("VIOLATED" in r for r in verdict.blocking_reasons)
        assert any("corpus/bad.toml" in r for r in verdict.blocking_reasons)

    def test_one_violated_among_green_blocks(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "corpus-supported", "statement": "a"},
            {"kind": "down-taxonomy", "grade": "violated", "statement": "b"},
        ])
        assert not evaluate_certificate_gate(_config(tmp_path), str(tmp_path)).ok


class TestMissingOrMalformed:
    def test_configured_but_missing_certificate_hard_errors(self, tmp_path):
        with pytest.raises(ConfigError, match="does not exist"):
            evaluate_certificate_gate(_config(tmp_path), str(tmp_path))

    def test_malformed_certificate_hard_errors(self, tmp_path):
        (tmp_path / "cert.json").write_text("{ not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            evaluate_certificate_gate(_config(tmp_path), str(tmp_path))

    def test_certificate_without_claims_hard_errors(self, tmp_path):
        (tmp_path / "cert.json").write_text(json.dumps({"schema_id": "x"}))
        with pytest.raises(ConfigError, match="claims"):
            evaluate_certificate_gate(_config(tmp_path), str(tmp_path))

    def test_missing_certificate_key_hard_errors(self, tmp_path):
        config = {"publish_mode": "ci", "strictspec_gate": {}}
        with pytest.raises(ConfigError, match="certificate is required"):
            evaluate_certificate_gate(config, str(tmp_path))

    def test_unknown_gate_key_hard_errors(self, tmp_path):
        config = {"publish_mode": "ci",
                  "strictspec_gate": {"certificate": "cert.json", "bogus": 1}}
        with pytest.raises(ConfigError, match="unknown key"):
            evaluate_certificate_gate(config, str(tmp_path))


class TestAdjudication:
    def _write_adj(self, tmp_path, entries, name="adj.toml"):
        lines = [
            "format_version = 1",
            'schema_id = "rlsbl-release-file"',
            "old_format_version = 1",
            "new_format_version = 2",
        ]
        for e in entries:
            lines += [
                "[[adjudications]]",
                f'claim_kind = "{e["claim_kind"]}"',
                f'scope = "{e["scope"]}"',
                f'justification = "{e["justification"]}"',
                f'author = "{e["author"]}"',
                f"date = {e['date']}",
            ]
        (tmp_path / name).write_text("\n".join(lines) + "\n")

    def test_unsupported_without_adjudication_blocks(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "no-corpus", "statement": "s"},
        ])
        verdict = evaluate_certificate_gate(_config(tmp_path), str(tmp_path))
        assert not verdict.ok
        assert any("no adjudication file" in r for r in verdict.blocking_reasons)

    def test_unsupported_discharged_by_adjudication(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "no-corpus",
             "statement": "greenfield claim"},
        ])
        self._write_adj(tmp_path, [
            {"claim_kind": "flip-scan", "scope": "greenfield claim",
             "justification": "zero at-rest corpus; reviewed manually",
             "author": "maintainer", "date": "2026-07-27"},
        ])
        verdict = evaluate_certificate_gate(
            _config(tmp_path, adjudication="adj.toml"), str(tmp_path)
        )
        assert verdict.ok, verdict.blocking_reasons

    def test_dangling_adjudication_entry_blocks(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "corpus-supported", "statement": "ok"},
        ])
        self._write_adj(tmp_path, [
            {"claim_kind": "down-taxonomy", "scope": "nonexistent",
             "justification": "j", "author": "a", "date": "2026-07-27"},
        ])
        verdict = evaluate_certificate_gate(
            _config(tmp_path, adjudication="adj.toml"), str(tmp_path)
        )
        assert not verdict.ok
        assert any("dangling" in r for r in verdict.blocking_reasons)

    def test_missing_adjudication_file_hard_errors(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "no-corpus", "statement": "s"},
        ])
        with pytest.raises(ConfigError, match="does not exist"):
            evaluate_certificate_gate(
                _config(tmp_path, adjudication="missing.toml"), str(tmp_path)
            )

    def test_malformed_adjudication_hard_errors(self, tmp_path):
        _write_cert(tmp_path, [
            {"kind": "flip-scan", "grade": "no-corpus", "statement": "s"},
        ])
        # Missing required author/date -> strictspec validation fails.
        (tmp_path / "adj.toml").write_text(
            "format_version = 1\n"
            'schema_id = "x"\nold_format_version = 1\nnew_format_version = 2\n'
            "[[adjudications]]\n"
            'claim_kind = "flip-scan"\nscope = "s"\njustification = "j"\n'
        )
        with pytest.raises(ConfigError, match="adjudication file"):
            evaluate_certificate_gate(
                _config(tmp_path, adjudication="adj.toml"), str(tmp_path)
            )

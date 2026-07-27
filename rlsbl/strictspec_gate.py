"""strictspec diff-certificate deploy gate.

rlsbl can consume a strictspec ``strictspec diff`` CERTIFICATE as a
``format_version`` deploy gate. The gate is feature-flagged by CONFIG PRESENCE:
a project opts in by adding a ``strictspec_gate`` section to ``.rlsbl/config.json``;
projects without it are untouched (the built-in check skips).

Grade semantics (strictspec spec/appendix-certificates.md Part A, decision 25):

- ``violated`` -- a corpus document IS the counterexample. BLOCKS release.
- ``corpus-supported`` -- no counterexample in the declared corpus. GREEN.
- ``proven`` -- reserved for the future analyzer; treated as GREEN.
- any other / unsupported claim -- must be discharged by a committed ADJUDICATION
  file (Part B). An unsupported, unadjudicated claim BLOCKS release. There is no
  bypass.

The certificate is deliberately UN-GATED (it carries ``certificate_format_version``,
not a document ``format_version``), so rlsbl parses it as plain JSON and inspects
the claim grades natively -- a strictspec document schema, which mandates the
version gate, cannot validate an intentionally un-gated artifact. The ADJUDICATION
file, by contrast, IS a gated strictspec document and is validated via the
strictspec-generated adjudication validator.
"""

import json
import os
from dataclasses import dataclass, field

from .errors import ConfigError

# Claim grades that are a green light for the gate.
_GREEN_GRADES = frozenset({"corpus-supported", "proven"})
_VIOLATED_GRADE = "violated"

CONFIG_KEY = "strictspec_gate"


@dataclass
class GateVerdict:
    """The outcome of evaluating the certificate deploy gate."""

    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    # Human-readable blocking reasons (empty iff ok or skipped).
    blocking_reasons: list[str] = field(default_factory=list)
    # Informational lines (e.g. per-claim green summaries).
    notes: list[str] = field(default_factory=list)


def validate_gate_config(config):
    """Validate the ``strictspec_gate`` config section shape.

    Returns the section dict when present, or ``None`` when absent (opt-out).
    Raises :class:`ConfigError` on a malformed section.
    """
    section = config.get(CONFIG_KEY)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(
            f"{CONFIG_KEY} must be a JSON object, got {type(section).__name__}"
        )
    cert = section.get("certificate")
    if not isinstance(cert, str) or not cert.strip():
        raise ConfigError(
            f"{CONFIG_KEY}.certificate is required and must be a non-empty "
            f"string (path to the strictspec diff certificate JSON)"
        )
    adj = section.get("adjudication")
    if adj is not None and (not isinstance(adj, str) or not adj.strip()):
        raise ConfigError(
            f"{CONFIG_KEY}.adjudication must be a non-empty string (path to a "
            f"committed adjudication file) when present"
        )
    unknown = set(section) - {"certificate", "adjudication"}
    if unknown:
        raise ConfigError(
            f"{CONFIG_KEY} has unknown key(s): {', '.join(sorted(unknown))}. "
            f"Allowed keys: adjudication, certificate"
        )
    return section


def _resolve(project_root, rel):
    if os.path.isabs(rel):
        return rel
    return os.path.join(str(project_root), rel)


def _load_certificate(path):
    """Load and shape-check the certificate JSON. Raises ConfigError on failure.

    The certificate is un-gated by design, so it is parsed as plain JSON; only
    the fields the gate consumes are shape-checked.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"certificate {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"certificate {path} must be a JSON object")
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise ConfigError(
            f"certificate {path} is missing a 'claims' array"
        )
    return data


def _load_adjudications(config, project_root, section):
    """Load + validate the adjudication file (a gated strictspec document).

    Returns the list of adjudication entry dicts, or ``None`` when no
    adjudication file is configured. Raises ConfigError on a missing or invalid
    file.
    """
    adj_rel = section.get("adjudication")
    if adj_rel is None:
        return None
    adj_path = _resolve(project_root, adj_rel)
    if not os.path.isfile(adj_path):
        raise ConfigError(
            f"{CONFIG_KEY}.adjudication points to {adj_path}, which does not "
            f"exist"
        )
    from .strictspec_gen import adjudication_validator as _adj

    with open(adj_path, "rb") as f:
        raw = f.read()
    _root, diags = _adj.validate_bytes(raw, "toml")
    if diags:
        lines = [f"adjudication file {adj_path} failed strictspec validation:"]
        for d in diags:
            lines.append(f"  {d.path}: {d.message}  [{d.code}]")
        raise ConfigError("\n".join(lines))
    import tomllib

    data = tomllib.loads(raw.decode("utf-8"))
    return data.get("adjudications", [])


def _claim_label(claim, index):
    stmt = claim.get("statement")
    if isinstance(stmt, str) and stmt.strip():
        return stmt
    return f"claim #{index} (kind={claim.get('kind', '?')})"


def evaluate_certificate_gate(config, project_root):
    """Evaluate the strictspec certificate deploy gate against *config*.

    Returns a :class:`GateVerdict`. When the ``strictspec_gate`` section is
    absent, the verdict is ``skipped`` (opt-out; no behavior change). A missing
    certificate file, malformed certificate, or malformed/missing adjudication
    file raises :class:`ConfigError` (a hard error -- if configured, it must
    work). Otherwise the verdict reflects the claim grades.
    """
    section = validate_gate_config(config)
    if section is None:
        return GateVerdict(ok=True, skipped=True, skip_reason="not configured")

    cert_path = _resolve(project_root, section["certificate"])
    if not os.path.isfile(cert_path):
        raise ConfigError(
            f"{CONFIG_KEY}.certificate points to {cert_path}, which does not "
            f"exist. Produce it with `strictspec diff` (or remove the "
            f"{CONFIG_KEY} section to opt out)."
        )

    cert = _load_certificate(cert_path)
    claims = cert["claims"]

    blocking = []
    notes = []
    unsupported = []  # (index, claim)
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ConfigError(
                f"certificate {cert_path}: claims[{i}] must be an object"
            )
        grade = claim.get("grade")
        label = _claim_label(claim, i)
        if grade == _VIOLATED_GRADE:
            witnesses = []
            for ce in claim.get("counterexamples", []) or []:
                if isinstance(ce, dict) and ce.get("document_path"):
                    witnesses.append(str(ce["document_path"]))
            wtxt = f" (witness: {', '.join(witnesses)})" if witnesses else ""
            blocking.append(f"claim '{label}' is VIOLATED{wtxt}")
        elif grade in _GREEN_GRADES:
            notes.append(f"claim '{label}': {grade}")
        else:
            unsupported.append((i, claim))

    # An adjudication file is loaded and validated whenever configured (if it is
    # configured, it must work). Its entries must each discharge a real
    # unsupported claim; a stray entry is dangling even when no claim is
    # unsupported (Part B).
    adjudications = _load_adjudications(config, project_root, section)
    if adjudications is not None:
        _discharge_unsupported(
            unsupported, adjudications, cert_path, blocking, notes
        )
    elif unsupported:
        for _i, claim in unsupported:
            blocking.append(
                f"claim '{_claim_label(claim, _i)}' is unsupported "
                f"(grade={claim.get('grade')!r}) and no adjudication file "
                f"is configured to discharge it"
            )

    if blocking:
        return GateVerdict(ok=False, blocking_reasons=blocking, notes=notes)
    return GateVerdict(ok=True, notes=notes)


def _discharge_unsupported(unsupported, adjudications, cert_path, blocking, notes):
    """Match each unsupported claim to an adjudication entry; flag stragglers.

    An adjudication entry discharges a claim when its ``claim_kind`` equals the
    claim's ``kind`` and its ``scope`` equals the claim's ``statement``. An
    unsupported claim with no matching entry BLOCKS; an adjudication entry that
    matches no unsupported claim is dangling and also BLOCKS (per Part B).
    """
    matched_entries = set()
    for idx, claim in unsupported:
        kind = claim.get("kind")
        stmt = claim.get("statement")
        found = None
        for j, entry in enumerate(adjudications):
            if not isinstance(entry, dict):
                continue
            if entry.get("claim_kind") == kind and entry.get("scope") == stmt:
                found = j
                break
        if found is None:
            blocking.append(
                f"claim '{_claim_label(claim, idx)}' is unsupported and no "
                f"adjudication entry discharges it (need claim_kind={kind!r}, "
                f"scope={stmt!r})"
            )
        else:
            matched_entries.add(found)
            notes.append(
                f"claim '{_claim_label(claim, idx)}': discharged by adjudication"
            )

    for j, entry in enumerate(adjudications):
        if j not in matched_entries and isinstance(entry, dict):
            blocking.append(
                f"adjudication entry #{j} (claim_kind="
                f"{entry.get('claim_kind')!r}, scope={entry.get('scope')!r}) "
                f"matches no unsupported claim in the certificate (dangling)"
            )

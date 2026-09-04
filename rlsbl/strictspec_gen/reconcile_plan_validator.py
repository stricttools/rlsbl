# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.2.3
# schema:              rlsbl-reconcile-plan (format_version 1)
# regenerate:          strictspec gen --manifest strictspec.toml
#
# Released under the MIT license (unencumbered). This file is machine-generated;
# edit the schema and regenerate, never this file.
# ruff: noqa
from __future__ import annotations

from dataclasses import dataclass, replace

import strictspec
from strictspec import Diagnostic, Value

# GENERATED_BY is the strictspec release that produced this file. The runtime
# pairing guard hard-errors unless it matches the linked runtime exactly.
GENERATED_BY = "0.2.3"
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "reconcile-plan.schema.toml": "# strictspec schema for the RECONCILE PLAN file written by\n# `rlsbl release reconcile --plan` and consumed by\n# `rlsbl release reconcile --apply`.\n#\n# The plan is the preview's output artifact and the apply step's only input.\n# It records what was observed, what each subject's verdict was, and a digest of\n# the world the verdicts were computed from -- the apply re-observes and refuses\n# when that digest no longer matches, so a plan can never be applied against a\n# remote that moved under it.\n\nname = \"rlsbl-reconcile-plan\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"toml\"\nrole = \"schema\"\nroot = \"ReconcilePlan\"\ntargets = [\"python\"]\ndescription = \"The observed plan `rlsbl release reconcile --apply` consumes: one verdict per release ref or GitHub Release, plus a digest of the world they were judged against.\"\n\n[types.ReconcilePlan]\ntype = \"record\"\n\n[types.ReconcilePlan.fields.generated_at]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"When the plan was written (RFC 3339 with a UTC offset).\"\n\n[types.ReconcilePlan.fields.generated_by]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The rlsbl version that wrote the plan.\"\n\n[types.ReconcilePlan.fields.world_digest]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Digest over the observed remote refs and GitHub Release listing. The apply step re-observes and refuses when this no longer matches.\"\n\n[types.ReconcilePlan.fields.items]\ntype = \"array\"\nrequired = true\ndescription = \"One verdict per subject, in the order the preview rendered them. Empty when the reconcile found nothing to do.\"\n[types.ReconcilePlan.fields.items.item]\ntype = \"PlanItem\"\n\n[types.PlanItem]\ntype = \"record\"\n\n[types.PlanItem.fields.key]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The subject judged: a ref name, or `release:<tag>` for a GitHub Release.\"\n\n[types.PlanItem.fields.kind]\ntype = \"enum\"\nrequired = true\nvalues = [\"ref\", \"release\"]\ndescription = \"Which kind of subject this item judges.\"\n\n[types.PlanItem.fields.state]\ntype = \"enum\"\nrequired = true\nvalues = [\n  \"materialize\",\n  \"already-correct\",\n  \"re-point-with-lease\",\n  \"refuse-foreign\",\n  \"refuse-identity-mismatch\",\n]\ndescription = \"The verdict class.\"\n\n[types.PlanItem.fields.version]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The released version this subject belongs to, when the release record names one.\"\n\n[types.PlanItem.fields.target]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The commit the subject should end up at.\"\n\n[types.PlanItem.fields.observed]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"What the remote held when the plan was written -- the force-with-lease expectation.\"\n\n[types.PlanItem.fields.summary]\ntype = \"string\"\nrequired = false\ndescription = \"The one-line headline the preview printed for this item.\"\n",
}
_EMBEDDED_MAIN_FILE = "reconcile-plan.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[ReconcilePlan | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[ReconcilePlan | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_ReconcilePlan(v), result.diagnostics


def validate_value(v: Value) -> tuple[ReconcilePlan | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_ReconcilePlan(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class ReconcilePlan:
    """Frozen typed binding of the "ReconcilePlan" record. Immutable; use with_* for
    copy-on-write.
    """

    generated_at: str
    generated_by: str
    world_digest: str
    items: list[PlanItem]

    def with_generated_at(self, v: str) -> ReconcilePlan:
        return replace(self, generated_at=v)

    def with_generated_by(self, v: str) -> ReconcilePlan:
        return replace(self, generated_by=v)

    def with_world_digest(self, v: str) -> ReconcilePlan:
        return replace(self, world_digest=v)

    def with_items(self, v: list[PlanItem]) -> ReconcilePlan:
        return replace(self, items=v)


def _bind_ReconcilePlan(v: Value) -> ReconcilePlan | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_generated_at = v.field("generated_at")
    f_generated_by = v.field("generated_by")
    f_world_digest = v.field("world_digest")
    f_items = v.field("items")
    return ReconcilePlan(
        generated_at=(f_generated_at[0].string()[0] if f_generated_at[1] else ""),
        generated_by=(f_generated_by[0].string()[0] if f_generated_by[1] else ""),
        world_digest=(f_world_digest[0].string()[0] if f_world_digest[1] else ""),
        items=([_bind_PlanItem(e) for e in f_items[0].items()] if f_items[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class PlanItem:
    """Frozen typed binding of the "PlanItem" record. Immutable; use with_* for
    copy-on-write.
    """

    key: str
    kind: str
    state: str
    version: str
    target: str
    observed: str
    summary: str

    def with_key(self, v: str) -> PlanItem:
        return replace(self, key=v)

    def with_kind(self, v: str) -> PlanItem:
        return replace(self, kind=v)

    def with_state(self, v: str) -> PlanItem:
        return replace(self, state=v)

    def with_version(self, v: str) -> PlanItem:
        return replace(self, version=v)

    def with_target(self, v: str) -> PlanItem:
        return replace(self, target=v)

    def with_observed(self, v: str) -> PlanItem:
        return replace(self, observed=v)

    def with_summary(self, v: str) -> PlanItem:
        return replace(self, summary=v)


def _bind_PlanItem(v: Value) -> PlanItem | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_key = v.field("key")
    f_kind = v.field("kind")
    f_state = v.field("state")
    f_version = v.field("version")
    f_target = v.field("target")
    f_observed = v.field("observed")
    f_summary = v.field("summary")
    return PlanItem(
        key=(f_key[0].string()[0] if f_key[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        state=(f_state[0].string()[0] if f_state[1] else ""),
        version=(f_version[0].string()[0] if f_version[1] else ""),
        target=(f_target[0].string()[0] if f_target[1] else ""),
        observed=(f_observed[0].string()[0] if f_observed[1] else ""),
        summary=(f_summary[0].string()[0] if f_summary[1] else ""),
    )



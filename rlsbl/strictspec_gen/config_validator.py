# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.1.0
# schema:              rlsbl-config (format_version 1)
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
GENERATED_BY = "0.1.0"
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "config.schema.toml": "# strictspec schema for the rlsbl project config (.rlsbl/config.json).\n# Source of truth: rlsbl/config.py (validate_config_schema,\n# validate_pipelines_config, validate_pipeline_target_links) and\n# rlsbl/external_checks.py (validate_external_checks).\n#\n# SCOPE (honest subset): this schema models the CORE, well-understood document\n# shape -- publish_mode, targets, pipelines, external_checks.\n# It is NOT wired into config reading yet: (1) the fleet's config.json files do\n# not carry the mandatory strictspec format_version gate, and (2) the full\n# config surface has additional sections (services, test_env, batch_limits,\n# deploy, test, ...) plus cross-layer merge semantics (config.merge_config,\n# releasable inheritance) that strictspec cannot see. Those remain native.\n# Full wiring lands with the fleet-wide format_version stamp.\n\nname = \"rlsbl-config\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"json\"\nrole = \"schema\"\nroot = \"Config\"\ntargets = [\"python\"]\ndescription = \"Per-project rlsbl configuration: publish mode, targets, publish pipelines, and config-declared external checks (core document shape).\"\n\n[types.Config]\ntype = \"record\"\n\n[types.Config.fields.publish_mode]\ntype = \"enum\"\nrequired = true\nvalues = [\"ci\", \"none\"]\ndescription = \"Required, no default (get_publish_mode raises when absent). \\\"ci\\\" publishes via CI; \\\"none\\\" suppresses.\"\n\n[types.Config.fields.targets]\ntype = \"array\"\nrequired = false\ndescription = \"Release targets. Each element is a bare string OR a {name,path} record. An empty list is additionally rejected natively.\"\n[types.Config.fields.targets.item]\ntype = \"TargetRef\"\n\n[types.Config.fields.pipelines]\ntype = \"map\"\nrequired = false\nkey_pattern = \"^[A-Za-z0-9_-]+$\"\ndescription = \"Publish pipelines keyed by pipeline name.\"\n[types.Config.fields.pipelines.value]\ntype = \"Pipeline\"\n\n[types.Config.fields.external_checks]\ntype = \"array\"\nrequired = false\ndescription = \"Config-declared subprocess checks (a discriminated union per entry).\"\n[types.Config.fields.external_checks.item]\ntype = \"ExternalCheck\"\n\n# external_check names unique across the list.\n[[types.Config.constraints]]\nform = \"unique-by\"\ncollection = \"external_checks\"\nfield = \"name\"\nnormalization = \"none\"\n\n# --- named types ---\n\n# TargetRef -- a node-kind union: a bare string, or a {name,path} record.\n[types.TargetRef]\ntype = \"node-kind-union\"\ndescription = \"String form (target name) OR record form.\"\n[types.TargetRef.arms.string]\ntype = \"string\"\n[types.TargetRef.arms.record]\ntype = \"TargetObject\"\n\n[types.TargetObject]\ntype = \"record\"\n[types.TargetObject.fields.name]\ntype = \"string\"\nrequired = true\nnon_empty = true\n[types.TargetObject.fields.path]\ntype = \"string\"\nrequired = false\n\n# Pipeline -- mandatory type/local, mandatory target link, conditional keys.\n[types.Pipeline]\ntype = \"record\"\n\n[types.Pipeline.fields.type]\ntype = \"enum\"\nrequired = true\nvalues = [\"cloudflare-pages\", \"deno\", \"docker\", \"go\", \"hex\", \"maven\", \"maven-central\", \"npm\", \"pypi\"]\ndescription = \"Pipeline kind (PIPELINE_TYPES). Required.\"\n\n[types.Pipeline.fields.local]\ntype = \"boolean\"\nrequired = true\ndescription = \"Whether the pipeline runs locally. Required (no default).\"\n\n[types.Pipeline.fields.target]\ntype = \"nullable\"\nrequired = true\ndescription = \"Target name (must resolve in Config.targets) or null (targetless publisher).\"\n[types.Pipeline.fields.target.inner]\ntype = \"string\"\n\n[types.Pipeline.fields.provenance]\ntype = \"boolean\"\nrequired = false\ndescription = \"npm provenance. Required WHEN type == \\\"npm\\\".\"\n\n[types.Pipeline.fields.artifact]\ntype = \"enum\"\nrequired = false\nvalues = [\"binary\", \"library\", \"launcher\"]\ndescription = \"Artifact kind. Required WHEN type == \\\"go\\\". \\\"launcher\\\" wraps a sibling binary pipeline.\"\n\n[types.Pipeline.fields.wraps]\ntype = \"string\"\nrequired = false\ndescription = \"For a launcher: the sibling pipeline whose artifact is \\\"binary\\\". Required WHEN artifact == \\\"launcher\\\".\"\n\n[types.Pipeline.fields.binary_source]\ntype = \"enum\"\nrequired = false\nvalues = [\"github-release\"]\ndescription = \"For a launcher: where the wrapped binary comes from. Required WHEN artifact == \\\"launcher\\\".\"\n\n[types.Pipeline.fields.download]\ntype = \"enum\"\nrequired = false\nvalues = [\"first-run\", \"postinstall\"]\ndescription = \"For a launcher: how the wrapped binary is fetched. Required WHEN artifact == \\\"launcher\\\".\"\n\n[types.Pipeline.fields.assets]\ntype = \"boolean\"\nrequired = false\n\n[types.Pipeline.fields.max_asset_size_mb]\ntype = \"integer\"\nrequired = false\nmin = 1\ndescription = \"Required WHEN assets == true OR custom_assets present.\"\n\n[types.Pipeline.fields.custom_assets]\ntype = \"array\"\nrequired = false\ndescription = \"Custom-built release assets.\"\n[types.Pipeline.fields.custom_assets.item]\ntype = \"CustomAsset\"\n\n[types.CustomAsset]\ntype = \"record\"\n[types.CustomAsset.fields.name]\ntype = \"string\"\nrequired = true\nnon_empty = true\n[types.CustomAsset.fields.build]\ntype = \"string\"\nrequired = true\nnon_empty = true\n\n# type == \"npm\" => provenance required\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"provenance\"\nwhen = { field = \"type\", predicate = \"equals\", value = \"npm\" }\n\n# type == \"go\" => artifact required\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"artifact\"\nwhen = { field = \"type\", predicate = \"equals\", value = \"go\" }\n\n# artifact == \"launcher\" => wraps + binary_source + download required\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"wraps\"\nwhen = { field = \"artifact\", predicate = \"equals\", value = \"launcher\" }\n\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"binary_source\"\nwhen = { field = \"artifact\", predicate = \"equals\", value = \"launcher\" }\n\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"download\"\nwhen = { field = \"artifact\", predicate = \"equals\", value = \"launcher\" }\n\n# assets == true => max_asset_size_mb required\n[[types.Pipeline.constraints]]\nform = \"conditional-required\"\nfield = \"max_asset_size_mb\"\nwhen = { field = \"assets\", predicate = \"equals\", value = true }\n\n# pipeline.target string must resolve to a Config.targets element.\n[[types.Pipeline.constraints]]\nform = \"intra-document-references\"\nreference = \"target\"\nresolves_into = \"targets\"\nresolves_by = \"node-kind-union-key\"\ndescription = \"target names a configured release target.\"\n\n# ExternalCheck -- discriminated union on kind.\n[types.ExternalCheck]\ntype = \"discriminated-union\"\ndiscriminator = \"kind\"\ndescription = \"kind=\\\"structured\\\" (rlsbl-composed tool+paths) or kind=\\\"freeform\\\" (opaque shell command).\"\n[types.ExternalCheck.arms.structured]\ntype = \"StructuredCheck\"\n[types.ExternalCheck.arms.freeform]\ntype = \"FreeformCheck\"\n\n[types.StructuredCheck]\ntype = \"record\"\n[types.StructuredCheck.fields.kind]\ntype = \"literal\"\nvalue = \"structured\"\nrequired = true\n[types.StructuredCheck.fields.name]\ntype = \"string\"\nrequired = true\nnon_empty = true\nregex = \"^[a-z][a-z0-9-]*$\"\n[types.StructuredCheck.fields.tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\n[types.StructuredCheck.fields.tool]\ntype = \"enum\"\nrequired = true\nvalues = [\"mypy\", \"ruff-check\", \"ruff-format\"]\n[types.StructuredCheck.fields.paths]\ntype = \"array\"\nrequired = true\nmin_len = 1\n[types.StructuredCheck.fields.paths.item]\ntype = \"string\"\n[types.StructuredCheck.fields.depends_on]\ntype = \"array\"\nrequired = false\n[types.StructuredCheck.fields.depends_on.item]\ntype = \"string\"\n[types.StructuredCheck.fields.cwd]\ntype = \"string\"\nrequired = false\n\n[types.FreeformCheck]\ntype = \"record\"\n[types.FreeformCheck.fields.kind]\ntype = \"literal\"\nvalue = \"freeform\"\nrequired = true\n[types.FreeformCheck.fields.name]\ntype = \"string\"\nrequired = true\nnon_empty = true\nregex = \"^[a-z][a-z0-9-]*$\"\n[types.FreeformCheck.fields.tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\n[types.FreeformCheck.fields.command]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Opaque shell command. First-token binary existence is checked natively.\"\n[types.FreeformCheck.fields.depends_on]\ntype = \"array\"\nrequired = false\n[types.FreeformCheck.fields.depends_on.item]\ntype = \"string\"\n[types.FreeformCheck.fields.cwd]\ntype = \"string\"\nrequired = false\n",
}
_EMBEDDED_MAIN_FILE = "config.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[Config | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[Config | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_Config(v), result.diagnostics


def validate_value(v: Value) -> tuple[Config | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_Config(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class Config:
    """Frozen typed binding of the "Config" record. Immutable; use with_* for
    copy-on-write.
    """

    publish_mode: str
    targets: list[Value]
    pipelines: Value
    external_checks: list[Value]

    def with_publish_mode(self, v: str) -> Config:
        return replace(self, publish_mode=v)

    def with_targets(self, v: list[Value]) -> Config:
        return replace(self, targets=v)

    def with_pipelines(self, v: Value) -> Config:
        return replace(self, pipelines=v)

    def with_external_checks(self, v: list[Value]) -> Config:
        return replace(self, external_checks=v)


def _bind_Config(v: Value) -> Config | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_publish_mode = v.field("publish_mode")
    f_targets = v.field("targets")
    f_pipelines = v.field("pipelines")
    f_external_checks = v.field("external_checks")
    return Config(
        publish_mode=(f_publish_mode[0].string()[0] if f_publish_mode[1] else ""),
        targets=([e for e in f_targets[0].items()] if f_targets[1] else []),
        pipelines=(f_pipelines[0] if f_pipelines[1] else Value(None, "json")),
        external_checks=([e for e in f_external_checks[0].items()] if f_external_checks[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class TargetObject:
    """Frozen typed binding of the "TargetObject" record. Immutable; use with_* for
    copy-on-write.
    """

    name: str
    path: str

    def with_name(self, v: str) -> TargetObject:
        return replace(self, name=v)

    def with_path(self, v: str) -> TargetObject:
        return replace(self, path=v)


def _bind_TargetObject(v: Value) -> TargetObject | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_name = v.field("name")
    f_path = v.field("path")
    return TargetObject(
        name=(f_name[0].string()[0] if f_name[1] else ""),
        path=(f_path[0].string()[0] if f_path[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class Pipeline:
    """Frozen typed binding of the "Pipeline" record. Immutable; use with_* for
    copy-on-write.
    """

    type: str
    local: bool
    target: str | None
    provenance: bool
    artifact: str
    wraps: str
    binary_source: str
    download: str
    assets: bool
    max_asset_size_mb: int
    custom_assets: list[CustomAsset]

    def with_type(self, v: str) -> Pipeline:
        return replace(self, type=v)

    def with_local(self, v: bool) -> Pipeline:
        return replace(self, local=v)

    def with_target(self, v: str | None) -> Pipeline:
        return replace(self, target=v)

    def with_provenance(self, v: bool) -> Pipeline:
        return replace(self, provenance=v)

    def with_artifact(self, v: str) -> Pipeline:
        return replace(self, artifact=v)

    def with_wraps(self, v: str) -> Pipeline:
        return replace(self, wraps=v)

    def with_binary_source(self, v: str) -> Pipeline:
        return replace(self, binary_source=v)

    def with_download(self, v: str) -> Pipeline:
        return replace(self, download=v)

    def with_assets(self, v: bool) -> Pipeline:
        return replace(self, assets=v)

    def with_max_asset_size_mb(self, v: int) -> Pipeline:
        return replace(self, max_asset_size_mb=v)

    def with_custom_assets(self, v: list[CustomAsset]) -> Pipeline:
        return replace(self, custom_assets=v)


def _bind_Pipeline(v: Value) -> Pipeline | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_type = v.field("type")
    f_local = v.field("local")
    f_target = v.field("target")
    f_provenance = v.field("provenance")
    f_artifact = v.field("artifact")
    f_wraps = v.field("wraps")
    f_binary_source = v.field("binary_source")
    f_download = v.field("download")
    f_assets = v.field("assets")
    f_max_asset_size_mb = v.field("max_asset_size_mb")
    f_custom_assets = v.field("custom_assets")
    return Pipeline(
        type=(f_type[0].string()[0] if f_type[1] else ""),
        local=(f_local[0].bool()[0] if f_local[1] else False),
        target=((None if f_target[0].is_null() else f_target[0].string()[0]) if f_target[1] else None),
        provenance=(f_provenance[0].bool()[0] if f_provenance[1] else False),
        artifact=(f_artifact[0].string()[0] if f_artifact[1] else ""),
        wraps=(f_wraps[0].string()[0] if f_wraps[1] else ""),
        binary_source=(f_binary_source[0].string()[0] if f_binary_source[1] else ""),
        download=(f_download[0].string()[0] if f_download[1] else ""),
        assets=(f_assets[0].bool()[0] if f_assets[1] else False),
        max_asset_size_mb=(f_max_asset_size_mb[0].int()[0] if f_max_asset_size_mb[1] else 0),
        custom_assets=([_bind_CustomAsset(e) for e in f_custom_assets[0].items()] if f_custom_assets[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class CustomAsset:
    """Frozen typed binding of the "CustomAsset" record. Immutable; use with_* for
    copy-on-write.
    """

    name: str
    build: str

    def with_name(self, v: str) -> CustomAsset:
        return replace(self, name=v)

    def with_build(self, v: str) -> CustomAsset:
        return replace(self, build=v)


def _bind_CustomAsset(v: Value) -> CustomAsset | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_name = v.field("name")
    f_build = v.field("build")
    return CustomAsset(
        name=(f_name[0].string()[0] if f_name[1] else ""),
        build=(f_build[0].string()[0] if f_build[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class StructuredCheck:
    """Frozen typed binding of the "StructuredCheck" record. Immutable; use with_* for
    copy-on-write.
    """

    kind: str
    name: str
    tag: str
    tool: str
    paths: list[str]
    depends_on: list[str]
    cwd: str

    def with_kind(self, v: str) -> StructuredCheck:
        return replace(self, kind=v)

    def with_name(self, v: str) -> StructuredCheck:
        return replace(self, name=v)

    def with_tag(self, v: str) -> StructuredCheck:
        return replace(self, tag=v)

    def with_tool(self, v: str) -> StructuredCheck:
        return replace(self, tool=v)

    def with_paths(self, v: list[str]) -> StructuredCheck:
        return replace(self, paths=v)

    def with_depends_on(self, v: list[str]) -> StructuredCheck:
        return replace(self, depends_on=v)

    def with_cwd(self, v: str) -> StructuredCheck:
        return replace(self, cwd=v)


def _bind_StructuredCheck(v: Value) -> StructuredCheck | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_kind = v.field("kind")
    f_name = v.field("name")
    f_tag = v.field("tag")
    f_tool = v.field("tool")
    f_paths = v.field("paths")
    f_depends_on = v.field("depends_on")
    f_cwd = v.field("cwd")
    return StructuredCheck(
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        name=(f_name[0].string()[0] if f_name[1] else ""),
        tag=(f_tag[0].string()[0] if f_tag[1] else ""),
        tool=(f_tool[0].string()[0] if f_tool[1] else ""),
        paths=([e.string()[0] for e in f_paths[0].items()] if f_paths[1] else []),
        depends_on=([e.string()[0] for e in f_depends_on[0].items()] if f_depends_on[1] else []),
        cwd=(f_cwd[0].string()[0] if f_cwd[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class FreeformCheck:
    """Frozen typed binding of the "FreeformCheck" record. Immutable; use with_* for
    copy-on-write.
    """

    kind: str
    name: str
    tag: str
    command: str
    depends_on: list[str]
    cwd: str

    def with_kind(self, v: str) -> FreeformCheck:
        return replace(self, kind=v)

    def with_name(self, v: str) -> FreeformCheck:
        return replace(self, name=v)

    def with_tag(self, v: str) -> FreeformCheck:
        return replace(self, tag=v)

    def with_command(self, v: str) -> FreeformCheck:
        return replace(self, command=v)

    def with_depends_on(self, v: list[str]) -> FreeformCheck:
        return replace(self, depends_on=v)

    def with_cwd(self, v: str) -> FreeformCheck:
        return replace(self, cwd=v)


def _bind_FreeformCheck(v: Value) -> FreeformCheck | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_kind = v.field("kind")
    f_name = v.field("name")
    f_tag = v.field("tag")
    f_command = v.field("command")
    f_depends_on = v.field("depends_on")
    f_cwd = v.field("cwd")
    return FreeformCheck(
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        name=(f_name[0].string()[0] if f_name[1] else ""),
        tag=(f_tag[0].string()[0] if f_tag[1] else ""),
        command=(f_command[0].string()[0] if f_command[1] else ""),
        depends_on=([e.string()[0] for e in f_depends_on[0].items()] if f_depends_on[1] else []),
        cwd=(f_cwd[0].string()[0] if f_cwd[1] else ""),
    )



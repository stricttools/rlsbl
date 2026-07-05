# Multiple strictcli mains per Go module (split from monorepo-consumer-needs.md)

## Context

A consumer monorepo (Go + npm workspace members, one releasable group, root-level docs site) surfaced four gaps during setup planning. All four were verified against rlsbl source before filing.

## 4. Multiple strictcli mains per Go module

**Problem:** rlsbl/strictcli_detect.py hard-errors during release when more than one main package in a Go module imports strictcli ("Fix the project layout"). A consumer Go module legitimately has several internal CLIs (an ops CLI, a codegen tool, a benchmark harness) and wants all of them on strictcli rather than being forced to hand-roll flag parsing in all but one. Single `.strictcli/schema.json` per project is the underlying assumption.

**Solutions:**
- (a) **Recommended:** support N strictcli mains: detection enumerates all strictcli-importing mains; the release schema-dump step dumps one schema per main (layout to be coordinated with strictcli's own multi-main schema output — a corresponding todo is filed there).
- (b) Config key in `.rlsbl/config.json` naming the "primary" strictcli main; others ignored. Simpler, but ignored CLIs get no schema → no docs, and the restriction survives.

**Affected:** rlsbl/strictcli_detect.py, release schema-dump step, checks that read `.strictcli/schema.json`; coordinate with strictcli and selfdoc consumers of the schema layout.
**Effort:** M (cross-project coordination).

---

## Deferral rationale (2026-07-05)

Deferred: rejected for now on merit. Multiple CLIs in one repo have an ecosystem-native answer — rlsbl monorepo members (each CLI as a member with its own go.mod, own .strictcli/schema.json, own scaffold), or command groups within a single strictcli app. The one friction (shared internal/ packages must become an importable member) is a pattern the ecosystem already runs. Building per-main schema machinery across strictcli + rlsbl + selfdoc is not justified while no consumer has a strong case for separate binary distribution. Revisit if a consumer demonstrates a genuine need for separately-distributed binaries that monorepo members cannot serve. (Decided 2026-07-05 in triage session.)

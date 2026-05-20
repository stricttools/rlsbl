# Architectural layer rules

## Context

The F monorepo has implicit architectural layers: foundation (schema, models, infra), specs (sdui_spec, llm_spec), domain contracts (marketplace_contract, payments_contract), implementations (marketplace, payments_stripe), flows (flow_order, flow_listing), and the app shell (app). Dependencies flow strictly downward: flows depend on contracts, contracts depend on models, models depend on schema.

This layering is documented in README prose but not enforced. Nothing prevents flow_order from importing directly from payments_stripe (bypassing payments_contract) or marketplace from importing from flow_listing (upward dependency).

## What we need

A way to declare and enforce dependency direction rules at the workspace level. Not per-package (that's what pubspec.yaml does) but across the workspace as architectural constraints.

Examples of rules we'd want to enforce:
- `flow_*` packages may depend on `*_contract` packages but never on concrete implementations (`marketplace`, `payments_stripe`, `shipping_adapters`)
- `*_contract` packages may only depend on `models` and `schema`
- `app` may depend on everything (it's the composition root)
- `conformance` and `testing` may depend on anything (they're test infrastructure)
- No package may depend on `app` (no reverse deps on the shell)
- No `flow_*` may depend on another `flow_*`
- `legacy_schema` and `legacy_bridge` may not be depended upon by any non-migration package

## How it could work

Workspace.toml or a separate config file declares rules using package name patterns (globs) and allowed/forbidden dependency directions. `monorepo lint` checks the actual dependency graph against these rules. CI fails on violations.

## Why this matters

In a 41-package monorepo with one developer (an AI agent), accidental layering violations are easy to introduce and hard to notice in review. Automated enforcement catches them at commit time. The rules also serve as living documentation of the intended architecture.

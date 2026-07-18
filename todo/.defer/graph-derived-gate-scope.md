# Graph-derived gate scope (rung 5 of the structured-gate-scope ladder)

Deferred by decision 2026-07-18. The parent todo
(`structured-gate-scope-for-external-checks.md`) proposed a 5-rung escalation
ladder for killing check-scope drift; the decision was to build a **narrow rung
1** now (mypy adapter + pure competing-scope check + mandatory freeform marker)
and park the end-state here. Revisit when the fleet exhibits many-gates ×
many-packages scale, or when a second trap-bearing tool / second affected
consumer appears.

## The end state

Exactly one machine-readable model of a project's code universe — the same
package/workspace graph rlsbl already models for packaging and releasing —
where every node has a **kind** (first-party package, test suite, generated,
docs). Gates are pure predicates over kinds ("all first-party Python", "all
test modules"), never path lists. At run time rlsbl resolves the node set from
the graph and invokes each tool with forced resolved paths; schema validation
forbids tools from carrying independent scope.

Properties:

- Scope literally cannot diverge from what ships, because gates and the release
  pipeline consume the same graph.
- Adding a package automatically extends every relevant gate.
- Omitting a node from a gate is structurally impossible.
- Natural extension of the dev_node philosophy: derive behavior from a node's
  declared role, don't enumerate.

## Design considerations recorded during investigation (2026-07-18)

- **Terminology hazard:** "scope" is already a taken word in the check system —
  `rlsbl/checks/scope.py` implements a `scope_adapter` (registered via
  `app.set_scope_adapter`) whose tokens (`workspace`, `non_dev_node`,
  `library`, `releasable`, `push`) filter by project/workspace-graph role, a
  different axis from path/file gate scope. Rung 5 is conceptually adjacent to
  this adapter (both are predicates over graph roles) — that is both a reuse
  opportunity and a naming collision to resolve deliberately.
- **Placement:** build in rlsbl, not strictcli. strictcli owns the check
  registry/runner but has no project model; scope is a project-model property.
  strictcli needs no changes.
- **Prerequisite modeling work:** extend `workspace_graph.py` /
  `workspace_types.py` with node kinds; a gate-as-predicate resolver over
  kinds; forced-resolved-path invocation; schema forbidding tool-carried scope.
- **Freeform gates remain:** custom checkers (AST checkers, golden-file
  scripts, tool-owned scope like `--dir` flags) cannot be graph-resolved; the
  mandatory freeform/unmanaged marker from rung 1 stays the escape valve.
- Rungs 2-4 (sole entry point, named path-sets, layout-inferred profiles) are
  intermediate states; if demand appears they can be built incrementally on
  rung 1 without waiting for rung 5.

## Effort

Large — a design milestone with its own design cycle (node-kind vocabulary,
predicate schema, migration), not an incremental change. Do not fold into an
execution plan without a completed design round.

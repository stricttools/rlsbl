# Structured gate scope for external checks (kill scope drift structurally)

## Context

`external_checks` entries in `.rlsbl/config.json` are freeform `{name, command, tag}` objects. The command is an opaque shell string; rlsbl validates only that its first token is an existing binary. Any scope the check covers (which paths/targets the tool examines) lives inside that string, or inside the tool's own config file, or both.

A consumer project wiring multiple preflight gates (a type checker over package and tests, a linter over the repo, a formatter check, a custom AST checker over tests) hit the core flaw: **the scope of a gate has no principled home**. Two naive placements both fail:

- **Bare command + tool config as source of truth** (command `uv run mypy`, scope in the tool's `files=` config key): DRY, but the check entry is opaque — reading config.json tells you nothing about what is actually gated.
- **Explicit paths in the command** (`uv run mypy <pkg> <tests>`): self-describing, but scope now exists in two places, and they drift silently. This is not hypothetical: mypy's positional paths silently OVERRIDE its config `files=` key, so a command listing fewer paths than the config produces a gate that passes forever while checking less than the project believes. The consumer project caught this near-miss only by luck during a review.

A silently under-scoped gate is the worst failure mode a check system can have: it is a configured guardrail that reports green while not guarding. This is exactly the "no silent degradation" class.

## Problem

1. Scope drift between check commands and tool configs is possible and silent.
2. Freeform command strings make check entries non-self-describing.
3. When one logical scope ("first-party code plus tests") backs several gates, each entry re-states it independently, so gates can also disagree with each other.
4. Nothing structural prevents any of this; correctness rests on authors remembering tool-specific override semantics (e.g., mypy positional-beats-config).

Consumer-side workarounds exist (wrapper scripts holding the one canonical invocation per gate; consistency checkers parsing both places), but they are per-project policing. The structural fix belongs in rlsbl, since rlsbl owns the check registry and runner.

## Suggested direction: an escalation ladder

The following five designs are ordered from least to most structural; each removes a specific weakness of the previous. They are suggestions for triage, not a mandate; even the first rung eliminates the silent-drift class within a project.

### Rung 1: Structured check schema; rlsbl composes the argv

Replace freeform `command` with a structured entry: `{name, tool, paths, tag}` (keeping freeform as a separate, explicitly-named entry type if arbitrary commands must remain possible). rlsbl knows how to build each supported tool's argv from `paths` and invokes it with those forced, resolved paths; registration-time validation errors if the tool's own config carries competing scope (e.g., mypy `files=` present when rlsbl composes positional paths).

- Pros: single copy of scope, self-describing at the point of use, drift structurally impossible for that gate, no consumer-side wrappers or parsers.
- Cons: rlsbl carries per-tool adapters (each tool's scope semantics encoded once, centrally — arguably where that knowledge belongs); ad-hoc tool runs outside rlsbl remain unprotected.

### Rung 2: rlsbl as the sole gate entry point

Make `rlsbl check --tag <tag>` the one sanctioned way to run any gate; tool configs hold no scope at all, so no competing invocation path exists. Ad-hoc bare tool runs fail loudly (no scope to anchor on) rather than silently checking a different set.

- Pros: one representation, one runner, nothing to drift against.
- Cons: the same logical scope is still re-listed in each check entry, so sibling gates can disagree among themselves.

### Rung 3: Named scope sets

Config defines named path-sets once (e.g., `sources`, `tests`, `all`) and each check references a set by name instead of restating paths. Changing a set atomically changes every gate pointing at it.

- Pros: intra-project scope disagreement between gates becomes impossible; check entries become even more readable (`"paths": "all"`).
- Cons: every project still hand-declares essentially the same sets.

### Rung 4: Gate profiles with layout-inferred scope

rlsbl ships reusable gate profiles keyed by project archetype (e.g., a Python-library profile bundling type check, lint, and format gates over standard sets), deriving scope from metadata projects already declare: package discovery from pyproject, test directories by convention.

- Pros: per-project re-declaration disappears; every project on a profile behaves identically; new projects get correct gates by opting in.
- Cons: inference can miss real intent (multiple packages, generated code to exclude, non-standard layouts), and each override reintroduces a local, drift-able list.

### Rung 5: Scope as a derived property of the project graph

The end state: exactly one machine-readable model of a project's code universe — the same package/workspace graph rlsbl already models for packaging and releasing — where every node has a kind (first-party package, test suite, generated, docs). Gates are pure predicates over kinds ("all first-party Python", "all test modules"), never path lists. At run time rlsbl resolves the node set from the graph and invokes each tool with forced resolved paths; schema validation forbids tools from carrying independent scope.

- Pros: scope literally cannot diverge from what ships, because gates and the release pipeline consume the same graph; adding a package automatically extends every relevant gate; omitting a node from a gate is structurally impossible; ecosystem-universal.
- Cons: rlsbl must fully own tool invocation, and a novel tool must declare how it accepts a resolved node set before it can be gated. This is inherent complexity rather than drift surface, but it is real work.

## Notes for triage

- Rungs build on each other; 1 alone is a large improvement, 1+2+3 is a coherent mid-state, 5 is the horizon.
- The freeform `command` escape hatch deserves scrutiny under the no-escape-hatch principle: if it survives, consider requiring an explicit `"scope": "unmanaged"` marker so unmanaged scope is a visible, deliberate declaration rather than the default.
- Monorepo workspaces already have a graph-like model (workspace.toml, releasables, dev nodes); rung 5 is a natural extension of the dev_node philosophy — derive behavior from a node's declared role, don't enumerate.

## Affected files

- `rlsbl/external_checks.py` (schema, validation, invocation)
- `rlsbl/checks/` and check registration (tag/scope resolution)
- `.rlsbl/config.json` schema docs and scaffold templates
- For later rungs: workspace/graph model modules

## Effort

Rung 1: moderate (schema + adapters for the common tools + validation). Rung 2: small on top of 1. Rung 3: small on top of 2. Rung 4: moderate. Rung 5: large — a design milestone, not an incremental change.

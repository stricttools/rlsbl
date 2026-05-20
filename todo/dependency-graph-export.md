# Dependency graph export and visualization

## Context

`monorepo status` shows numeric dep/rdep counts in a table, but there's no way to see the actual graph structure. For a 41-package monorepo with layered architecture, understanding dependency flow is critical for architecture reviews, onboarding, and catching accidental coupling.

## What we need

### Short term: text and data exports

- **Plain text tree**: like `tree` output but for dependencies. Show each package with its deps indented below it. Useful in terminals and AI agent context.
- **DOT format**: Graphviz-compatible `.dot` file. Renderable to SVG/PNG with `dot -Tsvg`. Standard format, widely supported.
- **JSON export**: the full graph as a JSON adjacency list. Consumable by any tooling.

A command like `monorepo graph` with format flags (`--format=text|dot|json`).

### Medium term: filtering and queries

- Show the subgraph rooted at a specific package: "what does flow_order depend on, transitively?"
- Show the reverse subgraph: "what would break if I changed models?"
- Show only a specific layer or target type.
- Highlight cycles (if any).

### Long term: web visualization

- Interactive graph in a browser. Nodes are packages, edges are dependencies.
- Click a node to see its README, version, deps, rdeps.
- Filter by layer, target type, or search.
- This is a separate project (web app or static site), not part of the rlsbl CLI itself. rlsbl just generates the data (JSON) that the web app consumes.

## Where it fits

`monorepo graph` as a new subcommand alongside `monorepo status`, `monorepo release-order`, `monorepo lint`.

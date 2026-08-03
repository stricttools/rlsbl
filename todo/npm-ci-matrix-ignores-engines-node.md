# npm CI template hardcodes a Node matrix that contradicts the `engines.node` it prints

## Context

The npm CI templates build the test matrix like this:

```
    strategy:
      matrix:
{{#if npm.minRequiredNode}}        # engines.node: >= {{npm.minRequiredNode}}
{{/if}}        node-version: [20, 22, 24]
```

`minRequiredNode` is already parsed out of `package.json`'s `engines.node` and
handed to the template (`rlsbl/targets/npm.py:225`). It is used for **nothing but
the comment**. The matrix immediately below it is a hardcoded literal.

## Problem

The generated workflow states a floor and then violates it in the next line. For
a package declaring `engines.node: ">= 22"`, rlsbl emits:

```yaml
        # engines.node: >= 22
        node-version: [20, 22, 24]
```

Node 20 is below the declared floor, so the Node 20 leg tests a configuration
the package explicitly does not support. That is not merely cosmetic: it fails
outright for a very common package shape. `node --test` gained **glob support in
Node 21**, so any package whose test script passes a glob (the shape rlsbl's own
npm scaffolding encourages) cannot run its suite on Node 20 at all. Fresh scaffold
plus `rlsbl release run` gives a red CI leg on a package that is perfectly
healthy.

Because the publish gate requires **every** matching check run to conclude
`success`, one impossible matrix leg is not a cosmetic wart — it blocks
publishing entirely until a human edits the generated file.

So every affected consumer hand-patches the generated matrix. At least two
separate repos in the fleet carry the same local edit, with the same explanatory
comment reinvented independently:

```yaml
        # engines.node: >= 22 -- the scaffold's default matrix still lists 20,
        # which this package genuinely cannot run on: `node --test` gained glob
        # support in 21, and the test script passes one.
        node-version: [22, 24]
```

That patch has to survive every `rlsbl scaffold` re-run via three-way merge, and
it is exactly the kind of consumer-side drift the generator exists to prevent.
The information needed to generate the matrix correctly is already in the
template context; it is simply not used.

## Evidence

- `rlsbl/templates/npm/ci.yml.tpl:22` — `node-version: [20, 22, 24]`
- `rlsbl/templates/npm/ci-yarn.yml.tpl:22` — same literal
- `rlsbl/templates/npm/ci-pnpm.yml.tpl:22` — same literal
- `rlsbl/targets/npm.py:225` — `result["minRequiredNode"] = m.group(1)`, populated
  from `engines.node`, consumed only by the adjacent comment
- Two independent consumer repos ship the identical hand-edit narrowing the
  matrix to `[22, 24]`

## Solution options

### Option A — Derive the matrix from `engines.node`

Filter the candidate list by `minRequiredNode`: emit only versions satisfying the
declared floor.

- Pros: single source of truth; the comment and the matrix can no longer
  disagree; eliminates the hand-patch for every consumer; uses data already in
  the context; no new configuration surface.
- Cons: needs a decision about the candidate pool and how it ages (see below);
  packages with no `engines.node` still need a default.

### Option B — Derive, and refuse when `engines.node` is missing

As A, but make `engines.node` mandatory for npm projects — no floor, no scaffold.

- Pros: matches the fleet's "mandatory over implicit default" stance; a package
  that has not decided its Node floor has an unanswered question, and the
  scaffold is the right place to force it.
- Cons: breaks scaffolding for existing npm projects that omit `engines.node`
  until they add it; needs a clear error message and a migration note.

### Option C — Config key for the matrix

Add an explicit `node_versions` key in `.rlsbl/config.json`.

- Pros: total control; handles projects wanting to test *below* their declared
  floor deliberately.
- Cons: new surface that duplicates `engines.node`; two sources of truth that can
  disagree — the exact failure being fixed; consumers must now maintain it.

### Option D — Just drop 20 from the literal

- Pros: one-line change; unblocks today's consumers immediately.
- Cons: fixes nothing structural. The matrix is still a literal that will
  contradict some future `engines.node`, and the same bug returns when 22 goes
  EOL. Only worth shipping as a stopgap alongside A.

### Sub-decision for A/B: where does the candidate pool come from?

- A static list in the template, filtered by the floor — simple, but ages and
  needs periodic bumping.
- Derived from Node's release schedule (current LTS + newer) — self-maintaining,
  but adds a data source or a periodic refresh.

Worth settling explicitly rather than inheriting a literal by default, since an
un-bumped pool is how the current bug arose.

### Recommendation shape

A, with B as the more correct end state given the fleet's stance against implicit
defaults for values that matter. C reintroduces the two-sources-of-truth problem
that is the actual defect. D only as a same-day stopgap if consumers are blocked.

## Affected files

- `rlsbl/templates/npm/ci.yml.tpl`
- `rlsbl/templates/npm/ci-yarn.yml.tpl`
- `rlsbl/templates/npm/ci-pnpm.yml.tpl`
- `rlsbl/targets/npm.py` — `minRequiredNode` extraction; would need to emit the
  resolved matrix (and enforce presence under Option B)
- npm CI generation tests
- Any scaffold fixtures/goldens asserting the current matrix
- Consumers' generated `ci.yml` / inlined monorepo `ci-router.yml` on
  regeneration — note the monorepo router inlines these jobs, so the router
  generator picks up the fix only when it re-renders from the same source

## Effort estimate

~1-2 hours for Option A: a small change in `npm.py` to compute the version list,
a template edit in three files, and a test asserting that a package declaring
`engines.node: ">= 22"` produces a matrix with no leg below 22 (and that the
emitted comment matches the emitted matrix). Add ~1 hour for Option B's
required-key error path and its message.

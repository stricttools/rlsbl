+++
description = "rlsbl workspaces: root, nested, and dev-node members, the dependency graph, the CI and publish routers, batch releases, maintenance, and workspace checks."
+++

# Workspaces

A **workspace** is a repository holding several members, each a directory, versioned under one or more releasables. `.strictmetadata/releasables/releasables.toml` declares it with `repository_layout = "workspace"` ([declarations](declarations.md)); a workspace whose only member is the root member is still a workspace.

- A **member** is a directory rlsbl knows. It owns the files under its path that no deeper member claims.
- A **releasable** is the unit of versioning: one version, one changelog, one tag scheme, one release at a time. A member is versioned under one releasable, or under none (`releasable = false`). Several members may share a releasable and release together under one version.

## Getting started

```bash
# Declare the workspace: the root member is a dev node, or versioned under a releasable
rlsbl monorepo init --root-member root-dev-node --release-branch main

# Add members; a releasable the workspace does not declare yet is created,
# with its tag format and publish mode stated
rlsbl monorepo add packages/core --releasable core --tag-format '{name}@v{version}' --publish-mode ci --license MIT
rlsbl monorepo add packages/cli --releasable cli --tag-format '{name}@v{version}' --publish-mode ci --license MIT --depends-on core
rlsbl monorepo add tests --releasable false --dev-only

rlsbl monorepo list
rlsbl monorepo status
```

`monorepo init` writes the declarations for a repository that declares nothing yet, with the root member alone, and commits them. `--root-member` states what the root member is: `root-dev-node` declares it dev-only and versioned under no releasable, so the root's files need no changelog entry; `root-releasable` versions it under the releasable `--releasable` names, created with the stated `--tag-format` and `--publish-mode` (neither has a default), and a root releasable publishing from CI also states `--publish-ci-check-pattern`, the pattern of the check-run names the root's own CI reports. The root releasable states `--license` too, and gets the same lifecycle-and-license entries as a releasable `monorepo add` creates. `--release-branch` names each branch a release may run from. A repository whose declarations exist is refused.

`monorepo add <path>` declares a member, scaffolds it as `rlsbl scaffold` does (which regenerates the routers), and commits what the three wrote as one commit. The member is named `--name`, or after its directory. It needs a release target, detected or named with `--target`. `--releasable` names the releasable it is versioned under, or `false`; naming one the workspace does not declare creates it, and then `--tag-format`, `--publish-mode`, and `--license` are required, since a tag scheme, a publish mode, and a license are stated, never derived, and each is refused when no releasable is created. `--license` is an SPDX identifier or `proprietary`, which requires GitHub to report the repository private, as `rlsbl transition classify` requires. The created releasable gets, from today, its `active` lifecycle period, its license period, and its `releasable-name` identity owning its tag namespace in the [lifecycle-and-license record](lifecycle-and-license.md), written before the scaffold, which renders the member's `LICENSE` from it. `--depends-on` (repeatable), `--library`, `--dev-only`, and `--registry-name` fill the member's keys. When anything fails, every path the add changed is put back.

`monorepo remove <path>` deletes a member's declaration (the path as `releasables.toml` writes it), leaving its files and committing nothing. It refuses the root member, the only member of a releasable, a member another member depends on, and a member the lifecycle-and-license record holds an open or pending entry for.

## The root member

Every workspace declares the repository root as a member, `path = "."`, named `root`. It exists so that no tracked file falls outside the ownership model: every file belongs to the member with the most specific path, and the root member owns whatever no other member claims. Job keys, router filters, and check-run names derive from its name, so the root member is always `root` and no other member may take the name.

| Root member | Declared | Meaning |
| --- | --- | --- |
| dev node | `dev_only = true`, `releasable = false` | The root's files need no changelog entry and stand outside every releasable. |
| versioned | `releasable = "<name>"` | The root's files are covered by that releasable's changelog. A releasable owning the root member and publishing from CI declares `publish_ci_check_pattern`. |

`rlsbl scaffold` does not scaffold the root member: `rlsbl monorepo sync --auto-commit` generates its workflows, and a root member publishing from CI has its publish jobs rendered from scaffold's templates into the publish router.

## Nested members

A member may sit inside another member's directory: `draw` and `draw/cmd`, or `sdk` with `sdk/python` and `sdk/npm`. Nothing declares the nesting; it follows from the paths. Every file belongs to the most specific member: `draw/cmd/main.go` is `draw/cmd`'s, and everything rlsbl does with a member's files follows that.

| Area | What happens with nested members |
| --- | --- |
| Changelog | A commit is attributed to the members owning the files it changes. |
| Tags | A tag belongs to a member only when its releasable's tag format renders it. |
| CI filters | A member's filter excludes its nested members, except one holding a member it depends on. |
| Test runners | `rlsbl scaffold` writes a path-exact `--ignore=<path>` per nested member into the member's pytest `addopts`, and `nested-member-runner-exclusion` fails while one is missing; the root member's are written by hand. |
| Uploads | A member's npm package and Go module zip may not carry a nested member's files (`nested-member-upload-contents`); a Python upload is checked in the member's CI. |
| uv sources | A `{ workspace = true }` source belongs in the uv workspace root's `pyproject.toml`, never in a nested member's (`nested-member-uv-sources`). |
| Hooks and checks | A member's hook `dir` and external check `cwd` must lie in its own territory. |
| Extract | `rlsbl monorepo extract` refuses a releasable one of whose members encloses a member that stays. |

A parent and its nested member may share a releasable. When both are Go modules, each gets its own [companion tag](targets.md#companion-tags) at every release: a releasable tagged `gfx/v{version}` with members `gfx` and `gfx/shader` creates `gfx/v0.2.0` and `gfx/shader/v0.2.0`. A path-style `tag_format` must name the path of one of the releasable's own Go members (`path-tag-format-go-member`).

Go modules of one workspace reach each other through a committed `go.work`, never a `replace` pointing into the workspace (`go install <module>@<version>` refuses a module carrying one), which `go-workspace-replace` reports with the migration. rlsbl does not raise a dependent's `require` line when a sibling releases, because the dependent's `go.sum` needs the new version's hash, which exists only once the tag reaches the module proxy; `go-workspace-require-current` reports a requirement below the sibling's latest release, naming the `go get <module>@v<version>` to run.

## Dev nodes

A member that is dev-only and versioned under no releasable is a **dev node**: test infrastructure, conformance suites, development tools. A dev node has no changelog and no releases; `changelog add` and the release commands refuse it, and batch releases leave it out. Nothing that ships may depend on it at runtime: `dev-only-boundary` reports a member that is not dev-only with a runtime dependency on a dev-only one, and `unversioned-boundary` a releasable member with a runtime dependency on a member versioned under none. Dev dependencies are allowed.

A dev node is outside releases but not outside their effects: when its `uv.lock` records a releasable sibling through an editable path source, a release bumping that sibling re-locks it in the version-bump commit, so the candidate is consistent from its first push.

## The dependency graph

The graph's edges come from the members' manifests (`pyproject.toml` dependencies, extras, and groups, and `package.json` dependency tables) and from their `depends_on` declarations, which state every other dependency, a Go module's on a sibling included, each edge with its scope (runtime, dev, peer, explicit). It decides the release order, impact, the boundaries, and the router filters. A manifest the graph cannot read is refused, never passed over.

```bash
rlsbl monorepo graph --format tree              # each member with its dependencies below it
rlsbl monorepo graph --format dot --output graph.dot
rlsbl monorepo graph --format tree --root core --depth 2
rlsbl monorepo graph --format tree --reverse core   # what depends on core
rlsbl monorepo graph --format tree --json       # members in topological order, with versions, targets, edges
rlsbl monorepo outdated                         # dependencies whose constraints exclude the sibling's version
```

`--json` prints the graph as a document: the members in topological order (each after the members it depends on) with their versions, targets, releasables, flags, dependencies, and dependents, and the edges in the same order. A cycle is refused, naming the members on it.

### Impact analysis

```bash
rlsbl monorepo impact core                 # by member name
rlsbl monorepo impact ./packages/core/api.py   # by path relative to the repository root
rlsbl monorepo impact --since v0.4.0       # the files the commits since a revision changed
rlsbl monorepo impact core --depth 1
```

The report names the members the change touches, the members depending on them directly, and every member depending on them within `--depth` steps (any distance without it): the ones to test and to consider releasing. An argument is a member's name or a path that exists; one that is neither, one naming a member that lies in another, and a path that is one of rlsbl's own records are refused, and so are arguments together with `--since`.

## The CI router and the publish router

GitHub Actions reads workflows only from the repository root, so `rlsbl monorepo sync --auto-commit` regenerates two routers in `.github/workflows/`:

- **`ci-router.yml`** inlines every member's own CI jobs (its `.github/workflows/ci.yml` and `ci-*.yml`) under keys and names prefixed with the member and file, and runs each member's jobs when its path filter matched the push, or when the router is dispatched with `run_all=true`.
- **`publish.yml`** inlines the publish jobs of every member whose releasable publishes from CI, each run only for its releasable's tags, behind one `wait-for-ci` job that holds every publish until the releasing project's CI passed on the release commit ([the publish workflow](release-workflow.md#the-publish-workflow)).

Jobs are inlined because GitHub refuses a workflow calling 20 or more reusable workflows. Each inlined job is named `<prefix> / <job>`, the check-run names the CI check patterns match. Both routers are written read-only with a generated-file header. A member whose Python package directory is named other than the member gets that name declared as its `import_name`. The sync removes, through saferm, a router left with nothing to route, the publish workflows of members publishing nothing, and per-member workflow copies an older sync left at the root. `--auto-commit` commits what it wrote and removed. Scaffolding a member regenerates both routers too.

### Router path filters

Each member's filter is derived from the workspace, never declared. It holds:

- the member's own territory, `path/**`;
- the territory of every member it depends on, at any distance and in every scope (a dev dependency's change breaks the dependent's tests);
- the manifests and lockfiles present at the repository root, so a root dependency change runs every member;
- the router itself;
- the changelog file every release of its releasable writes;
- a negated exclude of every member nested in its territory, except one holding a member it depends on (for the root member, `**` narrowed by every other member's territory).

The filter step declares `predicate-quantifier: some-with-excludes`: under the action's default a negated pattern matches everything outside itself. The `router-filters-fresh` check compares the committed filters with a fresh derivation; `rlsbl monorepo sync --auto-commit` regenerates them.

Every release of a releasable writes its changelog file, which every member's filter holds, so a release commit runs the CI jobs of every member of the releasable, including members whose code did not change. That is the accepted cost: neither the release's CI check nor the publish workflow's `wait-for-ci` job reads a skipped check run as a pass, since a skipped check proves nothing about the commit.

### Running every job on one commit

The CI router declares a `workflow_dispatch` input, `run_all`. Dispatched with `run_all=true`, every inlined job runs on the dispatched commit, whatever the filters say:

```bash
gh workflow run ci-router.yml --ref main -f run_all=true
```

This is the way out for a candidate whose commits touch few members: after a red verdict, the fix-forward commits touch only the members they fix, so on the next candidate every other member's jobs are skipped. Inventing a commit to widen the push would put churn into the history and the changelog; running the same commit with the filters bypassed does not. Nothing is waived: the jobs run, and a failure still blocks the release. When a same-named check run is skipped on the push and concluded on the dispatch, the concluded one counts; a matrix job skipped under its bare name is covered by its expanded runs. The router's concurrency group includes the input, so a dispatch never cancels the push run.

The release dispatches it itself when it can see the case coming: a resumed release whose new candidate would leave members skipped, after an earlier attempt already pushed a candidate, pushes the candidate, dispatches `run_all`, ties the created run to the candidate by its commit, and reads that run. A fresh release whose own candidate would leave a member skipped is refused before the push, since that is a defect of the filters.

## Batch releases

`rlsbl monorepo release run --watch` releases several releasables in one flow, in topological order, each through the single-releasable [release](release-workflow.md) from one representative member, in the release checkout, under one CI wait.

1. `rlsbl monorepo release init` writes `.strictmetadata/batch-releases/unreleased.toml` and commits it: one `[releasables.<name>]` table per releasable a repeated `--releasables <name>` names, or, with `--all`, per declared releasable (one of the two is required: the set is never inferred), with `bump` and `description` blank for a person to fill in and every target of the releasable's members in `include`. A releasable with no commit needing a changelog entry since its latest release is written commented out, and one with no target is refused, as is a file somebody already filled in.
2. Edit each table: `bump`, `description`, and optionally `context`, `include`, `exclude`, as in a single release file.
3. `rlsbl monorepo release run --watch --approve-consequential`.

`rlsbl monorepo release order` prints the order (with `--json`, the members in order, whether no member depends on another, and the releasables in order). A releasable's position is the highest of its members', ties broken by name, so every releasable is released after the releasables its members depend on. Before anything is released, the version and tag of every item are written to `.strictmetadata/.release-state/batch-plan.toml` and never recomputed mid-flight, so a run after a failure skips the items the plan proves shipped. A releasable whose lifecycle is on hold or retired is refused, and a server releasable is ordered after the clients it depends on. An uncommitted change to a path the batch writes refuses it, naming the path; every other is listed and left alone. A finished batch archives its file as `.strictmetadata/batch-releases/batch-<UTC time>.toml`.

## Status and listing

| Command | What it reports |
| --- | --- |
| `rlsbl monorepo list` | Every member in declaration order: name, path, releasable, flags. |
| `rlsbl monorepo status` | Per releasable: its version file's version, its latest release, its changelog coverage (counted as `rlsbl status` counts it), and its members. Per member: targets, version, releasable, flags, and dependency counts. |
| `rlsbl monorepo outdated` | Every dependency between members with the depended-on member's version: `ok`, `outdated`, or `versioned` (a constraint the evaluation does not read); a path, npm workspace, or explicit dependency shows its form. |
| `rlsbl monorepo check-names --target <target>` | The name of every member that is not dev-only (its `registry_name` as declared, or its name with `--prefix` and `--suffix`), with `check-name`'s verdicts and exit codes. |

## Maintenance

| Command | What it does |
| --- | --- |
| `rlsbl monorepo sync --auto-commit` | Regenerates the routers. |
| `rlsbl monorepo cleanup` | Removes the old layout's residue through saferm ([residue](on-disk-layout.md#residue-of-the-old-layout)). |
| `rlsbl monorepo rename-releasable <old> <new>` | Renames a releasable: its declarations, its directories, its record entries, and its routers, in one commit; when the tag format holds `{name}`, past archives record their old tag in `shipped_as` and one boundary alias tag is pushed at the current version ([renaming a releasable](conversions.md#renaming-a-releasable)). |
| `rlsbl monorepo extract`, `rlsbl monorepo absorb` | Move a releasable out of the workspace into its own repository, or a repository in ([repository conversions](conversions.md)). |

## Workspace checks

`rlsbl check --tag workspace`, run from the workspace root or any member, runs the workspace's checks; the list, with what each verifies, is in [the check system](checks.md#workspace).

+++
description = "The release declarations in .strictmetadata/releasables/releasables.toml: layout, release branches, timeouts, releasables, members, targets, pipelines, hooks, external checks, test settings, and dependency floors, plus the sandboxed test runner's test-runner.toml and the settings that belong to an option."
+++

# Declarations

What a repository is, for rlsbl, is declared in one file: `.strictmetadata/releasables/releasables.toml`. Every command that works on a project reads it, and nothing is derived where it could be declared: a releasable's name, its tag format, its publish mode, and the repository's layout are always written out. Behavior switches are not declarations; they are [options](checks.md#options), filed under `.strictmetadata/options/`.

The file is strict TOML validated by a strictspec schema (`.strictspec/releasables.schema.toml`). An unknown key at any level is refused, and so is a missing required one; an absent optional key stays absent when rlsbl rewrites the file, and comments are kept. Reading reports every problem it finds, in three passes: the schema, the typed decode, and the rules that relate one part of the document to another. The `declarations-valid` check reports the same problems, beside the options' refusals.

## A standalone project

```toml
format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "ci"

[[members]]
path = "."
name = "root"
releasable = "portal"
targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"
```

A standalone layout has one member, the root (`path = "."`, `name = "root"`), versioned under one releasable. A workspace is declared the same way with `repository_layout = "workspace"` and as many members and releasables as it holds; see [monorepo](monorepo.md).

## Top-level keys

| Key | Required | Meaning |
| --- | --- | --- |
| `format_version` | yes | `1`. |
| `repository_layout` | yes | `standalone` or `workspace`. Never derived from the member count. |
| `release_branches` | yes | The branches a release may run from, at least one. Every release, `release scrub`, and `transition declassify` refuse to run elsewhere, and the pre-push hook refuses a manual push to one. |
| `github_repository` | no | `owner/name`. Absent means the `origin` remote names the repository. |
| `environment_file` | no | A `KEY=VALUE` file loaded into the release's environment: an absolute path, a `~/` path, or a path relative to the repository root. A line of any other form is refused, naming it. |
| `[timeouts]` | no | `push_seconds`, `ci_seconds`, `check_seconds`, and `hook_seconds`, each optional. An absent key means the shipped timeout: 300 seconds for a push, 3600 for CI, 900 for a check; an absent `hook_seconds` lets hooks run without a timeout. The release's `--push-timeout`, `--ci-timeout`, `--check-timeout`, and `--hook-timeout` flags override them for one run. |
| `[[releasables]]` | yes | The units of versioning. |
| `[[members]]` | yes | The directories rlsbl knows, at least one. |

## Releasables

A releasable has one version, one changelog, one tag scheme, and one release at a time.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | One path segment, not hidden; it names the releasable's directories under `.strictmetadata/`. |
| `tag_format` | yes | One `{version}` placeholder and optionally `{name}`, which the releasable's name fills: `v{version}`, `{name}@v{version}`, or a Go path tag `<path>/v{version}`. A format whose rendered tag git refuses is refused, and so are two releasables whose formats render the same tags. |
| `publish_mode` | yes | `ci` publishes from CI through the members' pipelines; `none` publishes nothing and gets no publish workflow. |
| `publish_ci_check_pattern` | in one case | The pattern of the check-run names the root member's own CI reports, which publishing waits for. Required when, and only when, the layout is `workspace`, the releasable owns the root member, and it publishes from CI. |
| `deploy_command` | no | An argv the release runs after the GitHub Release exists, `{version}` replaced by the released version, for a server releasable. Allowed only on a releasable whose license is `proprietary` and whose `publish_mode` is `none` ([servers](lifecycle-and-license.md#the-rules)). |
| `hooks` | no | The releasable's own [hooks](#hooks). |

## Members

| Key | Required | Meaning |
| --- | --- | --- |
| `path` | yes | Repository-relative, canonical; the root member's is `.`. A member owns the files under its path that no deeper member claims. |
| `name` | yes | One path segment, unique; the root member's is `root`. |
| `releasable` | yes | The releasable the member is versioned under, or `false` for none. |
| `dev_only`, `library`, `test_only` | no | Flags. A dev-only member is development tooling; nothing that ships may depend on it at runtime (`dev-only-boundary`). |
| `depends_on` | no | Members this member depends on beyond what its manifests say. Naming itself is refused. |
| `import_name` | no | The name its code is imported by, where it differs from the member's name. |
| `registry_name` | no | The name it is published under, where it differs from the member's name. |
| `description` | no | What the member is; the one description a Homebrew formula takes. |
| `lint_allow` | no | Imports strictcode allows this member. |
| `internal_dep_floors` | no | [Dependency floors](#dependency-floors) to keep current. |
| `targets` | no | The member's release targets, each `{ name, path }` with `path` relative to the member (absent: the member's own directory). Absent means they are detected from its manifests. A declared target whose directory does not exist is refused. |
| `hooks` | no | The member's own [hooks](#hooks). |
| `external_checks` | no | [Checks](#external-checks) the member declares. |
| `test` | no | [Test settings](#test-settings). |
| `[[members.pipelines]]` | no | The member's [publish pipelines](pipelines.md). |

A target path, a hook directory, or an external check's directory inside another member's territory is refused.

## Hooks

`hooks` holds three lists, each optional: `pre_checks` (run before the checks), `pre_release` (run before the release commit), and `post_release` (run after the release). An entry is a shell command line, or a table `{ cmd, dir, env }` naming the command, the directory it runs in, and extra environment variables. An empty command is refused.

A releasable's hooks run from the directory of the member the release was started from, or from `dir`, which must lie in one of the releasable's members; a member's hooks run from the member's directory, or from `dir`, which must lie in its own territory. At pre-checks and post-release the releasable's hooks run first and then each member's, by name; at pre-release the members' run first. A declared `pre_release` hook on the releasable or the member replaces that member's built-in tests and nothing else: every preflight check still runs. Hooks get `RLSBL_VERSION`, `RLSBL_PREV_VERSION`, `RLSBL_BUMP_TYPE`, and `RLSBL_DESCRIPTION`, and a member's hook also `RLSBL_PACKAGE`, the member's name; a pre-checks or pre-release hook that exits non-zero stops the release, and every post-release hook runs, its failures recorded and named.

## External checks

A member declares checks of its own as shell commands:

```toml
[[members.external_checks]]
name = "typecheck"
tag = "preflight"
command = "npm run check"
depends_on = []
cwd = "."
```

`name` is lowercase letters, digits, and hyphens, and may not be a name a shipped check holds; `tag` is the tag it is selected under (`preflight` puts it in every release's preflight); `cwd` is relative to the member. The command runs through `sh -c` with the check budget (`check_seconds`) as its timeout, and a non-zero exit fails it. A missing program fails that check alone, when it runs. External checks are declared impure, so `--dry-run` lists them without starting them.

Each runs with the release context in its environment, resolved once per check run:

| Variable | Value |
| --- | --- |
| `RLSBL_PROJECT_ROOT` | The repository root, absolute. |
| `RLSBL_LAST_TAG` | The releasable's latest release tag, or empty when it has none (and for a member versioned under no releasable). Inside a release's preflight it is still the previous release. |
| `RLSBL_UNRELEASED_RANGE` | `<last tag>..HEAD`, or `HEAD` when there is no last tag. |

The hook variables (`RLSBL_VERSION` and the rest) are not given to checks; a check comparing against the last release reads `RLSBL_LAST_TAG`.

## Test settings

`test` changes how the built-in tests run for the member's targets; both keys are optional and an empty value is refused.

| Key | Effect |
| --- | --- |
| `pypi_markers` | Passed to pytest as `-m <markers>`. |
| `go_command` | Run through a shell from the target's directory in place of the built-in `go test`. |

The `test-suite` check and the release's built-in tests run the same command with the same budget. A setting narrows or replaces what runs; it never lets a failing test pass.

## Dependency floors

`internal_dep_floors` lists the ecosystem-internal packages whose declared `>=` floor must keep up with the version the lockfile resolves, so a consumer of the published artifact never resolves an older sibling than the one the repository was tested against. In a workspace every member's package name is policed too, without being listed. The `dep-floors` check enforces it; its option, `rlsbl:dep-floors`, is an adoption option, off by default, and the list is required while the option is on for the member and refused while it is off. `rlsbl rewrite uv-path-sources` and `rlsbl monorepo extract` add names to it and switch the option on where it is off.

| Ecosystem | Declared floor | Locked version |
| --- | --- | --- |
| pypi | `pyproject.toml` dependencies, extras, and dependency groups | `uv.lock` |
| npm | `package.json` `dependencies`, `peerDependencies`, `optionalDependencies` | `package-lock.json` |
| go | `go.mod` `require` | the same line: a `require` is the minimum, so Go needs no comparison |

The check fails when the lock resolves a policed dependency the manifest declares without a readable `>=` floor, or whose locked major.minor is above the floor's. Patch drift is allowed. Floors are not pins.

## The sandboxed test runner

`.strictmetadata/test-runner/test-runner.toml` declares the sandboxed test runner, the outer layer of testisolation's test isolation: the suite runs in a throwaway copy of the tree with the repository bound read-only, a throwaway `HOME`, and no network. It belongs to the `rlsbl:test-sandbox` option (`on > off`, default `off`): required while the option is on and refused while it is off. `rlsbl scaffold` renders the runner script, and `testisolation-floor` holds the repository to it.

| Key | Required | Meaning |
| --- | --- | --- |
| `format_version` | yes | `1`. |
| `runner_path` | yes | Where the runner script is written, relative to the repository root. |
| `command` | yes | The command run inside the sandbox; arguments given to the runner are appended. |
| `default_args` | no | Arguments used when the runner is given none. |
| `caches` | no | Toolchain caches bound into the sandbox, from `uv`, `go`, and `python_user_base`. |
| `prewarm` | no | Commands run outside the sandbox, with network, before it is entered. |
| `extra_env` | no | Variables exported inside the sandbox. |
| `ci_workflows` | no | Workflow files that must run the runner; one that does not fails `testisolation-floor`. |
| `carry_ignored` | no | Git-ignored paths copied into the sandbox when present. |

## Settings that belong to an option

A declaration that exists only while an option is on is required while it is on and refused while it is off, and each refusal names both ways out (deleting the declaration, or the `rlsbl options set` line that switches the option on):

| Option | Declaration |
| --- | --- |
| `rlsbl:dep-floors` (per member) | the member's `internal_dep_floors` |
| `rlsbl:test-sandbox` (the root member) | `.strictmetadata/test-runner/test-runner.toml` |

## Where the old configuration went

The old per-project `.rlsbl/config.json`, the workspace's `.rlsbl-monorepo/workspace.toml`, and the user-level `~/.rlsbl/config.json` are not read. `rlsbl migrate records` converts them ([the record migration](on-disk-layout.md#the-record-migration)): declarations move into `releasables.toml`; the `tag` switch becomes the `rlsbl:ecosystem-tagging` option; the lint, format, and type-check blocks, library-lint settings, and dead-module exclusions move to strictcode's `strictcode.toml`; `batch_limits.exclusions` becomes a `batch_reason` on each excluded changelog entry; and keys of dropped subsystems are deleted, or refused where they carry a value nothing can convert.

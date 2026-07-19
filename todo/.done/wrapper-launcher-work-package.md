# Wrapper/launcher publishing work package (decided 2026-07-17)

Successor to the (now `.done/`) pipeline-dir-field todo, which was investigated and
found half-superseded: targets already accept an explicit subdirectory path
(`{"name": "npm", "path": "packaging/npm"}` via `_parse_target_entry`,
`rlsbl/targets/__init__.py:108`), explicit targets bypass auto-detection entirely,
and version-bump / version-consistency / secret-scan are already path-aware. What
remains is the publish paths and the launcher story. All design decisions below are
RESOLVED (user-approved after a 10-solution exploration + worked-example analysis);
this is an execution spec, not a design doc.

## Part 1 — bug-close pass (pure correctness, zero migration, ship first)

1. **Local-publish primary_path collapse.** `execute.py:1834` passes a single
   `primary_path` to ALL pipelines. Fix: each pipeline publishes from its own linked
   target's path — `pl.publish(target_paths.get(pl.target, primary_path), ...)`.
   Live bug for any project with two `local: true` pipelines on different subdir
   targets. Red-green: two-local-pipeline fixture on distinct subdirs.
2. **Single-target scaffold emits root-anchored publish.yml.** `run_cmd`
   (`init_cmd.py:1616-1624`) renders publish templates with no working-directory
   injection, while the merged multi-target generator (`_generate_merged_publish`,
   `init_cmd.py:1990-2003`) already injects `defaults.run.working-directory`,
   rewrites `packages-dir`, and prefixes version-file inputs. Fix: route the
   single-target path through `_generate_merged_publish` with a one-element target
   list; delete the divergent raw-template branch. Byte-audit generated output for
   existing root projects (three-way merge will surface diffs).
3. **Monorepo root publisher disables subdir rewriting.** `publish_inline.py:354-356`
   hardcodes `target_paths={}` for the root package. Fix: pass real target_paths.
4. **Stale-artifact scan gap.** `clean_stale_artifacts` (`secret_scan.py:115`) cleans
   only the ROOT `dist/`; subdir targets' `dist/` dirs are scanned but never
   pre-cleaned. Fix: clean each target_paths subdir dist, mirroring the scan.

## Part 2 — `artifact: "launcher"` pipeline kind (the 5-variant)

Config shape:

```json
"pipelines": {
  "go":   {"type": "go",   "local": false, "target": "go",   "artifact": "binary"},
  "npm":  {"type": "npm",  "local": false, "target": "npm",  "artifact": "launcher",
           "wraps": "go", "binary_source": "github-release"},
  "pypi": {"type": "pypi", "local": false, "target": "pypi", "artifact": "launcher",
           "wraps": "go", "binary_source": "github-release"}
}
```

Mechanism: the `artifact` value selects launcher publish + scaffold templates via the
existing pipeline `template_mappings` seam (`_resolve_publish_template`,
`init_cmd.py:1851-1880` — the same seam Go's `library|binary` split already uses).
The wrapper manifest is a DECLARED SUBDIR TARGET, so version bump, version
consistency, and secret scan cover it with zero new code.

Four mandatory closures (each closes a verified failure mode — none optional):

1. **`needs` dependency from `wraps`.** Every launcher publish job must emit
   `needs: [gate, <producer's job>]`. Without it a shim can publish before its binary
   assets exist — a permanently broken package on a registry that cannot un-publish.
   The monorepo router must preserve the dependency when inlining.
2. **URL verify-before-publish.** The launcher job, before publishing, requests its
   own constructed release-asset URL for the runner's platform and hard-fails on 404.
   Turns goreleaser asset-naming drift (e.g. a custom name_template) into a red
   publish job at the release that introduced it, instead of silent 404s for all
   future installs.
3. **Per-ecosystem `binary_source` semantics, documented.** npm `github-release` =
   postinstall download. pypi has NO postinstall hook: `github-release` = first-run
   download to a cache dir. Embedded platform wheels are explicitly OUT of launcher
   scope (that is the platform-matrix model, Part 3's family). `binary_source` is
   required, no default.
4. **Manifest is the name authority.** Scaffold NEVER invents a package name: it
   hard-errors if the subdir manifest is absent, directing the user to create it with
   their chosen (check-name'd) name; scaffold then generates shim code and fills
   non-name manifest fields around it. Shim code is scaffold-managed (three-way
   merged); the name field is never touched.

Plus: a `wrapper-producer` check — `wraps` must name a declared target whose pipeline
has `artifact: "binary"`; hard error otherwise. Note the boundary: a launcher wraps a
BINARY. A root-level npm shim that wraps a Python package is a plain npm target and
must not be forced into this model.

Scope note: same-registry multiplicity is NOT a goal here — one launcher per registry
per project. Repos needing multiple same-registry packages already have the honest
model: monorepo members in a shared releasable (multi-artifact releasables publish
every member at the shared version). Document that decision rule in the launcher docs.

## Part 3 — unscoped wrapper family fix (standalone, do now)

The per-platform binary-wrapper family (npm_wrapper/crates_wrapper) activates on
scope-truthiness: `targets/go.py:256-288` gates on `npm_wrapper_config.get("scope")`,
so the ONLY way to use the feature is scoped npm names — which the ecosystem policy
bans. Same pattern duplicated in `targets/zig.py:126,157`. Fleet usage verified ZERO
(2026-07-17: no config anywhere sets npm_wrapper/crates_wrapper/npm_scope), so there
is no migration.

Fix: activation becomes an explicit `"npm_wrapper": {"enabled": true}` (exactly how
crates_wrapper already works — the asymmetry between the two is evidence the scope
gate was an accident); package names become bare suffixed names
(`<bin>-<platform>`), which per the never-assume-names policy require explicit
check-name'd approval like any other names; delete the `scope`/`npm_scope` keys
entirely from go.py AND zig.py; update the ~13 test files exercising these keys.
The family is NOT superseded (registry-hosted per-platform binaries are the
higher-quality distribution model vs download-at-install launchers) — per the
dead-code policy it gets wired up properly, not deleted.

## Sequencing

Part 1 first (prerequisite for Part 2's CI correctness; independently valuable).
Part 3 is independent and can run in parallel. Part 2 last.

## Effort

Part 1: small-medium (fixes + red-green fixtures + byte-audit).
Part 2: medium (templates, resolver branch, needs-emission, verify step, check, docs).
Part 3: small-medium (activation flip, name change, two files, 13 test files).

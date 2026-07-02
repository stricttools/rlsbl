# Maven Central / JVM support: audit findings

## Context

An independent audit of the Maven Central publishing and JVM monorepo support (the work described in `todo/.done/maven-central-and-jvm-monorepo.md`) verified each spec item against the code on disk. Most of the work is correct and well-tested, but the audit found 8 concrete bugs/gaps and some doc drift. Line numbers are as of the audit; verify before editing.

## Bugs

### 1. `maven-central-metadata` check never runs during release

The check exists (`checks/quality.py:304-322`, registered in `checks/__init__.py:106`) with tags `["quality", "maven"]` in `data/checks.toml:460` -- but it lacks the `preflight` tag, so `rlsbl release run` never executes it. The entire point of the check was to validate POM requirements before Maven Central rejects the upload, not after. Currently a user must remember to run `rlsbl check --tag maven` manually.

**Fix:** add `preflight` to the check's tags (scoped to maven targets so it no-ops elsewhere).

### 2. False-pass in sources/javadoc jar validation

`_check_source_javadoc_jars` in `maven_central.py:108, 119` has a regex that matches the plain `maven-publish` Gradle plugin id, with a comment claiming "vanniktech plugin auto-handles this." That's wrong: the plain `maven-publish` plugin does NOT generate sources/javadoc jars. The vanniktech plugin id is `com.vanniktech.maven.publish` and is matched by a separate pattern. Result: a project with only `id("maven-publish")` and zero jar configuration passes both checks, then gets rejected by Maven Central.

**Fix:** the `maven-publish` pattern must not count as evidence of sources/javadoc jar configuration. Only the vanniktech plugin (or explicit `withSourcesJar()`/`withJavadocJar()` / `java { withSourcesJar() ... }` blocks) should pass.

**Red-green:** write a test with a build.gradle.kts containing only `id("maven-publish")` and assert the check FAILS, then fix.

### 3. `dead-modules` registry/implementation mismatch

`CHECK_TARGETS` in `checks/__init__.py:86` declares maven support for `dead-modules`, but the check body at `checks/quality.py:91` has `supported = {"pypi", "go", "npm", "dart"}` and skips maven entirely. There is no `find_dead_jvm_modules` in `dep_validation.py`. Worse, `tests/test_feature_matrix.py:87-89` asserts the registry constant with a docstring claiming it matches the implementation -- it doesn't. The feature matrix advertises capability that does not exist.

**Fix:** either implement JVM dead-module detection (the import scanners in `import_scanners.py:554-743` already provide the raw data) or remove maven from the registry entry. Per the no-silent-degradation philosophy, a registry that lies is worse than a missing feature. If implementing, add the missing `find_dead_jvm_modules`.

### 4. `circular-deps` excludes maven without documented reason

The check's targets are `frozenset({"pypi", "npm", "dart"})` (`checks/__init__.py:87`), no JVM branch in the implementation (`quality.py:164`), and no `CHECK_EXCLUDED_TARGETS` entry justifying the exclusion (`checks/__init__.py:117-119`). Go has a justified exclusion (the compiler rejects circular imports); the JVM does not -- javac/kotlinc happily compile circular package references, so this is a genuine detection hole, and it was one of the four checks the original spec named for JVM import scanning.

**Fix:** add maven support to circular-deps using the JVM import scanners, or add a documented `CHECK_EXCLUDED_TARGETS` entry explaining why not.

### 5. `test-suite-workspace` omits maven

`checks/workspace.py:614` has `recognized = {"pypi", "go", "npm"}`. The standalone/release-path `test-suite` check recognizes maven (`checks/quality.py:284`) and works, but the pre-push affected-projects path silently skips JVM monorepo projects. A JVM project in a workspace gets zero pre-push test enforcement.

**Fix:** add maven to the recognized set in `test-suite-workspace`, dispatching to the same `_run_maven_tests` logic as the release path.

### 6. Version catalog hijacks the version source with no escape

`_find_version_file` in `targets/maven.py:262-265` makes `gradle/libs.versions.toml` Priority 0 with no opt-out. The common modern Gradle setup uses the version catalog ONLY for dependency versions, with the project version in `build.gradle.kts` or `gradle.properties`. Such a project cannot release: rlsbl demands `version_catalog_key` exist in the catalog's `[versions]` table and offers no config to redirect to another version source.

**Fix options:**
- (a) Only treat the catalog as the version source when `version_catalog_key` is explicitly set in `.rlsbl/config.json`; otherwise fall through to gradle.properties / build.gradle.kts / pom.xml. This fits the explicit-mode-selection philosophy: the config key's presence IS the choice.
- (b) Add a `version_source` config key selecting among the four file types.

Option (a) is the minimal, philosophy-consistent fix: no key -> catalog is ignored for versioning; key present -> catalog is authoritative and hard-error if the key is missing from `[versions]`.

### 7. Release build failures are non-fatal

`commands/release/execute.py:906-909` wraps all targets' `build()` calls in `except Exception: print Warning`, so a failed `./gradlew build` / `mvn package` prints a warning and the release proceeds to tag and push a broken artifact. This is a pre-existing pattern shared with other targets, but it contradicts both the original spec's intent ("verify the project compiles before proceeding") and the hard-errors-not-warnings philosophy.

**Fix:** make build failures abort the release. If some target genuinely needs non-fatal builds, that should be an explicit per-target property, not a blanket try/except. Audit which targets currently rely on the swallow before changing.

### 8. Version-catalog dependency declarations invisible to the dep graph

`MavenScanner` (`workspace_graph.py:223-524`) parses build.gradle.kts, Groovy, and pom.xml dependency declarations, but not `gradle/libs.versions.toml`. Deps declared as `implementation(libs.foo)` only surface as "unrecognized pattern" warnings and never enter the workspace graph -- so `monorepo impact` and release ordering are blind to them. Version catalogs were explicitly listed in the original spec.

**Fix:** parse the catalog's `[libraries]` table (tomlkit, same as the version-bump code path) to build an alias -> `group:artifact` map, then resolve `libs.<alias>` references in Gradle files against it. Cross-project references via catalog are rarer than `project(":module")` but external-dep scoping checks (deps-unused etc.) need the mapping too.

## Doc drift

- `docs/pipelines.md` documents only the old `maven` (GitHub Packages) pipeline (lines 260-268); the `maven-central` pipeline type is undocumented.
- The maven target's `publishSetup` scaffold hint (`targets/maven.py:524`) still says only "Requires GITHUB_TOKEN secret (auto-provided for GitHub Packages)" -- should describe the maven-central pipeline's 4 required secrets (`SONATYPE_USERNAME`, `SONATYPE_PASSWORD`, `GPG_SIGNING_KEY`, `GPG_SIGNING_KEY_PASSWORD`) when that pipeline is configured.
- The original spec's "scaffold should document or validate that the required secrets exist on the repo (or at least warn)" was never implemented -- no `gh secret list` check, no scaffold warning. `required_env_vars()` is only enforced for `local=true` publishes (`commands/release/validate.py:174-184`). Consider a preflight check that verifies the repo has the 4 secrets set when the maven-central pipeline is configured.

## Minor

- `_LOCKFILE_SYNC_TIMEOUT = 30`s is optimistic for a cold Gradle daemon (`execute.py`); gradle.lockfile sync degrades to a warning-level skip on timeout. Consider a longer per-tool timeout for gradle.
- The `maven-central-metadata` check does not verify that signing is configured at all (the original spec asked for "at least that signing is configured"). A cheap heuristic: vanniktech plugin present, or `signing` plugin + `useInMemoryPgpKeys`/`signAllPublications` reference.

## Affected files

- `data/checks.toml` -- bug 1
- `rlsbl/maven_central.py` -- bugs 2, minor signing check
- `rlsbl/checks/__init__.py` -- bugs 3, 4
- `rlsbl/checks/quality.py` -- bugs 3, 4
- `rlsbl/dep_validation.py` -- bug 3 (if implementing)
- `rlsbl/checks/workspace.py` -- bug 5
- `rlsbl/targets/maven.py` -- bug 6, doc drift
- `rlsbl/commands/release/execute.py` -- bug 7, minor timeout
- `rlsbl/workspace_graph.py` -- bug 8
- `docs/pipelines.md` -- doc drift
- `tests/test_feature_matrix.py` -- bug 3

## Effort estimate

- Bugs 1, 5: trivial (tag addition, set membership + dispatch)
- Bugs 2, 6: small (regex fix + red-green test; priority reorder gated on config key)
- Bug 7: small but needs an audit of existing targets relying on the swallow
- Bugs 3, 4, 8: medium (each builds on existing scanners/parsers)
- Doc drift + minors: small

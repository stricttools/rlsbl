# Maven Central publishing and JVM monorepo support

## Context

The `maven` target exists and handles basics: auto-detection (build.gradle.kts, build.gradle, pom.xml), version read/write, name extraction, and CI/publish templates. But the publish pipeline only targets GitHub Packages, which requires consumer authentication even for public repos -- useless for open-source libraries.

A real use case has arrived: reviving a set of Kotlin/Java libraries as an rlsbl-managed monorepo with CI that publishes to Maven Central.

Background on Maven Central publishing (post-OSSRH sunset, June 2025):
- All publishing goes through the Central Portal at central.sonatype.com
- Publisher authenticates with a user token (username + password pair generated from account page)
- GPG signing is mandatory -- every artifact (.jar, .pom, -sources.jar, -javadoc.jar) needs a .asc signature
- Artifacts are uploaded as ZIP bundles via the Central Portal API
- POM must include: groupId, artifactId, version, name, description, url, licenses, developers, scm
- Must provide -sources.jar and -javadoc.jar
- No OIDC Trusted Publishing (unlike PyPI) -- manual secrets required
- No SNAPSHOT hosting on Central Portal

Key tooling:
- vanniktech/gradle-maven-publish-plugin: most popular community plugin, handles signing + POM + sources/javadoc
- DanySK/publish-on-central: alternative, actively maintained
- Gradle's built-in `maven-publish` + `signing` plugins as the low-level foundation
- In-memory GPG signing via `ORG_GRADLE_PROJECT_signingInMemoryKey` env vars

## What needs to change

### Tier 1: Blocks publishing to Maven Central

#### 1. Maven Central publish pipeline

The current `maven` pipeline (`rlsbl/pipelines/maven.py`) only does `./gradlew publish` or `mvn deploy` with `GITHUB_TOKEN` for GitHub Packages. Needs a separate publishing mode (or replacement) that targets the Central Portal.

Two approaches:
- **A) Delegate to Gradle plugin**: The publish workflow runs `./gradlew publish` or `./gradlew publishToMavenCentral` and the project's build.gradle.kts configures the vanniktech plugin (or similar) to target Central. rlsbl's role is scaffolding the CI workflow with the right secrets and env vars. The actual Maven Central interaction happens inside Gradle.
- **B) rlsbl drives the Central Portal API directly**: rlsbl uploads the bundle ZIP via the Portal REST API, polls deployment status, etc. More control but duplicates what Gradle plugins already do.

Option A is strongly preferred -- it's how the ecosystem works and avoids rlsbl reimplementing complex Maven Central interaction logic.

#### 2. Publish workflow template for Maven Central

Current `rlsbl/templates/maven/publish.yml.tpl` only sets up `GITHUB_TOKEN`. Needs a Maven Central variant (or replacement) that:
- Passes Sonatype credentials: `SONATYPE_USERNAME`, `SONATYPE_PASSWORD` (or `MAVEN_CENTRAL_USERNAME`/`MAVEN_CENTRAL_PASSWORD`)
- Passes GPG signing material: `GPG_SIGNING_KEY` (base64-encoded private key), `GPG_SIGNING_KEY_PASSWORD`
- Maps these to `ORG_GRADLE_PROJECT_*` env vars so Gradle's signing plugin picks them up
- Runs the appropriate Gradle publish task

Required GitHub secrets (4): Sonatype username, Sonatype password, GPG private key (base64), GPG passphrase.

#### 3. GPG signing setup in CI

The workflow template must configure in-memory PGP signing. Standard pattern:
```yaml
env:
  ORG_GRADLE_PROJECT_signingInMemoryKey: ${{ secrets.GPG_SIGNING_KEY }}
  ORG_GRADLE_PROJECT_signingInMemoryKeyPassword: ${{ secrets.GPG_SIGNING_KEY_PASSWORD }}
  ORG_GRADLE_PROJECT_mavenCentralUsername: ${{ secrets.SONATYPE_USERNAME }}
  ORG_GRADLE_PROJECT_mavenCentralPassword: ${{ secrets.SONATYPE_PASSWORD }}
```

rlsbl scaffold should document or validate that the required secrets exist on the repo (or at least warn).

#### 4. Artifact requirements validation

Maven Central rejects uploads that don't meet its requirements. rlsbl should validate before releasing, not after Central rejects. Checks:
- POM has required metadata: name, description, url, licenses, developers, scm
- Build produces -sources.jar and -javadoc.jar
- All artifacts have .asc signatures (or at least that signing is configured)

This could be a new check tag (e.g., `--tag maven-central`) or integrated into the existing `--tag release` checks.

How to implement: either parse the POM XML directly, or run `./gradlew generatePomFileForMavenPublication` and inspect the output, or inspect the build.gradle.kts for the required metadata blocks. POM inspection after generation is most reliable.

#### 5. Test execution during release

`rlsbl/testing.py` `run_project_tests()` silently returns `True` for maven targets (falls through to the else branch). Must run `./gradlew test` (or `./gradlew check`).

Small fix -- add maven to the target dispatch in `run_project_tests()`.

### Tier 2: Needed for monorepo functionality

#### 6. JVM dependency graph parsing

Without this, `rlsbl monorepo impact`, dependency ordering in `rlsbl monorepo release`, and cross-project checks don't work. Currently deferred in `todo/.defer/dep-graph-ecosystems.md`.

Needs to parse dependency declarations from:
- `build.gradle.kts`: `implementation(project(":module"))`, `implementation("group:artifact:version")`
- `build.gradle` (Groovy): `implementation project(':module')`, `implementation 'group:artifact:version'`
- `gradle/libs.versions.toml` (version catalogs): libraries and versions sections

Complexity: Gradle files are executable code (Kotlin/Groovy), not static config. Perfect parsing is impossible without running Gradle. Pragmatic approach: regex/AST extraction covers 90%+ of real-world patterns. Could also shell out to `./gradlew dependencies --configuration runtimeClasspath` for authoritative output, but that's slow.

#### 7. JVM import scanning

Enables `deps-unused`, `deps-undeclared`, `circular-deps`, `dead-modules` checks. Needs to scan `.kt` and `.java` files for import statements and map them to declared dependencies.

Simpler than dependency graph parsing -- import statements are syntactically trivial (`import com.example.Foo`). The hard part is mapping package names to artifacts (which artifact provides `com.example.Foo`?).

#### 8. `read_metadata` capability for maven target

Extract license, description, and other metadata from Gradle files or POM. Useful for:
- Validating Maven Central requirements (item 4)
- `rlsbl config` display
- Future features that need project metadata

### Tier 3: Polish

#### 9. Gradle version catalog support for version bumping

Currently version write targets: `gradle.properties`, `build.gradle.kts`, `build.gradle`, `pom.xml`. Should also support `libs.versions.toml` where a version is defined in the `[versions]` table.

#### 10. Build step during release

`build()` method on the maven target is not overridden (no-op). Should run `./gradlew build` (or `./gradlew assemble`) to verify the project compiles before proceeding with the release.

#### 11. Lint execution for JVM

No lint tooling is configured for maven targets. Could integrate `./gradlew detekt` (Kotlin) or `./gradlew checkstyleMain` (Java) if detected, or just run `./gradlew check` which typically includes both tests and static analysis.

#### 12. Lockfile sync

`execute.py` syncs `uv.lock`, `package-lock.json`, `go.sum` but not `gradle.lockfile`. Minor since Gradle lockfiles are less commonly used than in other ecosystems.

## Affected files

- `rlsbl/targets/maven.py` -- items 4, 8, 9, 10
- `rlsbl/pipelines/maven.py` -- item 1
- `rlsbl/templates/maven/publish.yml.tpl` -- items 2, 3
- `rlsbl/templates/maven/ci.yml.tpl` -- possibly item 3
- `rlsbl/testing.py` -- item 5
- `rlsbl/checks/__init__.py` -- items 4, 7 (registering new checks for maven)
- `rlsbl/import_scanners.py` -- item 7
- `rlsbl/dep_graph/` or equivalent -- item 6
- `rlsbl/commands/release/execute.py` -- item 12

## Effort estimate

- Tier 1 (items 1-5): Medium. Items 1-3 are the core work (publish pipeline + template + signing). Item 4 is a new check. Item 5 is a one-liner.
- Tier 2 (items 6-8): Large. Dependency graph parsing (item 6) is the hardest piece -- Gradle files are code, not config.
- Tier 3 (items 9-12): Small individually, low priority.

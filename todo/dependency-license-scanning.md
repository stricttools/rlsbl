# Scan every dependency's license before a release

## Context

The family is adopting a release-time license check modeled on homebrew-core's
rule that packaged software must carry a license compatible with the Debian
Free Software Guidelines. Three parts of that check are decided for rlsbl's
release preflight:

1. a `LICENSE` file exists in the project;
2. its text matches the license declared in every manifest the project
   publishes (npm `package.json`, PyPI `pyproject.toml`, and any Go or other
   release metadata that states a license);
3. that license is on an approved list of open-source licenses.

Projects that are deliberately proprietary declare an exception as an option
in `.strictmetadata/options/` (fields `current`, `ideal`, and `reason`), never
through a flag.

The fourth part, checking the license of every dependency the release ships,
was deliberately left out of that first step because doing it correctly is
hard. This todo records what "correctly" requires, so the fourth part can be
designed and built later.

## Problem

A release can ship code whose dependencies carry licenses that are
incompatible with the project's own license or with the approved list (for
example a copyleft dependency inside a permissively licensed binary, or a
dependency with no license at all). Nothing detects this today.

## What correct scanning requires

- **The shipped set, not the declared set.** The dependencies that end up in
  the released artifact: for Go, the modules compiled into the binary (build
  constraints and platforms included); for npm and PyPI packages, the runtime
  dependency tree an install resolves, excluding development-only
  dependencies but including transitive and optional ones that can be
  installed.
- **Per-platform differences.** A Go binary built for several platforms can
  compile different modules per platform; npm and PyPI trees can differ by
  platform and by optional extras.
- **Declared identifier versus actual text.** Manifests declare SPDX
  identifiers that may be missing, wrong, or non-standard; the license text in
  a dependency's source may disagree with its declaration. A correct scan
  decides which one is authoritative and refuses on disagreement rather than
  guessing.
- **Compound expressions.** SPDX expressions such as `MIT OR Apache-2.0`
  (either may be chosen) and `GPL-2.0-only WITH Classpath-exception-2.0` need
  real evaluation against the approved list and the project's own license.
- **Compatibility, not just membership.** A dependency on the approved list
  can still be incompatible with the project's license in a given combination
  (copyleft obligations); the rule for combinations must be declared, not
  improvised per case.
- **Vendored and generated code.** Code copied into the repository rather
  than declared as a dependency carries its own license and is invisible to
  manifest-based scanning.
- **No network-dependent guesses.** The scan must be reproducible for a given
  release; results that depend on a registry answering at release time are a
  hard error when the registry does not answer, never a skipped dependency.

## Solutions

1. **Per-ecosystem scanners run by rlsbl.** For Go, derive the compiled module
   list per platform from the build (for example `go version -m` on the built
   binaries, which lists the modules actually linked) and read each module's
   license from the module cache; for npm and PyPI, resolve the lockfile's
   runtime tree.
   - Pros: exact for what ships, especially the Go path through the built
     binaries.
   - Cons: several scanners to maintain; license text detection is its own
     problem.
2. **Adopt established tools per ecosystem** (license scanners for Go
   modules, npm, and Python) behind one rlsbl check.
   - Pros: less detection logic to own.
   - Cons: each tool has its own model of "shipped" and its own
     identifier handling; their outputs must be normalized and their gaps
     understood.
3. **A declared allowlist of dependency licenses per project, checked against
   the lockfile.** The project records each dependency's license once, and the
   check refuses when the lockfile gains a dependency not recorded or when a
   recorded license changes.
   - Pros: every license decision is explicit and reviewable.
   - Cons: maintenance on every dependency change.

## Affected files

- The release preflight and the check registry.
- New scanner modules per ecosystem.
- The approved-license list and the combination rule, declared in one place.
- Tests: fixtures per ecosystem, including compound expressions, missing
  licenses, and a text-versus-declaration disagreement.

## Effort

Large. Each ecosystem's shipped-set derivation and license detection is its
own piece of work; the Go path through built binaries is the most tractable
starting point.

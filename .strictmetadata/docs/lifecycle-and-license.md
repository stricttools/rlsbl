+++
description = "The lifecycle-and-license record: each releasable's dated lifecycle, license, and identity periods, held registry names, and unversioned tags; confidential repositories and the confidential-name index; the rules rlsbl enforces from it; and the transition commands that write it, classify, and declassify."
+++

# Lifecycle and license

Each repository keeps one record of what its releasables are over time: `.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml`. The directory belongs to strictspec, which owns the record's schema and the library that evaluates it; rlsbl, selfdoc, and safegit link that library at the points where they enforce its rules. rlsbl writes the record through the `transition` commands and through the operations that change an identity (`monorepo rename-releasable`, `rewrite project-name`, `monorepo extract`, `monorepo absorb`).

## The record

```toml
format_version = 1
codenames = []                        # confidential repositories: extra names to protect
distinctive_terms = []                # confidential repositories: prose terms to protect

[[lifecycle]]
subject = "portal"                    # a releasable or member name
status = "active"                     # "active", "on-hold", or "retired"
from = 2026-10-07
reason = "..."

[[licenses]]
subject = "portal"
license = "MIT"                       # an SPDX identifier, or "proprietary"
from = 2026-10-07
reason = "..."

[[identities]]
subject = "portal"
facet = "releasable-name"             # releasable-name, package-name, project-name, go-module-path, tag-format, repository-url
value = "portal"
registry = ""                         # "npm", "pypi", "go", or "" for an identity that is not a registry name
tag_patterns = ["v*"]                 # the tag namespace the identity owned while its period ran
from = 2026-10-07
until = 2027-01-01                    # present once the period is closed
reason = "..."

[[identities]]                        # a pending identity, in effect from a version not yet released
subject = "portal"
facet = "package-name"
value = "portal-client"
registry = "npm"
tag_patterns = ["v*"]
effective_version = "0.5.0"           # in place of from
reason = "..."

[[registry_names]]
registry = "npm"
name = "portal"
subject = "portal"
recorded_since = 2026-10-07

[[unversioned_tags]]
tag = "nightly"
reason = "..."
recorded = 2026-10-07
```

Every table holds dated periods: `from` is a date, `until` closes the period, and a period without `until` is the one in effect. Periods of one subject in one table never overlap, and at most one is open. A closed period is never changed or removed, and a registry name, once recorded, stays: the record is append-only, and the `lifecycle-record-valid` check holds it to the record at the releasable's nearest release commit.

A **pending** identity carries `effective_version` instead of `from`. `rewrite project-name` writes one for each name it changes, effective from the version the next release ships; the release of that version turns it into a period starting on the release commit's date and closes the identity it replaces, in the same commit as the release archive.

A subject is a releasable or member the declarations hold, the subject of a closed period, or the subject of a `retired` lifecycle period, which need not be declared anywhere else.

## Confidential and public

No field declares a repository's disclosure. A repository is **confidential** while one of its releasables has a `proprietary` license period in effect, and **public** otherwise; a repository without a record is public. Every tool derives this from the license periods, offline. `codenames` and `distinctive_terms` are refused while the repository is public.

While a repository is confidential, its names (every proprietary releasable's name and identity values, the registry names recorded for them, the repository's own name when no releasable carries a non-proprietary license, the codenames, and the distinctive terms) are kept in a machine-local **confidential-name index**, `<user config directory>/strictspec/confidential-names.toml`, outside every repository and keyed by the repository's normalized origin URL. Each rlsbl `transition` command that writes the record brings the repository's entry in step after writing it (the names while the repository is confidential, no entry while it is public), and so does the record migration; selfdoc's mutating commands and every safegit commit do the same. Read-only commands only read the index. A confidential repository therefore needs an `origin` remote, and a confidential record in a repository without one is refused before anything is written.

The index is what keeps those names out of public places. safegit refuses a commit, in a public repository, whose message, added lines, or new paths contain one; selfdoc refuses to deploy or post a page that contains one; and rlsbl refuses, in a public repository, a release whose published text or packed artifacts contain one (the `confidential-names` check reports tracked files and changelog entries that do). Matching is case-insensitive on whole tokens.

## The rules

The rules are fixed in the library's code; nothing in the record names one. `rlsbl transition show` prints each rule's verdict on the command's date.

| Rule | What it decides | Where rlsbl enforces it |
| --- | --- | --- |
| proprietary requires private | A releasable whose license is `proprietary` makes the repository confidential, and GitHub must report the repository private. A private repository with no proprietary releasable is refused too. | Release validation; the `repository-visibility` check. |
| proprietary refuses public output | A proprietary releasable publishes to no registry and makes no registry write (no npm deprecate, no Go retract), gets no build attestation, no public docs, and no blog post. | Release validation of its pipelines; `release yank`. |
| private repository publishing | In a confidential or private repository: no npm build attestations (`--provenance`), no PyPI attestations, no Go module proxy request, no Go `library` pipeline, no Homebrew tap, and no published manifest field naming the repository (`package.json` `repository`, `homepage`, `bugs`; `pyproject.toml` `[project.urls]`). | Release validation; the `private-repo-publishing` check; the publish workflows `rlsbl scaffold` renders. |
| lifecycle allows release | A subject whose lifecycle is `on-hold` or `retired` is not released. | Release validation and batch release planning; `releasable-residue` leaves a retired subject's history alone. |
| confidential names | While the repository is confidential, its names go into the index. | The `transition` commands that write the record; the `confidential-names` check and release validation read the index. |
| proprietary history is squashed | Before the repository goes public, the commits of each proprietary period are squashed into one commit per period whose message names no confidential term. | `transition declassify`. |
| identity owns its tags | A tag matching an identity's `tag_patterns`, created while that identity's period ran, belongs to it; once the period closes, those tags are accounted for, never demanded, scrubbed, or refused. | `release backfill`, `release reconcile`, `unpublished-refs`, `go-companion-tags`. |
| registry names are held | A recorded registry name stays in the record, and no rlsbl command unpublishes a package. | `lifecycle-record-valid`. |
| closed periods are final | A closed period of any table is never changed or removed. | `lifecycle-record-valid`. |

A **server** releasable, one declaring `deploy_command` in its declarations, must be proprietary and must publish nothing (`publish_mode = "none"`): registries receive a client, and the server holding the proprietary logic is deployed instead. The release runs the deploy command after the GitHub Release exists ([the release steps](release-workflow.md#the-release-steps)).

## The transition commands

Every command that changes the record closes the period in effect on the command's date and opens the new one, validates the whole record against the declarations, writes it, brings the confidential-name index in step, and commits the record alone with the `Autogenerated: true` trailer. All but `show` and `init-minimal-record` are consequential.

| Command | What it records |
| --- | --- |
| `rlsbl transition show` | Nothing: prints the record and every rule's verdict. Works without declarations and without a record. |
| `rlsbl transition lifecycle --subject <name> --status active\|on-hold\|retired --reason <text>` | A lifecycle period. `on-hold` refuses the subject's releases; `retired` makes its release history a record. |
| `rlsbl transition license --subject <releasable> --license <SPDX> --reason <text>` | A license period. `proprietary` is refused (use `classify`), and so is a change that would leave no releasable proprietary in a confidential repository (use `declassify`). |
| `rlsbl transition identity --subject <name> --facet <facet> --value <value> [--registry <registry>] [--tag-pattern <glob>]... [--from <date> [--until <date>]] --reason <text>` | An identity period. Without `--from` it starts on the command's date and closes the subject's open identity of the facet; with `--from` (and `--until`) it is recorded as given, which is how a dead identity's tags become accounted for. In a tag pattern `*` matches any run of characters, slashes included. |
| `rlsbl transition unversioned-tag --tag <tag> --reason <text>` | A tag that releases no version (a nightly marker, an imported vendor tag), so the backfill, the reconcile, and the tag checks account for it. |
| `rlsbl transition classify --subject <releasable> --reason <text>` | A `proprietary` license period, which makes the repository confidential. GitHub must already report the repository private; making it private is the owner's step outside rlsbl. Deprecate and yank anything the releasable published first: afterwards no registry write is made for it. |
| `rlsbl transition declassify --license <releasable>=<SPDX>... --reason <text>` | Takes a confidential repository public (below). |
| `rlsbl transition init-minimal-record` | Writes a record holding only `format_version = 1`, with its manifest, in a repository rlsbl does not manage, so safegit and selfdoc have a record to read. A repository holding release declarations or an old-layout directory is refused, and so is one that already holds a record. |

### Declassifying

`rlsbl transition declassify` takes one `--license` for every releasable whose license is `proprietary`, and no other. In order, it:

1. squashes each proprietary period's commits on the release branch's first-parent history (dated by committer) into one commit with a fixed message that names no confidential term, through `safegit scrub squash`, keeping the public history before and after; a period whose commits are not contiguous, or that holds a merge commit, is refused before anything is rewritten, naming the commit;
2. repairs the changelog's commit ids and the archives' release commits through the rewrite, recording a release unrecoverable when its commit was folded into a commit carrying another tree;
3. closes the proprietary license periods and opens the `--license` ones;
4. removes the codenames, the distinctive terms, and the repository's index entry;
5. writes a history-rewrite archive and commits everything;
6. force-pushes the branch and every moved tag with leases taken before the first squash, rewrites each moved tag's GitHub Release from the record, and makes the GitHub repository public.

A run that stops is finished by running the same command again. It is refused while a release or a scrub is in progress, off a release branch, without an `origin` remote, and when `--reason` names a confidential term. It requires safegit 0.31.0 or newer. `--dry-run` prints the squashes and writes nothing.

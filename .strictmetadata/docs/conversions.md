+++
description = "Moving releasables and renaming identities: monorepo extract and absorb with their tag policy, verification, refusals, and next steps; splitting a member out of a shared releasable; monorepo rename-releasable; rewrite project-name and its pending identities; and the transition record of repository surgery."
+++

# Repository conversions

The **releasable** is the unit a repository boundary moves: it owns a version, a changelog, a tag scheme, and a release record, and every one of them has to arrive with its code for the history to stay true. `rlsbl monorepo extract` moves a releasable out of a workspace into a repository of its own; `rlsbl monorepo absorb` moves a repository into a workspace as a member. Both are consequential, both print their whole plan under `--dry-run`, and both make every refusal before anything is written.

A conversion moves code, history, records, and tags. It pushes nothing and administers no external system: creating the remote repository, registering a trusted publisher, and archiving the old repository are printed as next steps for the operator.

## Extract

```bash
rlsbl monorepo extract widget ../widget --dry-run
rlsbl monorepo extract widget ../widget --approve-consequential
```

`rlsbl monorepo extract <releasable> <target path>` creates a new repository at the target path, which must not exist:

1. The members' history is rewritten by `git-filter-repo` on a fresh clone; a lone member is hoisted to the repository root.
2. Each member's tree must be the tree that left: the git tree of the member at the source's `HEAD` is compared with the tree at its new path, and a difference stops the extract with the source untouched.
3. The releasable's changelog and release records move to the new repository's `.strictmetadata/`, every commit id and release commit mapped through `git-filter-repo`'s commit map. A changelog entry none of whose commits carried over is dropped, a release commit the rewrite did not carry is left as recorded, and each is named; a recorded tree the rewrite changed stops the extract.
4. Its tags take the new repository's scheme: `v{version}` for a lone member, its own tag format otherwise. The current version keeps its old name beside the new one (the boundary alias), and another releasable's tags are deleted in the new repository.
5. The new repository gets its declarations, a lifecycle-and-license record holding every entry of the departing subjects, and a transition record explaining the conversion, each committed.
6. The source loses the members, the releasable, and its records in one commit: the departure of its tag namespace is recorded (`departed-globs`), the departing subjects' open periods and identities are closed in its lifecycle-and-license record, the departing package names join the `internal_dep_floors` of every member of a releasable that stays (with `rlsbl:dep-floors` switched on where it is off), and the routers are regenerated. A failure before that commit puts every path back as it was.

Deletions go through saferm unless `--delete-with-rm` is passed. The source's tags of the departed releasable stay where they are: deleting a published tag acts on a namespace consumers already resolve, and the `departed-globs` event is what explains them.

Extract refuses, before anything is written: a releasable holding the root member; a departing member enclosing a member that stays; a departing member with nothing tracked or holding a submodule; a target path that exists; a missing `git-filter-repo` or saferm; uncommitted changes; a release or batch release in progress; options entries scoped to a departing member; a member that stays depending on one that leaves; a renamed tag colliding with another tag; and declarations or records the result would leave invalid.

### Severing an inbound edge

A member that stays and depends on one that leaves would be left pointing at nothing, so extract refuses it and prints every edit that severs the edge, read from the depending member's manifests:

| Ecosystem | The edit |
| --- | --- |
| Python | `rlsbl rewrite uv-path-sources`, run in the depending member, deletes the `[tool.uv.sources]` entry and floors the dependency at the version the lock resolves. |
| Go | `rlsbl rewrite go-module-path --from-module <old> --to-module <new>` at the repository root, before extracting, since the module path moves with the code. |
| npm | A hand edit: a published range in place of the workspace spec. |
| Declared | Remove the name from the member's `depends_on` in `releasables.toml`. |

A departing member's dependency on a member that stays is reported in the plan, not refused: it becomes an ordinary registry dependency.

### After an extract

The printed next steps: create the remote repository and add it as `origin` there; run `rlsbl scaffold` there for its workflows and hooks; review the regenerated routers here. A departing target whose publisher is authorized per repository (PyPI's trusted publishing) needs the new repository registered before its next release, and a publish that failed for want of it is retried with `rlsbl release retry --watch`, never by burning a version.

Extract keeps no state and has no resume: everything before the source's commit is undone by deleting the target directory, and a failure in the source's step restores every path it changed. Fix the cause, delete the target directory, and run it again.

## Absorb

```bash
rlsbl monorepo absorb ../widget packages/widget --tag-format '{name}@v{version}' --license MIT --dry-run
rlsbl monorepo absorb ../widget packages/widget --tag-format '{name}@v{version}' --license MIT --approve-consequential
```

`rlsbl monorepo absorb <source repository> <destination path>` brings a repository into the workspace as a member:

1. Its history is rewritten under the destination path by `git-filter-repo` on a clone inside this repository's git directory, fetched without tags, and merged. The merge carries trailers naming the member, the source's root commit, and the releasable, by which a run completing an interrupted absorb finds it.
2. Its version tags are created under the releasable's scheme at the rewritten commits, and its tag of the current version beside them under its own name. Tags are created by rlsbl, never fetched, so no tag here is ever moved or deleted.
3. Its records move into this repository's layout: its changelog and release archives into the releasable's directories, every commit id and release commit mapped and every recorded tree checked at the new commit; its lifecycle-and-license entries into this repository's record under the member's name, with the source recorded as the member's closed `repository-url` identity; and its transition record's events into this repository's, scoped to the releasable.
4. The arriving changelog and release directories and its `CHANGELOG.md` are deleted (through saferm unless `--delete-with-rm`); the rest of its `.strictmetadata/` is [residue](on-disk-layout.md#residue-of-the-old-layout) that `rlsbl monorepo cleanup` removes.
5. The member is declared and scaffolded, which regenerates the routers, and the absorb is recorded in the transition record, each committed.

`--releasable` joins a declared releasable. Without it, a releasable named after the member is created with the stated `--tag-format` and `--license`, and with `--publish-mode` when the source declares none; `--license` is refused with `--releasable`. The created releasable keeps the lifecycle period and the license period the source's record holds for it (a `--license` naming another license is refused), and otherwise gets an `active` lifecycle period and a license period under `--license` from today; its `releasable-name` identity takes its own name. A `proprietary` license requires GitHub to report this repository private. `--name` and `--registry-name` fill the member's keys. The source may be a standalone project in the new layout or a repository declaring nothing; one in the old layout, and a workspace, are refused.

Absorb refuses, before anything is written: a dirty source or workspace; a destination path, a name, a tag, or a version this workspace holds already (one version is one release, and two records cannot both be it); a version the source cannot state; and records the result would leave invalid. The source repository is never changed, and nothing is pushed. A run that stops is completed by running the same command again.

After an absorb, review the arriving member against the workspace's conventions and the regenerated routers, and archive the source repository yourself; the `old-repo-archived` check reports it until GitHub shows it archived.

## Splitting a member out of a shared releasable

Extract takes whole releasables. To move one member of a releasable it shares with others, split the releasable inside the workspace first, then extract the new one:

1. Declare a new `[[releasables]]` entry in `releasables.toml`, with its `tag_format` and `publish_mode`, and point the departing member's `releasable` at it.
2. Write its starting version into `.strictmetadata/releases/<new releasable>/version`. Whether the member continues the shared version, restarts, or takes the version it last published under its own name is a judgment only a person can make.
3. Move the entries that belong to the departing member from the shared releasable's `unreleased.jsonl` to the new releasable's. Released changelog files and archives stay with the shared releasable: they record what it shipped.
4. Record the new releasable's identities in the lifecycle-and-license record (`rlsbl transition identity`, `rlsbl transition license`, `rlsbl transition lifecycle`), commit everything, and run `rlsbl monorepo extract <new releasable> <path> --dry-run`.

## Renaming a releasable

```bash
rlsbl monorepo rename-releasable widget gadget --dry-run
rlsbl monorepo rename-releasable widget gadget --approve-consequential
```

`rlsbl monorepo rename-releasable <old> <new>` rewrites the `[[releasables]]` name and every member's `releasable` field in place; moves the changelog, release, and run-state directories to the new name; removes a file at its reserved changelog validation cache path, which no rlsbl command writes, through saferm when one is there; closes, on the rename's date, every open lifecycle period, license period, and identity of the old name in the lifecycle-and-license record and opens each again under the new name (the `releasable-name` identity taking the new name, and each identity the tag namespace the new name renders); and regenerates the routers. All of it is one commit without the `Autogenerated: true` trailer, so changelog coverage decides whether it needs an entry, and when it does the closing message prints the `rlsbl changelog add` for it.

When the tag format holds `{name}`, the tags change spelling. Each past release whose old tag stands at its release commit records that tag in its archive's `shipped_as`, so every later command resolves the old release under the tag it shipped under; and the current version's tag under the new name is created at its old tag's commit, recorded as a `boundary-alias` event, and pushed to origin, the rename's one remote write.

It refuses, before anything is written: a new name that is declared, a member's name, or holding another subject's state; uncommitted changes; a release or batch release in progress, or a batch release file naming the old releasable; and a record without the releasable's open `releasable-name` identity, or holding a pending identity of it. A run whose declarations already name the new releasable completes an interrupted rename.

## Renaming a standalone project

`rlsbl rewrite project-name` renames a standalone project's published identity, the name consumers install:

```bash
rlsbl rewrite project-name --from widget --to gadget --dry-run
rlsbl rewrite project-name --from widget --to gadget --approve-consequential
```

- **The package name** in every manifest whose name is a field: `package.json` `"name"` and `pyproject.toml` `[project].name`.
- **The Go module path**, for a Go target: moved to a path whose last element is `--to`, through the same rewrite as `rlsbl rewrite go-module-path`.
- **Pending identities** in the [lifecycle-and-license record](lifecycle-and-license.md): `package-name`, once for each registry whose manifest carries the name (`npm`, `pypi`), and `go-module-path` when the latest release published another module path (read from `go.mod` at its release commit). Each takes effect from the version the next release ships, decided as the release decides it, keeps the tag namespace of the identity of its registry it replaces, and becomes a dated period when that version is released. From then on, `rlsbl release reconcile` refuses to recreate an earlier version's Go refs under the new identity.

The rename and the record are committed separately, so a crash between them is completed by running again; the record commit carries the `Autogenerated: true` trailer and the rename commit does not, so changelog coverage asks for the breaking entry the closing message prints. It moves no source directory, renames no command (npm `bin`, PyPI `[project.scripts]`), contacts no registry, and renames no repository; the closing message lists those steps in order.

It refuses, before anything is written: a workspace (naming `rlsbl monorepo rename-releasable`); a `--from` the manifests do not declare; a `--to` equal to `--from` or invalid as a package name for any target; a missing release file; a next version the release could not decide; a Go target whose latest release is unrecoverable, since the module path it published cannot be established; another pending identity of the same facet (and registry, for a package name); and a dirty working tree.

## The transition record

`.strictmetadata/transitions/transitions.jsonl` records repository surgery: one JSON event per line, `format_version` 2, discriminated by `event`. It records history and never drives it: a reader consults it to explain a divergence it has already observed. Facts about what a releasable is (its lifecycle, its identities, its tags outside the version model) belong to the [lifecycle-and-license record](lifecycle-and-license.md), not here.

| Event | Written when |
| --- | --- |
| `conversion` | An extract or absorb ran: the direction, both endpoints with their tag formats, and the commit. |
| `tag-map` | Tags were renamed or created by a conversion: each old-to-new name with its commit. |
| `release-commit-remap` | A rewrite moved archived release commits: the rewrite and each old-to-new pair. Written by `release scrub`, `release reconcile`, the conversions, and `transition declassify`. |
| `boundary-alias` | An alias tag was created at a version: by a conversion, or by `monorepo rename-releasable`. |
| `departed-globs` | In the source of an extract: the tag globs that stopped belonging here, and where they went. |

The events a workspace scopes to one releasable carry a `releasable` field, and the events of one conversion carry `related_to`, the id of their `conversion` event. Every line is validated by a generated validator, and event ids are unique within the file. The record is appended by reading it and writing it back whole by atomic replacement, earlier lines byte for byte, under the release lock.

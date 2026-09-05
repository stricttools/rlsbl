---
description: "Moving a releasable between repositories: extract's two engines and absorb, tag policy, tree and release commit verification, splitting a releasable, and the transition record with every event kind it holds and the two an operator declares."
---

# Repository conversions

A conversion moves a **releasable** across a repository boundary. `rlsbl monorepo extract` moves one out of a workspace into a repository of its own; `rlsbl monorepo absorb` moves an external repository in as a member of a workspace. They are the same operation in opposite directions, and they share one unit, one shape, and one set of promises.

Both commands are mutating and consequential: a real run prompts before it starts unless `--approve-consequential` is passed, and `--dry-run` renders the whole plan and stops without writing.

## The releasable is the portable unit

A releasable owns a version, a changelog, a directory of archived release files, and a tag scheme. A single member package owns none of those on its own, so "extract this package" was never a complete question -- there would be no version to carry, no changelog to move, and no tags to translate. Extraction therefore takes a releasable name and moves every member it owns:

```bash
rlsbl monorepo extract <releasable_name> <target_path> --dry-run
```

Naming a member instead of a releasable is refused by name, listing what the workspace actually declares:

```
Error: releasable 'pkga' not found in this workspace. Available: core, solo
```

Absorption is the same rule read backwards. An external repository already *is* a releasable -- it has a version, a changelog and tags -- so it arrives as one. `--releasable` names an existing group for it to join; without it, a singleton releasable named after the arriving member is created, with its `tag_format` written out explicitly rather than inherited.

```bash
rlsbl monorepo absorb <source_repo> <dest_path> --dry-run
```

The destination's shape follows the member count. A releasable with one member becomes a flat standalone repository: the member's directory is hoisted to the repository root, its state moves to `.rlsbl/`, and `.rlsbl/releasable.toml` records the releasable's name and the standalone tag format `v{version}`. A releasable with more than one member becomes a workspace of its own, with a `.rlsbl-monorepo/workspace.toml` carrying a dev-node root member, the extracted members at their existing paths, and the releasable declared with an explicit `tag_format`.

## Two engines: filter, or promote the mirror

Extraction has two engines, and one fact decides which runs: whether the releasable declares a `subtree_remote` — whether it is [mirrored](monorepo.md#mirror). Everything else in this chapter applies to both, except where it names the filter.

An **unmirrored** releasable is *filtered*. A fresh clone of the source is rewritten with `git-filter-repo` down to the union of the member paths, hoisted to the repository root when the releasable has a single member.

A **mirrored** releasable is *promoted*. The mirror already holds this subtree's standalone history — every commit that touched the member has a synthetic counterpart there, produced by the deterministic subtree split — and consumers already resolve those commit ids. Filtering the monorepo again would build a second standalone history of the same code under commit ids nobody resolves, so the destination starts from the mirror instead:

- the destination is a clone of the mirror, whose remote becomes the new repository's `origin`;
- the monorepo-to-mirror correspondence is derived by splitting each commit the conversion has to translate, and every changelog hash and release commit is remapped through it rather than through a filter-repo commit map;
- the correspondence is persisted into the destination's transition record as a `promotion-split-map` [event](#what-gets-recorded), so the promoted repository can explain its own hashes without the monorepo;
- `git-filter-repo` is neither required nor invoked. A promotion filters nothing, so the missing-tool precondition does not apply to it.

A releasable with more than one member may not declare a `subtree_remote` at all — there would be no single subtree to mirror — so a promotion is always a single-member extract to a flat standalone repository.

After a promotion the mirror stops being a derived artifact: nothing regenerates it, and a force-push to it is destructive. It also carries no publish workflow (a mirror's scaffold renders none, and every convergence sweeps any that reached it another way), so a promoted repository that publishes needs `rlsbl scaffold` run in it.

## Observe, then render or apply

Both commands are reconcilers. Observation resolves everything the apply will act on and refuses everything that cannot be done, and it runs before anything is written -- so a `--dry-run` refuses exactly what a real run would refuse, and neither has written anything by the time it does. What the preview renders is the apply pipeline: each item is a step, applied in the order it was printed.

Extract's plan:

| Item | What it covers |
| ---- | -------------- |
| `releasable` | Which members leave, the tag-format change, and the engine: the `git-filter-repo` invocation that produces the new history, or — for a mirrored releasable — that the mirror is cloned and promoted |
| `dependencies` | Edges that leave the workspace with the members (edges *into* them are a refusal, not a plan item) |
| `trees` | The per-member tree hash that must survive the filter unchanged; for a promotion, the one member tree that must already equal the mirror's |
| `state` | The state directory being transplanted, and the release commits that will be remapped |
| `tags` | Translations, the boundary alias, and the foreign tags being pruned |
| `destination` | The `workspace.toml` or `releasable.toml` the new repository gets |
| `transition-record` | The events recorded on both sides |
| `source` | What the source loses, where the dependency floors are declared, and that the tags stay behind |
| `next-steps` | The external administration rlsbl does not perform |

Absorb's plan:

| Item | What it covers |
| ---- | -------------- |
| `source` | The arriving repository, its targets, and the working clone under `.git/rlsbl/` |
| `releasable` | Whether a releasable is created or joined, and the tag format either way |
| `history` | The tag-free fetch and the unrelated-histories merge, with the trailers that identify it |
| `tags` | The version tags to import, the boundary alias, the tags that are not version tags, and anything already present from an earlier run |
| `state` | The changelog and archives moving into the releasable, and the per-package residue being removed |
| `workspace` | The `workspace.toml` entry, the scaffold, the sync and the snapshot |
| `transition-record` | The events recorded in the releasable |
| `next-steps` | The external administration rlsbl does not perform |

## What a conversion refuses

Every refusal below is raised during observation, so it costs nothing and fires identically under `--dry-run`.

| Direction | Refusal | Why, and what to do |
| --------- | ------- | ------------------- |
| extract | The releasable **owns the root member** (`path = "."`) | A workspace has exactly one root member and it owns every file no other member claims, so extracting it would leave the source with no root. Move the root member into a releasable that stays, or give the repository root a member of its own. |
| extract | A **remaining member depends on a departing member** | The edge would dangle. Extract never rewrites somebody's manifest as a side effect, so it refuses and names the exact edit -- see [Severing an inbound edge](#severing-an-inbound-edge). |
| extract | The releasable has **no state directory** | Its version, changelog and release archives are what the conversion moves. `rlsbl monorepo sync` creates the directory; if the releasable has already shipped, put its real version in the version file first. |
| extract | A member contains a **submodule**, or has **nothing tracked** at its path | A gitlink cannot be named in the source-side commit, so the conversion would delete the member and then fail to record it. An empty path has no tree to verify identity against. |
| extract | A **remaining** member's target declaration is broken | Its tags cannot be told apart from the departing releasable's, so the pruning decision would be a guess. A member with no `.rlsbl/config.json` is fine -- targets are auto-detected -- but one that has a config file must declare `targets` in it. |
| absorb | The **destination path is the repository root**, or the member name is `root` | Reserved for the member that owns the repository root. Absorb into a subdirectory, and pass `--name` if the basename collides. |
| absorb | The **path or name is already taken**, on disk or in `workspace.toml` | Two repositories cannot occupy one member path. |
| absorb | `--releasable` names a releasable that **does not exist**, or one with **no release state** | The arriving changelog and archives need a state directory to move into; `rlsbl monorepo sync` creates it. |
| absorb | `--tag-format` passed together with `--releasable` | The flag applies only to the releasable the command creates. An existing releasable brings its own format; change it in `workspace.toml` if it is wrong. |
| absorb | The source declares targets spanning **both tag schemes** | A releasable has exactly one tag format, and picking whichever target was detected first would tag the unit under a scheme nobody chose. State it with `--tag-format`. |
| absorb | The source's version cannot be determined | A releasable's version file is state the release flow bumps from, so it is never invented. Set the version in the source's manifest, or tag its current release. |
| absorb | A **tag name or a version collides** with the destination's | See [Collisions](#collisions). |
| both | The working tree is dirty, a release is in flight, or `saferm` is absent without `--delete-with-rm` | A history rewrite captures only committed state, so uncommitted work would be silently dropped. The tree is re-checked under rlsbl's advisory lock immediately before the first write. |
| extract | `git-filter-repo` is missing | Only for an **unmirrored** releasable, which is the only one whose history is filtered. A [promotion](#two-engines-filter-or-promote-the-mirror) filters nothing, so it never asks for the tool. |

## What rlsbl deliberately does not do

A conversion moves code, history, state and tags. It **administers no external system**, and it pushes nothing -- every tag it creates is local. Creating the remote repository, registering a Trusted Publisher, archiving the source repository, and scaffolding a brand-new standalone successor are printed as next steps and left to the operator. A tag reaches a remote through the release flow, which owns that namespace.

Extract also never rewrites a manifest. When a dependency edge has to be severed, it names the command that does it and stops.

## The tag policy

A conversion changes which repository a release history belongs to, so it has to answer what each tag now means. The rules below are the same in both directions; only the direction of the translation differs.

### Translation

Extract translates the releasable's **own** tags -- those matching the glob derived from its `tag_format` and name -- into the destination's scheme. Nothing translates when the format does not change, which is the case for a multi-member extract: the new workspace keeps `{name}@v{version}`, so every tag arrives under the name it already had. A single-member extract arrives in a standalone repository whose format is `v{version}`, so `solo@v0.1.0` becomes `v0.1.0`. A tag that matches the glob but does not parse as a version tag is left alone rather than renamed into something arbitrary.

Absorb translates in the other direction: every source tag that parses as a version tag under any scheme is created at the mapped commit under the destination releasable's format, so `v0.4.0` becomes `thing@v0.4.0`. Source tags that are not version tags are never imported, and are named in the plan as skipped. A tag whose commit the rewrite did not carry over is not imported either, and says so on stderr.

Absorb's tags are **created by rlsbl, never fetched**. The fetch runs with `--no-tags` on purpose: an ordinary fetch auto-follows the source's tags, and that is how a destination tag can end up moved or deleted. Bringing in no tag at all means every tag in the repository afterwards was created deliberately, at a commit rlsbl mapped.

### The boundary alias

Exactly one alias is created at the conversion point: the **current version keeps its pre-conversion name alongside its new one**, so a consumer that knows the old tag still resolves it in the new repository. Every other historical tag is renamed outright.

A single-member extract of `solo` at version 0.2.0 therefore produces both `v0.2.0` and `solo@v0.2.0` in the new repository, standing at the same commit. Absorbing that repository back produces `thing@v0.4.0` and `v0.4.0` the same way. The alias exists only when the names actually differ *and* a tag stands at the current version; a releasable whose current version was never tagged gets no alias, and neither does a conversion that changes no names.

### Pruning, and the conservative keep

The extracted clone starts as a full clone, so it carries every tag the source had. Tags matching another live member's or releasable's scheme are **pruned**: they are that project's release history and it stays behind.

What is left over is a tag that parses as a version tag under some scheme but belongs to neither the extracted releasable nor any current member. That tag is **kept**, and the reason is printed:

```
tag: keeping 'oldname@v0.1.0' -- it matches no current member's scheme,
so it is most likely this releasable's own history under an older prefix.
```

Release history is never destroyed on a guess. A tag left over from a rename is far more likely to be this releasable's own than anyone else's, so it travels, and the printed line is what makes an otherwise mysterious tag in a fresh repository explicable.

### Collisions

A collision is a preview error, not a runtime surprise. Tag names are the same in a clone as in the source, and the imported names are a pure function of the source's tags and the destination's format, so every collision is answerable before anything is written.

- **Extract, ref-name collision.** The translated name already exists, is not this releasable's, and stands at a different commit. Hard error naming both. When the existing tag stands at the *same* commit it is an earlier inbound conversion's boundary alias -- the translation it names has already happened, so the existing tag is kept rather than recreated.
- **Absorb, ref-name collision.** The name rlsbl would create already exists and is not this absorb's own earlier work. A destination tag belongs to the destination's release history and is never overwritten or deleted; resolve the conflict first.
- **Absorb, version collision.** A tag already stands at a version the source also carries, under whatever spelling, or the destination releasable's own release record (`changes/<version>.jsonl`, `releases/v<version>.toml`) already records that version. One version is one release, and two records cannot both be that version's -- `changelog generate` would have two sources for one section. Absorb into a releasable that does not carry the version, or reconcile the histories first.

### Go's path scheme

Go tags under the module proxy's path scheme (`pkgs/thing/v{version}`) rather than `{name}@v{version}`, and a releasable's `tag_format` carries that literally. Absorb derives a created releasable's format from the arriving member's primary target, so a Go source gets the path scheme written explicitly into `workspace.toml`; a source whose targets span both schemes has no single answer and is refused with `--tag-format` named as the remedy. Extract reads the format off the releasable, so a Go releasable's `pkgs/thing/v0.1.0` translates to `v0.1.0` in a standalone successor -- which is the scheme a root-level Go module actually publishes under.

### Departed tags stay in the source

An extract does **not** delete the departed releasable's tags from the source repository. Deleting a published tag is a destructive act on a namespace consumers already resolve, and the conversion never performs one. Instead the source records a `departed-globs` [transition record event](#transition-records) naming the tag globs that stopped belonging to it and where they went:

```json
{"kind":"departed-globs","globs":["solo@v*"],
 "destination":{"repo":"/path/to/solo","releasable":"solo","tag_format":"v{version}"}}
```

The tags are still there; the record is what explains them.

## Verification

A conversion's whole claim is that the code that arrived is the code that left. Two checks make that claim checkable rather than assumed.

### Member trees

Before the filter runs, extract records the git tree object of every member at the source's `HEAD`. After the filter, it recomputes each one at the member's destination path -- the repository root for a single-member extract -- and compares. A tree object is content-addressed, so equality is exact identity of the whole subtree, not a heuristic.

A [promotion](#two-engines-filter-or-promote-the-mirror) makes the same comparison against a different second hash: the source's `HEAD:<member>` must equal the root tree of the mirror's pre-scaffold split commit. That is also what justifies deleting the monorepo's copy -- a mirror that is behind the monorepo stops the promotion, with `rlsbl monorepo mirror <project>` named as the remedy, rather than losing the newer tree.

A mismatch is a hard error naming both hashes, and nothing further is written:

```
tree verification failed for member 'solo': the source tree at solo/ is <hash>,
but the filtered result at <repo root> is <hash>. The extracted history is not
the history that left, so nothing further was written; the source is untouched
and /path/to/target can be deleted.
```

Absorb has no equivalent per-member step, because its arriving content is a whole repository rather than a slice of one; it verifies through the release commits instead.

### Release commits

An archived release file (`releases/v<version>.toml`) records which commit a version shipped from and the git tree of every path it shipped. Both statements are made in the source's object graph, which the rewrite has just replaced, so both are rewritten: the commit through `git-filter-repo`'s commit map -- or, for a promotion, through the monorepo-to-mirror subtree-split correspondence -- and the trees recomputed at the new commit and the path the member now has.

The recomputed tree is **checked against the recorded one**, not merely written over it. A faithful rewrite reproduces a content-addressed hash exactly, so a disagreement means the content of a historical release changed under the rewrite -- and this is the only place that fact is observable. It is a hard error, raised while the destination can still be deleted and the source is still untouched.

Two outcomes are recorded rather than fatal, because failing here would leave a half-converted pair of repositories:

- a release commit whose commit the rewrite pruned is **left exactly as recorded** and named on stderr. Rewriting it to nothing would be worse: the fields are the record of what shipped.
- a recorded path that does not resolve at the rewritten commit gets the same treatment.

Both are reported again at the end of the run, and the transition record is what explains the stale value afterwards.

### Reading back through rlsbl's own loader

The last structural check is that the result is a repository rlsbl can actually read. An extract loads the new repository through the same loader every other command uses and hard-errors if the releasable does not read back under its own name; an absorb re-loads the workspace and hard-errors if it no longer declares the member or the releasable. Both directions also refuse to finish with a dirty working tree: each step's own output is committed by the step that wrote it, so anything left over was written by something else.

## After the conversion

### The printed next steps

The plan's final item is the list of things rlsbl deliberately leaves to the operator. For an extract:

- create the remote repository and add it as `origin` in the new repository;
- run `rlsbl scaffold` there for CI, hooks and workflows;
- review the regenerated CI router in the source before its next release. `monorepo sync` is re-run automatically, but which jobs the remaining members need is a human decision.

A [promotion](#two-engines-filter-or-promote-the-mirror) inherits the first step and rewrites the second. The mirror remote is already the new repository's `origin`, and the printed step says instead that it has stopped being a derived artifact -- nothing regenerates it now, and a force-push to it is destructive. `rlsbl scaffold` is still named, for the one thing the promoted repository does not inherit: a mirror's scaffold layer renders no publish workflow, because its Releases came from a monorepo release flow that no longer publishes for it.

For an absorb:

- review the arriving member against the workspace's conventions -- it was scaffolded for a standalone project, and the conversion re-scaffolds it as a member, but the conventions are yours;
- review the regenerated CI router;
- archive the source repository yourself. The conversion never touches it.

Both directions add one step per arriving or departing target whose **publisher is authorized per repository rather than per package** -- see the generated axis table in [Release targets](targets.md) for which targets declare that. Trusted Publishing does not follow the code across a repository boundary, so the new repository (extract) or this repository (absorb) has to be registered before its next release there. A publish that fails for want of that registration is recovered with `rlsbl release retry`, not by burning a new version.

### Severing an inbound edge

When a member that stays behind depends on one that would leave, extract refuses and prints the exact edit, decided by reading the depending member's manifests rather than by trusting the graph's edge type:

```
Error: members that stay behind depend on members that would leave:
  - 'pkga' depends on 'solo' (versioned, scope runtime)
    in pkga/pyproject.toml: delete [tool.uv.sources].solo and floor
    [dependencies] entry 'solo', so 'solo' resolves from the registry
    ('solo>=<the version the lock resolves>').
```

Every remedy that applies is printed, because one edge can be declared in more than one place -- a `depends_on` in `workspace.toml` *and* the manifest that really carries it -- and severing one leaves the other.

| Ecosystem | The edit, and what performs it |
| --------- | ------------------------------ |
| Python | `rlsbl rewrite uv-path-sources` deletes the `[tool.uv.sources]` entry and floors the dependency at the version the lock resolves. It reads the lock beside the manifest, or the uv-workspace lock of the nearest ancestor declaring `[tool.uv.workspace]` whose globs claim the directory; when neither exists it says so and the edit is by hand. See [rlsbl rewrite](cli-rewrite.md). |
| Go | `rlsbl rewrite go-module-path --from-module <old> --to-module <new>`, run at the repository root **before** extracting, since the module path moves with the code. |
| npm | A hand edit: replace the workspace spec with a published range and drop the member from any `workspaces` array. No rewrite command owns `package.json`. |
| Declared | Remove the name from `depends_on` in `.rlsbl-monorepo/workspace.toml`. |

Edges in the other direction -- from a departing member to one that stays -- are reported in the plan, not refused. That reference becomes an ordinary registry dependency, which is resolvable; the inbound direction is refused because it would leave a repository that stays behind pointing at nothing.

### Dependency floors

The departing packages are external packages from the source's point of view the moment they leave, so extract declares them in [`internal_dep_floors`](configuration.md#internal_dep_floors) in **every releasable that stays**:

```
internal_dep_floors: solo declared in .rlsbl-monorepo/releasables/core/config.json,
.rlsbl-monorepo/releasables/pkgb/config.json
```

Those are the configs the `dep-floors` check reads -- it compares a manifest against its lock using the config resolved for that releasable, so a declaration anywhere else polices nothing. When no releasable stays behind there is no config to write, and the conversion says so rather than inventing a home for it.

## Splitting one member out of a shared releasable

Extract takes releasables, whole. There is no command that pulls a single member out of a releasable it shares with others, and that is deliberate: two of the steps -- which changelog entries belong to the departing member, and what version it starts at -- are judgments only a person can make. A releasable's released changelogs and release archives describe the group, not any one member of it.

The procedure is to split the releasable **inside the workspace** first, then extract the now-singleton releasable mechanically.

**1. Declare the new releasable.** Add a `[[releasables]]` entry in `.rlsbl-monorepo/workspace.toml`, and change the departing member's `releasable` key to point at it:

```toml
[[releasables]]
name = "core"

[[releasables]]
name = "pkgb"

[[projects]]
path = "pkgb"
name = "pkgb"
releasable = "pkgb"
```

`tag_format` stays absent unless the new releasable needs a scheme other than the workspace's `{name}@v{version}` -- a Go member needs its path scheme, and a releasable that owns the repository root must declare one (the loader refuses one that does not). Absence is carried through loading and saving, so rlsbl neither invents the key nor deletes a line you wrote.

**2. Create its state directory.** A releasable declared in `workspace.toml` with nothing behind it is exactly what extract refuses, naming this command:

```bash
rlsbl monorepo sync
```

Sync writes `.rlsbl-monorepo/releasables/pkgb/version` (containing `0.0.0`) and an empty `.rlsbl-monorepo/releasables/pkgb/changes/unreleased.jsonl` for every declared releasable. Both files are user-owned: created once, never overwritten. Sync also regenerates the CI router, which now derives a paths filter for the new releasable's member.

**3. Set the starting version.** Put the version the member should carry into the new `version` file. This is the human judgment: the shared releasable's version described the group, and only you can say whether the departing member continues that number, restarts, or picks something else. If the member has already published under its own name, use the version it published.

**4. Attribute the unreleased changelog.** Move the entries that belong to the departing member from the shared releasable's `changes/unreleased.jsonl` into the new one's. Both files are ordinary writable JSONL, one entry per line, so this is a line move. Nothing splits them for you -- an entry describes a change, and which member a change belongs to is not derivable from the file.

**Released** changelogs and release archives stay with the original releasable. They are locked read-only records of what that releasable shipped, and a shipped version cannot retroactively belong to a member that did not exist as a releasable at the time. The departing member starts a fresh release history, and its old releases stay reachable under the original releasable's tags.

**5. Commit, then extract.** Both commands need a clean tree:

```bash
safegit commit -m "split pkgb into its own releasable" -- .rlsbl-monorepo
rlsbl monorepo extract pkgb ../pkgb --dry-run
```

From here everything is mechanical. The plan reports a single-member extract to a standalone repository, hoisting `pkgb/` to the root. The tag section will usually report nothing to translate, since a freshly split releasable has no tags of its own yet, and the shared releasable's tags are pruned from the clone as another live releasable's history.

## Healing and recovery

### Absorb re-runs to completion

Absorb detects every step before it repeats it, so a run interrupted anywhere is completed by running the same command again:

| Step | How a re-run recognizes it |
| ---- | -------------------------- |
| The merge | Its own `Rlsbl-Absorb` trailer, plus two identities the merge commit records: the source's root commit in `Rlsbl-Absorb-Source` and the target releasable in `Rlsbl-Absorb-Releasable` |
| A tag | It already stands at the mapped commit. A tag standing anywhere else is a hard error -- a tag rlsbl did not just create is never moved or deleted. |
| The workspace entry | Its content already declares the member at that path with that releasable |
| An unreleased changelog entry | Its entry id is already present in the releasable's `unreleased.jsonl` -- or, for an entry that carries no id, its content |
| A released changelog or a release archive | The file is already there and compares byte for byte. A file that differs is a hard error: a released version's record is immutable, so neither copy can be chosen over the other. |

Both merge identities are part of what a re-run is allowed to assume. The same member name and destination path absorbed from a *different* repository is not a re-run, it is a collision, and it is refused. A re-run aimed at a *different* releasable is refused too, because healing skips the version-overlap check on exactly the grounds that the target is unchanged -- skipping it while pointing somewhere new would bypass the one check guarding the releasable it just started pointing at. A merge that records no releasable trailer cannot answer that question, so it is refused rather than guessed at.

**A heal re-derives nothing.** Every value it writes comes from what the first run recorded, not from the re-run's source: a fork whose manifest has moved on cannot overwrite the version this conversion shipped. The re-run's source repository answers one question -- is this the same conversion? -- and nothing else. (A member that joined an *existing* releasable has no recorded version to prefer, because the absorb never wrote that releasable's version in the first place.)

The plan says all of this out loud on a re-run -- `history-already-merged`, `already present from an earlier run` -- so a `--dry-run` shows exactly how much is left to do.

### Extract has no resume

Extract has no state file and no resume, because everything before its last step is reversible by deletion. The apply order is: clone and filter (or, for a promotion, clone the mirror and derive its split correspondence), verify trees, transplant and remap state, apply the destination's tags, write its identity and commit it, record transition record, and only then edit the source.

- **A failure anywhere up to and including the transition record step leaves the source untouched.** The target directory is a self-contained partial result; delete it, fix the cause, and re-run. This is exactly what the tree-verification and release commit-verification errors say.
- **A failure inside the source-side edit** is the one case that needs a hand. That step appends the departure record, declares the floors, deletes the departed directories, rewrites `workspace.toml`, re-runs sync, regenerates the snapshot and commits all of it as one commit. A crash part-way leaves those edits uncommitted in the source's working tree, and the completed destination beside it. The deletions went through `saferm` unless `--delete-with-rm` was passed, so they are recoverable; the rest is ordinary uncommitted work. Finish or revert it by hand -- a re-run will refuse anyway, because the target path now exists and the source tree is dirty.
- **A leftover dirty tree after the commit** is reported rather than swept up: each step commits the files it wrote, so anything left belongs to something else.

## Transition records

A transition record is an append-only JSONL file, one event per line, recording what a conversion actually did. It **records history and never drives it** -- nothing in rlsbl branches on a transition record event. A reader consults the record to explain a divergence it has already observed, which is what makes it useful to later repair machinery, and what keeps it from becoming a hidden switch that changes behavior depending on a file's contents.

### Where a record lives

| Home | Path | Used by |
| ---- | ---- | ------- |
| Releasable | `.rlsbl-monorepo/releasables/<name>/transitions.jsonl` | A releasable's own conversion facts, in a workspace |
| Standalone | `<project>/.rlsbl/transitions.jsonl` | A standalone repository, including a standalone successor produced by an extract |
| Workspace | `<root>/.rlsbl-monorepo/transitions.jsonl` | Facts about the repository rather than any releasable in it |

The workspace-scoped home exists because a departure is a fact about the source repository's tag namespace, not about a releasable that is no longer there -- and there is nowhere else to put it. A workspace has no root `.rlsbl/` at all (the `root-rlsbl-conflict` check refuses one beside `.rlsbl-monorepo/`), and filing a repository-wide fact under some surviving releasable would be arbitrary.

### What gets recorded

| Event | Recorded when |
| ----- | ------------- |
| `conversion` | Always, first, naming the direction (`extract` or `absorb`), both endpoints with their tag formats, and the commit |
| `tag-map` | Tags were renamed or imported: every old-to-new correspondence with the new commit |
| `release-commit-remap` | Release commits moved: the rewrite that moved them, and every old-SHA-to-new-SHA pair |
| `boundary-alias` | An alias tag was created: the post-conversion name, the pre-conversion name it aliases, and the commit |
| `departed-globs` | Written in the **source** of an extract: the tag globs that stopped belonging here, and where they went |
| `identity-transition` | A published identity changed (a Go module path, a package name), effective from a stated version |
| `promotion-split-map` | A mirror was promoted: the subtree-split correspondence it produced |
| `release-history-closed` | A member's or releasable's release history is deliberately closed: the subject and an operator reason. Not written by a conversion — an operator declares it with `rlsbl transition record` |
| `non-version-tag` | One tag stands deliberately outside the version model: its name and an operator reason. Not written by a conversion — an operator declares it with `rlsbl transition record` |

### The two facts an operator declares

Every other event above is written by the operation that performed the surgery. The last two are not things a command did -- they are statements about a repository somebody read, and nothing can derive them. `rlsbl transition record` is their door:

```
rlsbl transition record --non-version-tag nightly-2026-01-01 \
    --reason "a nightly build marker"
rlsbl transition record --release-history-closed widget \
    --reason "extracted into its own repository"
```

Exactly one of the two facts must be elected, `--reason` is required and states why in the operator's own words, and the event is appended to the **repository-scoped** record -- `.rlsbl-monorepo/transitions.jsonl` in a workspace, `.rlsbl/transitions.jsonl` in a standalone repository -- and committed. A tag namespace belongs to the repository rather than to any one releasable, and a releasable whose release history just closed may be one whose state directory is about to leave with it.

The command is consequential: only a human may declare what a repository's history *is*, because both facts silence a reader that would otherwise keep reporting a divergence. A second declaration of the same kind about the same subject is refused, naming the event already recorded -- the record is append-only, so a duplicate would stand beside the first forever with nothing to say which one is meant. `--dry-run` prints the line it would append and writes nothing.

Recording a `release-history-closed` changes one answer immediately: the `releasable-residue` check stops reporting that member's release archives, changelog directory and version tags. Without the declaration they are a member's frozen release state that no release flow will ever finalize, advance or add to, and the check says so and names three ways out (move the member into a releasable, delete the state, or record the history as closed). With it they are the record of what that member released, and the check leaves them alone.

Recording a `non-version-tag` changes two answers immediately: `rlsbl release backfill` stops listing the tag as unexplained, and `rlsbl release reconcile` stops owing a verdict on it. Both readers ask the tag-namespace question over the repository-scoped record as well as the project's own, in a workspace exactly as in a standalone repository — the declaration is about a tag name, which is unique across a repository. (Version-keyed alias derivation stays scoped to the one project, because version numbers do collide across releasables.)

The `tag-map`, `release-commit-remap` and `boundary-alias` events a conversion writes carry `related_to`, pointing at the id of the `conversion` event that heads them, so the events of one conversion are recoverable as a group from a file that has accumulated several. A `departed-globs` event stands alone: it is written in the source, where the conversion event it would point at does not exist.

### Format and reading

Every line carries `format_version` as its leading key, and the shape of a line is validated by a strictspec-generated validator: the format gate, the `kind` discriminator, field types, required fields and unknown-key rejection. There is no legacy mode -- the format was created with the gate, so a line without it, or with any other value, is a hard error.

That error fires at the point a record is read **for use**, never during detection. Code that merely asks whether a repository has a record checks for the file's existence and validates nothing, so a malformed record breaks the one command that consumes it rather than every command that walks the tree. Reading the whole file also enforces the two properties a per-line validator cannot see: that the bytes are UTF-8, and that no event id repeats within a file. Both are reported by file and line.

Appends are one write per call, carrying the whole batch, and prior content is never read back and rewritten -- so a concurrent writer cannot be clobbered and an event already on disk is never modified. What it does not promise is durability across a machine crash: there is no `fsync`. The record explains history; it is not a transaction log.

+++
description = "The confidential-term list a release scans what it pushes and publishes against: hits, their ids, the confidential-terms check, false-positive judgments and owner approvals, and the release record."
+++

# Confidential terms

Some terms must never appear in anything public: the names of private projects, details about the owner, machine paths. They are kept in one place, the **confidential-term list**, outside every repository, and a release refuses to push or publish a **hit** (an occurrence of a term) until it is resolved. A commit is never refused for a term: a false positive never interrupts work, and every hit of a release is seen together.

## The list

The list is a TOML file encrypted with [age](https://age-encryption.org) to the identity in `~/.age-key.txt`, at the absolute path the `confidential_term_list` setting names in the family's settings file, `stricttools/config.toml` under the user's configuration directory (`$XDG_CONFIG_HOME`, else `~/.config`):

```toml
format_version = 1
confidential_term_list = "/absolute/path/of/terms.toml.age"
```

Nothing is filled in for it: a missing settings file or setting is an error naming what to write, and so is a path that is not absolute. Only the commands and checks that read the list refuse without it. rlsbl decrypts it in memory and never writes the plaintext anywhere. A list that does not exist has no terms, and every scan says so in its output; a list that exists and cannot be decrypted or read is an error. strictspec's `confidential` package reads it, and its documentation states the format: each `[[terms]]` entry has one `term` (a literal) or one `pattern` (a Go regular expression), the day it was `added`, a one-line `reason`, and a `scope`, either `"everywhere"` or `{ except = ["<repository>", ...] }`, which leaves out the repositories named (by their directory's name or their `origin` remote's last path segment).

Matching ignores case, and a literal matches anywhere, inside a longer word too: whether a match is a leak is judged hit by hit, never by the matcher.

## What a release scans

In a public repository, a release scans everything it would push or publish:

- the commits each push of the release branch carries: every commit's whole message (its trailers included), the path of every file it adds, and every line it adds or changes in a text file (a line the file already held in the version the commit changes is not the commit's to answer for, and a term added and removed again before the push is still a hit, since the push carries every commit);
- the version's changelog section, which `CHANGELOG.md` and the GitHub Release carry;
- every text file of the packed artifacts;
- the names of the tags the version owns;
- the GitHub Release body, and every Release body a later command rewrites (`release edit`, `release deprecate`, `release yank`, `release reconcile`, `release scrub`, `transition declassify`).

In a confidential repository only the packed artifacts are scanned: a registry is public whatever the repository is, while its pushes and Releases stay private, and its other public outputs are refused by the [lifecycle-and-license rules](lifecycle-and-license.md).

## Hits and their ids

Every hit is reported at once, each with its **id**, the list entry that matched, its location (a file at a commit, a commit's message, a Release body), its line and column, and the text around it. The id is 16 hexadecimal digits derived from the entry, the matched text, and the words that follow it on its line, ignoring case and punctuation: the same phrase has the same id in a changelog entry, in `CHANGELOG.md`, and in the Release body, so one resolution covers it everywhere. The id names no confidential text.

The `confidential-terms` check (tags `project` and `preflight`) reports the hits of the next push of the branch: `rlsbl check --name confidential-terms` lists them before a release, and every release runs it in its preflight.

## Resolving a hit

A release refuses while any hit has no resolution in `.strictmetadata/confidential-hits/resolutions.toml`, a file strictspec owns (its `manifest.toml` names strictspec) and the resolution commands write. Each hit is judged by the agent running the release:

- A hit the agent is at least 70% certain is a false positive (the term used as an ordinary word, a public name that happens to match) is recorded with `rlsbl confidential judge-false-positive --hit <id> --certainty <70-100> --reason <one line>`. A certainty below 70 is refused.
- Every other hit goes to the owner, who either has it fixed (the text reworded; a commit not yet pushed is rewritten with `safegit scrub match`, then `rlsbl changelog remap --from-journal` follows the changelog through the rewrite) or approves publishing it. Approving is the owner's call alone, hit by hit, with the owner's reason: `rlsbl confidential approve-hit --hit <id> --reason <the owner's reason>`, which is consequential, so an agent passes `--approve-consequential` only when the owner approved that specific hit.

Both commands accept only an id the next push or a tracked file of the repository carries, refuse a reason that itself carries a term (the file is committed), record the location with every match redacted, refuse a hit resolved already, and commit the file with the `Autogenerated: true` trailer. A resolution stays in the file, so the same phrase is resolved wherever it appears again, and selfdoc reads the same file before it publishes.

## The release record

At its archive step, the release records in the version's release archive (`.strictmetadata/releases/<releasable>/v<version>.toml`) every resolution that resolved a hit of the release, under `[[confidential_hits]]`: the hit's id, its redacted location, `resolution` (`false-positive` or `approved`), the certainty of a false positive, the reason, and the day it was recorded. Only the release writes `confidential_hits`; the editable release file refuses it.

## Why it is built this way

- **One shared store, not the release file.** Resolutions live in `.strictmetadata/confidential-hits/resolutions.toml`, not in a release file, so the same judgment serves every release and selfdoc's publishing, in a workspace's batch releases as in a single one; the archive records which ones each release used.
- **Ids from the words after a match.** The same phrase appears as a JSON changelog line, a `CHANGELOG.md` list item, and a Release body; leaving out what precedes the match keeps one id across them, so a judgment made before the release still holds when the release renders its notes.
- **Only added lines of a push.** A line the file already held was published with it earlier; scanning it again on every push that touches the file would ask for the same judgment over and over.
- **Ids are checked against what the repository carries.** The resolution commands accept only an id of a hit in the next push or a tracked file, so a mistyped id is refused instead of recorded as a resolution that resolves nothing.

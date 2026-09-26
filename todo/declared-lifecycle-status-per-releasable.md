# Declared lifecycle status per releasable

## Context

Nothing in rlsbl records where a releasable is in its life. The absence has
concrete effects today:

- A project shelved months ago still has live npm and PyPI packages with no
  notice of any kind, and nothing in its repository, its registries, or any
  generated documentation says it is shelved.
- A documentation roster and a landing site's tool data copy facts by hand
  (names, versions) and drift from what rlsbl's release records say.
- rlsbl itself was created by renaming an earlier project, share-it-on, which
  had reached 0.4.4. The version count restarted at 0.1.0, and the only trace
  of the connection is prose in the first release archive and in
  `CHANGELOG.md`. The old npm name has no pointer to its successor.

The owner decided the design below through a series of explicit rulings.
Each ruling is stated as decided; the reasoning that produced it is included
so an implementor can tell what is essential.

## Decided design

### Where the status lives

- **Per releasable, not per repository.** A standalone project has one
  releasable; a monorepo declares a status per releasable, because version
  lines inside one repository can have different fates (for example one
  language implementation retired while others stay active). Repository-level
  effects are derived from the releasables, never declared separately.
- **A required key in existing configuration**, not a new file:
  `.rlsbl/config.json` for a standalone project, and the releasable's
  `[[releasables]]` table in `workspace.toml` for a monorepo. It is validated
  where `publish_mode` is validated (`get_publish_mode` and
  `validate_config_schema` in `rlsbl/config.py`). A missing or unknown status
  is a hard error; there is no default anywhere.
- `rlsbl scaffold` must require the status explicitly and must not default it
  the way it defaults `publish_mode` for public repositories.
- A releasable's `config.json` in a monorepo is merged under its members'
  configs, so it is not a safe home: a member could override the status.

### The states

| Status | Declared with it | Meaning |
| --- | --- | --- |
| `active` | nothing | Maintained and released normally. |
| `on-hold` | `reason` | Work is paused and may resume. |
| `retired` | `reason`, and `successor` (a name, or an explicit null) | Not maintained. Revivable: it may be set back to `active`. |

A planned rename is deliberately NOT a state. Renaming is a separate
dimension from lifecycle (a shelved project can also be intended for a
rename, which one state cannot express). A rename already committed is
recorded in the transition record as identity transitions effective from the
next release, and rlsbl reads it from there. A rename merely intended, with
no name chosen, is not a machine fact.

### How the status changes

- Only through a new rlsbl command group for lifecycle, which writes the
  configuration key and appends a transition record event describing the
  change, in one commit. The command is consequential.
- A status that changed without a matching event (a hand edit) is refused at
  the next release.
- A read-only subcommand of the same group reports the status as JSON with a
  declared payload schema: the status, its reason and successor, and any
  committed-but-unreleased rename read from the transition record. Other
  tools (the documentation site generator, a landing site's tool data) read
  the status only through this subcommand, never from the raw configuration.

### Enforcement per state

**On-hold** refuses `rlsbl release run` until the status is set back to
`active`. It changes nothing on any registry: a deprecation notice tells
users to leave, which is wrong for a pause.

**Retired** requires, in this order, as one consequential action of the
lifecycle command:

1. A final release whose changelog entry announces the retirement and names
   the successor if there is one. The command refuses to record `retired`
   until that release has shipped.
2. Registry deprecation, each verified by reading the registry rather than
   trusted:
   - npm: the package deprecated as a whole, naming the successor. No
     whole-package deprecation exists in rlsbl today; the target protocol
     has only per-version `yank` (`rlsbl/targets/protocol.py`).
   - Go: the module marked `// Deprecated:`, verified the way the existing
     `go-deprecation-published` check (`rlsbl/data/checks.toml`) verifies it
     through the module proxy.
   - PyPI: the project reporting `archived`. PyPI has no deprecation command,
     but its public index API reports a project status, readable with a plain
     request (the JSON form of the Simple API returns
     `"project-status": {"status": "active"}` for a normal project). Owners
     set `archived` in PyPI's web interface and can unarchive at any time;
     archived projects stay installable and refuse uploads. Setting it stays
     a manual step the command prints; verifying it is automatic.
3. When every releasable in a repository is retired, the GitHub repository is
   archived through `PATCH /repos/{owner}/{repo}` with `{"archived": true}`
   (admin rights required), and a check verifies the archived flag.

Reviving a retired releasable reverses this: unarchive the repository first,
because an archived repository is entirely read-only, including tags and
releases; then withdraw the notices and unarchive on PyPI. Re-archiving later
resets GitHub's displayed archive date, so the transition record events are
the durable history of when a releasable was retired.

The release refusals belong in release validation (where the configuration
schema is validated before any mutation), not in preflight checks: when a
pre-release hook is customized, the built-in preflight checks are skipped
(`rlsbl/commands/release/__init__.py`).

### Relation to release-history-closed

The existing `release-history-closed` declaration stays. It means a version
line has ended permanently (for example after an extraction or a rename), it
can never be reopened, and it implies `retired`. `retired` alone means not
maintained for now and can be revived.

### Renames

- Releases are allowed while a committed rename is not yet released: the next
  release is what ships the new identity.
- After a rename ships, a check requires the old package names to be
  deprecated, naming the new one, verified per registry as above.
  `rlsbl rewrite project-name` currently deprecates nothing, and its printed
  list of remaining steps should name the old-name deprecation.
- A new transition record event kind, **renamed-from**, records a rename after
  which the version count restarted: the old name, its last version, the new
  name's first version, and the registries involved. The existing
  identity-transition event cannot express this, because it assumes one
  continuous version line and a restart makes the two lines' numbers
  overlap. The operator declares it through `rlsbl transition record`. rlsbl's
  own first use: share-it-on 0.4.4 followed by rlsbl 0.1.0.

### Adoption across existing projects

Once the key is required, every project without it fails configuration
validation. Adoption is a one-time step with real values, never a blanket
default: the proposed status for each releasable is drawn from its recent
activity, the owner confirms each one that is not plainly active, and then a
one-time script writes them. This is a pre-stable change with no
compatibility period.

### Downstream behavior

Retired releasables are removed from the documentation site and the landing
site, with redirects to a successor where one exists. The archived repository,
the final release, and the registry notices carry the message after that.

## Folded-in fix

`rlsbl/targets/pypi.py`'s `yank` says in its docstring that nothing can
verify a PyPI yank, and relies on operator confirmation. PyPI's JSON API
(`https://pypi.org/pypi/<name>/json`) reports `yanked` and `yanked_reason`
per release file, so the yank can be verified by reading it. Fix it with the
same registry-reading code the retirement checks need, with a red-green test.

## Constraints

- No implicit defaults, refuse over guess, hard errors rather than warnings,
  and no escape hatch that disables a refusal.
- Every error message that names a fix gets a red-green test that performs
  the fix and asserts the error clears.
- Checks that can block a release read only inputs the repository owns; the
  registry-reading checks are networked follow-up checks in the style of
  `go-deprecation-published`, declared as such.
- Plain descriptive names for every new concept, command, and event kind.

## Affected areas

- `rlsbl/config.py` and the `workspace.toml` reader: the required key.
- The release validation path in `rlsbl/commands/release/`: refusals.
- `rlsbl/transition_record.py`, `.strictspec/transition-record-event.schema.toml`,
  and `rlsbl/commands/transition_record_cmd.py`: the lifecycle-change and
  renamed-from event kinds.
- A new lifecycle command group registered in `rlsbl/__init__.py`.
- `rlsbl/targets/`: whole-package deprecation for npm, the PyPI status and
  yank readers, and a GitHub repository archive step.
- `rlsbl/data/checks.toml` and `rlsbl/checks/`: the networked deprecation and
  archive checks.
- `rlsbl/commands/rewrite/project_name.py`: the remaining-steps list.
- `rlsbl/commands/init_cmd.py`: scaffold requiring the status.
- Documentation under `.stricttools/docs/`.

## Effort

Large: a new command group, two new event kinds, new registry readers for
three registries plus GitHub, new checks, a scaffold change, and an adoption
pass across every managed project.

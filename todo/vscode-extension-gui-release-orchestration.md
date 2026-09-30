# A VS Code extension for GUI release orchestration

## Context

rlsbl drives a release from the command line: `rlsbl release init` scaffolds
`.rlsbl/releases/unreleased.toml` (bump type, description, and target
selection), changelog entries accumulate in `.rlsbl/changes/unreleased.jsonl`
through `rlsbl changelog add`, and `rlsbl release run` validates, bumps,
pushes the untagged candidate, waits for the repository's own CI on that exact
commit, and only then tags, creates the GitHub Release, and publishes. A failed
release is continued with `rlsbl release resume` from the state file
(`.rlsbl/releases/in-progress.json`, or the per-releasable one under
`.rlsbl-monorepo/releasables/<name>/releases/`). Monorepos add the `monorepo`
commands, with releasables, a dependency graph, and impact analysis.

Much of the state an operator looks at before releasing is already exposed in
machine form: `status --json`, the coverage report of `unreleased` under
`--json`, `monorepo graph` and `monorepo impact` under the framework-owned
`--json`, the committed `.rlsbl-monorepo/snapshot.json`, the options registry,
and the command surface itself through strictcli's `help --json`.

## The problem

Preparing a release means reading several commands' output and editing several
files by hand: which commits lack changelog entries, which targets and
pipelines are detected, what the next version would be, which checks fail, and
in a monorepo which releasables a change affects. During the release, progress
is a long terminal log, and after a failure the operator has to find the state
file and know which command continues it. None of this has a visual overview,
and the editor, where the operator already is, shows none of it.

## The rule every design choice below follows

Releasing is consequential: only a human approves one. rlsbl declares its
release commands `consequential` (for example `release run`, `release resume`,
and `release yank`; the command declarations in `rlsbl/__init__.py` are the
authority), so strictcli prompts before running them, and
`--approve-consequential` skips the prompt. The extension must never approve a release on the user's behalf:

- It never passes `--approve-consequential` itself, and has no setting,
  command argument, or remembered choice that makes it do so.
- Consequential commands are not reachable without a human in the loop:
  AI agents inside the editor can call extension commands through
  `vscode.commands.executeCommand` and can be offered extension-contributed
  language model tools, so consequential actions are never contributed as
  language model tools, and the approval step is one an agent cannot perform.
- Releases stay the only push. The extension offers no push, branch, or
  tag-creation action of its own; everything that writes a remote goes through
  an rlsbl command.

Two ways to satisfy this for the release itself:

| Approach | Pros | Cons |
|---|---|---|
| Run consequential commands in an integrated terminal (`vscode.window.createTerminal`) with no approval flag, so strictcli's own prompt is the approval the human answers (recommended) | The approval is strictcli's, the same prompt as on the command line; the extension holds no approval logic; an agent calling the command still leaves an unanswered prompt a human must answer | Progress parsing from a terminal is limited (the shell integration API, `vscode.window.onDidEndTerminalShellExecution`, gives the exit status, not structured progress) |
| A modal dialog (`showWarningMessage` with `modal: true`) the human clicks, then run the command with the approval flag as a child process | Structured progress is easy to read from a child process | The extension would be the one passing the approval flag, which is what the rule forbids; an approval dialog in the extension is a second approval mechanism beside strictcli's |

A structured progress channel for the terminal route (for example a machine
progress stream a release writes to a file the extension watches) is a
separate rlsbl feature, to be designed if the terminal route is chosen.

## Proposed extension

### Features

- **Release overview view** (`vscode.window.createTreeView` in its own view
  container): project version, branch, latest release, detected targets and
  pipelines, unreleased commits with their changelog coverage (uncovered commits
  flagged), and check results from `failing-checks`. Built from the `--json`
  outputs, refreshed on file changes through
  `vscode.workspace.createFileSystemWatcher` on `.rlsbl/**` and on git state.
- **Changelog coverage actions**: from an uncovered commit, a command that
  prompts for the entry type and description and runs `rlsbl changelog add` (a
  mutating but not consequential command) with those values; nothing is
  prefilled with a guessed type.
- **Release file editing**: a webview form or a custom editor
  (`vscode.window.registerCustomEditorProvider`) for `unreleased.toml`, with the
  bump type, description, and target selection; the preview of the resulting
  version comes from rlsbl, not from the extension's own arithmetic. Diagnostics
  (`vscode.languages.createDiagnosticCollection`) on the release file and on
  `unreleased.jsonl` surface validation errors rlsbl reports.
- **Dry run** (`vscode.commands.registerCommand`): runs `rlsbl release run
  --dry-run` and shows the would-do log in an output channel
  (`vscode.window.createOutputChannel`) or a read-only document. A dry run needs
  no approval, so it runs as a child process.
- **Release**: a command that opens an integrated terminal running the release
  command without the approval flag, as described above. `--watch` and
  `--no-watch` have no default in rlsbl, so the command asks which one; it
  never picks for the user.
- **In-progress release**: when a state file exists, a status bar item
  (`vscode.window.createStatusBarItem`) and a banner in the view show the
  release's version and completed steps, with a "Resume in terminal" action
  (again a terminal, again no approval flag) and a link to `release abandon`,
  which is consequential and handled the same way.
- **CI status**: `rlsbl watch <sha>` results for the release commit in the
  view, with links to the workflow runs.
- **Monorepo support**: a releasables view (`monorepo list` and `monorepo
  status`), a dependency graph webview (`vscode.window.createWebviewPanel`) from
  `monorepo graph --json`, and impact highlighting from `monorepo impact --json`
  for the files changed since the last release. When the workspace holds more
  than one releasable, every release action asks which releasable it is for,
  never inferring one from the active file or folder.
- **Code lenses** (`vscode.languages.registerCodeLensProvider`) on
  `unreleased.toml` and `workspace.toml`: "Dry run", "Release in terminal", and
  per-releasable status.
- **Version check**: the extension requires a minimum rlsbl version and refuses
  with an error naming the upgrade when the installed one is older or its
  `help --json` schema lacks a command the extension calls.

### The CLI side this needs

- A read-only JSON document for the in-progress release state, so the
  extension does not parse `in-progress.json` directly and couple itself to an
  internal file.
- A read-only JSON document of detected targets and pipelines (`targets`),
  and of check results (`check` / `failing-checks`), if those do not already
  offer `--json`.
- A committed schema for each JSON document the extension consumes, so its
  TypeScript types are generated from or checked against them.
- Optionally, the structured progress stream mentioned above.

## Distribution

- An extension is TypeScript/JavaScript packaged as a VSIX with `vsce`.
- Publish to both the Visual Studio Marketplace and Open VSX (the open registry
  VSCodium, Cursor, and other VS Code derivatives install from), with `ovsx` for
  the latter; each needs its own publisher namespace and token.
- rlsbl has no VS Code extension release target or publish pipeline. This
  extension would be the natural first consumer of one: a target that versions
  the extension's `package.json` (distinct from the npm target, since a VSIX is
  not published to npm) and a pipeline that builds the VSIX and uploads it to
  both registries, with each registry's token as a repository secret. Designing
  that target is part of this work or a prerequisite of it.
- Where the extension lives is a decision: a subdirectory of this repository
  (the root `package.json` is the npm wrapper, so the extension needs its own
  manifest and must stay out of the wrapper's published files) or a separate
  family repository.
- The extension's name, publisher ID, and package name are to be chosen.

## Affected files and new components

- `rlsbl/__init__.py` and `rlsbl/commands/`: the JSON outputs listed above,
  all read-only.
- `rlsbl/targets/`: a new target for VS Code extensions, and the matching
  publish pipeline and scaffold templates, if this repository hosts the
  publishing side.
- A new directory (or repository) for the extension with its own
  `package.json`, `tsconfig.json`, and tests (`@vscode/test-electron`).
- README and docs: an editor section, including the approval rule above, once
  the extension exists.

## Effort estimate

- JSON outputs for release state, targets, and checks, with schemas and tests:
  one to two days.
- Extension overview, coverage actions, dry run, terminal release and resume,
  status bar, and CI status: three to five days.
- Release file custom editor and monorepo graph webview: three to four days.
- VS Code extension target and pipeline in rlsbl, with tests: two to four days.

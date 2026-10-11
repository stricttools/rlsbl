# check-name reports "available" for names the registries refuse

## The problem

`rlsbl check-name <name> --target npm` reported a five-character candidate name as available:

    "<name>" is available on npm.

    Checked: npm, variants, moniker similarity

and exited 0. `rlsbl claim-name <name> --target npm` then ran `npm publish` on its placeholder, and the
registry refused it:

    npm error code E403
    npm error 403 403 Forbidden - PUT https://registry.npmjs.org/<name> - Package name too similar to existing packages read,clear; try renaming your package to '@<scope>/<name>' and publishing with 'npm publish --access=public' instead

The same name was accepted by PyPI.

`.strictmetadata/docs/_CLAUDE.md` says a question rlsbl cannot answer is an error, never read as
absence or as a pass, and `check-name`'s help (`internal/cli/names.go`) promises that a similar name the
registry cannot answer about makes the status error, never available. Both are broken today.

## Why the name passed

`checkNpm` (`internal/registry/checkname.go`):

- "npm": `registry.npmjs.org/<name>` answered 404, so the name was set to available.
- "variants": every separator spelling (`<na-me>`, `<n.ame>`, ...) answered 404. Every variant shares the
  candidate's moniker, so this check can only catch punctuation-only collisions.
- "moniker similarity": `npmSearchConflicts` keeps only search results whose moniker EQUALS the
  candidate's. It is an equality check, not a similarity check, despite the label, so the `Checked` line
  claims a check that never runs.

## What is known about npm's similarity rule

- npm documents only the punctuation rule (its "moniker rules" announcement) and says unscoped names
  should not be spelled "in a similar way" to another, with "similar" undefined. There is no published
  algorithm, no staff explanation, and no endpoint answers "would this name be refused" without
  publishing: `npm publish --dry-run` does not run the check (npm/cli#9188, open).
- Refusals with the names npm listed: the name above (read, clear), `bencharts` (bizcharts, echarts,
  recharts), `graph8` (graphql), and `noxe` (node, secondhand).
- The closest reconstruction, raw Levenshtein distance of 2 or less against packages with about 500 or
  more dependents (wooorm's npm-high-impact `top-dependent` list), reproduces those lists name for name.
- Names npm accepted contradict any fixed threshold: `romzo` next to `rome` and `rustier` next to
  `restler` (both distance 2, created within days of the investigation), `clmx` next to `clsx`
  (distance 1), and `isomorphic-msw` next to `isomorphic-ws`. A dependents threshold near 700 fits the
  recent cases, but nothing anchors it, and npm's own dependents counts are not available through any
  API.
- Downloads alone do not explain it: `crelt` (16M a week) and `creem` were not listed while `clear`
  (228k) was.
- The investigation's prototypes are in `experiments/npm-similarity/`: `scan/main.go` (every unscoped
  name within edit distance 2 of a candidate, from the all-the-package-names list), `downloads.py`,
  `predict.py`, and a sample of recently created names from the registry's changes feed. The data there
  (about 120 MB) is disposable.

## Sibling defects (confirmed with rlsbl 0.133.0, read-only)

1. **PyPI ultranormalization misses l and i.** `ultranormVariants` gives `l` and `1` the options {l, 1}
   and `i` the options {i, 1}, but Warehouse folds l, i, and 1 together (migration d18d443f89f0).
   `check-name fiask --target pypi` reports available, though PyPI refuses it as too similar to `flask`.
   Variants also keep the candidate's own separators, so an existing `c-l1` is never asked about for
   `cli`. Warehouse compares against every project name; the exact fix is to read PyPI's root simple
   index (every project name, no versions) and fold each name, which is a download of tens of MB per
   check and needs a resource measurement first.
2. **PyPI's standard-library list is too narrow.** `pystdlib.go` uses Python 3.14's top-level
   `sys.stdlib_module_names`; Warehouse refuses every module of every Python version in `stdlib-list`,
   Python 2 and dotted submodules included. `htmllib`, `mimetools`, `sgmllib`, `os-path`,
   `email-mime-multipart`, and `xml-etree-elementtree` all report available and would be refused.
3. **The PyPI separator-insertion cap is not an error.** At its limit (`checkname.go`) it only adds a
   note to `Checked`, and the status can still be available; the ultranormalization cap, by contrast,
   is an error.
4. **Name syntax is never validated.** `UPPERCASEPROBE` and `a~b` report available on npm (npm refuses
   uppercase and `~'!()*` in new names), and `qx~zzyq` on PyPI (it fails PEP 508). `NpmNameProblems`
   and `PypiNameProblems` exist (`internal/registry/names.go`) but check-name never calls them; the
   `invalid` status is Go-only.
5. **claim-name reports a refused publish as a generic failure.** The npm 403 comes back from
   `ClaimPlaceholder` as a process error, exiting 1 like "taken", with npm's reason only in the streamed
   output; claim-name's help does not document this exit. The PyPI `uv publish` path behaves the same.

PyPI's post-creation typosquatting check only flags a project after it exists, so it needs no change,
and the Go target is checked offline by documented rules and is unaffected.

## Decided by the owner

- Do not imitate npm's similarity rule client-side; check-name states that the policy cannot be checked
  before publishing. [%%]
- A name not registered whose registry policy cannot be checked gets a new status (not available),
  exiting 1; check-name never exits 0 for npm.
- PyPI names get the same status, because PyPI's prohibited-names list is private; its printed caveat
  (a warning while reporting available) goes away. check-name then exits 0 for no registry target.
- claim-name publishes on the new status, and when the registry refuses, reports the refusal plainly
  with the registry's reason (for example "npm refused the name: too similar to existing packages read,
  clear; nothing was published") instead of a generic process failure.

## Still open

1. The refusal's exit code: 1, like taken, or its own code, so an agent can tell "someone holds it" from
   "the registry will not allow it".
2. The names of the new status and of the refusal's reason and rule token (a `--json` schema change for
   `check-name` and `monorepo check-names`); the naming rules apply.
3. PyPI's lookalike check: read the full simple index and fold every name the way Warehouse does
   (exact, needs measuring), or keep enumerating variants with l, i, and 1 folded together.
4. Whether `claim-name`'s default of writing its placeholder under the system temporary directory is
   fixed in the same work.

## Footprint

- `internal/registry/checkname.go`, `names.go`, `pystdlib.go` with its generator under
  `internal/registry/gen` and a regenerated `python-stdlib-modules.json`, and `claim.go` (capture and
  classify the publish refusal; it streams today).
- `internal/cli/names.go` (help, schema enums, rendering, claim-name's handling of the new status) and
  `internal/cli/monorepo_reports.go`.
- Red tests first in `internal/registry/checkname_test.go` and `internal/cli/names_test.go` with the fake
  HTTP fixtures: the refused npm name must not exit 0; `fiask`; `htmllib`; `UPPERCASEPROBE`; the
  insertion cap; and a claim-name run whose fake `npm publish` prints the E403 above must report the
  refusal with the decided exit code. Any refusal text naming a fix gets a test performing it.
- Docs: `.strictmetadata/docs/utilities.md`, `monorepo.md`, `_CLAUDE.md` if conventions change, the
  README command table, the CLI schema dump, selfdoc output, and changelog entries.
- One minor release at the end: user-visible behavior and the JSON schema change.

## Risks

- npm can change its rule without notice, which is why it is not imitated.
- Parsing npm's free-text 403 is fragile: pin the observed message forms in tests, and treat an
  unparsed 403 as an error.
- Scripts that test for `available` or exit 0 break once no registry target exits 0.

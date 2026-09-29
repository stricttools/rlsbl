# Delete PyPI files and releases from rlsbl, using a recorded browser flow

## Context

PyPI has no API for deleting a release file or a whole release. Its upload
API and API tokens can only upload; deletion exists only as HTML forms in the
web interface, behind a logged-in session, each asking the owner to type a
confirmation (the version string, or the project name). rlsbl already has
`yank` and `deprecate` (`rlsbl/commands/yank.py`, `rlsbl/commands/deprecate.py`),
but a yanked file is still served to anyone who asks for that exact version or
fetches its URL; only deletion stops PyPI serving it.

The need is real and recurring: a published-package audit found private files
(screenshots of a private local instance, under `todo/`) inside eleven
wesktop sdists, four of them yanked and all still downloadable, and a
different project whose confidential source was published in 25 releases. The
owner had to delete each file by hand in the web interface, typing every
version, which is slow and error-prone for dozens of files.

pypi.org runs Warehouse, deployed from its public repository
(github.com/pypi/warehouse), so the delete forms' URLs, fields, CSRF handling,
and any re-authentication step can be read from source. What the source does
not show is the live configuration (feature flags, CDN, rate limits, whether a
recent-login confirmation is enforced).

## The idea: record the flow once, then script it

1. The owner deletes one file by hand in their browser with the developer
   tools' Network tab recording, and saves the recording as a HAR file into
   rlsbl's gitignored `experiments/` directory. The HAR holds every request in
   plain text: URLs, form fields, the CSRF token flow, and any redirect to
   re-authenticate. It also holds the session cookie, which is a credential:
   it stays local, is never committed, and is deleted after use.
2. The recorded requests are checked against Warehouse's source for the same
   flow, so the script matches the live site and not only the repository.
3. rlsbl gains a command that performs the same requests for a list of files
   or releases, using the owner's logged-in browser session:
   - a dry run first, listing every file or release it would delete, taken
     from the project's own release records and the public JSON API;
   - deletion only with explicit approval (it is permanent: a deleted file
     name can never be uploaded again);
   - afterwards, a re-check of the public JSON API confirming each target is
     gone (allowing for the API's cache lag).

## Options for using the session

- **Read the session cookie from the browser profile.** Simple requests
  replay; must never write the cookie to disk or logs; breaks when the cookie
  format or profile location changes (the owner's browser is LibreWolf in a
  Flatpak).
- **Drive the logged-in browser itself** (browserbuddy). The real browser
  performs the forms, so CSRF and re-authentication behave exactly as by hand;
  heavier to set up.
- **Replay the HAR's requests directly.** Fastest to build, but a HAR is a
  snapshot: tokens expire and the flow may change.

## Open questions

- Does Warehouse's "sudo mode" (a password or 2FA re-confirmation before
  destructive actions) apply to file and release deletion on pypi.org? If so,
  the command works inside a window the owner opens once in the browser.
- File deletion versus release deletion: a project with clean wheels (wesktop)
  keeps its versions installable by deleting only the sdists; a project whose
  source must not be public deletes whole releases or the project.
- Should the command be PyPI-only, or a general "withdraw content" command
  whose npm half uses `npm unpublish` (time-limited) and whose Go half is
  impossible (the module proxy is permanent) and says so?
- Consequential classification: deletion is permanent and belongs to a human;
  the command must require `--approve-consequential` and list targets first.

## Affected areas

- A new command beside `yank` and `deprecate`, and its help, docs, and
  changelog entry.
- The release records: record that a file or release was deleted, so status
  and audit views stop claiming it is published.

## Effort

About two to three days: one HAR capture by the owner, reading Warehouse's
delete views, the command with dry run and re-check, and tests against a
recorded fixture (no live PyPI in tests).

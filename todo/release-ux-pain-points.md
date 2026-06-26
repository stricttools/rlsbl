# Release UX pain points from downstream projects

Three issues surfaced during shopkeep and toolstream releases that cause friction on every release. All three have precedent in rlsbl's existing design (env vars for push/hook timeouts, config-driven overrides) but are missing the specific affordance.

## 1. Test timeout is hardcoded at 120s

`testing.py` hardcodes `timeout=120` in 7 `subprocess.run` calls across `_run_pypi_tests`, `_run_go_tests`, `_run_maven_tests`, `_run_npm_tests`, and `sync_workspace`. There is no config key, env var, or CLI flag to override it.

Projects with integration tests (Postgres-backed, browser-based) regularly exceed 120s. The completed todo `pre-release-hook-ux.md` documented the same class of problem -- selfdoc's 596 tests taking ~170s.

The workaround is a custom `hooks.pre_release` in config.json that replaces the built-in test runner entirely (no timeout). This works but loses the benefit of the built-in runner (target-aware test selection, workspace sync).

**Fix:** Add `RLSBL_TEST_TIMEOUT` env var and/or `test_timeout` key in config.json. Precedent: `RLSBL_PUSH_TIMEOUT` (push), `RLSBL_HOOK_TIMEOUT` (hooks). Default stays 120s for backward compat.

**Affected files:** `rlsbl/testing.py` (7 call sites).

## 2. GitHub Release creation fails with SSH remote aliases

`gh release create` cannot auto-detect the repo when the git remote uses a custom SSH alias like `git@gw:Org/repo.git`. The `gw` alias is a valid SSH config host, but `gh` expects `github.com` in the URL to extract the org/repo pair.

Every release for projects with SSH aliases requires manual intervention: either `gh release create --repo Org/repo` after the release, or switching `gh` auth contexts. This has caused release failures on both shopkeep (`git@gw:GreenCapitals/shopkeep.git`) and toolstream (`git@gw:smm-h/toolstream.git`).

**Fix:** Either (a) add a `github_repo` config key in `.rlsbl/config.json` that rlsbl passes to `gh --repo`, or (b) have rlsbl parse the remote URL more aggressively -- strip any SSH alias prefix and extract the `Org/repo` path portion (everything after the `:` in `git@<host>:<path>.git`).

**Affected files:** Wherever rlsbl invokes `gh release create` and `gh release edit`.

## 3. GH_TOKEN env var causes account mismatch

When `GH_TOKEN` is set in the shell environment (e.g., for a different GitHub account or a CI token), it overrides `gh`'s keyring-based auth. If the token belongs to an account that lacks push access to the repo, the release fails at the push step. The error message is opaque (generic auth failure, no mention of which account is being used).

Every release requires `unset GH_TOKEN` as a manual pre-step. This is easy to forget and has caused release rollbacks on both shopkeep and toolstream.

**Fix:** Before the push step, have rlsbl check if `GH_TOKEN` is set and if so, verify the authenticated user (`gh api user --jq .login`) matches the repo owner or has push access. Warn or error if there's a mismatch, with a message like "GH_TOKEN is set for account X but the repo belongs to Y -- unset GH_TOKEN or use a token with push access."

**Affected files:** The release pipeline, near the push/GitHub Release steps.

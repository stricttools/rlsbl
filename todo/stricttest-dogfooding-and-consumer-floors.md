# stricttest: rlsbl dogfooding + remaining consumer floors

Successor to `todo/.done/test-floor-program.md` (split 2026-08-06). The master
record's rlsbl-side DISTRIBUTION work shipped: the pytest plugin, the Go
hygiene module, the ephemeral-Postgres cluster (both languages), the runner
template, and the adoption check. Two remainders survive the split; the
authority for both is the campaign plan at
`~/Projects/ark/todo/ecosystem-fix-campaign.md`.

## 1. rlsbl does not dogfood the plugin it distributes

`tests/conftest.py` is still ~1370 lines carrying the entire isolation floor
verbatim (env-poisoning, bare-run threshold, TMPDIR refusal, chdir guard, push
guard); `stricttest` appears nowhere in `pyproject.toml`. Every floor
improvement now ships to consumers while the distributor runs a divergent
private copy — the drift is structural until the conftet's floor layers are
replaced by the plugin dependency plus `[tool.pytest.ini_options]` config
(the ~750 rlsbl-specific conftest lines stay). This was the hardening
campaign's item 1.6, audited MISSING 2026-08-05, and it fell out of the fix
campaign's plan during assembly (gap recorded and re-added there 2026-08-06).

## 2. Consumer-side floor adoptions

The remaining Python consumers without the floor adopt it at their fix-campaign
Phase-6 visits (recipes and stances are pinned per repo in the campaign plan).
This item exists here only so the split loses nothing; the per-repo work is not
rlsbl's.

## Effort

Item 1: medium — the extraction was designed for this replacement; the work is
config mapping, deleting the duplicated layers, and proving the meta-guards
still fire (deliberate probes). Item 2: rides the campaign's consumer wave.

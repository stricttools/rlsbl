# Monorepo selfdoc.json version bump is meaningless

## Problem

`_bump_selfdoc_version` in `release.py` bumps the root `selfdoc.json`'s `version` field during every release. In a monorepo, each sub-project has its own version. The root selfdoc.json gets bumped to whichever sub-project was released last -- the field becomes meaningless noise.

Example: incantino has sub-projects ios (v0.3.1), core (v1.0.0), tooling (v0.5.0). After releasing ios, selfdoc.json says `"version": "0.3.1"`. After releasing core, it says `"version": "1.0.0"`. The value doesn't represent anything coherent.

## Affected projects

Any rlsbl monorepo with a root selfdoc.json: incantino, F, strictcli, WWW, shopkeep, gamehome.

## Investigation findings

This is rlsbl's problem, not selfdoc's. selfdoc's unified builder already handles versioning correctly:

- The docs-site's `versions` array drives URL structure (e.g., `/en/0.8.1/page/`)
- Each version entry can pin constituent projects to specific versions
- Constituent project versions appear on landing page cards
- The docs-site version is entirely separate from code versions -- it represents the documentation structure version, not any code artifact

The root selfdoc.json version for a unified/monorepo docs-site should only change when the documentation structure itself meaningfully changes (new sections, reorganization, locale additions), not every time a sub-project releases a patch.

## Recommended fix

1. rlsbl should NOT bump selfdoc.json version for docs-site/unified projects -- projects whose selfdoc.json has a `unified` section, or projects whose selfdoc.json serves a monorepo root
2. The docs-site project should have its own independently-managed version, bumped only when the documentation structure meaningfully changes
3. Implementation: either a flag in rlsbl config (e.g., `skip_selfdoc_version = true`) or auto-detection of the `unified` section in selfdoc.json

## Specific rlsbl changes needed

- In `release.py`, `_bump_selfdoc_version`: before bumping, read the target selfdoc.json and check for a `unified` key. If present, skip the bump.
- Alternatively, add a `skip_selfdoc_version` boolean to `.rlsbl/config.json` schema. When true, `_bump_selfdoc_version` is a no-op. This is more explicit and doesn't couple rlsbl's logic to selfdoc's config schema.
- For monorepo roots (detected via `.rlsbl-monorepo/workspace.toml`), apply the same skip logic -- the root selfdoc.json is a docs-site config, not a sub-project version file.
- Update `rlsbl scaffold` to set `skip_selfdoc_version = true` by default for monorepo roots that have a selfdoc.json.

## Effort

Small. The detection logic is straightforward -- either a config flag check or a JSON key lookup. The main work is deciding which approach (flag vs. auto-detect) and updating the scaffold templates.

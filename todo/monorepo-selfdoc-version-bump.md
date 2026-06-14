# Monorepo selfdoc.json version bump is meaningless

## Problem

`_bump_selfdoc_version` in `release.py` bumps the root `selfdoc.json`'s `version` field during every release. In a monorepo, each sub-project has its own version. The root selfdoc.json gets bumped to whichever sub-project was released last — the field becomes meaningless noise.

Example: incantino has sub-projects ios (v0.3.1), core (v1.0.0), tooling (v0.5.0). After releasing ios, selfdoc.json says `"version": "0.3.1"`. After releasing core, it says `"version": "1.0.0"`. The value doesn't represent anything coherent.

## Affected projects

Any rlsbl monorepo with a root selfdoc.json: incantino, F, strictcli, WWW, shopkeep, gamehome.

## Options

1. Skip `_bump_selfdoc_version` for monorepo releases (detect via `.rlsbl-monorepo/workspace.toml`)
2. Let selfdoc handle its own versioning for monorepos (e.g., docs version independent of code versions)
3. Bump to the releasing sub-project's version but prefix with the project name (e.g., `"version": "ios@0.3.1"`) — probably wrong since selfdoc uses this for URL paths

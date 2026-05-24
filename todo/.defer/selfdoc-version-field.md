# Should selfdoc.json have a version field?

## Context

rlsbl's DocsTarget always returns "0.0.0" from `read_version()` because `selfdoc.json` has no version field. This causes the `version-consistency` check to fail for any project with both selfdoc.json and another versioned target (e.g., pyproject.toml). The interim fix in rlsbl is to skip targets where `version_file()` returns None.

## Question

Should `selfdoc.json` include a `"version"` field that rlsbl can read and bump during releases? Arguments:

- **For:** Consistency with other targets. Docs could be versioned alongside the project. rlsbl already knows how to bump versions in JSON files.
- **Against:** Docs aren't independently versioned or published to a registry. They deploy to Cloudflare Pages on every release regardless of version. A version field would be artificial.

## Filed from

rlsbl session discussing DocsTarget version-consistency check failure. The rlsbl-side workaround (skip versionless targets) is implemented; this todo is about whether selfdoc should have its own version.

## Effort

Small if yes (add field to schema, read/write in DocsTarget). Zero if no (keep the skip workaround).

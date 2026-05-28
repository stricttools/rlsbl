# Docker template {{version}} collides with rlsbl template engine

## Problem

The Docker publish template (`docker/publish.yml.tpl`) contains `{{version}}` which is a Docker metadata-action runtime placeholder. But rlsbl's template engine (Pass 2 in `process_template()`) resolves any `{{word}}` pattern if the word exists in `vars_dict`. When the Go target adds `version` to the merged vars dict, `{{version}}` gets replaced with the literal version string (e.g., `0.11.1`).

Result: `type=semver,pattern=0.11.1` (broken, hardcoded) instead of `type=semver,pattern={{version}}` (correct, dynamic).

## Root cause

Namespace collision: both rlsbl and Docker metadata-action use `{{placeholder}}` syntax. The template engine has no way to distinguish them.

## Affected

Any multi-target project with both Docker and Go targets (e.g., safegit). Single-target Docker projects are unaffected because the Docker target's own `template_vars()` doesn't include `version`.

## Fix options

1. **Escape syntax**: Use `\{{version}}` in the template; engine strips the backslash and emits `{{version}}` literally. Minimal change.
2. **Different rlsbl delimiter**: Change rlsbl to `<%=key%>` or `<<key>>`. Larger change, all templates affected.
3. **Raw block**: `{{raw}}...{{/raw}}` suppresses replacement within the block.

Option 1 is recommended — smallest change, targeted fix.

## Effort

Small. Add escape handling to `process_template()` (~5 lines), update the Docker template.

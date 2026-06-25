# Target detection priority matrix

## Problem

Target detection has had 13 fixes across v0.35-v0.65 for collision resolution issues (PlainTarget vs DocsTarget, Go library vs cmd, npm moniker). Detection does not have an explicit priority order — collisions are resolved ad-hoc.

## Suggested approach

- Define an explicit priority matrix: when two targets both detect, which wins
- Add collision tests covering known problematic combinations
- Make detection stop at first match in priority order rather than collecting all matches and resolving later

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), target detection section.

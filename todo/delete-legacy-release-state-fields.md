# Delete legacy ReleaseState fields

ReleaseState in commands/release/execute.py still carries 5 legacy fields (registry, target, primary_path, target_paths, secondary_targets) alongside the new resolved_targets, bridged by try/except blocks. Once all fleet configs carry the pipeline `target:` field (the sweep ran on 5 key repos; ~6 more pending), delete the legacy fields + derivation helpers + try/except bridges.

## Scope

- ReleaseState legacy field definitions in execute.py
- ~17 production references to those fields
- ~12-17 test files using the legacy fields
- try/except bridges between old and new field access

## Precondition

All fleet configs must carry the pipeline `target:` field. The sweep has completed on 5 key repos; approximately 6 more are pending.

## Effort

Medium (mechanical once the precondition holds).

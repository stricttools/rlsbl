# Add test coverage reporting to CI

## Problem

No coverage reporting in CI. Test coverage is unknown and cannot be tracked over time. The project enforces 85% locally but has no CI gate.

## Suggested approach

- Add `pytest-cov` to CI runs with `--cov=rlsbl --cov-report=term-missing`
- Set a coverage floor (85% based on existing local enforcement)
- Consider uploading to a coverage service for trend tracking

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), test coverage section.

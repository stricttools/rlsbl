# Pluggable versioning strategy

## Problem

rlsbl is hardcoded to SemVer (MAJOR.MINOR.PATCH). Some projects use alternative versioning schemes that rlsbl can't support.

## Versioning systems to consider

| System | Format | Use case |
|--------|--------|----------|
| SemVer | MAJOR.MINOR.PATCH | Default, meaning in every bump |
| CalVer | YYYY.MM.MICRO or YY.MM.DD | Time-based, no compatibility promise |
| PEP 440 epochs | N!MAJOR.MINOR.PATCH | Python-specific version reset (1!1.0.0 > 99.99.99) |
| BreakVer | BREAKING.RELEASE | Only tracks breaking changes |
| Sequential | N | Integer: 1, 2, 3 (Chrome, Firefox) |
| RomVer | HUMAN.MAJOR.MINOR | First segment is vibes-based |
| SemVer + pre-release | 1.0.0-rc.1, 2.0.0-beta.3 | SemVer with formal pre-release identifiers |
| Ubuntu-style CalVer | YY.MM | Two-segment date, optional point release |

## Proposed design

Add a `version_scheme` key to `.rlsbl/config.json` that selects the parser, bumper, and formatter. SemVer is the default.

## Scope of impact

Touches nearly everything: version parsing and comparison, bump logic (CalVer bumps are date-derived), tag format, registry compatibility (npm has no epoch concept), and the `release-init` template (bump type makes no sense for CalVer).

## Pre-release as first step

SemVer pre-release identifiers (1.0.0-rc.1, 2.0.0-beta.3) are the most commonly requested gap and should be the first extension. They stay within the SemVer model, touching bump logic, tag format, and registry upload (both PyPI and npm support pre-release natively).

## Effort

Large -- architectural change requiring a pluggable strategy pattern across the codebase.

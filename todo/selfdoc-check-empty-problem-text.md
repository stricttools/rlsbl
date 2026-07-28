# selfdoc check raises "problem text must be a non-empty string" during release

## Problem

`rlsbl release run` fails with `Error: problem text must be a non-empty string` during the
selfdoc check step, even though `selfdoc check` alone exits 0. The error originates from
strictcli (strictcli/__init__.py:2507/2578). The failing project has only SEO007 warnings
(no errors) and 100% coverage. The error may come from rlsbl processing selfdoc's warning
output and constructing a strictcli Problem with an empty text field.

Observed in a project with 6 SEO007 warnings in the selfdoc lint output.

## Reproduction

1. Have a selfdoc-managed project with SEO007 warnings but no errors
2. Run `selfdoc check` alone — exits 0
3. Run `rlsbl release run` — fails at selfdoc check with the error above

# Rewriting rlsbl in Go: decisions

Append-only record of the owner's rulings for slimming rlsbl and rewriting it in Go. A later ruling overrides an earlier one.

## Slimming before the rewrite

- rlsbl keeps three release targets: go, npm, and pypi. Every other target is removed: dart, deno, docker, flutter, hex, maven, native_android, native_ios, native_changes, pgdesign, plain, swift, swift_apple, and zig, with every registry entry, template, check, option, and test that exists only for them.
- The slimming happens in the Python first, by deletion, and the Go rewrite then ports the smaller tree. The removed code stays recoverable from git history.
- The two conformance release configs, stricttools/tools/strictcli/conformance/.rlsbl/config.json and stricttools/tools/strictspec/conformance/.rlsbl/config.json, are deleted with the plain target; the conformance suites are deleted under the Great Refinement rule anyway.
- Projects whose configs name a removed target (gamehome's zig and pgdesign targets, safegit's docker target) are left as they are; they are fixed when they are next released.
- A read-only survey maps rlsbl's remaining subsystems (what each does, who uses it, its size), and the owner rules keep or drop on each before the rewrite plan is written.

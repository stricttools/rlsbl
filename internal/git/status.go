package git

import (
	"fmt"
	"strings"
)

// Untracked is how a status read reports untracked content.
type Untracked string

// The untracked modes. Normal collapses a wholly untracked directory into one
// record; All lists every untracked file in it; No leaves untracked content
// out.
const (
	UntrackedNo     Untracked = "no"
	UntrackedNormal Untracked = "normal"
	UntrackedAll    Untracked = "all"
)

// statusPrefix is the argv every working-tree status read starts with.
// --no-optional-locks keeps git from refreshing the index and taking
// index.lock, which in a worktree several sessions share could make a
// concurrent commit fail; it is also what puts the read on the observe
// allowlist, so it runs for real under --dry-run.
var statusPrefix = []string{"--no-optional-locks", "status", "--porcelain", "-z"}

// StatusArgs is the git argv (after "git") of a status read narrowed to
// paths (none: the whole tree), reporting untracked content in the given
// mode. An unknown mode is refused.
func StatusArgs(paths []string, untracked Untracked) ([]string, error) {
	switch untracked {
	case UntrackedNo, UntrackedNormal, UntrackedAll:
	default:
		return nil, fmt.Errorf("unknown untracked mode %q; state one of no, normal, all", untracked)
	}
	args := append(append([]string{}, statusPrefix...), "--untracked-files="+string(untracked))
	if len(paths) > 0 {
		args = append(append(args, "--"), paths...)
	}
	return args, nil
}

// ParseStatusRecords splits raw `git status --porcelain -z` output into its
// records, each "XY <path>" with its status columns intact. A rename or copy
// carries its origin path as a second field, which belongs to the record
// before it and is consumed with it.
func ParseStatusRecords(stdout string) []string {
	fields := strings.Split(stdout, "\x00")
	var records []string
	for i := 0; i < len(fields); i++ {
		record := fields[i]
		if strings.TrimSpace(record) == "" {
			continue
		}
		records = append(records, record)
		if len(record) >= 2 && (record[0] == 'R' || record[0] == 'C' || record[1] == 'R' || record[1] == 'C') {
			i++
		}
	}
	return records
}

// ParseStatusPaths is the changed path of every record in raw `git status
// --porcelain -z` output, verbatim: -z output is never quoted, and a rename
// yields its new path.
func ParseStatusPaths(stdout string) []string {
	var paths []string
	for _, record := range ParseStatusRecords(stdout) {
		if len(record) >= 4 {
			paths = append(paths, record[3:])
		}
	}
	return paths
}

// Status is the working tree's status records ("XY <path>"), narrowed to
// paths (none: the whole tree).
func (r Repo) Status(paths []string, untracked Untracked) ([]string, error) {
	args, err := StatusArgs(paths, untracked)
	if err != nil {
		return nil, err
	}
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	return ParseStatusRecords(out), nil
}

// ChangedPaths is every path the working tree reports a change for,
// relative to the repository root, narrowed to paths (none: the whole tree).
func (r Repo) ChangedPaths(paths []string, untracked Untracked) ([]string, error) {
	args, err := StatusArgs(paths, untracked)
	if err != nil {
		return nil, err
	}
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	return ParseStatusPaths(out), nil
}

// IsClean reports whether the working tree has no change at all, untracked
// files included.
func (r Repo) IsClean() (bool, error) {
	records, err := r.Status(nil, UntrackedNormal)
	if err != nil {
		return false, err
	}
	return len(records) == 0, nil
}

// StashEntries lists the repository's stash entries, one line each. A stash
// is uncommitted work with no branch of its own: nothing records what it
// belongs to, so every operation that rewrites, commits, or force-pushes
// this working tree asks here first.
func (r Repo) StashEntries() ([]string, error) {
	out, err := r.output("stash", "list")
	if err != nil {
		return nil, err
	}
	return lines(out), nil
}

// StashRefusal is the message an operation refuses a present stash with.
// operation names what is refused ("release"); detail is one sentence saying
// what that operation does to this working tree.
func StashRefusal(entries []string, operation, detail string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "refusing to %s: this repository has %d stash entries:\n", operation, len(entries))
	for _, e := range entries {
		fmt.Fprintf(&b, "    %s\n", e)
	}
	fmt.Fprintf(&b, "  %s\n", detail)
	b.WriteString("  A stash is uncommitted work with no branch of its own, and nothing here can tell what it belongs to.\n")
	b.WriteString("  Inspect each entry with `git stash show -p`, commit its work where it belongs, then `git stash drop` it, and run this again.")
	return b.String()
}

// RefuseStash is an error carrying StashRefusal when the repository has a
// stash, and nil when it has none.
func (r Repo) RefuseStash(operation, detail string) error {
	entries, err := r.StashEntries()
	if err != nil {
		return err
	}
	if len(entries) > 0 {
		return fmt.Errorf("%s", StashRefusal(entries, operation, detail))
	}
	return nil
}

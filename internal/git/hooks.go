package git

import (
	"fmt"
	"slices"
	"strings"
)

// ParseRewriteMap reads a map from old commit ids to new ones: the lines
// git's post-rewrite hook receives on stdin ("<old> <new>", optionally
// followed by more fields, which are ignored), the same lines in a file, or
// git-filter-repo's commit-map (whose first line is the header "old new").
// Blank lines and lines starting with # are skipped.
//
// Every other line must map one full object id to another. A malformed
// line, a line mapping to the null id (git-filter-repo's marker for a pruned
// commit: there is nothing to rewrite the old id to), and an old id mapped
// twice to different new ids are refused with their line number: a remap
// that silently skipped them would leave the changelog naming commits that
// no longer exist.
func ParseRewriteMap(text string) (map[string]string, error) {
	m := map[string]string{}
	seenContent := false
	for i, line := range strings.Split(text, "\n") {
		n := i + 1
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		fields := strings.Fields(trimmed)
		first := !seenContent
		seenContent = true
		if first && len(fields) == 2 && fields[0] == "old" && fields[1] == "new" {
			continue
		}
		if len(fields) < 2 || !IsObjectID(fields[0]) || !IsObjectID(fields[1]) {
			return nil, fmt.Errorf("line %d of the rewrite map is not \"<old commit id> <new commit id>\": %q", n, trimmed)
		}
		old, next := fields[0], fields[1]
		if IsNullObjectID(next) {
			return nil, fmt.Errorf("line %d of the rewrite map maps %s to the null id (a pruned commit); a changelog entry naming it cannot be remapped to anything, so remove that commit from the entry first, then remap with the line taken out of the map", n, old)
		}
		if prev, ok := m[old]; ok && prev != next {
			return nil, fmt.Errorf("line %d of the rewrite map maps %s to %s, but an earlier line maps it to %s", n, old, next, prev)
		}
		m[old] = next
	}
	return m, nil
}

// PushedRef is one line of the pre-push hook's stdin: what is pushed, and
// where.
type PushedRef struct {
	LocalRef  string
	LocalID   string
	RemoteRef string
	RemoteID  string
}

// ParsePrePushLines reads the pre-push hook's stdin lines ("<local ref>
// <local id> <remote ref> <remote id>"). Blank lines are skipped; any other
// line without four fields is refused.
func ParsePrePushLines(input []string) ([]PushedRef, error) {
	var refs []PushedRef
	for i, line := range input {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		if len(fields) != 4 {
			return nil, fmt.Errorf("line %d of the pre-push input is not \"<local ref> <local id> <remote ref> <remote id>\": %q", i+1, line)
		}
		refs = append(refs, PushedRef{LocalRef: fields[0], LocalID: fields[1], RemoteRef: fields[2], RemoteID: fields[3]})
	}
	return refs, nil
}

// ManualPushBranches names the release branches a push writes to. A push the
// pre-push hook sees is a manual one: rlsbl's own pushes run with
// --no-verify and never reach the hook. A branch is written to when the
// remote ref is refs/heads/<branch>, whatever local ref the push sends from
// and whether it updates or deletes the branch.
func ManualPushBranches(refs []PushedRef, releaseBranches []string) []string {
	var pushed []string
	for _, ref := range refs {
		branch, ok := strings.CutPrefix(ref.RemoteRef, "refs/heads/")
		if ok && slices.Contains(releaseBranches, branch) && !slices.Contains(pushed, branch) {
			pushed = append(pushed, branch)
		}
	}
	return pushed
}

// PushChangedFiles lists the files a push changes, relative to the
// repository root, in the order first seen: for an updated ref the
// difference from the remote's old commit, for a new ref the files of every
// commit no remote-tracking ref reaches. A deletion changes no file.
func (r Repo) PushChangedFiles(refs []PushedRef) ([]string, error) {
	var files []string
	seen := map[string]bool{}
	for _, ref := range refs {
		if IsNullObjectID(ref.LocalID) {
			continue
		}
		var args []string
		if IsNullObjectID(ref.RemoteID) {
			args = []string{"log", "--name-only", "--format=", "-z", ref.LocalID, "--not", "--remotes"}
		} else {
			args = []string{"--no-optional-locks", "diff", "--name-only", "-z", ref.RemoteID + ".." + ref.LocalID}
		}
		out, err := r.output(args...)
		if err != nil {
			return nil, err
		}
		for _, f := range nulFields(out) {
			f = strings.Trim(f, "\n")
			if f != "" && !seen[f] {
				seen[f] = true
				files = append(files, f)
			}
		}
	}
	return files, nil
}

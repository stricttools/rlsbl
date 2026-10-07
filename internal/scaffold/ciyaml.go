package scaffold

import (
	"fmt"
	"regexp"
	"strings"
)

// Workflows are generated from templates and edited as text, never parsed
// and re-serialized: what scaffold writes is what the template says, so a
// local edit and the next scaffold merge line by line. The edits here know
// the shape of rlsbl's own templates: a job is a key two spaces in under
// jobs:.

// The markers git merge-file writes around a conflict.
const (
	conflictStart = "<<<<<<<"
	conflictEnd   = ">>>>>>>"
)

// ConflictRegion is one conflict a three-way merge left in a file, by its
// 1-based first and last line.
type ConflictRegion struct {
	Start, End int
}

// ConflictRegions are the conflicts marked in text. An unterminated one
// ends at the last line, so a truncated conflict is still reported.
func ConflictRegions(text string) []ConflictRegion {
	var regions []ConflictRegion
	start := 0
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	for i, line := range lines {
		switch {
		case strings.HasPrefix(line, conflictStart):
			start = i + 1
		case strings.HasPrefix(line, conflictEnd) && start != 0:
			regions = append(regions, ConflictRegion{start, i + 1})
			start = 0
		}
	}
	if start != 0 {
		regions = append(regions, ConflictRegion{start, len(lines)})
	}
	return regions
}

// DescribeConflicts names a file's conflicts by their lines.
func DescribeConflicts(source string, regions []ConflictRegion) string {
	parts := make([]string, len(regions))
	for i, r := range regions {
		parts[i] = fmt.Sprintf("lines %d-%d", r.Start, r.End)
	}
	return source + ": " + strings.Join(parts, ", ")
}

var (
	jobHeader        = regexp.MustCompile(`^  [A-Za-z0-9_-]+:\s*$`)
	versionFileInput = regexp.MustCompile(`^(\s+(?:go|python|node)-version-file:\s*)(\S+)(\s*)$`)
)

// jobsInDirectory makes every job of jobs text (keys two spaces in) run its
// steps in dir: a defaults.run.working-directory under each job key, and
// each setup action's version-file input, which actions read from the
// repository root, prefixed with dir.
func jobsInDirectory(text, dir string) string {
	dir = strings.TrimSuffix(dir, "/")
	lines := strings.Split(text, "\n")
	var out []string
	for _, line := range lines {
		if m := versionFileInput.FindStringSubmatch(line); m != nil && !strings.HasPrefix(m[2], "/") && !strings.HasPrefix(m[2], dir+"/") {
			line = m[1] + dir + "/" + m[2] + m[3]
		}
		out = append(out, line)
		if jobHeader.MatchString(line) {
			out = append(out, "    defaults:", "      run:", "        working-directory: "+dir)
		}
	}
	return strings.Join(out, "\n")
}

// workflowInDirectory makes a whole workflow's jobs run in dir; the text
// before its jobs: line is left as it is.
func workflowInDirectory(text, dir string) string {
	head, jobs, found := strings.Cut(text, "\njobs:\n")
	if !found {
		return text
	}
	return head + "\njobs:\n" + jobsInDirectory(jobs, dir)
}

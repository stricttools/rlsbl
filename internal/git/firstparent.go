package git

import (
	"fmt"
	"strings"
	"time"
)

// FirstParentCommit is one commit of a first-parent history.
type FirstParentCommit struct {
	SHA     string
	Parents []string
	// Committed is the committer date, in the committer's own offset.
	Committed time.Time
	Subject   string
}

// FirstParentHistory is the first-parent history of rev, oldest first: the
// commits a release branch's own line of work consists of, merged-in
// branches left out.
func (r Repo) FirstParentHistory(rev string) ([]FirstParentCommit, error) {
	out, err := r.output("log", "--first-parent", "--reverse", "--format=%H%x00%P%x00%cI%x00%s%x00", rev, "--")
	if err != nil {
		return nil, err
	}
	var commits []FirstParentCommit
	for _, line := range strings.Split(out, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		fields := strings.Split(line, "\x00")
		if len(fields) != 5 || !IsObjectID(fields[0]) {
			return nil, fmt.Errorf("git log of %s in %s printed an unreadable line %q", rev, r.dir, line)
		}
		when, err := time.Parse(time.RFC3339, fields[2])
		if err != nil {
			return nil, fmt.Errorf("the committer date of %s in %s is unreadable: %w", fields[0], r.dir, err)
		}
		commits = append(commits, FirstParentCommit{SHA: fields[0], Parents: strings.Fields(fields[1]), Committed: when, Subject: fields[3]})
	}
	return commits, nil
}

package git

import (
	"fmt"
	"strings"
)

// CommitSubject is one commit of a listing with its subject line.
type CommitSubject struct {
	SHA     string
	Subject string
}

// CommitSubjects lists the commits reachable from every include revision and
// from no exclude revision, newest first, each with its subject line, in one
// git call.
func (r Repo) CommitSubjects(include, exclude []string) ([]CommitSubject, error) {
	if len(include) == 0 {
		return nil, fmt.Errorf("listing commits needs at least one revision to start from")
	}
	args := append([]string{"log", "--format=%H%x00%s"}, include...)
	if len(exclude) > 0 {
		args = append(append(args, "--not"), exclude...)
	}
	args = append(args, "--")
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	var commits []CommitSubject
	for _, line := range lines(out) {
		sha, subject, ok := strings.Cut(line, "\x00")
		if !ok || !IsObjectID(sha) {
			return nil, fmt.Errorf("git log in %s printed an unreadable line %q", r.dir, line)
		}
		commits = append(commits, CommitSubject{SHA: sha, Subject: subject})
	}
	return commits, nil
}

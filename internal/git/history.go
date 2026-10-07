package git

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Ancestry is the answer to "is A an ancestor of B?", with "git cannot tell"
// spelled out rather than folded into "no".
type Ancestry string

// The three answers. git merge-base --is-ancestor exits 0 for yes and 1 for
// no, and anything else when it cannot answer (an object the repository does
// not have). In a shallow repository its "no" is not an answer either: the
// walk stops at the graft boundary, so a commit whose connecting history was
// never fetched reads as unrelated.
const (
	IsAncestor             Ancestry = "ancestor"
	NotAncestor            Ancestry = "not-ancestor"
	AncestryIndeterminable Ancestry = "indeterminable"
)

// Ancestry answers whether ancestor is reachable from descendant (a commit
// is its own ancestor). A git that cannot be run, or a shallowness probe that
// fails, is an error; each caller decides what AncestryIndeterminable means
// for it.
func (r Repo) Ancestry(ancestor, descendant string) (Ancestry, error) {
	res, err := r.read(localTimeout, nil, "merge-base", "--is-ancestor", ancestor, descendant)
	if err != nil {
		return "", err
	}
	switch res.code {
	case 0:
		return IsAncestor, nil
	case 1:
		shallow, err := r.IsShallow()
		if err != nil {
			return "", err
		}
		if shallow {
			return AncestryIndeterminable, nil
		}
		return NotAncestor, nil
	}
	return AncestryIndeterminable, nil
}

// IsShallow reports whether the repository is a shallow clone.
func (r Repo) IsShallow() (bool, error) {
	out, err := r.output("rev-parse", "--is-shallow-repository")
	if err != nil {
		return false, err
	}
	switch strings.TrimSpace(out) {
	case "true":
		return true, nil
	case "false":
		return false, nil
	}
	return false, fmt.Errorf("git rev-parse --is-shallow-repository in %s printed %q, neither true nor false", r.dir, out)
}

// TreeRevSpec is the revision spec naming the tree of path at commit sha.
// "." and "" both mean the repository root, which git spells <sha>^{tree};
// every other path is <sha>:<path>.
func TreeRevSpec(sha, path string) string {
	if path == "." || path == "" {
		return sha + "^{tree}"
	}
	return sha + ":" + path
}

// TreeAt is the tree object of path at commit sha. found is false when the
// path does not exist at that commit, which a caller recording history may
// treat as a fact rather than a failure.
func (r Repo) TreeAt(sha, path string) (tree string, found bool, err error) {
	return r.resolve(TreeRevSpec(sha, path))
}

// CommitFiles lists the files commit sha changed, relative to the repository
// root, against its first parent (a merge is charged with what it brought
// in). A root commit lists every file it created. A commit git cannot read
// is an error naming it: which member a commit is attributed to cannot be
// guessed.
func (r Repo) CommitFiles(sha string) ([]string, error) {
	args := []string{"diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "-m", "--first-parent", "--root", sha}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, fmt.Errorf("cannot determine the files changed by commit %s: git diff-tree exited %d (%s). The commit may be missing from this repository (a shallow clone, a rewritten history); fetch it or work in a full clone", sha, res.code, strings.TrimSpace(res.stderr))
	}
	return nulFields(res.stdout), nil
}

// Commits lists the commits reachable from every include revision and from
// no exclude revision, newest first.
func (r Repo) Commits(include, exclude []string) ([]string, error) {
	if len(include) == 0 {
		return nil, fmt.Errorf("listing commits needs at least one revision to start from")
	}
	args := append([]string{"rev-list"}, include...)
	if len(exclude) > 0 {
		args = append(append(args, "--not"), exclude...)
	}
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	return lines(out), nil
}

// CountCommits counts the commits reachable from include and from no
// exclude revision.
func (r Repo) CountCommits(include, exclude []string) (int, error) {
	if len(include) == 0 {
		return 0, fmt.Errorf("counting commits needs at least one revision to start from")
	}
	args := append([]string{"rev-list", "--count"}, include...)
	if len(exclude) > 0 {
		args = append(append(args, "--not"), exclude...)
	}
	out, err := r.output(args...)
	if err != nil {
		return 0, err
	}
	n, err := strconv.Atoi(strings.TrimSpace(out))
	if err != nil {
		return 0, fmt.Errorf("git rev-list --count in %s printed %q, not a count", r.dir, out)
	}
	return n, nil
}

// commitField reads one --format field of commit sha.
func (r Repo) commitField(sha, format string) (string, error) {
	return r.output("log", "-1", "--format="+format, sha, "--")
}

// CommitSubject is the subject line of commit sha.
func (r Repo) CommitSubject(sha string) (string, error) {
	out, err := r.commitField(sha, "%s")
	return strings.TrimSpace(out), err
}

// CommitMessage is the whole message of commit sha, without its trailing
// newline.
func (r Repo) CommitMessage(sha string) (string, error) {
	out, err := r.commitField(sha, "%B")
	return strings.TrimRight(out, "\n"), err
}

// CommitSummary is what a listing of commits shows of one commit.
type CommitSummary struct {
	SHA     string
	Subject string
	// Author is the author's name.
	Author string
	// Date is the author date, ISO 8601 with its offset.
	Date string
}

// Summarize reads the subject, author name, and author date of commit sha
// in one git call.
func (r Repo) Summarize(sha string) (CommitSummary, error) {
	out, err := r.commitField(sha, "%H%x00%s%x00%an%x00%aI")
	if err != nil {
		return CommitSummary{}, err
	}
	fields := strings.Split(strings.TrimRight(out, "\n"), "\x00")
	if len(fields) != 4 {
		return CommitSummary{}, fmt.Errorf("git log of %s in %s printed an unreadable summary %q", sha, r.dir, out)
	}
	return CommitSummary{SHA: fields[0], Subject: fields[1], Author: fields[2], Date: fields[3]}, nil
}

// CommitterDate is when commit sha was committed.
func (r Repo) CommitterDate(sha string) (time.Time, error) {
	out, err := r.commitField(sha, "%cI")
	if err != nil {
		return time.Time{}, err
	}
	when, err := time.Parse(time.RFC3339, strings.TrimSpace(out))
	if err != nil {
		return time.Time{}, fmt.Errorf("the committer date of %s is unreadable: %w", sha, err)
	}
	return when, nil
}

// TrailerValues lists the values of every trailer named key in commit sha's
// message, in order.
func (r Repo) TrailerValues(sha, key string) ([]string, error) {
	if key == "" || strings.ContainsAny(key, ",)%: \t\n") {
		return nil, fmt.Errorf("%q is not a trailer key", key)
	}
	out, err := r.commitField(sha, "%(trailers:key="+key+",valueonly)")
	if err != nil {
		return nil, err
	}
	var values []string
	for _, line := range lines(out) {
		values = append(values, strings.TrimSpace(line))
	}
	return values, nil
}

// AutogeneratedTrailer is the trailer key that marks a commit a tool made.
const AutogeneratedTrailer = "Autogenerated"

// IsAutogenerated reports whether commit sha carries Autogenerated: true. A
// commit with no such trailer, or with Autogenerated: false, is not
// autogenerated; any other value, or more than one, is refused naming the
// commit, because a misspelled marker must not silently decide coverage.
func (r Repo) IsAutogenerated(sha string) (bool, error) {
	values, err := r.TrailerValues(sha, AutogeneratedTrailer)
	if err != nil {
		return false, err
	}
	switch {
	case len(values) == 0:
		return false, nil
	case len(values) == 1 && values[0] == "true":
		return true, nil
	case len(values) == 1 && values[0] == "false":
		return false, nil
	}
	return false, fmt.Errorf("commit %s carries %s trailers %q; the trailer is one line, %s: true or %s: false", sha, AutogeneratedTrailer, values, AutogeneratedTrailer, AutogeneratedTrailer)
}

// FileAt is the content of path at revision rev, byte for byte. found is
// false when rev does not hold the path.
func (r Repo) FileAt(rev, path string) (content string, found bool, err error) {
	object, found, err := r.resolve(rev + ":" + path)
	if err != nil || !found {
		return "", found, err
	}
	content, err = r.Blob(object)
	return content, err == nil, err
}

// FilesAt lists the files under the repository-relative directory dir in
// the tree of revision rev, recursively, relative to the repository root.
// A directory rev does not hold lists no files; a revision git cannot read
// is an error.
func (r Repo) FilesAt(rev, dir string) ([]string, error) {
	if _, found, err := r.resolve(rev + "^{tree}"); err != nil {
		return nil, err
	} else if !found {
		return nil, fmt.Errorf("%s names no tree in %s", rev, r.dir)
	}
	args := []string{"ls-tree", "-r", "-z", "--name-only", "--full-tree", rev, "--", dir}
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	return nulFields(out), nil
}

// Blob is the content of the blob object, byte for byte. It is read through
// git cat-file --batch, whose framing states the content's size, so a final
// newline is neither lost nor invented.
func (r Repo) Blob(object string) (string, error) {
	args := []string{"cat-file", "--batch"}
	res, err := r.read(localTimeout, []byte(object+"\n"), args...)
	if err != nil {
		return "", err
	}
	if res.code != 0 {
		return "", r.failed(args, res)
	}
	header, body, _ := strings.Cut(res.stdout, "\n")
	fields := strings.Fields(header)
	if len(fields) == 2 && fields[1] == "missing" {
		return "", fmt.Errorf("object %s is missing from %s", object, r.dir)
	}
	if len(fields) != 3 {
		return "", fmt.Errorf("git cat-file --batch in %s printed an unreadable header %q", r.dir, header)
	}
	if fields[1] != "blob" {
		return "", fmt.Errorf("object %s is a %s, not a blob", object, fields[1])
	}
	size, err := strconv.Atoi(fields[2])
	if err != nil || size > len(body) {
		return "", fmt.Errorf("git cat-file --batch in %s printed a header %q that does not match the content", r.dir, header)
	}
	return body[:size], nil
}

// TrackedFiles lists every file git tracks, relative to the repository root.
func (r Repo) TrackedFiles() ([]string, error) {
	out, err := r.output("ls-files", "-z")
	if err != nil {
		return nil, err
	}
	return nulFields(out), nil
}

// TrackedAndUntrackedFiles lists every tracked file plus every untracked file
// no ignore rule excludes, relative to the repository root.
func (r Repo) TrackedAndUntrackedFiles() ([]string, error) {
	out, err := r.output("ls-files", "-z", "--cached", "--others", "--exclude-standard")
	if err != nil {
		return nil, err
	}
	return nulFields(out), nil
}

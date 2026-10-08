package git

import (
	"fmt"
	"strings"
)

// RangeCommit is one commit of a range: its id, its whole message, and every
// blob it adds or changes against each of its parents.
type RangeCommit struct {
	SHA     string
	Message string
	Changes []BlobChange
}

// BlobChange is one blob a commit adds or changes, at its path.
type BlobChange struct {
	Path string
	Blob string
}

// RangeChanges lists the commits reachable from every include revision and
// from no exclude revision, newest first, each with its message and the
// blobs it adds or changes. A merge's changes are taken against every
// parent, so content a merge introduces itself is listed too. Deletions and
// submodule entries carry no blob and are left out.
func (r Repo) RangeChanges(include, exclude []string) ([]RangeCommit, error) {
	if len(include) == 0 {
		return nil, fmt.Errorf("listing a range's changes needs at least one revision to start from")
	}
	args := append([]string{"log", "-z", "--format=%H%x00%B"}, include...)
	if len(exclude) > 0 {
		args = append(append(args, "--not"), exclude...)
	}
	args = append(args, "--")
	out, err := r.output(args...)
	if err != nil {
		return nil, err
	}
	var commits []RangeCommit
	byID := map[string]int{}
	fields := strings.Split(out, "\x00")
	for i := 0; i+1 < len(fields); i += 2 {
		sha := strings.TrimLeft(fields[i], "\n")
		if !IsObjectID(sha) {
			return nil, fmt.Errorf("git log in %s printed %q where a commit id belongs", r.dir, sha)
		}
		byID[sha] = len(commits)
		commits = append(commits, RangeCommit{SHA: sha, Message: strings.TrimRight(fields[i+1], "\n")})
	}
	if len(commits) == 0 {
		return nil, nil
	}
	var stdin strings.Builder
	for _, c := range commits {
		stdin.WriteString(c.SHA + "\n")
	}
	diffArgs := []string{"diff-tree", "--stdin", "-r", "-m", "--root", "--no-renames", "-z"}
	res, err := r.read(localTimeout, []byte(stdin.String()), diffArgs...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, r.failed(diffArgs, res)
	}
	tokens := strings.Split(res.stdout, "\x00")
	current := -1
	for i := 0; i < len(tokens); i++ {
		tok := strings.TrimLeft(tokens[i], "\n")
		if tok == "" {
			continue
		}
		if !strings.HasPrefix(tok, ":") {
			idx, ok := byID[tok]
			if !ok {
				return nil, fmt.Errorf("git diff-tree in %s printed %q, which is no commit of the range", r.dir, tok)
			}
			current = idx
			continue
		}
		meta := strings.Fields(tok)
		if current < 0 || len(meta) != 5 || i+1 >= len(tokens) {
			return nil, fmt.Errorf("git diff-tree in %s printed an unreadable entry %q", r.dir, tok)
		}
		i++
		path := tokens[i]
		newMode, newBlob, status := meta[1], meta[3], meta[4]
		if status == "D" || newMode == "160000" || IsNullObjectID(newBlob) {
			continue
		}
		commits[current].Changes = append(commits[current].Changes, BlobChange{Path: path, Blob: newBlob})
	}
	return commits, nil
}

// GrepArgv is the git grep argv prefix FilesHolding runs: binary files
// left out (-I), file names only (-l), case ignored (-i), fixed strings (-F),
// and NUL-terminated names (-z). The observe allowlist admits it pinned
// whole, since other git grep options open files in a pager.
var GrepArgv = []string{"grep", "-I", "-l", "-i", "-F", "-z"}

// grepPathsPerCall bounds the pathspecs one git grep takes.
const grepPathsPerCall = 200

// FilesHolding lists which of paths, in the tree of commit, are text files
// (as git tells binary from text) holding one of terms anywhere, ignoring
// case. It is a prefilter: a listed file holds the term's characters, not
// necessarily the term as a whole token.
func (r Repo) FilesHolding(commit string, paths, terms []string) ([]string, error) {
	var found []string
	if len(paths) == 0 || len(terms) == 0 {
		return nil, nil
	}
	for start := 0; start < len(paths); start += grepPathsPerCall {
		args := append([]string(nil), GrepArgv...)
		for _, t := range terms {
			args = append(args, "-e", t)
		}
		args = append(args, commit, "--")
		for _, p := range paths[start:min(start+grepPathsPerCall, len(paths))] {
			args = append(args, ":(literal)"+p)
		}
		res, err := r.read(localTimeout, nil, args...)
		if err != nil {
			return nil, err
		}
		switch res.code {
		case 0:
			for _, f := range nulFields(res.stdout) {
				path, ok := strings.CutPrefix(f, commit+":")
				if !ok {
					return nil, fmt.Errorf("git grep in %s printed %q, which is no file of %s", r.dir, f, commit)
				}
				found = append(found, path)
			}
		case 1:
		default:
			return nil, r.failed(args, res)
		}
	}
	return found, nil
}

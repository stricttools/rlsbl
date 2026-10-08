package git

import (
	"fmt"
	"strconv"
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

// Blobs reads the content of every blob in ids, byte for byte, through one
// git cat-file --batch. A missing object, or one that is no blob, is an
// error.
func (r Repo) Blobs(ids []string) (map[string]string, error) {
	out := map[string]string{}
	if len(ids) == 0 {
		return out, nil
	}
	args := []string{"cat-file", "--batch"}
	res, err := r.read(localTimeout, []byte(strings.Join(ids, "\n")+"\n"), args...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, r.failed(args, res)
	}
	rest := res.stdout
	for _, id := range ids {
		header, body, ok := strings.Cut(rest, "\n")
		if !ok && header == "" {
			return nil, fmt.Errorf("git cat-file --batch in %s stopped before %s", r.dir, id)
		}
		fields := strings.Fields(header)
		if len(fields) == 2 && fields[1] == "missing" {
			return nil, fmt.Errorf("object %s is missing from %s", id, r.dir)
		}
		if len(fields) != 3 {
			return nil, fmt.Errorf("git cat-file --batch in %s printed an unreadable header %q", r.dir, header)
		}
		if fields[1] != "blob" {
			return nil, fmt.Errorf("object %s is a %s, not a blob", id, fields[1])
		}
		size, err := strconv.Atoi(fields[2])
		if err != nil || size > len(body) {
			return nil, fmt.Errorf("git cat-file --batch in %s printed a header %q that does not match the content", r.dir, header)
		}
		out[id] = body[:size]
		// Each object's content is followed by a newline; the effects handle
		// drops the last one from the whole output.
		rest = strings.TrimPrefix(body[size:], "\n")
	}
	return out, nil
}

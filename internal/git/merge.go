package git

import (
	"fmt"
	"strings"
)

// HashObject writes content to the object database as a blob and returns
// its id. A loose object is scratch until a ref names it, which is why the
// observe allowlist admits this write under --dry-run.
func (r Repo) HashObject(content string) (string, error) {
	args := []string{"hash-object", "-w", "--stdin"}
	res, err := r.read(localTimeout, []byte(content), args...)
	if err != nil {
		return "", err
	}
	if res.code != 0 {
		return "", r.failed(args, res)
	}
	id := strings.TrimSpace(res.stdout)
	if !IsObjectID(id) {
		return "", fmt.Errorf("git hash-object in %s printed %q, not an object id", r.dir, id)
	}
	return id, nil
}

// ThreeWayMerge merges the change from base to theirs into ours with git's
// own three-way merge and returns the result with the number of conflicts
// (zero: clean). Conflict markers name the sides ours, base, and theirs.
//
// The three texts and the result pass through the object database, never
// through files: no scratch file is written, a preview merges the same way a
// run does, and the result is read back byte for byte.
func (r Repo) ThreeWayMerge(ours, base, theirs string) (merged string, conflicts int, err error) {
	ids := make([]string, 3)
	for i, text := range []string{ours, base, theirs} {
		if ids[i], err = r.HashObject(text); err != nil {
			return "", 0, err
		}
	}
	args := []string{"merge-file", "--object-id", "-L", "ours", "-L", "base", "-L", "theirs", ids[0], ids[1], ids[2]}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return "", 0, err
	}
	// git merge-file exits with the number of conflicts (at most 127), and
	// with a value above that when it fails.
	if res.code < 0 || res.code > 127 {
		return "", 0, r.failed(args, res)
	}
	id := strings.TrimSpace(res.stdout)
	if !IsObjectID(id) {
		return "", 0, fmt.Errorf("git merge-file in %s printed %q, not the merged object's id", r.dir, id)
	}
	merged, err = r.Blob(id)
	if err != nil {
		return "", 0, err
	}
	return merged, res.code, nil
}

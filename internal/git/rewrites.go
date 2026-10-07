package git

import (
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// The reads and writes the history rewrites need beyond the release's:
// every ref a remote holds with annotated tags peeled, the local tag refs
// with their peeled commits, when each tag was created, the commits carrying
// a given message, a bounded run of commit subjects, and the linked
// worktrees.

// PeeledSuffix is the suffix git gives the peeled commit of an annotated
// tag in a ref listing (refs/tags/v1.2.0^{}).
const PeeledSuffix = "^{}"

// RemoteRefsPeeled maps every ref remote holds to its object, in one
// ls-remote call, with each annotated tag's commit beside it under
// <ref>^{}. A remote that cannot be read is an error, never an empty map.
func (r Repo) RemoteRefsPeeled(remote string) (map[string]string, error) {
	return r.remoteRefs(remote, nil)
}

// LocalTagRefs maps every local tag ref (refs/tags/<name>) to its object
// and <ref>^{} to the commit it points at: an annotated tag's peeled commit,
// a lightweight tag's own object.
func (r Repo) LocalTagRefs() (map[string]string, error) {
	out, err := r.output("for-each-ref", "--format=%(refname)%00%(objectname)%00%(*objectname)", "refs/tags/")
	if err != nil {
		return nil, err
	}
	refs := map[string]string{}
	for _, line := range lines(out) {
		fields := strings.Split(line, "\x00")
		if len(fields) != 3 || !IsObjectID(fields[1]) {
			return nil, fmt.Errorf("git for-each-ref in %s printed an unreadable line %q", r.dir, line)
		}
		refs[fields[0]] = fields[1]
		peeled := fields[1]
		if fields[2] != "" {
			peeled = fields[2]
		}
		refs[fields[0]+PeeledSuffix] = peeled
	}
	return refs, nil
}

// TagCreationTimes maps every local tag's name to when it was created: an
// annotated tag's tagger date, a lightweight tag's commit's committer date.
func (r Repo) TagCreationTimes() (map[string]time.Time, error) {
	out, err := r.output("for-each-ref", "--format=%(refname:strip=2)%00%(creatordate:unix)", "refs/tags/")
	if err != nil {
		return nil, err
	}
	times := map[string]time.Time{}
	for _, line := range lines(out) {
		name, stamp, ok := strings.Cut(line, "\x00")
		seconds, err := strconv.ParseInt(strings.TrimSpace(stamp), 10, 64)
		if !ok || err != nil {
			return nil, fmt.Errorf("git for-each-ref in %s printed an unreadable creation date %q", r.dir, line)
		}
		times[name] = time.Unix(seconds, 0).UTC()
	}
	return times, nil
}

// CommitsWithMessage lists the commits reachable from any ref whose whole
// message, without its trailing newlines, is one of messages, newest first.
// The comparison is exact and made here, not by a pattern git interprets.
func (r Repo) CommitsWithMessage(messages []string) ([]string, error) {
	wanted := map[string]bool{}
	for _, m := range messages {
		if m != "" {
			wanted[m] = true
		}
	}
	if len(wanted) == 0 {
		return nil, nil
	}
	out, err := r.output("log", "--all", "--format=%H%x00%B%x1e")
	if err != nil {
		return nil, err
	}
	var found []string
	for _, record := range strings.Split(out, "\x1e") {
		record = strings.TrimLeft(record, "\n")
		if record == "" {
			continue
		}
		sha, message, ok := strings.Cut(record, "\x00")
		if !ok || !IsObjectID(sha) {
			return nil, fmt.Errorf("git log in %s printed an unreadable record %q", r.dir, record)
		}
		if wanted[strings.TrimRight(message, "\n")] {
			found = append(found, sha)
		}
	}
	return found, nil
}

// Subjects lists the subject lines of at most limit commits reachable from
// sha and not from exclude, newest first; an empty exclude bounds the walk
// by limit alone.
func (r Repo) Subjects(sha, exclude string, limit int) ([]string, error) {
	if limit <= 0 {
		return nil, fmt.Errorf("a run of commit subjects needs a positive limit, not %d", limit)
	}
	args := []string{"log", "--max-count=" + strconv.Itoa(limit), "--format=%s", sha}
	if exclude != "" {
		args = append(args, "^"+exclude)
	}
	out, err := r.output(append(args, "--")...)
	if err != nil {
		return nil, err
	}
	var subjects []string
	for _, line := range lines(out) {
		subjects = append(subjects, strings.TrimSpace(line))
	}
	return subjects, nil
}

// Worktrees lists the paths of every working tree git has registered for
// the repository, the main one included, with symbolic links resolved where
// the directory still exists.
func (r Repo) Worktrees() ([]string, error) {
	out, err := r.output("worktree", "list", "--porcelain")
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, line := range lines(out) {
		if p, ok := strings.CutPrefix(line, "worktree "); ok {
			if resolved, err := filepath.EvalSymlinks(p); err == nil {
				p = resolved
			}
			paths = append(paths, p)
		}
	}
	return paths, nil
}

// RemoveWorktree removes the registered working tree at path and its
// registration, whatever untracked or ignored files it holds and whether
// or not its registration is locked.
func (r Repo) RemoveWorktree(path string) error {
	if !filepath.IsAbs(path) {
		return fmt.Errorf("refusing to remove the worktree %q: its path is not absolute", path)
	}
	return r.mutate(localTimeout, "worktree", "remove", "--force", "--force", path)
}

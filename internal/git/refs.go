package git

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"time"
)

// ResolveCommit resolves rev to the full id of the commit it names. found is
// false when rev names no commit in this repository; any other failure is an
// error.
func (r Repo) ResolveCommit(rev string) (sha string, found bool, err error) {
	return r.resolve(rev + "^{commit}")
}

// resolve resolves a revision spec to an object id.
func (r Repo) resolve(spec string) (string, bool, error) {
	args := []string{"rev-parse", "--verify", "--quiet", "--end-of-options", spec}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return "", false, err
	}
	switch res.code {
	case 0:
		return strings.TrimSpace(res.stdout), true, nil
	case 1:
		return "", false, nil
	}
	return "", false, r.failed(args, res)
}

// Head is the commit HEAD names. A repository without a commit is an error.
func (r Repo) Head() (string, error) {
	sha, found, err := r.ResolveCommit("HEAD")
	if err != nil {
		return "", err
	}
	if !found {
		return "", fmt.Errorf("HEAD names no commit in %s: the repository has no commit yet", r.dir)
	}
	return sha, nil
}

// Toplevel is the root of the working tree git finds from the Repo's
// directory, with symbolic links resolved.
func (r Repo) Toplevel() (string, error) {
	out, err := r.output("rev-parse", "--show-toplevel")
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(strings.TrimSpace(out))
}

// CommonDir is the absolute path of the repository's common git directory:
// the .git directory of the main working tree, shared by every linked one.
func (r Repo) CommonDir() (string, error) {
	out, err := r.output("rev-parse", "--path-format=absolute", "--git-common-dir")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(out), nil
}

// RequireToplevel refuses unless the Repo's directory is the root of the
// working tree git finds there. A directory that is no repository of its own
// makes git walk up into an enclosing repository; refusing here is what keeps
// a write meant for one repository out of another.
func (r Repo) RequireToplevel() error {
	top, err := r.Toplevel()
	if err != nil {
		return err
	}
	want, err := filepath.EvalSymlinks(r.dir)
	if err != nil {
		return err
	}
	if top != want {
		return fmt.Errorf("refusing to act on %s: git resolves it into the repository rooted at %s, so it is not the root of a repository of its own", r.dir, top)
	}
	return nil
}

// CurrentBranch is the branch HEAD is on. A detached HEAD is an error: every
// caller of this question acts on a named branch.
func (r Repo) CurrentBranch() (string, error) {
	args := []string{"symbolic-ref", "--quiet", "--short", "HEAD"}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return "", err
	}
	switch res.code {
	case 0:
		return strings.TrimSpace(res.stdout), nil
	case 1:
		return "", fmt.Errorf("HEAD is detached in %s: this operation acts on a named branch; check out the release branch", r.dir)
	}
	return "", r.failed(args, res)
}

// TagCommit is the commit the local tag points at, peeled through an
// annotated tag. found is false when there is no such tag.
func (r Repo) TagCommit(tag string) (sha string, found bool, err error) {
	return r.ResolveCommit("refs/tags/" + tag)
}

// RemoteTrackingCommit is the commit the remote-tracking ref of branch on
// remote points at (refs/remotes/<remote>/<branch>), as the last fetch left
// it. found is false when there is no such ref.
func (r Repo) RemoteTrackingCommit(remote, branch string) (sha string, found bool, err error) {
	return r.ResolveCommit("refs/remotes/" + remote + "/" + branch)
}

// TagCommits maps every local tag to the commit it points at, peeled through
// an annotated tag, in one git call.
func (r Repo) TagCommits() (map[string]string, error) {
	out, err := r.output("for-each-ref", "--format=%(refname)%00%(objectname)%00%(*objectname)", "refs/tags")
	if err != nil {
		return nil, err
	}
	tags := map[string]string{}
	for _, line := range lines(out) {
		fields := strings.Split(line, "\x00")
		if len(fields) != 3 {
			return nil, fmt.Errorf("git for-each-ref in %s printed an unreadable line %q", r.dir, line)
		}
		name := strings.TrimPrefix(fields[0], "refs/tags/")
		commit := fields[1]
		if fields[2] != "" {
			commit = fields[2]
		}
		tags[name] = commit
	}
	return tags, nil
}

// RefNames are the full names of the local refs under prefix (a ref
// namespace such as "refs/tags-of/github.com/acme/portal/"), sorted.
func (r Repo) RefNames(prefix string) ([]string, error) {
	if !strings.HasPrefix(prefix, "refs/") || !strings.HasSuffix(prefix, "/") {
		return nil, fmt.Errorf("%q is not a ref namespace: it starts with refs/ and ends with /", prefix)
	}
	out, err := r.output("for-each-ref", "--format=%(refname)", "--sort=refname", prefix)
	if err != nil {
		return nil, err
	}
	return lines(out), nil
}

// RemoteURL is the URL of the named remote.
func (r Repo) RemoteURL(remote string) (string, error) {
	out, err := r.output("remote", "get-url", remote)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(out), nil
}

// RemoteConfigured reports whether the named remote is configured. A
// repository with no such remote has nothing to be missing from, which is a
// different state from a remote that could not be reached.
func (r Repo) RemoteConfigured(remote string) (bool, error) {
	args := []string{"config", "--get", "remote." + remote + ".url"}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return false, err
	}
	switch res.code {
	case 0:
		return true, nil
	case 1:
		return false, nil
	}
	return false, r.failed(args, res)
}

// remoteRefs lists the refs remote holds, narrowed by ls-remote's flags and
// the given patterns, mapping each ref name to its object; a ref git peels
// (<ref>^{}) appears under that spelling.
func (r Repo) remoteRefs(remote string, flags []string, patterns ...string) (map[string]string, error) {
	args := append(append(append([]string{"ls-remote"}, flags...), remote), patterns...)
	res, err := r.read(networkTimeout, nil, args...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, fmt.Errorf("could not read the refs of %s (git ls-remote exited %d: %s); a remote that cannot be read is not evidence the refs are absent", remote, res.code, strings.TrimSpace(res.stderr))
	}
	refs := map[string]string{}
	for _, line := range lines(res.stdout) {
		object, name, ok := strings.Cut(line, "\t")
		if !ok || !IsObjectID(object) {
			return nil, fmt.Errorf("git ls-remote %s printed an unreadable line %q", remote, line)
		}
		refs[name] = object
	}
	return refs, nil
}

// RemoteRef is the object the ref (a full ref name) holds on remote, read
// from the remote itself. found is false when the remote has no such ref.
func (r Repo) RemoteRef(remote, ref string) (object string, found bool, err error) {
	if !strings.HasPrefix(ref, "refs/") {
		return "", false, fmt.Errorf("%q is not a full ref name (refs/...)", ref)
	}
	refs, err := r.remoteRefs(remote, nil, ref)
	if err != nil {
		return "", false, err
	}
	object, found = refs[ref]
	return object, found, nil
}

// RemoteTagCommit is the commit the tag points at on remote: the peeled
// commit of an annotated tag, the tag's own object for a lightweight one.
// found is false when the remote has no such tag.
func (r Repo) RemoteTagCommit(remote, tag string) (sha string, found bool, err error) {
	ref := "refs/tags/" + tag
	refs, err := r.remoteRefs(remote, nil, ref, ref+"^{}")
	if err != nil {
		return "", false, err
	}
	if peeled, ok := refs[ref+"^{}"]; ok {
		return peeled, true, nil
	}
	direct, ok := refs[ref]
	return direct, ok, nil
}

// RemoteTagCommits maps every tag on remote to its commit, peeled through an
// annotated tag, in one ls-remote call: checking every release ever made
// costs the same single round trip as checking one.
func (r Repo) RemoteTagCommits(remote string) (map[string]string, error) {
	refs, err := r.remoteRefs(remote, []string{"--tags"})
	if err != nil {
		return nil, err
	}
	direct := map[string]string{}
	peeled := map[string]string{}
	for name, object := range refs {
		tag, ok := strings.CutPrefix(name, "refs/tags/")
		if !ok {
			continue
		}
		if base, ok := strings.CutSuffix(tag, "^{}"); ok {
			peeled[base] = object
		} else {
			direct[tag] = object
		}
	}
	for tag, commit := range peeled {
		direct[tag] = commit
	}
	return direct, nil
}

// TagPushPlan decides whether pushing tags to remote is needed. Every tag
// must exist locally. A tag remote already holds at the same commit needs no
// push; a tag remote holds at a different commit is refused, because a
// release never moves a tag; a remote that cannot be read is refused rather
// than pushed to blind. needsPush is true when at least one tag is absent
// from remote.
func (r Repo) TagPushPlan(remote string, tags []string) (needsPush bool, err error) {
	for _, tag := range tags {
		local, found, err := r.TagCommit(tag)
		if err != nil {
			return false, err
		}
		if !found {
			return false, fmt.Errorf("tag %s does not exist in %s, so there is nothing to push", tag, r.dir)
		}
		remoteCommit, present, err := r.RemoteTagCommit(remote, tag)
		if err != nil {
			return false, fmt.Errorf("refusing to push tag %s blind: %w", tag, err)
		}
		if !present {
			needsPush = true
			continue
		}
		if remoteCommit != local {
			return false, fmt.Errorf("tag %s already exists on %s at a different commit (local %s, remote %s); a release never moves an existing tag, so investigate the divergence before retrying", tag, remote, local, remoteCommit)
		}
	}
	return needsPush, nil
}

// RefUpdate is one ref a push changes on a remote, with the lease that
// guards it.
type RefUpdate struct {
	// Ref is the full name of the ref on the remote (refs/heads/main,
	// refs/tags/v1.2.0).
	Ref string
	// New is the object id the ref is set to; empty deletes the ref.
	New string
	// Expected is the object id the remote ref must hold when the push
	// lands; empty means the remote must not have the ref at all.
	Expected string
}

// Push changes one ref on remote, guarded by its lease: the remote applies
// the update only when the ref still holds Expected, so a push never
// overwrites a change it did not observe. One ref per push, because GitHub
// fires no events for a push deleting several tags at once. The push runs
// with --no-verify: it is rlsbl's own push, and the pre-push hook exists to
// catch manual ones.
func (r Repo) Push(remote string, u RefUpdate, timeout time.Duration) error {
	if !strings.HasPrefix(u.Ref, "refs/") {
		return fmt.Errorf("push refused: %q is not a full ref name (refs/...)", u.Ref)
	}
	if u.New == "" && u.Expected == "" {
		return fmt.Errorf("push refused: deleting %s requires the object it is expected to hold", u.Ref)
	}
	for _, id := range []string{u.New, u.Expected} {
		if id != "" && !IsObjectID(id) {
			return fmt.Errorf("push refused: %q is not a full object id", id)
		}
	}
	if timeout <= 0 {
		return errors.New("push refused: no push timeout was declared")
	}
	return r.mutate(timeout, "push", "--no-verify", "--force-with-lease="+u.Ref+":"+u.Expected, remote, u.New+":"+u.Ref)
}

// FetchOrigin fetches origin's branches into the remote-tracking refs. It is
// the one fetch rlsbl issues, spelled the way the observe allowlist admits
// it, so it runs for real under --dry-run.
func (r Repo) FetchOrigin() error {
	args := []string{"fetch", "origin", "--quiet"}
	res, err := r.read(networkTimeout, nil, args...)
	if err != nil {
		return err
	}
	if res.code != 0 {
		return r.failed(args, res)
	}
	return nil
}

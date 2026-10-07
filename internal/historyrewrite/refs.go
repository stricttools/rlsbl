package historyrewrite

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
)

// origin is the remote every history rewrite publishes to.
const origin = "origin"

// PushTimeout is the timeout of one ref push, resolved as the release
// resolves its own: --push-timeout when given, else the declarations'
// push_seconds, else the shipped one.
var PushTimeout = release.PushTimeout

// tagName is the tag a refs/tags/ ref names, and false for any other ref.
func tagName(ref string) (string, bool) {
	name, ok := strings.CutPrefix(ref, "refs/tags/")
	return name, ok && name != ""
}

// pushWithLease sets ref on origin to target, guarded by the lease expected
// (the value origin held when the rewrite started; empty when origin had no
// such ref). A push origin refuses is decided from what origin holds now: the
// target already, which a resumed run finds, is done; the expected value
// still, so the lease held and nothing moved the ref, is origin refusing the
// push, fixed and resumed by running the same command again; anything else
// is a ref somebody moved since the rewrite started, which is never
// force-pushed over.
func pushWithLease(repo git.Repo, ref, expected, target string, timeout time.Duration) error {
	pushErr := repo.Push(origin, git.RefUpdate{Ref: ref, New: target, Expected: expected}, timeout)
	if pushErr == nil {
		return nil
	}
	current, found, readErr := repo.RemoteRef(origin, ref)
	if readErr != nil {
		return fmt.Errorf("the push of %s failed (%v), and origin could not be read afterwards (%v), so whether the ref moved is unknown; nothing further was pushed. Run the same command again once origin answers", ref, pushErr, readErr)
	}
	if !found {
		current = ""
	}
	if current == target {
		return nil
	}
	if current == expected {
		return fmt.Errorf("origin refused the push of %s: %v\norigin still holds %s, the value the lease expects, so nothing moved it. Fix what refused the push, then run the same command again: it resumes from this push", ref, pushErr, orAbsent(expected))
	}
	return fmt.Errorf("refusing to force-push %s: origin now holds %s, but it held %s when the rewrite started, so somebody moved it since; force-pushing would destroy their work. Investigate what moved it before going on (%v)", ref, orAbsent(current), orAbsent(expected), pushErr)
}

func orAbsent(sha string) string {
	if sha == "" {
		return "<absent>"
	}
	return sha
}

// pushRewrittenTags force-pushes every tag a rewrite moved, each with the
// lease the snapshot of origin taken before the rewrite gives it. A
// refname that is not a tag is refused: a rewrite's tag list names tags.
func pushRewrittenTags(repo git.Repo, tags []TagRewrite, remote map[string]string, timeout time.Duration) error {
	for _, t := range tags {
		if _, ok := tagName(t.Refname); !ok {
			return fmt.Errorf("the rewrite's tag list names %q, which is not a tag ref (refs/tags/<name>); nothing further was pushed", t.Refname)
		}
		if err := pushWithLease(repo, t.Refname, remote[t.Refname], t.NewSHA, timeout); err != nil {
			return err
		}
	}
	return nil
}

// sameCommit reports whether two object names denote one commit, either
// abbreviated.
func sameCommit(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	n := min(len(a), len(b))
	return a[:n] == b[:n]
}

func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

package historyrewrite

import (
	"time"

	"github.com/stricttools/rlsbl/internal/git"
)

// PushWithLease sets ref on origin to target, guarded by the lease expected
// (what origin held before the rewrite; empty when it had no such ref), and
// reads a refused push from what origin holds afterwards: the target
// already is done, the expected value still is a refusal to fix and run
// again, and anything else is a ref somebody moved, never force-pushed
// over. It is the push every history rewrite publishes its refs with,
// `transition declassify` included.
func PushWithLease(repo git.Repo, ref, expected, target string, timeout time.Duration) error {
	return pushWithLease(repo, ref, expected, target, timeout)
}

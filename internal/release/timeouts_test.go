package release_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/release"
)

func TestTimeoutsTakeTheFlagThenTheDeclarationsThenTheShippedOnes(t *testing.T) {
	hygiene.Isolate(t)
	d := &declarations.Releasables{}
	got, err := release.ResolveTimeouts(d, release.TimeoutOverrides{})
	mustNotFail(t, err)
	if got != (release.Timeouts{Push: release.ShippedPushTimeout, CI: release.ShippedCITimeout, Check: release.ShippedCheckTimeout}) {
		t.Errorf("shipped timeouts: %+v", got)
	}
	d.Timeouts = declarations.Timeouts{PushSeconds: 10, CISeconds: 20, CheckSeconds: 30, HookSeconds: 40}
	got, err = release.ResolveTimeouts(d, release.TimeoutOverrides{})
	mustNotFail(t, err)
	if got != (release.Timeouts{Push: 10 * time.Second, CI: 20 * time.Second, Check: 30 * time.Second, Hook: 40 * time.Second}) {
		t.Errorf("declared timeouts: %+v", got)
	}
	got, err = release.ResolveTimeouts(d, release.TimeoutOverrides{Push: 1, PushGiven: true, CI: 2, CIGiven: true, Check: 3, CheckGiven: true, Hook: 4, HookGiven: true})
	mustNotFail(t, err)
	if got != (release.Timeouts{Push: time.Second, CI: 2 * time.Second, Check: 3 * time.Second, Hook: 4 * time.Second}) {
		t.Errorf("given timeouts: %+v", got)
	}
}

func TestATimeoutFlagBelowOneSecondIsRefusedNamingTheFlag(t *testing.T) {
	hygiene.Isolate(t)
	d := &declarations.Releasables{}
	_, err := release.ResolveTimeouts(d, release.TimeoutOverrides{CI: 0, CIGiven: true})
	if err == nil || !strings.Contains(err.Error(), "--ci-timeout must be a positive number of seconds") {
		t.Fatalf("a zero --ci-timeout was accepted: %v", err)
	}
	// The fix: a positive number of seconds.
	_, err = release.ResolveTimeouts(d, release.TimeoutOverrides{CI: 60, CIGiven: true})
	mustNotFail(t, err)
	if _, err := release.PushTimeout(d, -1, true); err == nil || !strings.Contains(err.Error(), "--push-timeout") {
		t.Fatalf("a negative --push-timeout was accepted: %v", err)
	}
}

func TestRemoveCheckoutRemovesOnlyTheRegisteredCheckout(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	var said []string
	say := func(s string) { said = append(said, s) }
	remove := func() {
		t.Helper()
		_, err := mutating(t, false, func(e *strictcli.Effects) error {
			live, err := git.Open(e, repo.Dir)
			if err != nil {
				return err
			}
			return release.RemoveCheckout(live, say)
		})
		mustNotFail(t, err)
	}
	// No checkout: nothing to remove, nothing said.
	remove()
	if len(said) != 0 {
		t.Fatalf("removing no checkout said: %v", said)
	}
	mustNotFail(t, inCheckout(t, repo, func(*release.Checkout) error { return nil }))
	checkout := filepath.Join(repo.Dir, ".git", "rlsbl", "release-checkout")
	if _, err := os.Stat(checkout); err != nil {
		t.Fatalf("the checkout was not created: %v", err)
	}
	remove()
	if _, err := os.Stat(checkout); !os.IsNotExist(err) {
		t.Fatalf("the checkout is still there: %v", err)
	}
	if len(said) != 1 || !strings.Contains(said[0], "Removed the release checkout") {
		t.Errorf("said: %v", said)
	}
}

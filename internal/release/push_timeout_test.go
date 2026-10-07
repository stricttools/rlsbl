package release

import (
	"errors"
	"fmt"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
)

// A push is taken as timed out when its error matches strictcli's timeout
// error, however it is worded, and never because its text reads like one.
func TestAPushTimedOutOnlyWhenItsErrorIsStrictclisTimeout(t *testing.T) {
	timedOut := fmt.Errorf("git push in /repo: %w", fmt.Errorf("the push was stopped: %w", strictcli.ErrTimedOut))
	if !pushTimedOut(timedOut) {
		t.Errorf("a push whose error matches strictcli.ErrTimedOut was not taken as timed out: %v", timedOut)
	}
	lookalike := errors.New(`git push origin abc:refs/heads/main in /work/check timed out: x exited 1: remote: policy check timed out: try later`)
	if pushTimedOut(lookalike) {
		t.Errorf("a push error that only reads like a timeout was taken as one: %v", lookalike)
	}
}

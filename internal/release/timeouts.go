package release

import (
	"fmt"
	"time"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The shipped timeouts, for the declarations that state none: one push, the
// whole CI wait on a candidate, and each program a check or the deploy
// starts. Hooks run without a bound unless hook_seconds states one.
const (
	ShippedPushTimeout  = 300 * time.Second
	ShippedCITimeout    = 3600 * time.Second
	ShippedCheckTimeout = 900 * time.Second
)

// PushTimeout is the timeout of one push: override when given is true (the
// command was given --push-timeout), else the declarations' push_seconds,
// else ShippedPushTimeout. Every command that pushes takes its timeout from
// here.
func PushTimeout(d *declarations.Releasables, override int, given bool) (time.Duration, error) {
	var declared int64
	if d != nil {
		declared = d.Timeouts.PushSeconds
	}
	return resolveTimeout("--push-timeout", override, given, declared, ShippedPushTimeout)
}

// TimeoutOverrides are the timeouts a release invocation was given, in
// seconds; a timeout not given is zero with its Given flag false.
type TimeoutOverrides struct {
	Push, CI, Check, Hook                     int
	PushGiven, CIGiven, CheckGiven, HookGiven bool
}

// Timeouts are a release's resolved timeouts. Hook zero runs hooks with no
// bound.
type Timeouts struct {
	Push, CI, Check, Hook time.Duration
}

// ResolveTimeouts resolves each timeout: the invocation's flag, else the
// declarations' key, else the shipped one. A flag given a value below one
// second is refused, naming the flag.
func ResolveTimeouts(d *declarations.Releasables, o TimeoutOverrides) (Timeouts, error) {
	var t Timeouts
	var err error
	if t.Push, err = PushTimeout(d, o.Push, o.PushGiven); err != nil {
		return Timeouts{}, err
	}
	if t.CI, err = resolveTimeout("--ci-timeout", o.CI, o.CIGiven, d.Timeouts.CISeconds, ShippedCITimeout); err != nil {
		return Timeouts{}, err
	}
	if t.Check, err = resolveTimeout("--check-timeout", o.Check, o.CheckGiven, d.Timeouts.CheckSeconds, ShippedCheckTimeout); err != nil {
		return Timeouts{}, err
	}
	if t.Hook, err = resolveTimeout("--hook-timeout", o.Hook, o.HookGiven, d.Timeouts.HookSeconds, 0); err != nil {
		return Timeouts{}, err
	}
	return t, nil
}

func resolveTimeout(flag string, override int, given bool, declared int64, shipped time.Duration) (time.Duration, error) {
	if given {
		if override <= 0 {
			return 0, fmt.Errorf("%s must be a positive number of seconds, not %d", flag, override)
		}
		return time.Duration(override) * time.Second, nil
	}
	if declared > 0 {
		return time.Duration(declared) * time.Second, nil
	}
	return shipped, nil
}

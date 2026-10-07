package testsupport

import (
	"net/http"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
)

// CommandOptions declares the throwaway command a test runs code in. Every
// field is stated by the test: there is no default classification and no
// default allowlist.
type CommandOptions struct {
	// Effect is strictcli.EffectReadOnly or strictcli.EffectMutating.
	Effect string
	// DryRun runs the command under --dry-run.
	DryRun bool
	// Allowlist is the app's observe allowlist; nil declares none.
	Allowlist [][]string
	// HTTPClient, when set, carries every effects HTTP request.
	HTTPClient *http.Client
}

// RunCommand runs fn as the handler of a throwaway strictcli command and
// returns what the dispatch produced. A non-nil error from fn is printed as
// the command's error and ends it with exit status 1.
//
// This is how a test reaches code that takes an effects handle: the handle
// exists only inside a dispatch.
func RunCommand(t testing.TB, o CommandOptions, fn func(ctx *strictcli.Context) error) strictcli.Result {
	t.Helper()
	if o.Effect != strictcli.EffectReadOnly && o.Effect != strictcli.EffectMutating {
		t.Fatalf("testsupport: CommandOptions.Effect is %q; state %q or %q", o.Effect, strictcli.EffectReadOnly, strictcli.EffectMutating)
	}
	var opts []strictcli.AppOption
	if o.Allowlist != nil {
		opts = append(opts, strictcli.WithProcObserveAllowlist(o.Allowlist))
	}
	if o.HTTPClient != nil {
		opts = append(opts, strictcli.WithHTTPClient(o.HTTPClient))
	}
	app := strictcli.NewApp("harness", "0.0.0", "A throwaway application a test runs one handler in", opts...)
	app.Command("run", "Run the handler under test", func(ctx *strictcli.Context, _ map[string]interface{}) strictcli.Outcome {
		if err := fn(ctx); err != nil {
			ctx.Error(err.Error())
			return strictcli.Exit(1)
		}
		return strictcli.Exit(0)
	}, strictcli.WithEffect(o.Effect))
	argv := []string{"run"}
	if o.DryRun {
		argv = append(argv, "--dry-run")
	}
	return app.Test(argv)
}

// RunEffects runs fn with the effects handle of a throwaway command and
// fails the test when the dispatch does not exit 0, printing what it wrote.
func RunEffects(t testing.TB, o CommandOptions, fn func(e *strictcli.Effects) error) strictcli.Result {
	t.Helper()
	r := RunCommand(t, o, func(ctx *strictcli.Context) error { return fn(ctx.Effects()) })
	if r.ExitCode != 0 {
		t.Fatalf("the command exited %d\nstdout:\n%s\nstderr:\n%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	return r
}

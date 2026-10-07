package cli

import (
	"os"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/checks"
)

// pushStdinEnv carries the pre-push hook's ref lines to the checks the hook
// selects; the hook reads git's stdin and exports it here.
const pushStdinEnv = "RLSBL_PUSH_STDIN"

// registerChecks registers rlsbl's checks, the provider of the external
// checks members declare, the resolver giving each check the value the
// repository's options assign it, and the factory building the context the
// `check` and `failing-checks` commands run the checks with. A command that
// runs checks itself builds the context with checks.NewContext from its own
// handle and runs them through r.checks, so the external checks and values
// are those of the directory its context names.
func registerChecks(r *commandSet) error {
	runner, err := checks.Register(r.app)
	if err != nil {
		return err
	}
	r.checks = runner
	r.app.SetCheckContext(checkContext)
	return nil
}

// checkContext is the context of a `check` or `failing-checks` run: the
// working directory, over the dispatch's effects handle, with the pushed
// refs when the pre-push hook runs it. A home directory or index location
// that cannot be found is left unstated, so only the checks that read them
// refuse.
func checkContext(ctx *strictcli.Context) (strictcli.CheckContext, error) {
	dir, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	in := checks.Inputs{Dir: dir, Now: time.Now()}
	if home, err := os.UserHomeDir(); err == nil {
		in.Home = home
	}
	if path, err := index.DefaultPath(); err == nil {
		in.IndexPath = path
	}
	if lines, ok := ctx.InfraValue(pushStdinEnv); ok {
		in.PushLines = strings.Split(lines, "\n")
	}
	return checks.NewContext(ctx.Effects(), in)
}

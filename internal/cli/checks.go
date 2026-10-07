package cli

import (
	"os"
	"strings"
	"sync"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// pushStdinEnv carries the pre-push hook's ref lines to the checks the hook
// selects; the hook reads git's stdin and exports it here.
const pushStdinEnv = "RLSBL_PUSH_STDIN"

// registerChecks registers rlsbl's checks, the provider of the external
// checks members declare, the resolver giving each check the value the
// repository's options assign it for the member the working directory lies
// in, and the factory building the context the `check` and `failing-checks`
// commands run the checks with. A command that runs checks itself builds
// the context with checks.NewContext from its own handle.
func registerChecks(app *strictcli.App) error {
	if err := checks.Register(app); err != nil {
		return err
	}
	values := &checkValues{}
	app.SetCheckValueResolver(values.value)
	app.SetCheckContext(checkContext)
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

// checkValues resolves check values from the options of the repository the
// working directory lies in, read once per directory.
type checkValues struct {
	mu       sync.Mutex
	dir      string
	resolver func(string) (strictcli.CheckValue, bool)
}

func (v *checkValues) value(name string) (strictcli.CheckValue, bool) {
	dir, err := os.Getwd()
	if err != nil {
		return strictcli.CheckValue{}, false
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.resolver == nil || v.dir != dir {
		v.dir, v.resolver = dir, resolverFor(dir)
	}
	return v.resolver(name)
}

// resolverFor is the check value resolver of the member dir lies in. Where
// the declarations or the options cannot be read, every check runs at its
// registered severity: each refuses to answer there, and declarations-valid
// names what cannot be read, so no value would be applied to anything.
func resolverFor(dir string) func(string) (strictcli.CheckValue, bool) {
	registered := func(string) (strictcli.CheckValue, bool) { return strictcli.CheckValue{}, false }
	ws, err := workspace.Discover(dir)
	if err != nil {
		return registered
	}
	reg, err := options.Shipped()
	if err != nil {
		return registered
	}
	opts, err := options.Load(reg, ws.Root, ws.Declarations)
	if err != nil {
		return registered
	}
	m, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return registered
	}
	return opts.Resolver(m.Path)
}

package cli

import (
	"os"
	"sync"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// registerChecks registers rlsbl's checks, the provider of the external
// checks members declare, and the resolver giving each check the value the
// repository's options assign it for the member the working directory lies
// in.
//
// strictcli's check context factory takes no argument, so the `check` and
// `failing-checks` commands cannot hand rlsbl's checks the dispatch's
// effects handle, which every check needs; no factory is set until
// strictcli offers one that receives the dispatch, and a command that runs
// checks builds the context with checks.NewContext from its own handle.
func registerChecks(app *strictcli.App) error {
	if err := checks.Register(app); err != nil {
		return err
	}
	values := &checkValues{}
	app.SetCheckValueResolver(values.value)
	return nil
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

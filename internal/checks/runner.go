package checks

import (
	"os"
	"sync"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/options"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Runner runs the checks registered on an app. strictcli materializes the
// external checks and resolves each check's value when a run reads the
// registry, and rlsbl's provider and resolver answer for a directory: the
// one a run through RunChecks names in its *Context, and the working
// directory otherwise (the `check` command, whose context is the working
// directory). A release's preflight runs in each member of its release
// checkout, wherever the process stands, so it runs its checks through a
// Runner.
type Runner struct {
	app *strictcli.App
	// run serializes the runs through the runner, so the directory set for
	// one is the directory its registry reads see.
	run sync.Mutex

	mu  sync.Mutex
	dir string
	// valuesDir and values are the value resolver of the directory read
	// last, kept so a run resolves its values once per directory.
	valuesDir string
	values    func(string) (strictcli.CheckValue, bool)
}

// directory is the directory the provider and the resolver answer for.
func (r *Runner) directory() (string, error) {
	r.mu.Lock()
	dir := r.dir
	r.mu.Unlock()
	if dir != "" {
		return dir, nil
	}
	return os.Getwd()
}

// answerFor makes dir the directory the provider and the resolver answer
// for, the working directory when dir is empty, and drops the external
// checks materialized for another directory.
func (r *Runner) answerFor(dir string) {
	r.mu.Lock()
	r.dir = dir
	r.mu.Unlock()
	r.app.ResetCheckProviderCache()
}

// RunChecks runs the selected checks with the check context cc. A context
// of rlsbl's own makes its directory the one the external checks and the
// check values are those of, for the length of the run.
func (r *Runner) RunChecks(cc strictcli.CheckContext, opts strictcli.RunChecksOptions) ([]strictcli.CheckRunResult, []string, int, error) {
	r.run.Lock()
	defer r.run.Unlock()
	if c, ok := cc.(*Context); ok {
		r.answerFor(c.in.Dir)
		defer r.answerFor("")
	}
	return r.app.RunChecks(cc, opts)
}

// value is the check value resolver: the value the repository's options
// assign the check for the member the directory lies in.
func (r *Runner) value(name string) (strictcli.CheckValue, bool) {
	dir, err := r.directory()
	if err != nil {
		return strictcli.CheckValue{}, false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.values == nil || r.valuesDir != dir {
		r.valuesDir, r.values = dir, resolverFor(dir)
	}
	return r.values(name)
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

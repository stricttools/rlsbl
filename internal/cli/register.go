package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/checks"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/lifecycleops"
)

// effect is a command's classification.
type effect int

// The two classifications. The zero value is no classification, which add
// refuses: every command states one.
const (
	readOnly effect = iota + 1
	mutating
)

func (e effect) name() string {
	switch e {
	case readOnly:
		return strictcli.EffectReadOnly
	case mutating:
		return strictcli.EffectMutating
	}
	return ""
}

// command is one command's declaration.
type command struct {
	// path is the command's words: {"status"}, {"release", "run"}.
	path []string
	help string
	// effect is read_only or mutating.
	effect effect
	// consequential marks a command whose effects only a human approves.
	consequential bool
	// noDryRun, when set, is why --dry-run is refused.
	noDryRun string
	// grants are the command's labelled authorizations for dangerous
	// effects, shown in its preview.
	grants []strictcli.Grant
	// scratch declares the command's scratch directory, where a program it
	// runs writes for itself (npm's cache when an npm upload is listed).
	scratch bool
	flags   []strictcli.Flag
	args    []strictcli.Arg
	// payload is the JSON Schema of the --json payload; nil declares none.
	payload map[string]any
	// render is the human rendering of the payload, required with payload.
	render func(payload any) string
	// options are further declarations the fields above do not carry: flag
	// constraints and update declarations.
	options []strictcli.CmdOption
	// run performs the command and returns its payload (nil for a command
	// declaring none). A non-nil error ends the command with exit status 1
	// and the error on stderr, or with the status an *exitStatus states; a
	// payload returned with it is still emitted.
	run func(ctx *strictcli.Context, kw map[string]any) (any, error)
}

// exitStatus is an error that ends a command with a status of its own, for
// a command whose exit status is part of its answer (check-name exits 1 for
// a taken name and 2 for a check that could not be made). An empty message
// prints nothing, because the command's output already said why.
type exitStatus struct {
	code    int
	message string
}

func (e *exitStatus) Error() string {
	if e.message == "" {
		return fmt.Sprintf("exit status %d", e.code)
	}
	return e.message
}

// commandSet registers commands and the groups that hold them.
type commandSet struct {
	app    *strictcli.App
	groups map[string]*strictcli.Group
	// checks runs the registered checks for a command that builds its own
	// check context; registerChecks sets it, and the handlers read it when
	// they run.
	checks *checks.Runner
}

func newRegistry(app *strictcli.App) *commandSet {
	return &commandSet{app: app, groups: map[string]*strictcli.Group{}}
}

// group declares the group at path with its help; its parent group must be
// declared first. A group is declared once.
func (r *commandSet) group(path []string, help string) {
	key := strings.Join(path, " ")
	if len(path) == 0 || help == "" {
		panic(fmt.Sprintf("cli: group %q needs a path and a help text", key))
	}
	if _, ok := r.groups[key]; ok {
		panic(fmt.Sprintf("cli: group %q is declared twice", key))
	}
	name := path[len(path)-1]
	if len(path) == 1 {
		r.groups[key] = r.app.Group(name, help)
		return
	}
	parent, ok := r.groups[strings.Join(path[:len(path)-1], " ")]
	if !ok {
		panic(fmt.Sprintf("cli: group %q is declared before its parent", key))
	}
	r.groups[key] = parent.Group(name, help)
}

// add registers a command. A declaration that breaks a rule is a
// registration-time panic, like strictcli's own.
func (r *commandSet) add(c command) {
	key := strings.Join(c.path, " ")
	switch {
	case len(c.path) == 0:
		panic("cli: a command needs a path")
	case c.help == "":
		panic(fmt.Sprintf("cli: command %q needs a help text", key))
	case c.effect.name() == "":
		panic(fmt.Sprintf("cli: command %q states no effect", key))
	case c.run == nil:
		panic(fmt.Sprintf("cli: command %q has no handler", key))
	case (c.payload == nil) != (c.render == nil):
		panic(fmt.Sprintf("cli: command %q must declare a payload schema and its rendering together", key))
	}
	opts := []strictcli.CmdOption{strictcli.WithEffect(c.effect.name())}
	if c.consequential {
		opts = append(opts, strictcli.WithConsequential())
	}
	if c.noDryRun != "" {
		opts = append(opts, strictcli.WithDryRunUnsupported(c.noDryRun))
	}
	if len(c.grants) > 0 {
		opts = append(opts, strictcli.WithGrants(c.grants...))
	}
	if c.scratch {
		opts = append(opts, strictcli.WithScratchDir())
	}
	if len(c.flags) > 0 {
		opts = append(opts, strictcli.WithFlags(c.flags...))
	}
	if len(c.args) > 0 {
		opts = append(opts, strictcli.WithArgs(c.args...))
	}
	if c.payload != nil {
		opts = append(opts, strictcli.PayloadSchema(c.payload), strictcli.PayloadRenderer(c.render))
	}
	opts = append(opts, c.options...)
	handler := func(ctx *strictcli.Context, kw map[string]interface{}) strictcli.Outcome {
		payload, err := c.run(ctx, kw)
		if c.effect == mutating && !ctx.DryRun() {
			// Even a command that failed may have written the record.
			err = errors.Join(err, refreshIndex(ctx))
		}
		if payload != nil {
			if c.payload == nil {
				err = errors.Join(err, fmt.Errorf("command %q returned a payload but declares none", key))
			} else if plain, perr := plainJSON(payload); perr != nil {
				err = errors.Join(err, perr)
			} else {
				ctx.Payload(plain)
			}
		}
		if err != nil {
			var status *exitStatus
			if errors.As(err, &status) && status.code != 0 {
				if status.message != "" {
					ctx.Error(status.message)
				}
				return strictcli.Exit(status.code)
			}
			ctx.Error(err.Error())
			return strictcli.Exit(1)
		}
		return strictcli.Exit(0)
	}
	name := c.path[len(c.path)-1]
	if len(c.path) == 1 {
		r.app.Command(name, c.help, handler, opts...)
		return
	}
	parent, ok := r.groups[strings.Join(c.path[:len(c.path)-1], " ")]
	if !ok {
		panic(fmt.Sprintf("cli: command %q is registered before its group", key))
	}
	parent.Command(name, c.help, handler, opts...)
}

// refreshIndex brings the confidential-name index in line with the record
// of the repository holding the working directory, after a mutating
// command: every mutating command that loads declarations owes it. Outside
// a git repository, or in one declaring no releasables, the command loaded
// no declarations and nothing is owed.
func refreshIndex(ctx *strictcli.Context) error {
	dir, err := os.Getwd()
	if err != nil {
		return err
	}
	root, inRepository, err := enclosingRepository(dir)
	if err != nil || !inRepository {
		return err
	}
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile))); errors.Is(err, fs.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	path, err := index.DefaultPath()
	if err != nil {
		return err
	}
	if err := lifecycleops.RefreshIndex(ctx.Effects(), root, path, time.Now()); err != nil {
		return fmt.Errorf("updating the confidential-name index %s: %w", path, err)
	}
	return nil
}

// enclosingRepository is the root of the git repository holding dir, with
// symbolic links resolved as declarations.FindRepositoryRoot resolves them,
// and false when no repository holds it.
func enclosingRepository(dir string) (string, bool, error) {
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", false, err
	}
	for {
		if git.IsRepositoryRoot(resolved) {
			return resolved, true, nil
		}
		parent := filepath.Dir(resolved)
		if parent == resolved {
			return "", false, nil
		}
		resolved = parent
	}
}

// plainJSON turns a payload of typed values into the plain maps, slices, and
// scalars strictcli validates against the payload schema.
func plainJSON(v any) (any, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("encoding the payload: %w", err)
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("decoding the payload: %w", err)
	}
	return out, nil
}

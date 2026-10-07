package previewapply

import (
	"errors"
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// WriteDuringObservation is the error of an observation that tried to start
// a program the observe allowlist does not admit.
type WriteDuringObservation struct {
	Argv []string
}

func (w *WriteDuringObservation) Error() string {
	what := "`" + strings.Join(w.Argv, " ") + "`"
	if sub := GitSubcommand(w.Argv); sub != "" {
		what = "`git " + sub + "`"
	}
	return what + " was run during observation: only argv on rlsbl's observe allowlist (internal/previewapply/allowlist.go) may run before the preview, and every write belongs to the apply"
}

// Observer is the view of the effects handle an observation gets: it starts
// only programs the observe allowlist admits, and sends only HTTP requests
// declared reads. It has no way to write a file. Observation is read-only by
// construction, not by convention: a reconciler whose observation would push
// a branch fails at the attempt instead of making --dry-run a lie.
type Observer struct {
	e *strictcli.Effects
}

// Run starts argv when the observe allowlist admits it, and refuses it with
// a *WriteDuringObservation otherwise. Its signature is the effects handle's,
// so git.Open takes an Observer as readily as the handle itself.
func (o Observer) Run(argv []interface{}, opts ...strictcli.EffectOption) (strictcli.Completed, error) {
	tokens := make([]string, len(argv))
	for i, a := range argv {
		s, ok := a.(string)
		if !ok {
			return strictcli.Completed{}, fmt.Errorf("an observation's argv must be plain strings; element %d is %T", i, a)
		}
		tokens[i] = s
	}
	if !Allowed(tokens) {
		return strictcli.Completed{}, &WriteDuringObservation{Argv: tokens}
	}
	return o.e.Run(argv, opts...)
}

// Get sends an HTTP GET declared a read, which changes nothing on the
// server.
func (o Observer) Get(url string, opts ...strictcli.EffectOption) (strictcli.Response, error) {
	return o.e.HTTP("GET", url, append(opts, strictcli.Read())...)
}

// Reconciler is how to observe a subject and how to apply its plan.
type Reconciler struct {
	// Observe judges the subject and returns the plan. It gets only an
	// Observer.
	Observe func(o Observer) (Preview, error)
	// Apply performs one item's writes. It is called once per item, in
	// preview order, and only outside --dry-run.
	Apply func(e *strictcli.Effects, item Item) error
	// ShowKeys prefixes each rendered headline with the item's key: a
	// reconciler judging many subjects states true, one judging a single
	// subject false.
	ShowKeys bool
}

// Reconcile observes, then either prints the plan (under --dry-run) or
// applies it item by item, and returns the preview either way.
//
// An item whose apply fails stops the run. Nothing is rolled back (the
// working tree is git's to restore), so the error names every item this run
// already applied: "nothing further was written" is otherwise read as
// "nothing at all was".
func Reconcile(ctx *strictcli.Context, r Reconciler) (Preview, error) {
	if r.Observe == nil || r.Apply == nil {
		return Preview{}, errors.New("a reconciler needs both an observation and an apply")
	}
	// Observation: no write happens before this line.
	p, err := r.Observe(Observer{e: ctx.Effects()})
	if err != nil {
		return Preview{}, err
	}
	if ctx.DryRun() {
		if p.Len() > 0 {
			ctx.Out(Render(p, r.ShowKeys))
		}
		return p, nil
	}
	// Apply: every write happens after this line.
	var applied []string
	for _, it := range p.items {
		if err := r.Apply(ctx.Effects(), it); err != nil {
			return p, fmt.Errorf("%s: %w\n%s", it.Key, err, AlreadyWritten(applied))
		}
		applied = append(applied, it.Key)
	}
	return p, nil
}

// AlreadyWritten is the sentence naming the items a run applied before it
// stopped.
func AlreadyWritten(applied []string) string {
	if len(applied) == 0 {
		return "Nothing had been written by this run before the failure."
	}
	return "Already written by this run, and still changed on disk: " + strings.Join(applied, ", ") + "."
}

// CountMoved is the refusal of an apply that found a different number of
// occurrences than the preview counted: the subject changed between the
// preview and the apply. It is nil when the counts agree.
func CountMoved(key string, previewed, found int) error {
	if previewed == found {
		return nil
	}
	return fmt.Errorf("the preview counted %d occurrence(s) in %s but it now has %d: it changed between the preview and the apply, and nothing further has been written; run the command with --dry-run, read the new plan, and apply again (a new run plans from the tree as it is now)", previewed, key, found)
}

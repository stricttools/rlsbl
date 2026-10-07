package declarations

import (
	"fmt"

	tomledit "github.com/stricttools/go-toml-edit"
)

// The edits rlsbl scaffold makes: --publish-mode sets the releasable's
// publish mode, and --target declares a target on the member scaffolded.

// SetPublishMode sets the named releasable's publish_mode.
func (ed *Editor) SetPublishMode(name string, mode PublishMode) error {
	if mode != PublishCI && mode != PublishNone {
		return fmt.Errorf("%q is not a publish mode; the publish modes are %q and %q", mode, PublishCI, PublishNone)
	}
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.releasableIndex(d, name)
	if err != nil {
		return err
	}
	if err := ed.apply(func(doc *tomledit.Document) error {
		return doc.Set(fmt.Sprintf("releasables[%d].publish_mode", i), string(mode))
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Releasables[i].PublishMode = mode
	return ed.verify("setting a releasable's publish mode", want)
}

// SetMemberTargets declares the targets of the member at path, replacing
// the ones it declares. A member that declared none had its targets
// detected; declaring any means every target it has must be in targets.
func (ed *Editor) SetMemberTargets(path string, targets []Target) error {
	if len(targets) == 0 {
		return fmt.Errorf("the member at %q would declare no target; a member declaring none has its targets detected, so declare at least one", path)
	}
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.memberIndex(d, path)
	if err != nil {
		return err
	}
	value := make([]any, len(targets))
	for j, t := range targets {
		pairs := []tomledit.Pair{{Key: "name", Value: t.Name}}
		if t.Path != "" {
			pairs = append(pairs, tomledit.Pair{Key: "path", Value: t.Path})
		}
		value[j] = pairs
	}
	if err := ed.apply(func(doc *tomledit.Document) error {
		return doc.Set(fmt.Sprintf("members[%d].targets", i), value)
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Members[i].Targets = append([]Target(nil), targets...)
	return ed.verify("declaring a member's targets", want)
}

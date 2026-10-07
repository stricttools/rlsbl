package previewapply

import (
	"fmt"
	"strings"
)

// Item is one subject's verdict in a preview.
type Item struct {
	// Key names the subject judged (a file, a package, a version); unique
	// within a preview.
	Key string
	// State is the classification, from the reconciler's own closed
	// vocabulary, in snake_case.
	State string
	// Summary is the one-line headline printed after the state label.
	Summary string
	// Label, when set, is printed instead of the state with underscores
	// turned into hyphens.
	Label string
	// Facts are the observed facts behind the verdict, one per line.
	Facts []string
	// Actions are what an apply would do, one per line; none for a state
	// that needs no action.
	Actions []string
	// Detail is a free-form block printed verbatim, for guidance too long
	// to be a fact line.
	Detail string
	// Data is the reconciler's own observation record, carried untouched
	// from observe to apply so the apply never re-derives it.
	Data any
}

// StateLabel is the label printed for the item's state.
func (i Item) StateLabel() string {
	if i.Label != "" {
		return i.Label
	}
	return strings.ReplaceAll(i.State, "_", "-")
}

// Preview is an ordered list of items with unique keys, in the reconciler's
// own order.
type Preview struct {
	items []Item
}

// NewPreview builds a preview from items, refusing a duplicate key.
func NewPreview(items ...Item) (Preview, error) {
	seen := map[string]bool{}
	for _, it := range items {
		if seen[it.Key] {
			return Preview{}, fmt.Errorf("duplicate preview key %q", it.Key)
		}
		seen[it.Key] = true
	}
	return Preview{items: append([]Item(nil), items...)}, nil
}

// Single is the one-item preview of a reconciler that judges one subject,
// a whole repository for example.
func Single(item Item) Preview {
	return Preview{items: []Item{item}}
}

// Items are the preview's items, in order.
func (p Preview) Items() []Item { return append([]Item(nil), p.items...) }

// Len is the number of items.
func (p Preview) Len() int { return len(p.items) }

// Keys are the items' keys, in order.
func (p Preview) Keys() []string {
	out := make([]string, len(p.items))
	for i, it := range p.items {
		out[i] = it.Key
	}
	return out
}

// States are the items' states, in order.
func (p Preview) States() []string {
	out := make([]string, len(p.items))
	for i, it := range p.items {
		out[i] = it.State
	}
	return out
}

// ByKey is the item with the given key.
func (p Preview) ByKey(key string) (Item, bool) {
	for _, it := range p.items {
		if it.Key == key {
			return it, true
		}
	}
	return Item{}, false
}

// Only is the single item of a one-item preview; any other length is an
// error.
func (p Preview) Only() (Item, error) {
	if len(p.items) != 1 {
		return Item{}, fmt.Errorf("a one-item preview was expected, and this one has %d items", len(p.items))
	}
	return p.items[0], nil
}

// Render writes the preview as a plan a person reads, one line per entry,
// without a final newline: each item's headline ("<key>: " when showKeys,
// then the state label, then ": <summary>" when there is one), its facts and
// actions indented by two spaces, and its detail verbatim. Every reconciler's
// plan is rendered here, so every plan reads the same way.
func Render(p Preview, showKeys bool) string {
	var out []string
	for _, it := range p.items {
		headline := it.StateLabel()
		if showKeys {
			headline = it.Key + ": " + headline
		}
		if it.Summary != "" {
			headline += ": " + it.Summary
		}
		out = append(out, headline)
		for _, f := range it.Facts {
			out = append(out, "  "+f)
		}
		for _, a := range it.Actions {
			out = append(out, "  "+a)
		}
		if it.Detail != "" {
			out = append(out, it.Detail)
		}
	}
	return strings.Join(out, "\n")
}

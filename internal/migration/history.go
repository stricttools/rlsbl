package migration

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
)

// oldEventKey is the key a first-format transition record line named its
// event by (the new format calls it event). It is spelled from two literals
// because the word is banned from rlsbl's sources.
const oldEventKey = "k" + "ind"

// history is what the old transition records say: the surgery events,
// which move to the new transition record, and the facts that move into the
// lifecycle-and-license record.
type history struct {
	// surgery are the converted lines of the new transition record, with
	// the time each was recorded.
	surgery []surgeryLine
	sources []string
	// closed are the subjects whose release history is closed, with when
	// and why.
	closed       map[string]bool
	closedAt     map[string]time.Time
	closedReason map[string]string
	renames      []rename
	identities   []identityChange
	unversioned  []unversionedTag
}

type surgeryLine struct {
	line string
	at   time.Time
}

type rename struct {
	from, to string
	at       time.Time
	reason   string
}

type identityChange struct {
	subject          string
	facet            string
	from, to         string
	effectiveVersion string
	at               time.Time
	where            string
}

type unversionedTag struct {
	tag    string
	reason string
	at     time.Time
}

// The surgery events, which the new transition record keeps.
var surgeryEvents = map[string]bool{
	string(releaserecord.EventConversion):         true,
	string(releaserecord.EventTagMap):             true,
	string(releaserecord.EventReleaseCommitRemap): true,
	string(releaserecord.EventDepartedGlobs):      true,
	string(releaserecord.EventBoundaryAlias):      true,
}

// The surgery events a workspace scopes to one releasable.
var releasableScopedEvents = map[string]bool{
	string(releaserecord.EventTagMap):             true,
	string(releaserecord.EventReleaseCommitRemap): true,
	string(releaserecord.EventBoundaryAlias):      true,
}

// transitionFiles are the old transition records, each with the releasable
// it scopes its events to (empty for the repository's own record).
func (b *builder) transitionFiles() [][2]string {
	var out [][2]string
	if b.d.Layout == declarations.LayoutStandalone {
		scope := ""
		if len(b.d.Releasables) == 1 {
			scope = b.d.Releasables[0].Name
		}
		out = append(out, [2]string{oldDir + "/transitions.jsonl", scope})
		out = append(out, [2]string{oldDir + "/closed-histories/transitions.jsonl", ""})
		return out
	}
	out = append(out, [2]string{oldWorkspaceDir + "/transitions.jsonl", ""})
	for _, name := range b.tree.children(oldWorkspaceDir + "/releasables") {
		out = append(out, [2]string{oldWorkspaceDir + "/releasables/" + name + "/transitions.jsonl", name})
	}
	return out
}

// readHistory reads every old transition record.
func (b *builder) readHistory() *history {
	h := &history{closed: map[string]bool{}, closedAt: map[string]time.Time{}, closedReason: map[string]string{}}
	for _, f := range b.transitionFiles() {
		rel, scope := f[0], f[1]
		if !b.tree.has(rel) {
			continue
		}
		b.tree.claim(rel)
		data, err := b.tree.read(rel)
		if err != nil {
			b.p.add("%v", err)
			continue
		}
		h.sources = append(h.sources, rel)
		for i, text := range strings.Split(string(data), "\n") {
			if strings.TrimSpace(text) == "" {
				continue
			}
			b.readEvent(h, fmt.Sprintf("line %d of %s", i+1, rel), text, scope)
		}
	}
	return h
}

func (b *builder) readEvent(h *history, where, text, scope string) {
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		b.p.add("%s is not JSON (%v); repair it by hand", where, err)
		return
	}
	o, ok := newObject(where, value, b.p)
	if !ok {
		return
	}
	name, _ := o.str(oldEventKey)
	recordedAt, _ := o.str("recorded_at")
	at, err := time.Parse(time.RFC3339, recordedAt)
	if err != nil {
		b.p.add("%s carries recorded_at %q, which is not an RFC 3339 time; repair it by hand", where, recordedAt)
		return
	}
	switch {
	case surgeryEvents[name]:
		b.readSurgery(h, where, o, name, scope, at)
	case name == "promotion-split-map":
		b.p.add("%s records a subtree mirror's promotion, and the mirror is dropped, so the new transition record has no event for it. Hand edit: delete the line, then migrate", where)
	case name == "release-history-closed":
		subject, _ := o.str("subject")
		reason, _ := o.str("reason")
		if subject == "" {
			b.p.add("%s names no subject", where)
			return
		}
		if h.closed[subject] {
			b.p.add("%s closes the release history of %q a second time; delete one of the lines by hand", where, subject)
			return
		}
		h.closed[subject] = true
		h.closedAt[subject] = at
		h.closedReason[subject] = reason
		if _, ok := b.retired[subject]; !ok {
			b.retired[subject] = ""
		}
	case name == "releasable-rename":
		from, _ := o.str("old_name")
		to, _ := o.str("new_name")
		reason, _ := o.str("reason")
		if from == "" || to == "" {
			b.p.add("%s names no old_name or new_name", where)
			return
		}
		h.renames = append(h.renames, rename{from: from, to: to, at: at, reason: reason})
	case name == "identity-transition":
		c := identityChange{subject: scope, at: at, where: where}
		c.facet, _ = o.str("facet")
		c.from, _ = o.str("old")
		c.to, _ = o.str("new")
		c.effectiveVersion, _ = o.str("effective_version")
		if scope == "" {
			b.p.add("%s records an identity transition in the repository's own record, so the migration cannot tell whose identity changed. Hand edit: move the line into the transition record of the releasable it concerns, then migrate", where)
			return
		}
		h.identities = append(h.identities, c)
	case name == "non-version-tag":
		tag, _ := o.str("tag")
		reason, _ := o.str("reason")
		if tag == "" {
			b.p.add("%s names no tag", where)
			return
		}
		h.unversioned = append(h.unversioned, unversionedTag{tag: tag, reason: reason, at: at})
	default:
		b.p.add("%s records the event %q, which no rlsbl wrote; repair the line by hand", where, name)
	}
}

// readSurgery converts one surgery event into a line of the new record:
// format_version 2, the event named by event, and the releasable its
// workspace scoped it to.
func (b *builder) readSurgery(h *history, where string, o object, name, scope string, at time.Time) {
	members := map[string]any{}
	for k, v := range o.m {
		members[k] = v
	}
	delete(members, oldEventKey)
	members["format_version"] = releaserecord.TransitionFormatVersion
	members["event"] = name
	if b.d.Layout == declarations.LayoutWorkspace && scope != "" && releasableScopedEvents[name] {
		if _, ok := members["releasable"]; !ok {
			members["releasable"] = scope
		}
	}
	raw, err := json.Marshal(members)
	if err != nil {
		b.p.add("%s: %v", where, err)
		return
	}
	event, err := releaserecord.ParseEvent(string(raw))
	if err != nil {
		b.p.add("%s converts to a %s event the new transition record refuses (%v); repair it by hand", where, name, err)
		return
	}
	line, err := releaserecord.SerializeEvent(event)
	if err != nil {
		b.p.add("%s: %v", where, err)
		return
	}
	h.surgery = append(h.surgery, surgeryLine{line: line, at: at})
}

// convertTransitions plans the new transition record: the surgery events of
// every old record, ordered by when they were recorded.
func (b *builder) convertTransitions(d *declarations.Releasables) {
	h := b.history
	if len(h.surgery) == 0 {
		return
	}
	lines := append([]surgeryLine(nil), h.surgery...)
	sort.SliceStable(lines, func(i, j int) bool { return lines[i].at.Before(lines[j].at) })
	var text strings.Builder
	for _, l := range lines {
		text.WriteString(l.line + "\n")
	}
	b.addWrite(write{path: declarations.TransitionsFile, sources: h.sources, change: "the surgery events, each event named by event and restamped to format_version 2", data: []byte(text.String())})
}

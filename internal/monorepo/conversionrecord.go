package monorepo

import (
	"fmt"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/releaserecord"
)

// A conversion moves subjects of the lifecycle-and-license record between
// repositories: the subjects that leave a workspace with an extracted
// releasable, and the subjects an absorbed repository brings. The record a
// subject leaves keeps its history: every open period and identity of it is
// closed on the conversion's date. The record it arrives in gets every entry
// of it, its history included, under the name it has there, so a license
// period (and with it whether the repository is confidential) moves with
// the code.

// movedEntries are the entries of the subjects a conversion moves, each
// carrying the subject's name in the record it moves to.
type movedEntries struct {
	lifecycle     []lifecycle.LifecyclePeriod
	licenses      []lifecycle.LicensePeriod
	identities    []lifecycle.Identity
	registryNames []lifecycle.RegistryName
}

// empty reports whether nothing moves.
func (m movedEntries) empty() bool {
	return len(m.lifecycle)+len(m.licenses)+len(m.identities)+len(m.registryNames) == 0
}

// entriesOf are rec's entries of the subjects the map names, each renamed
// to the name the map gives it.
func entriesOf(rec *lifecycle.Record, rename map[string]string) movedEntries {
	var m movedEntries
	for _, l := range rec.Lifecycle() {
		if to, ok := rename[l.Subject]; ok {
			l.Subject = to
			m.lifecycle = append(m.lifecycle, l)
		}
	}
	for _, l := range rec.Licenses() {
		if to, ok := rename[l.Subject]; ok {
			l.Subject = to
			m.licenses = append(m.licenses, l)
		}
	}
	for _, id := range rec.Identities() {
		if to, ok := rename[id.Subject]; ok {
			id.Subject = to
			m.identities = append(m.identities, id)
		}
	}
	for _, r := range rec.RegistryNames() {
		if to, ok := rename[r.Subject]; ok {
			r.Subject = to
			m.registryNames = append(m.registryNames, r)
		}
	}
	return m
}

// pendingIdentitiesOf refuses a pending identity of one of the subjects:
// only the release of its effective version converts it, and that release
// will not happen in the repository the subject leaves.
func pendingIdentitiesOf(rec *lifecycle.Record, subjects []string) error {
	wanted := map[string]bool{}
	for _, s := range subjects {
		wanted[s] = true
	}
	var found []string
	for _, id := range rec.Identities() {
		if wanted[id.Subject] && id.Pending() {
			found = append(found, fmt.Sprintf("the %s identity %q of %q, effective %s", id.Facet, id.Value, id.Subject, id.EffectiveVersion))
		}
	}
	if len(found) == 0 {
		return nil
	}
	return fmt.Errorf("%s holds a pending identity of a subject that leaves this repository (%s), and only the release of its effective version converts it, which will not happen here. Release that version first, or delete the pending [[identities]] entry from the record", lifecycle.RecordFile, strings.Join(found, "; "))
}

// closeSubjects closes, on the date of on, every open lifecycle period,
// license period, and identity of the subjects.
func closeSubjects(rec *lifecycle.Record, subjects []string, on time.Time) error {
	wanted := map[string]bool{}
	for _, s := range subjects {
		wanted[s] = true
	}
	for _, l := range rec.Lifecycle() {
		if wanted[l.Subject] && l.Open() {
			if err := rec.ClosePeriod(lifecycle.TableLifecycle, l.Subject, on); err != nil {
				return err
			}
		}
	}
	for _, l := range rec.Licenses() {
		if wanted[l.Subject] && l.Open() {
			if err := rec.ClosePeriod(lifecycle.TableLicenses, l.Subject, on); err != nil {
				return err
			}
		}
	}
	for _, id := range rec.Identities() {
		if wanted[id.Subject] && !id.Pending() && id.Open() {
			if err := rec.CloseIdentity(id.Subject, id.Facet, on); err != nil {
				return err
			}
		}
	}
	return nil
}

// moveTagNamespace closes, on the date of on, every open identity of
// subject whose tag patterns hold oldGlob, and opens it again with newGlob
// in its place: the tags the identity owns are spelled with newGlob from
// that day.
func moveTagNamespace(rec *lifecycle.Record, subject, oldGlob, newGlob string, on time.Time, reason string) error {
	if oldGlob == newGlob {
		return nil
	}
	for _, id := range rec.Identities() {
		if id.Subject != subject || id.Pending() || !id.Open() {
			continue
		}
		held := false
		var patterns []string
		for _, p := range id.TagPatterns {
			if p == oldGlob {
				held = true
				p = newGlob
			}
			patterns = append(patterns, p)
		}
		if !held {
			continue
		}
		if err := rec.CloseIdentity(subject, id.Facet, on); err != nil {
			return err
		}
		next := lifecycle.Identity{
			Subject:     subject,
			Facet:       id.Facet,
			Value:       id.Value,
			Registry:    id.Registry,
			TagPatterns: patterns,
			Period:      lifecycle.Period{From: on},
			Reason:      reason,
		}
		if err := rec.AddIdentity(next); err != nil {
			return err
		}
	}
	return nil
}

// closeRepositoryURL records that subject's repository was url until the
// date of on: its open repository-url identity naming url is closed, and a
// record holding none gets one from the date the repository started (from)
// to on. A record holding that closed identity already is left alone. The
// closed identity is what old-repo-archived reads to ask that the old
// repository is archived.
func closeRepositoryURL(rec *lifecycle.Record, subject, url string, from, on time.Time, reason string) error {
	day := on.UTC().Truncate(24 * time.Hour)
	for _, id := range rec.Identities() {
		if id.Subject != subject || id.Facet != lifecycle.FacetRepositoryURL || id.Pending() {
			continue
		}
		if id.Open() {
			if id.Value != url {
				return fmt.Errorf("%s holds the open repository-url identity %q of %q, and the repository it arrives from is %s; correct the record of the repository it comes from, then run this again", lifecycle.RecordFile, id.Value, subject, url)
			}
			return rec.CloseIdentity(subject, lifecycle.FacetRepositoryURL, on)
		}
		if id.Value == url {
			return nil
		}
	}
	// A root commit dated after the conversion (a clock running ahead)
	// cannot start the period after it ends.
	if from.UTC().Truncate(24 * time.Hour).After(day) {
		from = on
	}
	return rec.AddIdentity(lifecycle.Identity{
		Subject:     subject,
		Facet:       lifecycle.FacetRepositoryURL,
		Value:       url,
		TagPatterns: []string{},
		Period:      lifecycle.Period{From: from, Until: on},
		Reason:      reason,
	})
}

// addEntries adds the moved entries to rec through its mutators, in date
// order, leaving out every entry rec holds already (a conversion run again
// after it stopped part-way).
func addEntries(rec *lifecycle.Record, m movedEntries) error {
	// An entry is held when the record has one of the same subject, value,
	// and start: a run completing an interrupted conversion may have closed
	// it since.
	periodKey := func(subject, value string, p lifecycle.Period) string {
		return strings.Join([]string{subject, value, p.From.String()}, "\x00")
	}
	held := map[string]bool{}
	for _, l := range rec.Lifecycle() {
		held["lifecycle\x00"+periodKey(l.Subject, string(l.Status), l.Period)] = true
	}
	for _, l := range rec.Licenses() {
		held["licenses\x00"+periodKey(l.Subject, l.License, l.Period)] = true
	}
	identityKey := func(id lifecycle.Identity) string {
		return strings.Join([]string{"identities", id.Subject, string(id.Facet), id.Value, id.EffectiveVersion, id.From.String()}, "\x00")
	}
	for _, id := range rec.Identities() {
		held[identityKey(id)] = true
	}
	for _, r := range rec.RegistryNames() {
		held["registry\x00"+r.Registry+"\x00"+r.Name] = true
	}

	periods := append([]lifecycle.LifecyclePeriod(nil), m.lifecycle...)
	sort.SliceStable(periods, func(i, j int) bool { return periods[i].From.Before(periods[j].From) })
	for _, l := range periods {
		if held["lifecycle\x00"+periodKey(l.Subject, string(l.Status), l.Period)] {
			continue
		}
		if err := rec.OpenPeriod(lifecycle.TableLifecycle, l.Subject, string(l.Status), l.From, l.Reason); err != nil {
			return err
		}
		if !l.Open() {
			if err := rec.ClosePeriod(lifecycle.TableLifecycle, l.Subject, l.Until); err != nil {
				return err
			}
		}
	}
	licenses := append([]lifecycle.LicensePeriod(nil), m.licenses...)
	sort.SliceStable(licenses, func(i, j int) bool { return licenses[i].From.Before(licenses[j].From) })
	for _, l := range licenses {
		if held["licenses\x00"+periodKey(l.Subject, l.License, l.Period)] {
			continue
		}
		if err := rec.OpenPeriod(lifecycle.TableLicenses, l.Subject, l.License, l.From, l.Reason); err != nil {
			return err
		}
		if !l.Open() {
			if err := rec.ClosePeriod(lifecycle.TableLicenses, l.Subject, l.Until); err != nil {
				return err
			}
		}
	}
	identities := append([]lifecycle.Identity(nil), m.identities...)
	// Closed identities first, so an open one of the same facet is added
	// after the ones it follows.
	sort.SliceStable(identities, func(i, j int) bool {
		a, b := identities[i], identities[j]
		if a.Pending() != b.Pending() {
			return !a.Pending()
		}
		if a.Open() != b.Open() {
			return !a.Open()
		}
		return a.From.Before(b.From)
	})
	for _, id := range identities {
		if held[identityKey(id)] {
			continue
		}
		var err error
		if id.Pending() {
			err = rec.AddPendingIdentity(id)
		} else {
			err = rec.AddIdentity(id)
		}
		if err != nil {
			return err
		}
	}
	for _, r := range m.registryNames {
		if held["registry\x00"+r.Registry+"\x00"+r.Name] {
			continue
		}
		if err := rec.AddRegistryName(r.Registry, r.Name, r.Subject, r.RecordedSince); err != nil {
			return err
		}
	}
	return nil
}

// renderRecord is a new record document holding the moved entries, the
// confidential terms, and the unversioned tags.
func renderRecord(m movedEntries, codenames, terms []string, tags []lifecycle.UnversionedTag) []byte {
	q := tomledit.QuoteString
	day := func(t time.Time) string { return t.UTC().Format(time.DateOnly) }
	list := func(values []string) string {
		quoted := make([]string, len(values))
		for i, v := range values {
			quoted[i] = q(v)
		}
		return "[" + strings.Join(quoted, ", ") + "]"
	}
	var b strings.Builder
	b.WriteString("format_version = 1\n")
	if len(codenames) > 0 {
		b.WriteString("codenames = " + list(codenames) + "\n")
	}
	if len(terms) > 0 {
		b.WriteString("distinctive_terms = " + list(terms) + "\n")
	}
	period := func(p lifecycle.Period) {
		b.WriteString("from = " + day(p.From) + "\n")
		if !p.Open() {
			b.WriteString("until = " + day(p.Until) + "\n")
		}
	}
	for _, l := range m.lifecycle {
		b.WriteString("\n[[lifecycle]]\nsubject = " + q(l.Subject) + "\nstatus = " + q(string(l.Status)) + "\n")
		period(l.Period)
		b.WriteString("reason = " + q(l.Reason) + "\n")
	}
	for _, l := range m.licenses {
		b.WriteString("\n[[licenses]]\nsubject = " + q(l.Subject) + "\nlicense = " + q(l.License) + "\n")
		period(l.Period)
		b.WriteString("reason = " + q(l.Reason) + "\n")
	}
	for _, id := range m.identities {
		b.WriteString("\n[[identities]]\nsubject = " + q(id.Subject) + "\nfacet = " + q(string(id.Facet)) + "\nvalue = " + q(id.Value) + "\nregistry = " + q(id.Registry) + "\ntag_patterns = " + list(id.TagPatterns) + "\n")
		if id.Pending() {
			b.WriteString("effective_version = " + q(id.EffectiveVersion) + "\n")
		} else {
			period(id.Period)
		}
		b.WriteString("reason = " + q(id.Reason) + "\n")
	}
	for _, r := range m.registryNames {
		b.WriteString("\n[[registry_names]]\nregistry = " + q(r.Registry) + "\nname = " + q(r.Name) + "\nsubject = " + q(r.Subject) + "\nrecorded_since = " + day(r.RecordedSince) + "\n")
	}
	for _, t := range tags {
		b.WriteString("\n[[unversioned_tags]]\ntag = " + q(t.Tag) + "\nreason = " + q(t.Reason) + "\nrecorded = " + day(t.Recorded) + "\n")
	}
	return []byte(b.String())
}

// eventsOf are the transition record's events about the releasable named
// name: the tag maps, release-commit remaps, and boundary aliases scoped to
// it, and the conversions that brought it into the repository.
func eventsOf(events []releaserecord.Event, name string) []releaserecord.Event {
	var out []releaserecord.Event
	for _, ev := range events {
		switch e := ev.(type) {
		case *releaserecord.TagMapEvent:
			if e.Releasable == name {
				out = append(out, ev)
			}
		case *releaserecord.ReleaseCommitRemapEvent:
			if e.Releasable == name {
				out = append(out, ev)
			}
		case *releaserecord.BoundaryAliasEvent:
			if e.Releasable == name {
				out = append(out, ev)
			}
		case *releaserecord.ConversionEvent:
			if e.Destination.Releasable == name {
				out = append(out, ev)
			}
		}
	}
	return out
}

// scopedTo are the events with the releasable field of each event that has
// one set to name, the departed-globs events of the repository they came
// from left out: those state what left that repository, not what arrived
// here. Events already in present (by id) are left out too.
func scopedTo(events []releaserecord.Event, name string, present map[string]bool) []releaserecord.Event {
	var out []releaserecord.Event
	for _, ev := range events {
		if present[releaserecord.Header(ev).ID] {
			continue
		}
		switch e := ev.(type) {
		case *releaserecord.TagMapEvent:
			e.Releasable = name
		case *releaserecord.ReleaseCommitRemapEvent:
			e.Releasable = name
		case *releaserecord.BoundaryAliasEvent:
			e.Releasable = name
		case *releaserecord.DepartedGlobsEvent:
			continue
		}
		out = append(out, ev)
	}
	return out
}

// eventIDs are the ids of the events.
func eventIDs(events []releaserecord.Event) map[string]bool {
	out := map[string]bool{}
	for _, ev := range events {
		out[releaserecord.Header(ev).ID] = true
	}
	return out
}

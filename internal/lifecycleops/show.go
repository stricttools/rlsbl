package lifecycleops

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
)

// The verdicts a rule gets in `transition show`.
const (
	VerdictHolds         = "holds"
	VerdictRefuses       = "refuses"
	VerdictNotApplicable = "not applicable"
	VerdictUnknown       = "unknown"
)

// Report is what `transition show` prints: the record's entries and every
// rule's verdict on the command's date.
type Report struct {
	RecordFile       string               `json:"record_file"`
	Present          bool                 `json:"present"`
	Date             string               `json:"date"`
	Confidential     bool                 `json:"confidential"`
	Lifecycle        []ReportPeriod       `json:"lifecycle"`
	Licenses         []ReportPeriod       `json:"licenses"`
	Identities       []ReportIdentity     `json:"identities"`
	RegistryNames    []ReportName         `json:"registry_names"`
	UnversionedTags  []ReportTag          `json:"unversioned_tags"`
	PublicClients    []ReportPublicClient `json:"public_clients"`
	Codenames        []string             `json:"codenames"`
	DistinctiveTerms []string             `json:"distinctive_terms"`
	// Problems are why the record is not valid against the declarations on
	// the date; empty when it is.
	Problems []string  `json:"problems"`
	Verdicts []Verdict `json:"verdicts"`
}

// ReportPeriod is a lifecycle or license period; Value is the status or
// the license, and Until is empty while the period is open.
type ReportPeriod struct {
	Subject string `json:"subject"`
	Value   string `json:"value"`
	From    string `json:"from"`
	Until   string `json:"until"`
	Reason  string `json:"reason"`
}

// ReportIdentity is one identity; a pending one carries EffectiveVersion
// and no dates.
type ReportIdentity struct {
	Subject          string   `json:"subject"`
	Facet            string   `json:"facet"`
	Value            string   `json:"value"`
	Registry         string   `json:"registry"`
	TagPatterns      []string `json:"tag_patterns"`
	From             string   `json:"from"`
	Until            string   `json:"until"`
	EffectiveVersion string   `json:"effective_version"`
	Reason           string   `json:"reason"`
}

// ReportName is one held registry name.
type ReportName struct {
	Registry      string `json:"registry"`
	Name          string `json:"name"`
	Subject       string `json:"subject"`
	RecordedSince string `json:"recorded_since"`
}

// ReportTag is one unversioned tag.
type ReportTag struct {
	Tag      string `json:"tag"`
	Reason   string `json:"reason"`
	Recorded string `json:"recorded"`
}

// ReportPublicClient is one public-client declaration.
type ReportPublicClient struct {
	Subject  string `json:"subject"`
	Reason   string `json:"reason"`
	Declared string `json:"declared"`
}

// Verdict is one rule's verdict, for the repository or one subject.
type Verdict struct {
	Rule    string `json:"rule"`
	Class   string `json:"class"`
	Subject string `json:"subject"`
	Verdict string `json:"verdict"`
	Detail  string `json:"detail"`
}

func dateOrEmpty(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return day(t)
}

// Show reads the record of the repository holding the absolute directory
// dir and judges every rule on the command's date. The declarations are
// read when the repository has them; GitHub is asked for the repository's
// visibility, and a visibility that cannot be had is the unknown verdict
// with its reason.
func (inv Invocation) Show(dir string) (Report, error) {
	root, err := declarations.FindRepositoryRoot(dir)
	if err != nil {
		return Report{}, err
	}
	repo, err := git.Open(inv.E, root)
	if err != nil {
		return Report{}, err
	}
	var d *declarations.Releasables
	if _, err := os.Lstat(filepath.Join(root, filepath.FromSlash(declarations.ReleasablesFile))); err == nil {
		if d, err = declarations.Load(root); err != nil {
			return Report{}, err
		}
	} else if !os.IsNotExist(err) {
		return Report{}, err
	}
	rec, err := lifecycle.Load(root)
	if err != nil {
		return Report{}, err
	}
	on := inv.Now()
	r := Report{RecordFile: lifecycle.RecordFile, Present: rec.Present(), Date: day(on), Confidential: rec.Confidential(on),
		Lifecycle: []ReportPeriod{}, Licenses: []ReportPeriod{}, Identities: []ReportIdentity{}, RegistryNames: []ReportName{},
		UnversionedTags: []ReportTag{}, PublicClients: []ReportPublicClient{}, Codenames: append([]string{}, rec.Codenames()...),
		DistinctiveTerms: append([]string{}, rec.DistinctiveTerms()...), Problems: []string{}, Verdicts: []Verdict{}}
	for _, l := range rec.Lifecycle() {
		r.Lifecycle = append(r.Lifecycle, ReportPeriod{l.Subject, string(l.Status), day(l.From), dateOrEmpty(l.Until), l.Reason})
	}
	for _, l := range rec.Licenses() {
		r.Licenses = append(r.Licenses, ReportPeriod{l.Subject, l.License, day(l.From), dateOrEmpty(l.Until), l.Reason})
	}
	for _, id := range rec.Identities() {
		r.Identities = append(r.Identities, ReportIdentity{id.Subject, string(id.Facet), id.Value, id.Registry, append([]string{}, id.TagPatterns...), dateOrEmpty(id.From), dateOrEmpty(id.Until), id.EffectiveVersion, id.Reason})
	}
	for _, n := range rec.RegistryNames() {
		r.RegistryNames = append(r.RegistryNames, ReportName{n.Registry, n.Name, n.Subject, day(n.RecordedSince)})
	}
	for _, u := range rec.UnversionedTags() {
		r.UnversionedTags = append(r.UnversionedTags, ReportTag{u.Tag, u.Reason, day(u.Recorded)})
	}
	for _, p := range rec.PublicClients() {
		r.PublicClients = append(r.PublicClients, ReportPublicClient{p.Subject, p.Reason, day(p.Declared)})
	}
	var declared, releasables []string
	if d != nil {
		declared = d.SubjectNames()
		for _, rel := range d.Releasables {
			releasables = append(releasables, rel.Name)
		}
	}
	if err := rec.Validate(on, declared); err != nil {
		var ve *lifecycle.ValidationError
		if !errors.As(err, &ve) {
			return Report{}, err
		}
		r.Problems = ve.Problems
	}
	source := inv.visibilitySource(repo, d)
	previous, err := headRecord(repo)
	if err != nil {
		return Report{}, err
	}
	names, err := inv.confidentialNamesVerdict(repo, rec, on)
	if err != nil {
		return Report{}, err
	}
	for _, rule := range lifecycle.Rules() {
		verdicts, err := judge(rule, rec, previous, releasables, source, names, on)
		if err != nil {
			return Report{}, err
		}
		r.Verdicts = append(r.Verdicts, verdicts...)
	}
	return r, nil
}

// unknownVisibility is the source of a repository naming no GitHub
// repository: every question is unknown, with the reason.
type unknownVisibility struct{ reason error }

func (u unknownVisibility) Visibility() (lifecycle.Visibility, error) {
	return lifecycle.VisibilityUnknown, u.reason
}

// visibilitySource asks GitHub about the repository the declarations or the
// origin remote name.
func (inv Invocation) visibilitySource(repo git.Repo, d *declarations.Releasables) publishrules.VisibilitySource {
	declared := ""
	if d != nil {
		declared = d.GitHubRepository
	}
	url := ""
	configured, err := repo.RemoteConfigured(origin)
	if err != nil {
		return unknownVisibility{err}
	}
	if configured {
		if url, err = repo.RemoteURL(origin); err != nil {
			return unknownVisibility{err}
		}
	}
	slug, err := github.ResolveRepository(declared, url)
	if err != nil {
		return unknownVisibility{err}
	}
	client, err := github.New(inv.E)
	if err != nil {
		return unknownVisibility{err}
	}
	return publishrules.GitHubVisibility(client, slug)
}

// verdictOf is a rule's verdict from the error its evaluation returned.
func verdictOf(rule lifecycle.Rule, subject string, err error, holds string) Verdict {
	v := Verdict{Rule: string(rule), Class: string(rule.Class()), Subject: subject, Verdict: VerdictHolds, Detail: holds}
	if err != nil {
		v.Verdict, v.Detail = VerdictRefuses, err.Error()
	}
	return v
}

// confidentialNamesVerdict compares the confidential-name index entry of
// the repository, keyed by its record's open releasable-name identities,
// with the names the record makes confidential on the date of on: a
// confidential repository's entry must hold them, and a public repository
// must have none.
func (inv Invocation) confidentialNamesVerdict(repo git.Repo, rec *lifecycle.Record, on time.Time) (Verdict, error) {
	rule := lifecycle.RuleConfidentialNames
	v := Verdict{Rule: string(rule), Class: string(rule.Class())}
	if inv.IndexPath == "" {
		return Verdict{}, errors.New("no confidential-name index path was given to the command")
	}
	names, err := repositoryNames(repo)
	if err != nil {
		return Verdict{}, err
	}
	update, err := index.Plan(rec, on, names...)
	if err != nil {
		v.Verdict, v.Detail = VerdictRefuses, err.Error()
		return v, nil
	}
	idx, err := index.Load(inv.IndexPath)
	if err != nil {
		return Verdict{}, err
	}
	held := idx.Held(update.Subjects)
	key := strings.Join(update.Subjects, ", ")
	const fix = "the next mutating rlsbl command run in the repository brings the entry in step"
	if !update.Confidential() {
		if len(held) > 0 {
			v.Verdict, v.Detail = VerdictRefuses, fmt.Sprintf("the repository is public, but the index %s still holds names under %s (%s); %s", inv.IndexPath, describeEntries(held), strings.Join(heldNames(held), ", "), fix)
		} else {
			v.Verdict, v.Detail = VerdictNotApplicable, "the repository is public, and the index holds no entry for it"
		}
		return v, nil
	}
	if len(held) != 1 || strings.Join(held[0].Subjects, ", ") != key || !sameNames(held[0].Names, update.Names) {
		v.Verdict, v.Detail = VerdictRefuses, fmt.Sprintf("the repository is confidential, and the index %s holds [%s] under %s where the record names [%s] under %s; %s", inv.IndexPath, strings.Join(heldNames(held), ", "), describeEntries(held), strings.Join(update.Names, ", "), key, fix)
		return v, nil
	}
	v.Verdict, v.Detail = VerdictHolds, fmt.Sprintf("the index %s holds the repository's names under %s, and publishing them is refused", inv.IndexPath, key)
	return v, nil
}

// describeEntries names index entries by their subjects.
func describeEntries(entries []index.Entry) string {
	if len(entries) == 0 {
		return "no entry"
	}
	var parts []string
	for _, e := range entries {
		parts = append(parts, strings.Join(e.Subjects, ", "))
	}
	return strings.Join(parts, "; ")
}

// heldNames is every name the entries hold.
func heldNames(entries []index.Entry) []string {
	var out []string
	for _, e := range entries {
		out = append(out, e.Names...)
	}
	return out
}

// sameNames compares two name lists as the index stores them: trimmed,
// without empty names, and ignoring case and order.
func sameNames(a, b []string) bool {
	set := func(names []string) map[string]bool {
		out := map[string]bool{}
		for _, n := range names {
			if t := strings.TrimSpace(n); t != "" {
				out[strings.ToLower(t)] = true
			}
		}
		return out
	}
	x, y := set(a), set(b)
	if len(x) != len(y) {
		return false
	}
	for n := range x {
		if !y[n] {
			return false
		}
	}
	return true
}

// judge is one rule's verdicts on the date of on. previous is the record
// HEAD commits, which the permanent rules hold the working tree's record
// to; nil when HEAD holds none. names is the confidential-names verdict,
// decided against the index.
func judge(rule lifecycle.Rule, rec, previous *lifecycle.Record, releasables []string, source publishrules.VisibilitySource, names Verdict, on time.Time) ([]Verdict, error) {
	class := string(rule.Class())
	switch rule {
	case lifecycle.RuleProprietaryRequiresPrivate:
		visibility, askErr := source.Visibility()
		if visibility == lifecycle.VisibilityUnknown {
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictUnknown, Detail: fmt.Sprintf("GitHub's visibility of the repository could not be had: %v", askErr)}}, nil
		}
		return []Verdict{verdictOf(rule, "", rec.VisibilityAllowed(visibility, on), "GitHub reports the repository "+string(visibility)+", which the record's licenses on the date allow")}, nil
	case lifecycle.RuleProprietaryRefusesPublicOutput:
		var out []Verdict
		for _, subject := range releasables {
			l, ok := rec.LicenseOn(subject, on)
			if !ok || !l.Proprietary() {
				out = append(out, Verdict{Rule: string(rule), Class: class, Subject: subject, Verdict: VerdictNotApplicable, Detail: "not proprietary on the date"})
				continue
			}
			out = append(out, Verdict{Rule: string(rule), Class: class, Subject: subject, Verdict: VerdictRefuses, Detail: "proprietary since " + day(l.From) + ": no registry package, registry write, build attestation, public docs, or blog post"})
		}
		return out, nil
	case lifecycle.RulePrivateRepositoryPublishing:
		// A confidential repository refuses these outputs whatever GitHub
		// reports; a public one is asked.
		visibility := lifecycle.VisibilityPrivate
		var askErr error
		if !rec.Confidential(on) {
			visibility, askErr = source.Visibility()
		}
		var refused []string
		for _, o := range lifecycle.Outputs() {
			if err := rec.PrivateRepositoryOutputAllowed(o, visibility, on); err != nil {
				refused = append(refused, string(o))
			}
		}
		switch {
		case len(refused) == 0:
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictNotApplicable, Detail: "the repository is public on GitHub and in the record"}}, nil
		case visibility == lifecycle.VisibilityUnknown:
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictUnknown, Detail: fmt.Sprintf("GitHub's visibility could not be had (%v), so these outputs are refused: %s", askErr, strings.Join(refused, ", "))}}, nil
		}
		return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictRefuses, Detail: "a confidential or private repository makes none of: " + strings.Join(refused, ", ")}}, nil
	case lifecycle.RuleLifecycleAllowsRelease:
		var out []Verdict
		for _, subject := range releasables {
			out = append(out, verdictOf(rule, subject, rec.ReleaseAllowed(subject, on), "released while its lifecycle is active or unstated"))
		}
		return out, nil
	case lifecycle.RuleConfidentialNames:
		return []Verdict{names}, nil
	case lifecycle.RuleProprietaryHistoryIsSquashed:
		periods := rec.ProprietaryPeriods()
		if len(periods) == 0 {
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictNotApplicable, Detail: "no proprietary period"}}, nil
		}
		var spans []string
		for _, p := range periods {
			span := "from " + day(p.From)
			if !p.Open() {
				span += " until " + day(p.Until)
			}
			spans = append(spans, span)
		}
		return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictHolds, Detail: "`rlsbl transition declassify` squashes each proprietary period's commits into one: " + strings.Join(spans, "; ")}}, nil
	case lifecycle.RuleIdentityOwnsItsTags:
		var owners []string
		for _, id := range rec.Identities() {
			if !id.Pending() && !id.Open() && len(id.TagPatterns) > 0 {
				owners = append(owners, fmt.Sprintf("%s %s %q (%s)", id.Subject, id.Facet, id.Value, strings.Join(id.TagPatterns, ", ")))
			}
		}
		if len(owners) == 0 {
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictNotApplicable, Detail: "no closed identity owns tags"}}, nil
		}
		return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictHolds, Detail: "the tags these closed identities created are theirs: " + strings.Join(owners, "; ")}}, nil
	case lifecycle.RuleRegistryNamesAreHeld:
		if previous == nil {
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictNotApplicable, Detail: "HEAD commits no record to hold this one to"}}, nil
		}
		return []Verdict{verdictOf(rule, "", rec.CheckRegistryNamesHeld(previous), "every registry name HEAD's record holds is still held")}, nil
	case lifecycle.RuleClosedPeriodsAreFinal:
		if previous == nil {
			return []Verdict{{Rule: string(rule), Class: class, Verdict: VerdictNotApplicable, Detail: "HEAD commits no record to hold this one to"}}, nil
		}
		return []Verdict{verdictOf(rule, "", rec.CheckClosedPeriodsFinal(previous), "every period HEAD's record closed is unchanged")}, nil
	}
	return nil, fmt.Errorf("the lifecycle library has a rule rlsbl does not judge: %s; rlsbl needs updating for it", rule)
}
